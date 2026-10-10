# main.py

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.cache import cache_dir_scope, create_parse_cache_dir
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser, KugouMusicParser, QQMusicParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    async def initialize(self):
        """加载、重载插件时触发"""
        # 插件启动/重载时回收超过 24 小时且未被浏览器使用的 Playwright
        # 临时目录；清理失败不会阻断后续解析器和渲染器启动。
        await self.cleaner.clean_stale_playwright_profiles()
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 预热并复用 Playwright 的 Chrome Headless Shell。启动失败仅会让
        # 卡片功能跳过，不影响解析器注册和原媒体发送。
        await self.renderer.start()
        # 注册解析器
        self._register_parser()
        await self._check_qqmusic_cookie()

    async def _check_qqmusic_cookie(self):
        """Check the QQ Music Cookie once after enabled parsers are registered."""

        qq_parsers = {
            parser
            for parser in self.parser_map.values()
            if isinstance(parser, QQMusicParser)
        }
        for parser in qq_parsers:
            if not parser.has_cookie():
                logger.info("[QQ音乐] 未配置 Cookie，跳过 Cookie 可用性检查")
                continue
            try:
                available = await parser.check_cookie()
            except Exception as exc:
                logger.warning(f"[QQ音乐] Cookie 检查失败: {exc}")
                continue
            if available:
                logger.info("[QQ音乐] Cookie 检查通过，可用于受限歌曲音频")
            else:
                logger.warning("[QQ音乐] Cookie 检查未通过，受限歌曲可能无法获取音频")

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 先释放浏览器页面和文件句柄，再停止清理任务；缓存 PNG 仍由既有
        # CacheCleaner 周期统一清理。
        await self.renderer.close()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        text = event.message_str or ""
        plain_text = "".join(
            segment.text for segment in chain if isinstance(segment, Plain)
        )
        if plain_text and plain_text not in text:
            text = f"{text}\n{plain_text}" if text else plain_text

        # 指定机制：专门@其他bot的消息不解析
        self_id = event.get_self_id()
        mentioned_ids: set[str] = set()
        for seg in chain:
            if isinstance(seg, At):
                mentioned_ids.add(str(seg.qq))
            elif isinstance(seg, Plain):
                mentioned_ids.update(re.findall(r"<@!?([^>\s]+)>", seg.text))
        if (
            self.cfg.require_at_in_group
            and not isinstance(event, AiocqhttpMessageEvent)
            and event.get_message_type() == MessageType.GROUP_MESSAGE
            and self_id not in mentioned_ids
        ):
            return
        if mentioned_ids and self_id not in mentioned_ids:
            return

        # 卡片解析：扫描整条消息链，兼容 @ + JSON 卡片等组合消息。
        for seg in chain:
            if not isinstance(seg, Json):
                continue
            parsed_url = extract_json_url(seg.data)
            logger.debug(f"解析Json组件: {parsed_url}")
            if parsed_url:
                text = parsed_url
                break

        if not text:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析。每次解析使用独立缓存目录；目录通过 contextvar 传递给
        # 下载任务和解析器内部的合并/封装逻辑，不修改全局 cfg.cache_dir，
        # 因而并发消息不会互相覆盖。
        cache_root = getattr(self.cfg, "cache_dir", None)
        try:
            parse_cache_dir = (
                create_parse_cache_dir(cache_root, keyword)
                if cache_root is not None
                else None
            )
        except (OSError, TypeError, ValueError) as exc:
            # 缓存归档是增强项；目录不可写时沿用旧的全局缓存路径，
            # 不让一次缓存故障阻断平台解析本身。
            logger.warning(f"[缓存] 无法创建解析目录，回退到默认缓存目录: {exc}")
            parse_cache_dir = None
        with cache_dir_scope(parse_cache_dir):
            parse_res = await self.parser_map[keyword].parse(keyword, searched)
        if parse_cache_dir is not None:
            parse_res.cache_dir = parse_cache_dir

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        await self.sender.send_parse_result(event, parse_res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录QQ音乐", alias={"qqlogin", "登录qq音乐"})
    async def login_qqmusic(self, event: AstrMessageEvent):
        """扫码登录QQ音乐"""
        parser: QQMusicParser = self._get_parser_by_type(QQMusicParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        card_path = await self.renderer.render_qqmusic_login_card(qrcode)
        if card_path is not None:
            yield event.chain_result([Image.fromFileSystem(str(card_path))])
        else:
            # Keep login usable when Playwright/Jinja2 is unavailable, just as
            # the original command did before the designed card was added.
            yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录酷狗音乐", alias={"kugoulogin", "登录酷狗"})
    async def login_kugou(self, event: AstrMessageEvent):
        """扫码登录酷狗音乐"""
        parser: KugouMusicParser = self._get_parser_by_type(KugouMusicParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        card_path = await self.renderer.render_kugou_login_card(qrcode)
        if card_path is not None:
            yield event.chain_result([Image.fromFileSystem(str(card_path))])
        else:
            yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

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
from .core.apple_music_backend import AppleMusicBackend
from .core.apple_music_web import AppleMusicWeb
from .core.cache import cache_dir_scope, create_parse_cache_dir
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.group_parser_config import (
    PARSER_LABELS,
    GroupParserConfigService,
    group_enabled_parsers,
)
from .core.parsers import BaseParser, BilibiliParser, KugouMusicParser, QQMusicParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.session_scope import (
    is_group_session,
    normalize_session_scope,
    session_scope_in,
)
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
        self._parser_config_signature: tuple[tuple[str, bool], ...] = ()
        self.apple_music_backend = AppleMusicBackend(
            self.cfg,
            platform_status_provider=self._platform_account_statuses,
            runtime_parser_provider=lambda: set(self.parser_map.values()),
        )
        self.apple_music_web = AppleMusicWeb(self.apple_music_backend, self.cfg._plugin_name)
        self.group_parser_config = GroupParserConfigService(
            self.cfg, self.context, lock=self.apple_music_backend.lock
        )
        self.apple_music_web.group_parser_config = self.group_parser_config

    def _platform_account_statuses(self) -> list[dict]:
        """Describe local account cookies without exposing or validating secrets."""

        parsers = {
            parser.platform.name: parser
            for parser in set(self.parser_map.values())
            if getattr(getattr(parser, "platform", None), "name", None)
        }
        statuses: list[dict] = []
        for key in ("douyin", "bilibili", "qqmusic", "kugou"):
            get_parser = getattr(getattr(self.cfg, "parser", None), "get", None)
            item = get_parser(key) if callable(get_parser) else getattr(getattr(self.cfg, "parser", None), key, None)
            parser = parsers.get(key)
            cookie_configured = False
            if item is None or getattr(item, "enable", True) is not True:
                statuses.append(
                    {"key": key, "available": False, "text": "解析器未启用", "cookie_configured": False}
                )
                continue
            if parser is None:
                statuses.append(
                    {"key": key, "available": False, "text": "Cookie 状态未就绪", "cookie_configured": bool(getattr(item, "cookies", None))}
                )
                continue

            try:
                if key == "douyin":
                    available = parser.has_login_cookie()
                    cookie_configured = bool(available)
                    text = "登录 Cookie 已配置" if available else "登录 Cookie 未配置"
                elif key == "bilibili":
                    available = parser.login.has_cookie()
                    cookie_configured = bool(available)
                    text = "登录 Cookie 已配置" if available else "登录 Cookie 未配置"
                else:
                    cookie_configured = parser.has_cookie()
                    if key in {"qqmusic", "kugou"}:
                        backend = getattr(self, "apple_music_backend", None)
                        status_reader = getattr(backend, "music_cookie_status", None)
                        if callable(status_reader):
                            cookie_value = backend._music_cookie_value(key)
                            state = status_reader(
                                key,
                                cookie_value,
                                has_credentials=(
                                    parser.has_auth_credentials() if cookie_configured else False
                                ),
                            )
                            available, text = state["available"], state["cookie_status"]
                        else:
                            available = parser.has_auth_credentials()
                            text = "账号 Cookie 已配置" if available else (
                                "账号 Cookie 不完整" if cookie_configured else "账号 Cookie 未配置"
                            )
                    else:
                        available = parser.has_auth_credentials()
                        if available:
                            text = "账号 Cookie 已配置"
                        elif parser.has_cookie():
                            text = "账号 Cookie 不完整"
                        else:
                            text = "账号 Cookie 未配置"
            except (AttributeError, TypeError, ValueError):
                available, text = False, "状态读取失败"
            statuses.append({"key": key, "available": available, "text": text})
        return statuses

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
        web = getattr(self, "apple_music_web", None)
        if web is not None and not web.register(self.context):
            logger.info("[Apple Music] 当前 AstrBot 未提供 Plugin Pages Web API，保留信息卡片解析")
        await self._check_qqmusic_cookie()
        await self._check_kugou_cookie()

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
            if isinstance(available, bool):
                self.apple_music_backend.record_music_cookie_check(
                    "qqmusic", self.apple_music_backend._music_cookie_value("qqmusic"), available
                )
            if available is True:
                logger.info("[QQ音乐] 账号 Cookie 校验通过；具体歌曲播放权限由 QQ 音乐决定")
            elif available is False:
                logger.warning("[QQ音乐] 账号 Cookie 校验未通过；请检查登录是否过期并重新扫码")
            else:
                logger.warning("[QQ音乐] 账号 Cookie 校验暂时无法确认，保留待检查状态")

    async def _check_kugou_cookie(self):
        """Check configured Kugou account credentials after parser registration."""

        kugou_parsers = {
            parser
            for parser in self.parser_map.values()
            if isinstance(parser, KugouMusicParser)
        }
        for parser in kugou_parsers:
            if not parser.has_cookie():
                logger.info("[酷狗音乐] 未配置 Cookie，跳过 Cookie 可用性检查")
                continue
            if not parser.has_auth_credentials():
                self.apple_music_backend.record_music_cookie_check(
                    "kugou", self.apple_music_backend._music_cookie_value("kugou"), False
                )
                logger.warning(
                    "[酷狗音乐] Cookie 缺少账号字段；会员播放至少需要 KugooID 和 t（通常位于 KuGoo 内）"
                )
                continue
            try:
                available = await parser.check_cookie()
            except Exception:
                # Do not log request exceptions here: a third-party error may
                # contain the signed URL, which includes the account token.
                logger.warning("[酷狗音乐] Cookie 检查请求异常，暂时无法确认账号状态")
                continue
            if isinstance(available, bool):
                self.apple_music_backend.record_music_cookie_check(
                    "kugou", self.apple_music_backend._music_cookie_value("kugou"), available
                )
            if available is True:
                logger.info(
                    "[酷狗音乐] 账号 Cookie 校验通过；具体歌曲播放权限由酷狗音乐决定"
                )
            elif available is False:
                logger.warning(
                    "[酷狗音乐] 账号 Cookie 校验未通过；请检查登录是否过期并重新扫码"
                )
            else:
                logger.warning("[酷狗音乐] 账号 Cookie 校验暂不可用，无法确认登录状态")

    async def terminate(self):
        """插件卸载时触发"""
        await self.apple_music_web.close()
        await self.apple_music_backend.close()
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
        old_parsers = set(self.parser_map.values())
        self.parser_map.clear()
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
            if platform_name == "applemusic":
                parser.apple_music_backend = getattr(self, "apple_music_backend", None)

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
        self._parser_config_signature = self._current_parser_config_signature()
        # Management API may inspect currently instantiated cookie jars without
        # holding onto request data or serializing their values.
        self.apple_music_backend._runtime_parser_provider = lambda: set(self.parser_map.values())

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

        # Re-registration can happen after a management-page toggle. Close
        # sessions belonging to removed parser instances in the background so
        # enabling/disabling a parser takes effect without a plugin reload.
        current_parsers = set(self.parser_map.values())
        for parser in old_parsers - current_parsers:
            try:
                task = asyncio.create_task(parser.close_session())
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            except RuntimeError:
                pass

    def _current_parser_config_signature(self) -> tuple[tuple[str, bool], ...]:
        parser_config = getattr(self.cfg, "parser", None)
        nodes = getattr(parser_config, "_nodes", None)
        if not isinstance(nodes, dict):
            values = vars(parser_config) if parser_config is not None else {}
            nodes = {
                key: value for key, value in values.items()
                if not key.startswith("_") and hasattr(value, "enable")
            }
        return tuple(sorted(
            (str(key), getattr(item, "enable", True) is True)
            for key, item in nodes.items()
        ))

    def _ensure_parser_registration(self) -> None:
        current = self._current_parser_config_signature()
        previous = getattr(self, "_parser_config_signature", None)
        if previous is None:
            object.__setattr__(self, "_parser_config_signature", current)
            return
        if current != previous:
            self._register_parser()

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        self._ensure_parser_registration()
        umo = event.unified_msg_origin

        # 白名单
        if self.cfg.whitelist and not session_scope_in(umo, self.cfg.whitelist):
            return

        # 黑名单
        if session_scope_in(umo, self.cfg.blacklist):
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

        get_platform_name = getattr(event, "get_platform_name", None)
        platform_name = get_platform_name() if callable(get_platform_name) else None
        get_group_id = getattr(event, "get_group_id", None)
        group_id = (
            get_group_id()
            if is_group_session(umo) and callable(get_group_id)
            else ""
        )
        get_platform_id = getattr(event, "get_platform_id", None)
        platform_id = get_platform_id() if callable(get_platform_id) else ""
        group_parser_keys = (
            group_enabled_parsers(self.cfg, platform_id, group_id)
            if group_id and platform_name == "aiocqhttp"
            else None
        )
        group_parser_map = None
        if group_parser_keys is not None:
            group_parser_map = {
                keyword: parser
                for keyword, parser in self.parser_map.items()
                if getattr(getattr(parser, "platform", None), "name", None)
                in group_parser_keys
            }

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if group_parser_map is not None and kw not in group_parser_map:
                continue
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
        parser = (
            group_parser_map.get(keyword)
            if group_parser_map is not None
            else self.parser_map.get(keyword)
        )
        if parser is None:
            return
        with cache_dir_scope(parse_cache_dir):
            parse_res = await parser.parse(keyword, searched)
        if parse_cache_dir is not None:
            parse_res.cache_dir = parse_cache_dir

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        await self.sender.send_parse_result(event, parse_res)

    @filter.regex(r"^\s*/(关闭|开启)([^\s/]{1,40})解析\s*$")
    async def toggle_group_parser(self, event: AstrMessageEvent):
        """让 OneBot 群主或管理员快速切换本群的单个解析器。"""
        if not is_group_session(event.unified_msg_origin):
            return
        command_match = re.fullmatch(
            r"\s*/(关闭|开启)([^\s/]{1,40})解析\s*",
            event.message_str or "",
        )
        if command_match is None:
            return
        if event.get_platform_name() != "aiocqhttp":
            event.stop_event()
            yield event.plain_result("解析器快捷开关目前仅支持 OneBot V11 群聊")
            return
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        sender = raw_message.get("sender") if isinstance(raw_message, dict) else None
        sender_role = (
            str(sender.get("role", "")).lower()
            if isinstance(sender, dict)
            else ""
        )
        if sender_role not in {"owner", "admin"}:
            event.stop_event()
            yield event.plain_result("此指令仅限群主或群管理员使用")
            return

        platform_id, group_id = event.get_platform_id(), event.get_group_id()
        parser_key = self._resolve_group_parser_key(command_match.group(2))
        if not platform_id or not group_id:
            event.stop_event()
            yield event.plain_result("无法识别当前 OneBot 实例或群聊")
            return
        if parser_key is None:
            event.stop_event()
            supported = "、".join(
                label for key, label in PARSER_LABELS.items() if key != "music_base"
            )
            yield event.plain_result(f"未知解析器。支持：{supported}")
            return
        if getattr(self, "group_parser_config", None) is None:
            event.stop_event()
            yield event.plain_result("群聊解析器设置服务未就绪，请重载插件后重试")
            return

        enabled = command_match.group(1) == "开启"
        try:
            current = await self.group_parser_config.set_group_parser(
                platform_id, group_id, parser_key, enabled
            )
            event.stop_event()
            state = "已启用" if parser_key in current else "已关闭"
            yield event.plain_result(
                f"当前群聊的{PARSER_LABELS[parser_key]}解析{state}"
            )
        except Exception:
            logger.warning("保存群聊解析器快捷设置失败")
            event.stop_event()
            yield event.plain_result("群聊解析器设置失败，请检查全局开关和 OneBot 群聊状态后重试")

    @staticmethod
    def _resolve_group_parser_key(value: str) -> str | None:
        normalized = re.sub(r"[\s_-]+", "", value).casefold()
        aliases = {
            "bilibili": "bilibili", "b站": "bilibili", "哔哩哔哩": "bilibili",
            "douyin": "douyin", "抖音": "douyin",
            "xhs": "xhs", "小红书": "xhs",
            "pixiv": "pixiv", "p站": "pixiv",
            "qqmusic": "qqmusic", "qq音乐": "qqmusic",
            "netease": "netease", "网易云": "netease", "网易云音乐": "netease",
            "kugou": "kugou", "酷狗": "kugou", "酷狗音乐": "kugou",
            "qishui": "qishui", "汽水": "qishui", "汽水音乐": "qishui",
            "kuwo": "kuwo", "酷我": "kuwo", "酷我音乐": "kuwo",
            "applemusic": "applemusic", "苹果音乐": "applemusic",
        }
        return aliases.get(normalized)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(normalize_session_scope(umo))
        target = "当前群聊" if is_group_session(umo) else "当前会话"
        yield event.plain_result(f"{target}的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(normalize_session_scope(umo))
        target = "当前群聊" if is_group_session(umo) else "当前会话"
        yield event.plain_result(f"{target}的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        self._ensure_parser_registration()
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        card_path = await self.renderer.render_bilibili_login_card(qrcode)
        if card_path is not None:
            yield event.chain_result([Image.fromFileSystem(str(card_path))])
        else:
            yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录QQ音乐", alias={"qqlogin", "登录qq音乐"})
    async def login_qqmusic(self, event: AstrMessageEvent):
        """扫码登录QQ音乐"""
        self._ensure_parser_registration()
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
        self._ensure_parser_registration()
        parser: KugouMusicParser = self._get_parser_by_type(KugouMusicParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        card_path = await self.renderer.render_kugou_login_card(qrcode)
        if card_path is not None:
            yield event.chain_result([Image.fromFileSystem(str(card_path))])
        else:
            yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .cache import cache_dir_scope
from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
    normalize_motion_photo_send_mode,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 在全局开关开启时为每个解析结果发送一张独立信息卡片
    - 控制媒体是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    _ERROR_MEDIA_PATH = Path(__file__).with_name("resources") / "error_media.png"
    _LIVE_PHOTO_INFO_PATH = Path(__file__).with_name("resources") / "livep_info.png"
    _LIVE_PHOTO_PLATFORMS = frozenset({"douyin", "xhs"})

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _card_enabled(self) -> bool:
        """卡片开关开启时，才允许渲染并发送信息卡片。"""
        return bool(getattr(self.cfg, "card_enabled", True))

    async def _render_card_safely(self, result: ParseResult) -> Path | None:
        """隔离卡片渲染异常，确保不会阻断原媒体发送流程。

        ``Renderer.render_card`` 本身会捕获已知渲染错误；发送器再保留一层
        边界保护，兼容自定义 Renderer、插件热重载或第三方模板过滤器抛出
        的未预期异常。卡片失败只意味着没有卡片消息段，媒体仍按原计划处理。
        """
        try:
            return await self.renderer.render_card(result)
        except Exception as exc:
            # AstrBot logger 提供 exception；测试桩或旧版本可能只有 error。
            log_exception = getattr(logger, "exception", None)
            message = f"卡片渲染异常，已跳过卡片发送: {exc}"
            if callable(log_exception):
                log_exception(message)
            else:
                logger.error(message)
            return None

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    def _append_download_failure_fallback(
        self,
        segments: list[BaseMessageComponent],
        content: object,
    ) -> None:
        """为下载失败的媒体追加与类型相符的可发送兜底内容。

        图片、图文、视频和动态媒体都改为发送本地静态占位图，避免把
        ``此项媒体下载失败`` 作为单独文字消息发出。音频和文件没有合适的
        图片替代形式，仍沿用原有文字提示。关闭失败提示开关时，保持原有的
        静默跳过行为。
        """
        if not getattr(self.cfg, "show_download_fail_tip", True):
            return

        if isinstance(
            content,
            (ImageContent, GraphicsContent, VideoContent, DynamicContent),
        ):
            fallback_path = self._ERROR_MEDIA_PATH
            if fallback_path.is_file():
                segments.append(self._image_from_path(fallback_path))
                return
            logger.error(f"媒体下载失败占位图不存在，回退文字提示: {fallback_path}")

        segments.append(Plain("此项媒体下载失败"))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 信息卡片由 send_parse_result 按每个 ParseResult 单独处理一次，不能被
        # SendGroup 的内容数量或旧版 render_card 偏好影响。
        seg_count = len(light) + len(heavy)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "force_merge": force_merge,
        }

    async def _send_result_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ) -> bool:
        """
        按 ParseResult 发送唯一的信息卡片（独立消息）。

        该步骤始终先于媒体发送执行；媒体是否折叠仅由 SendGroup 和媒体数量
        决定，卡片不会被折叠进媒体转发节点。
        """
        if not self._card_enabled():
            return False

        if image_path := await self._render_card_safely(result):
            try:
                await event.send(event.chain_result([self._image_from_path(image_path)]))
                return True
            except Exception as exc:
                # 卡片是附加消息；发送失败也不能阻断后续媒体。
                logger.error(f"信息卡片发送失败，继续发送媒体: {exc}")
        return False

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                self._append_download_failure_fallback(segs, cont)
                continue

            match cont:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as g:
                    segs.append(self._image_from_path(path))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except (SizeLimitException, DurationLimitException) as exc:
                if self.cfg.show_download_fail_tip:
                    message = (
                        "此项媒体超过时长限制"
                        if isinstance(exc, DurationLimitException)
                        else "此项媒体超过大小限制"
                    )
                    segs.append(Plain(message))
                continue
            except DownloadException:
                self._append_download_failure_fallback(segs, cont)
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(self._video_from_path(path))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        and result.extra.get("audio_as_voice") is not True
                        else self._record_from_path(path)
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(
        self,
        result: ParseResult,
        *,
        omit_playlist_cover: bool = False,
    ) -> list[SendGroup]:
        if result.send_groups:
            groups = result.send_groups
        else:
            groups = [SendGroup(contents=list(MessageSender._iter_contents(result)))]

        preview_only = result.extra.get("playlist_cover_only") is True or result.extra.get(
            "card_preview_only"
        ) is True
        if not omit_playlist_cover or not preview_only:
            return groups

        # 卡片预览仍保留在 ParseResult.contents 中供卡片渲染；卡片已成功
        # 发送后，只从实际媒体发送分组中去掉同一个内容对象。
        cover = result.contents[0] if result.contents else None
        if cover is None:
            return groups

        return [
            SendGroup(
                contents=[content for content in group.contents if content is not cover],
                force_merge=group.force_merge,
                render_card=group.render_card,
            )
            for group in groups
        ]

    def _prepare_motion_photo_group(
        self,
        result: ParseResult,
        groups: list[SendGroup],
    ) -> list[SendGroup]:
        """为抖音/小红书实况图追加说明图并强制折叠转发。"""
        mode = normalize_motion_photo_send_mode(
            result.extra.get("motion_photo_send_mode")
        )
        # video_only 已经由解析器把实况图替换为效果视频；不追加说明图，
        # 也不把视频强制折叠成旧版 Live Photo 转发。
        if mode == "video_only":
            return groups

        if (
            result.platform.name.lower() not in self._LIVE_PHOTO_PLATFORMS
            or not result.has_motion_photo
            or not self._LIVE_PHOTO_INFO_PATH.is_file()
        ):
            return groups

        contents = [content for group in groups for content in group.contents]
        if not contents:
            return groups

        return [
            SendGroup(
                contents=[*contents, ImageContent(self._LIVE_PHOTO_INFO_PATH)],
                force_merge=True,
            )
        ]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
        )

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if not segs:
            return False

        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return False

    @staticmethod
    async def _send_audio_url_via_onebot(
        event: AstrMessageEvent,
        url: str,
    ) -> bool:
        """让 OneBot 端按 URL 拉取语音，避免把整段音频塞进 WebSocket 帧。"""

        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            return False

        self_id = str(event.get_self_id() or "").strip()
        routing = {"self_id": int(self_id)} if self_id.isdigit() else {}
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            if not group_id.isdigit():
                raise RuntimeError("无效的群号，无法发送语音")
            await call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=[{"type": "record", "data": {"file": url}}],
                **routing,
            )
            return True

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id.isdigit():
            raise RuntimeError("无效的用户号，无法发送语音")
        await call_action(
            "send_private_msg",
            user_id=int(user_id),
            message=[{"type": "record", "data": {"file": url}}],
            **routing,
        )
        return True

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        # 解析阶段创建的任务已经继承了同一个 contextvar；发送阶段重新
        # 激活结果目录，确保卡片、发送过程中新建的临时资源也归档到同一处。
        with cache_dir_scope(result.cache_dir):
            await self._send_parse_result(event, result)

    async def _send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 发送全局信息卡片（如启用）
        2. 为各媒体分组构建发送计划和消息段
        3. 必要时合并转发
        4. 发送媒体；没有可发送媒体时保留原有文本兜底
        """
        # 全局卡片开关开启时，每个解析结果固定先发送一张信息卡片。媒体分组
        # 只决定媒体本身是否折叠，避免图集因卡片计数而改变发送结构。
        card_sent = await self._send_result_card(event, result)

        groups = self._prepare_motion_photo_group(
            result,
            self._resolve_groups(result, omit_playlist_cover=card_sent),
        )

        # 音乐单曲仍先下载到本次解析目录，但 OneBot 发送时传媒体 URL，
        # 让 Napcat 自行拉取并转码，避免本地 Record 被编码成超大 WebSocket 帧。
        direct_audio_url = result.extra.get("audio_send_url")
        direct_audio_content: AudioContent | None = None
        if (
            result.extra.get("audio_as_voice") is True
            and isinstance(direct_audio_url, str)
            and direct_audio_url.startswith(("http://", "https://"))
            and callable(getattr(getattr(event, "bot", None), "call_action", None))
        ):
            direct_audio_content = next(
                (
                    content
                    for group in groups
                    for content in group.contents
                    if isinstance(content, AudioContent)
                ),
                None,
            )
            if direct_audio_content is not None:
                try:
                    # 语音改走 OneBot URL 发送，但仍确保文件下载并保存在解析缓存。
                    await direct_audio_content.get_path()
                except (
                    DownloadException,
                    DownloadLimitException,
                    SizeLimitException,
                    DurationLimitException,
                    ZeroSizeException,
                ):
                    direct_audio_content = None
                else:
                    groups = [
                        SendGroup(
                            contents=[
                                content
                                for content in group.contents
                                if content is not direct_audio_content
                            ],
                            force_merge=group.force_merge,
                            render_card=group.render_card,
                        )
                        for group in groups
                    ]

        sent = False
        for group in groups:
            sent = await self._send_group(event, result, group) or sent

        if direct_audio_content is not None:
            try:
                sent = (
                    await self._send_audio_url_via_onebot(
                        event,
                        direct_audio_url,
                    )
                    or sent
                )
            except Exception as exc:
                logger.error(f"音乐语音 URL 发送失败：{exc}")
                # 已经尝试发送语音，不再追加一条与原消息无关的文本兜底。
                sent = True

        if not sent:
            # 卡片预览已发送；过滤掉独立预览图后，不再额外发送一条纯文本
            # 兜底消息，避免卡片后又出现无意义的重复内容。
            if card_sent and (
                result.extra.get("playlist_cover_only") is True
                or result.extra.get("card_preview_only") is True
            ):
                return
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return

            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return

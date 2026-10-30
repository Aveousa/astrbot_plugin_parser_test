import re
from collections.abc import Mapping
from dataclasses import dataclass
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
    MediaContent,
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

_AUDIO_URL_TTL_SECONDS = 300
_PIXIV_IMAGE_MESSAGE_MAX_BYTES = 20 * 1024 * 1024
_MIB = 1024 * 1024


@dataclass(slots=True)
class _PixivFileUpload:
    """A Pixiv image that must be delivered through OneBot's file action."""

    path: Path
    size: int
    name: str


def _log_info(message: str) -> None:
    """Write an informational log when the host logger exposes that level."""

    info = getattr(logger, "info", None)
    if callable(info):
        info(message)


def _safe_log_error(error: object) -> str:
    """Keep temporary callback URLs out of diagnostic messages."""

    value = re.sub(r"https?://[^\s\"'<>]+", "[URL]", str(error))
    return value[:300]


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _size_label(size: int | None) -> str:
    return f"{size / 1024 / 1024:.2f}MB" if size is not None else "未知"


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 在全局开关开启时为每个解析结果生成信息卡片
    - 控制媒体是否强制合并转发
    - 按可选全局策略将所有非音频内容集中折叠，音频保持独立发送
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

    @staticmethod
    def _detect_delivery_mode(event: AstrMessageEvent) -> str:
        """Detect the outbound file-delivery protocol for this event."""

        bot = getattr(event, "bot", None)
        values: list[str] = []
        for getter_name in ("get_platform_name", "get_platform_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    value = None
                if value:
                    values.append(str(value).strip().lower())

        if any(value == "qq_official" for value in values):
            return "qq_official"

        event_class = getattr(event, "__class__", None)
        event_module = str(getattr(event_class, "__module__", "")).lower()
        event_name = str(getattr(event_class, "__name__", "")).lower()
        if (
            event_name == "qqofficialmessageevent"
            or ".qqofficial." in event_module
        ):
            return "qq_official"

        if callable(getattr(bot, "call_action", None)):
            return "onebot"
        return "native"

    @staticmethod
    def _log_delivery_mode(
        event: AstrMessageEvent,
        result: ParseResult,
        mode: str,
    ) -> None:
        """Log the protocol selected for every parsed result."""

        adapter = getattr(event, "get_platform_name", None)
        try:
            adapter_name = adapter() if callable(adapter) else "unknown"
        except Exception:
            adapter_name = "unknown"
        _log_info(
            f"解析发送适配器识别：mode={mode} adapter={adapter_name} "
            f"source={result.platform.name.lower()}"
        )

    def _compact_forward_non_audio_enabled(self) -> bool:
        """Return whether non-audio content should use the anti-spam mode."""

        return bool(getattr(self.cfg, "compact_forward_non_audio", False))

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
    def _audio_attachment_filename(title: object, path: Path, fallback: str) -> str:
        """Build a portable attachment filename from a track title."""
        suffix = path.suffix or ".m4a"
        stem = str(title or "").strip()
        if stem.lower().endswith(suffix.lower()):
            stem = stem[: -len(suffix)]
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .")
        if not stem:
            stem = path.stem or fallback
        if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            stem = f"_{stem}"
        # Leave room for the extension even on filesystems with short limits.
        stem = stem[: max(1, 180 - len(suffix))].rstrip(" .") or fallback
        return f"{stem}{suffix}"

    @staticmethod
    def _apple_music_audio_filename(title: object, path: Path) -> str:
        """Backward-compatible Apple Music filename helper."""
        return MessageSender._audio_attachment_filename(title, path, "Apple Music")

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

    @staticmethod
    def _suppress_result_text(result: ParseResult) -> bool:
        """Whether Bilibili body text is card-only, not a standalone message."""
        return (
            result.platform.name.lower() == "bilibili"
            and (
                result.extra.get("bilibili_opus_images_only") is True
                or result.extra.get("bilibili_text_card_only") is True
            )
        )

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
                    if g.text and not self._suppress_result_text(result):
                        segs.append(Plain(g.text))
                    if g.alt and not self._suppress_result_text(result):
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
                    qishui_audio_name = None
                    if result.platform.name.lower() == "qishui":
                        qishui_audio_name = self._audio_attachment_filename(
                            result.title, path, "汽水音乐"
                        )
                    audio_as_file = (
                        result.extra.get("audio_as_file") is True
                        or result.extra.get("audio_delivery_mode") == "file"
                        or (
                            result.extra.get("audio_send_via_url") is True
                            and result.extra.get("audio_as_voice") is not True
                        )
                        or (
                            self.cfg.audio_to_file
                            and result.extra.get("audio_as_voice") is not True
                        )
                    )
                    audio_file_name = qishui_audio_name or path.name
                    if audio_as_file and result.platform.name.lower() != "qishui":
                        fallback = {
                            "applemusic": "Apple Music",
                            "netease": "网易云音乐",
                            "qqmusic": "QQ 音乐",
                            "kugou": "酷狗音乐",
                        }.get(result.platform.name.lower(), "音乐")
                        audio_file_name = self._audio_attachment_filename(
                            result.title, path, fallback
                        )
                    segs.append(
                        File(
                            name=audio_file_name,
                            file=self._to_file_uri(path),
                        )
                        if audio_as_file
                        else self._record_from_path(path)
                    )
                case FileContent():
                    segs.append(
                        File(
                            name=cont.name or path.name,
                            file=self._to_file_uri(path),
                        )
                    )

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
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or "").strip()
        # OneBot custom nodes have no separate avatar URL field. Using the
        # triggering sender's QQ ID lets compatible adapters/clients resolve
        # that user's avatar; fall back to the bot identity if it is missing.
        node_uin = sender_id or event.get_self_id()
        node_name = sender_name or sender_id or "解析器"

        for seg in segs:
            nodes.nodes.append(
                Node(uin=node_uin, name=node_name, content=[seg])
            )

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        if MessageSender._suppress_result_text(result):
            return []

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

    def _prepare_compact_forward_groups(
        self,
        groups: list[SendGroup],
    ) -> list[SendGroup]:
        """Apply the optional global anti-spam policy to media groups.

        The policy intentionally operates on ``MediaContent`` before download
        and message-component conversion.  This keeps ``AudioContent``
        distinguishable from ordinary files, so audio remains a normal voice
        or file message while every other content type can be folded together.
        Explicit parser groups are flattened only in this opt-in mode; the
        default sender path keeps their existing semantics unchanged.
        """

        if not self._compact_forward_non_audio_enabled():
            return groups

        non_audio: list[MediaContent] = []
        audio_groups: list[SendGroup] = []
        for group in groups:
            group_audio = [
                content
                for content in group.contents
                if isinstance(content, AudioContent)
            ]
            non_audio.extend(
                content
                for content in group.contents
                if not isinstance(content, AudioContent)
            )
            if group_audio:
                # Audio must never be pulled into a forward node, even when a
                # parser explicitly marked its original group as mergeable.
                audio_groups.append(
                    SendGroup(
                        contents=group_audio,
                        force_merge=False,
                        render_card=group.render_card,
                    )
                )

        compact_group = None
        if non_audio:
            # A single item does not benefit from a forward node and would
            # make the message look more complicated rather than less noisy.
            compact_group = SendGroup(
                contents=non_audio,
                force_merge=len(non_audio) > 1,
            )

        return ([compact_group] if compact_group is not None else []) + audio_groups

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
        force_merge = plan["force_merge"]
        if self._compact_forward_non_audio_enabled() and len(segs) < 2:
            # A failed download or an empty TextContent can reduce a compact
            # group to one actual component; do not leave a pointless single
            # node behind in that case.
            force_merge = False
        if self._detect_delivery_mode(event) == "qq_official":
            # QQ Official's adapter serializes Image/Video/File/Record
            # components, but does not translate AstrBot forward Nodes back
            # into those media components. Keep the native media chain intact.
            force_merge = False
        segs = self._merge_segments_if_needed(event, segs, force_merge)

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
    def _pixiv_image_threshold_bytes(
        result: ParseResult, config: PluginConfig
    ) -> int:
        """Return the Pixiv single-image OneBot upload threshold in bytes."""
        configured = None
        try:
            parser_config = getattr(config, "parser", None)
            pixiv_config = getattr(parser_config, "pixiv", None)
            configured = getattr(pixiv_config, "image_file_threshold_mb", None)
        except (AttributeError, TypeError):
            configured = None
        if configured is None:
            configured = result.extra.get("pixiv_image_file_threshold_mb", 10)
        try:
            threshold_mb = int(configured)
        except (TypeError, ValueError, OverflowError):
            threshold_mb = 10
        # Keep runtime behavior inside the range exposed by the configuration UI,
        # including for old or hand-edited config files.
        return min(10, max(1, threshold_mb)) * _MIB

    @staticmethod
    def _pixiv_image_batches(
        contents: list[MediaContent],
        sizes: list[int | None],
        threshold_bytes: int,
        file_names: list[str] | None = None,
    ) -> list[list[MediaContent | _PixivFileUpload]]:
        """Split Pixiv pages in source order under the OneBot 20 MiB limit.

        An individual image above the configured threshold is emitted as a
        standalone file-upload item; remaining images are packed greedily into
        batches whose total image bytes never exceed 20 MiB.
        """
        batches: list[list[MediaContent | _PixivFileUpload]] = []
        batch: list[MediaContent | _PixivFileUpload] = []
        batch_size = 0

        def flush() -> None:
            nonlocal batch, batch_size
            if batch:
                batches.append(batch)
                batch = []
                batch_size = 0

        names = file_names or [
            content.path_task.name
            if isinstance(content, ImageContent)
            and isinstance(content.path_task, Path)
            else f"pixiv_p{index}.jpg"
            for index, content in enumerate(contents)
        ]
        for content, size, name in zip(contents, sizes, names, strict=True):
            if size is None:
                # A failed or unmeasurable image cannot be counted safely.
                # Isolate it so it never makes a neighboring known-size batch
                # exceed the hard cap; normal sending handles its failure.
                flush()
                batches.append([content])
                continue

            if size > threshold_bytes:
                flush()
                if isinstance(content, ImageContent) and size > 0:
                    path_task = content.path_task
                    if isinstance(path_task, Path):
                        batches.append([_PixivFileUpload(path_task, size, name)])
                        continue
                # A missing file or unexpected media type cannot use the direct
                # upload path; keep it in the regular image flow.
                batch.append(content)
                continue

            # Keep the hard message cap independent from the configurable
            # single-image threshold.
            if size > _PIXIV_IMAGE_MESSAGE_MAX_BYTES:
                flush()
                if isinstance(content, ImageContent) and isinstance(
                    content.path_task, Path
                ):
                    batches.append([_PixivFileUpload(content.path_task, size, name)])
                else:
                    batch.append(content)
                continue

            if batch and batch_size + size > _PIXIV_IMAGE_MESSAGE_MAX_BYTES:
                flush()
            batch.append(content)
            batch_size += size

        flush()
        return batches

    @staticmethod
    def _pixiv_image_filename(
        result: ParseResult, page_index: int, path: Path
    ) -> str:
        """Build a stable Pixiv-style attachment name from artwork/page IDs."""
        match = re.search(r"/artworks/([0-9]+)(?:[/?#]|$)", result.url or "")
        artwork_id = match.group(1) if match else "pixiv"
        suffix = path.suffix.lower()
        if (
            not suffix.startswith(".")
            or not suffix[1:].isascii()
            or not suffix[1:].isalnum()
            or len(suffix) > 11
        ):
            suffix = ".jpg"
        return f"{artwork_id}_p{page_index}{suffix}"

    @staticmethod
    def _pixiv_page_indexes(groups: list[SendGroup]) -> dict[int, int]:
        """Map each original image object to its zero-based artwork page index."""
        indexes: dict[int, int] = {}
        page_index = 0
        for group in groups:
            for content in group.contents:
                if isinstance(content, ImageContent) and id(content) not in indexes:
                    indexes[id(content)] = page_index
                    page_index += 1
        return indexes

    async def _prepare_pixiv_image_groups(
        self,
        result: ParseResult,
        groups: list[SendGroup],
    ) -> list[SendGroup | _PixivFileUpload]:
        """Download, measure, and batch Pixiv image groups before sending."""
        if result.platform.name.lower() != "pixiv":
            return list(groups)

        send_items: list[SendGroup | _PixivFileUpload] = []
        threshold_bytes = self._pixiv_image_threshold_bytes(result, self.cfg)
        page_indexes = self._pixiv_page_indexes(groups)

        for group in groups:
            if not group.contents or not all(
                isinstance(content, ImageContent) for content in group.contents
            ):
                send_items.append(group)
                continue

            ready_images: list[ImageContent] = []
            image_sizes: list[int | None] = []
            file_names: list[str] = []
            for image in group.contents:
                page_index = page_indexes.get(id(image), 0)
                try:
                    path = await image.get_path()
                except Exception as exc:
                    # Preserve the normal sender error/fallback behavior for a
                    # failed download. It will be retried through _send_group,
                    # which already contains the established failure policy.
                    _log_info(
                        f"Pixiv 图片预处理失败，回退常规发送：error={_safe_log_error(exc)}"
                    )
                    ready_images.append(image)
                    image_sizes.append(None)
                    file_names.append(
                        self._pixiv_image_filename(result, page_index, Path("image.jpg"))
                    )
                    continue
                size = _file_size(path)
                if size is None:
                    # Preserve standard handling for an unreadable or missing
                    # cached path; isolate it from the known-size batches.
                    ready_images.append(image)
                    image_sizes.append(None)
                    file_names.append(self._pixiv_image_filename(result, page_index, path))
                    continue
                ready_images.append(image)
                image_sizes.append(size)
                file_names.append(self._pixiv_image_filename(result, page_index, path))

            if not ready_images:
                send_items.append(group)
                continue

            batches = self._pixiv_image_batches(
                ready_images, image_sizes, threshold_bytes, file_names
            )
            if len(batches) == 1 and all(
                isinstance(content, ImageContent) for content in batches[0]
            ):
                send_items.append(group)
                continue

            for batch in batches:
                if len(batch) == 1 and isinstance(batch[0], _PixivFileUpload):
                    send_items.append(batch[0])
                    continue
                images = [
                    content
                    for content in batch
                    if isinstance(content, ImageContent)
                ]
                if images:
                    send_items.append(
                        SendGroup(
                            contents=images,
                            force_merge=(
                                group.force_merge
                                or (
                                    self._compact_forward_non_audio_enabled()
                                    and len(images) > 1
                                )
                            ),
                            render_card=group.render_card,
                        )
                    )

        return send_items

    async def _send_pixiv_file_upload(
        self,
        event: AstrMessageEvent,
        upload: _PixivFileUpload,
        delivery_mode: str,
    ) -> bool:
        """Send an oversized Pixiv image through the selected file protocol."""
        if delivery_mode == "qq_official":
            _log_info(
                f"QQ Official file send: source=pixiv name={upload.name} "
                f"size={_size_label(upload.size)}"
            )
            await event.send(
                event.chain_result(
                    [File(name=upload.name, file=self._to_file_uri(upload.path))]
                )
            )
            return True

        if not callable(getattr(getattr(event, "bot", None), "call_action", None)):
            _log_info("Pixiv file send skipped: current event has no file action")
            return False

        url = await self._register_local_file_url(
            upload.path,
            platform="pixiv",
            name=upload.name,
            file_size=upload.size,
        )
        if not url:
            _log_info(
                f"Pixiv 图片转文件发送跳过：name={upload.name} "
                "原因=OneBot 临时文件 URL 不可用"
            )
            return False

        return await self._send_url_via_onebot(
            event,
            url,
            message_type="file",
            name=upload.name,
            platform="pixiv",
            file_size=upload.size,
        )

    @staticmethod
    async def _send_url_via_onebot(
        event: AstrMessageEvent,
        url: str,
        *,
        message_type: str = "record",
        name: str | None = None,
        platform: str = "music",
        file_size: int | None = None,
        quality: str | None = None,
    ) -> bool:
        """让 OneBot 端按 URL 发送音频或上传文件。

        语音通过标准消息段发送；文件通过 OneBot 的 upload_*_file action
        上传到对应会话。日志记录状态、文件名和体积，不记录临时文件 URL。
        """

        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            return False

        self_id = str(event.get_self_id() or "").strip()
        routing = {"self_id": int(self_id)} if self_id.isdigit() else {}
        group_id = str(event.get_group_id() or "").strip()
        if message_type not in {"record", "file"}:
            raise RuntimeError("无效的 OneBot 音频消息类型")

        if message_type == "file":
            if not name:
                raise RuntimeError("OneBot 文件上传缺少文件名")
            quality_label = (
                f" quality={quality}"
                if isinstance(quality, str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", quality)
                else ""
            )
            if group_id:
                if not group_id.isdigit():
                    raise RuntimeError("无效的群号，无法上传 OneBot 群文件")
                action = "upload_group_file"
                action_args = {
                    "group_id": int(group_id),
                    "file": url,
                    "name": name,
                    **routing,
                }
                target = "group"
            else:
                user_id = str(event.get_sender_id() or "").strip()
                if not user_id.isdigit():
                    raise RuntimeError("无效的用户号，无法上传 OneBot 私聊文件")
                action = "upload_private_file"
                action_args = {
                    "user_id": int(user_id),
                    "file": url,
                    "name": name,
                    **routing,
                }
                target = "private"

            platform_label = "Pixiv" if platform == "pixiv" else "音乐"
            _log_info(
                f"开始 OneBot {platform_label}文件上传：platform={platform} "
                f"target={target} action={action} name={name} "
                f"size={_size_label(file_size)}{quality_label}"
            )
            try:
                response = await call_action(action, **action_args)
                if isinstance(response, Mapping) and response.get("status") == "failed":
                    raise RuntimeError(
                        response.get("wording")
                        or response.get("message")
                        or f"OneBot action {action} failed"
                    )
            except Exception as exc:
                _log_info(
                    f"OneBot {platform_label}文件上传失败：platform={platform} target={target} "
                    f"action={action} name={name} size={_size_label(file_size)}"
                    f"{quality_label} error={_safe_log_error(exc)}"
                )
                raise
            _log_info(
                f"OneBot {platform_label}文件上传成功：platform={platform} target={target} "
                f"action={action} name={name} size={_size_label(file_size)}{quality_label}"
            )
            return True

        data = {"file": url}
        if group_id:
            if not group_id.isdigit():
                raise RuntimeError("无效的群号，无法发送 OneBot 音频")
            await call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=[{"type": message_type, "data": data}],
                **routing,
            )
            return True

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id.isdigit():
            raise RuntimeError("无效的用户号，无法发送 OneBot 音频")
        await call_action(
            "send_private_msg",
            user_id=int(user_id),
            message=[{"type": message_type, "data": data}],
            **routing,
        )
        return True

    @staticmethod
    async def _register_local_file_url(
        path: Path,
        *,
        platform: str = "music",
        name: str | None = None,
        file_size: int | None = None,
    ) -> str | None:
        """Expose a cached local file through AstrBot's one-shot file URL.

        This lets OneBot/NapCat fetch the bytes itself instead of having
        AstrBot serialize a potentially large local file into the message.
        """

        label = name or path.name
        try:
            from astrbot.core import astrbot_config, file_token_service

            callback_base = astrbot_config.get("callback_api_base", "")
            if not isinstance(callback_base, str) or not callback_base.strip():
                platform_label = "Pixiv" if platform == "pixiv" else "音乐"
                _log_info(
                    f"OneBot {platform_label}临时文件 URL 注册跳过：platform={platform} "
                    f"name={label} size={_size_label(file_size)} "
                    "原因=callback_api_base 未配置"
                )
                return None
            callback_base = callback_base.strip()
        except Exception as exc:
            platform_label = "Pixiv" if platform == "pixiv" else "音乐"
            _log_info(
                f"OneBot {platform_label}临时文件 URL 注册失败：platform={platform} "
                f"name={label} size={_size_label(file_size)} "
                f"error={_safe_log_error(exc)}"
            )
            return None

        platform_label = "Pixiv" if platform == "pixiv" else "音乐"
        _log_info(
            f"开始注册 OneBot {platform_label}临时文件 URL：platform={platform} "
            f"name={label} size={_size_label(file_size)} "
            f"expires_in={_AUDIO_URL_TTL_SECONDS}s "
            f"有效期={_AUDIO_URL_TTL_SECONDS}秒"
        )
        try:
            token = await file_token_service.register_file(
                str(path), timeout=_AUDIO_URL_TTL_SECONDS
            )
            token = "" if token is None else str(token).strip()
            if not token:
                raise ValueError("file_token_service 未返回有效 token")
            url = f"{callback_base.rstrip('/')}/api/file/{token}"
        except Exception as exc:
            _log_info(
                f"OneBot {platform_label}临时文件 URL 注册失败：platform={platform} "
                f"name={label} size={_size_label(file_size)} "
                f"expires_in={_AUDIO_URL_TTL_SECONDS}s "
                f"有效期={_AUDIO_URL_TTL_SECONDS}秒 "
                f"error={_safe_log_error(exc)}"
            )
            return None

        _log_info(
            f"OneBot {platform_label}临时文件 URL 注册成功：platform={platform} "
            f"name={label} size={_size_label(file_size)} "
            f"expires_in={_AUDIO_URL_TTL_SECONDS}s "
            f"有效期={_AUDIO_URL_TTL_SECONDS}秒"
        )
        return url

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
            # The compact forwarding mode renders the lyric card before the
            # media groups are sent and injects it into the same non-audio
            # group.  The normal mode keeps the historical standalone lyric
            # message and sends it after the regular result.
            if not self._compact_forward_non_audio_enabled():
                await self._send_lyrics_card(event, result)

    async def _render_lyrics_card(self, result: ParseResult) -> Path | None:
        """Render an optional lyric card and return its local image path.

        Rendering is kept separate from sending so the compact forwarding
        policy can collect the lyric card together with the information card
        and ordinary media before deciding whether a forward node is needed.
        """

        platform_key = result.platform.name.lower()
        if result.extra.get("music_type") != "track":
            if platform_key == "kugou":
                _log_info("[酷狗音乐] 歌词卡片跳过：解析结果不是单曲")
            return None
        if platform_key not in {
            "netease",
            "qqmusic",
            "kugou",
            "applemusic",
        }:
            return None
        parser_config = getattr(getattr(self.cfg, "parser", None), platform_key, None)
        if not bool(getattr(parser_config, "parse_lyrics", False)):
            if platform_key == "kugou":
                _log_info("[酷狗音乐] 歌词卡片跳过：解析歌词开关未开启")
            return None
        lyrics = result.extra.get("lyrics")
        if not isinstance(lyrics, dict) or not lyrics.get("text"):
            if platform_key == "kugou":
                _log_info("[酷狗音乐] 歌词卡片跳过：解析结果中没有可呈现的歌词")
            return None
        render = getattr(self.renderer, "render_lyrics_card", None)
        if not callable(render):
            if platform_key == "kugou":
                _log_info("[酷狗音乐] 歌词卡片跳过：歌词卡片渲染器不可用")
            return None
        if platform_key == "kugou":
            lyric_text = str(lyrics["text"])
            _log_info(
                f"[酷狗音乐] 已取得歌词，开始生成歌词卡片（chars={len(lyric_text)}）"
            )

        # Keep the existing display by default for older configs.  This is a
        # presentation-only switch; lyric fetching and parsing have already
        # completed before the card is rendered.
        show_release_time = bool(
            getattr(self.cfg, "lyrics_show_release_time", True)
        )
        cover_path = None
        for content in result.contents:
            if isinstance(content, ImageContent):
                try:
                    cover_path = await content.get_path()
                except Exception:
                    cover_path = None
                break
        try:
            image_path = await render(
                lyrics["text"],
                platform_key=platform_key,
                platform_display_name=result.platform.display_name,
                song_title=str(lyrics.get("title") or result.title or "未知歌曲"),
                artists=lyrics.get("artists")
                or (result.author.name if result.author else None),
                album=lyrics.get("album"),
                duration_seconds=lyrics.get("duration_seconds"),
                release_text=(
                    lyrics.get("release_text") if show_release_time else None
                ),
                show_release_time=show_release_time,
                source_label=lyrics.get("source_label"),
                cover_path=cover_path,
            )
            if not image_path:
                if platform_key == "kugou":
                    _log_info("[酷狗音乐] 歌词卡片渲染器未生成图片，卡片跳过")
                return None
            if platform_key == "kugou":
                _log_info("[酷狗音乐] 歌词卡片生成成功")
            return Path(image_path)
        except Exception as exc:
            logger.error(
                f"[{result.platform.display_name}] 歌词卡片生成失败，已跳过: "
                f"error_type={type(exc).__name__}"
            )
            return None

    async def _send_lyrics_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ) -> None:
        """Send an optional lyrics card after the regular single-track result."""

        if image_path := await self._render_lyrics_card(result):
            try:
                await event.send(
                    event.chain_result([self._image_from_path(image_path)])
                )
                if result.platform.name.lower() == "kugou":
                    _log_info("[酷狗音乐] 歌词卡片发送成功")
            except Exception as exc:
                logger.error(
                    f"[{result.platform.display_name}] 歌词卡片发送失败，已跳过: "
                    f"error_type={type(exc).__name__}"
                )

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
        delivery_mode = self._detect_delivery_mode(event)
        self._log_delivery_mode(event, result, delivery_mode)
        unsupported_audio_url_delivery = False

        # 默认模式固定先发送信息卡片；紧凑模式则只渲染卡片，稍后与所有
        # 非音频内容一起整理，避免同一个解析结果产生多条独立消息。
        # Bilibili Opus requests explicitly keep the card separate from the
        # following image-only media message, even when global compact mode is
        # enabled.
        compact_mode = (
            self._compact_forward_non_audio_enabled()
            and not self._suppress_result_text(result)
        )
        card_path: Path | None = None
        pixiv_compact_card_sent = False
        if compact_mode:
            # In compact mode the information card is a non-audio item too.
            # Render it now, but defer sending until all result contents have
            # been collected so a video/image/card combination becomes one
            # forward message instead of several standalone messages.
            if self._card_enabled():
                card_path = await self._render_card_safely(result)
            card_sent = False
        else:
            # Keep the historical standalone information-card behavior when
            # the anti-spam option is disabled.
            card_sent = await self._send_result_card(event, result)

        lyrics_path: Path | None = None
        if compact_mode:
            # Lyrics cards are generated content as well; collect them before
            # the media groups are normalized.  The normal mode sends this
            # card after the regular result in ``send_parse_result``.
            lyrics_path = await self._render_lyrics_card(result)

        groups = self._prepare_motion_photo_group(
            result,
            self._resolve_groups(
                result,
                # A deferred compact card still contains the preview cover,
                # so remove the parser's duplicate cover from the media list.
                omit_playlist_cover=card_sent or card_path is not None,
            ),
        )

        if compact_mode:
            generated_non_audio: list[MediaContent] = []
            if card_path is not None:
                generated_non_audio.append(ImageContent(Path(card_path)))
            if lyrics_path is not None:
                generated_non_audio.append(ImageContent(Path(lyrics_path)))
            if generated_non_audio:
                # ``_prepare_compact_forward_groups`` will flatten this
                # synthetic group together with parser-owned groups while
                # keeping every AudioContent in its own message.
                if result.platform.name.lower() == "pixiv":
                    # Pixiv page batches must remain independent. Keep its
                    # optional card standalone instead of flattening it back
                    # together with every page.
                    for content in generated_non_audio:
                        if not isinstance(content, ImageContent):
                            continue
                        try:
                            card_image = await content.get_path()
                            await event.send(
                                event.chain_result([self._image_from_path(card_image)])
                            )
                            card_sent = True
                            pixiv_compact_card_sent = True
                        except Exception as exc:
                            logger.error(f"Pixiv 信息卡片发送失败，继续发送图片：{exc}")
                else:
                    groups = [SendGroup(contents=generated_non_audio), *groups]

        # 选择通过 OneBot URL 交付的音乐单曲先保存在本次解析目录，再注册
        # 临时 URL；Apple Music 与网易云/QQ/酷狗的文件模式共用此路径。
        direct_audio_url = result.extra.get("audio_send_url")
        audio_url_delivery_requested = result.extra.get("audio_send_via_url") is True
        use_local_audio_url = (
            audio_url_delivery_requested and delivery_mode == "onebot"
        )
        direct_audio_content: AudioContent | None = None
        direct_audio_name: str | None = None
        direct_audio_size: int | None = None
        audio_is_voice = (
            result.extra.get("audio_as_voice") is True
            or result.extra.get("audio_delivery_mode") == "voice"
        )
        audio_is_file = (
            result.extra.get("audio_as_file") is True
            or result.extra.get("audio_delivery_mode") == "file"
            or (
                result.platform.name.lower() == "applemusic"
                and not audio_is_voice
            )
        )
        has_onebot_action = delivery_mode == "onebot"
        if (
            (use_local_audio_url or audio_is_voice or audio_is_file)
            and (
                (
                    isinstance(direct_audio_url, str)
                    and direct_audio_url.startswith(("http://", "https://"))
                )
                or use_local_audio_url
            )
            and has_onebot_action
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
                    # OneBot URL 发送前确保文件已下载并保存在解析缓存。
                    audio_path = await direct_audio_content.get_path()
                except Exception as exc:
                    _log_info(
                        f"OneBot 音乐文件准备失败：platform={result.platform.name.lower()} "
                        f"error={_safe_log_error(exc)}"
                    )
                    direct_audio_content = None
                else:
                    direct_audio_size = _file_size(audio_path)
                    direct_audio_name = audio_path.name
                    if (
                        result.extra.get("audio_as_file") is True
                        or result.extra.get("audio_delivery_mode") == "file"
                        or (
                            result.platform.name.lower() == "applemusic"
                            and result.extra.get("audio_as_voice") is not True
                        )
                    ):
                        fallback = {
                            "applemusic": "Apple Music",
                            "netease": "网易云音乐",
                            "qqmusic": "QQ 音乐",
                            "kugou": "酷狗音乐",
                            "qishui": "汽水音乐",
                        }.get(result.platform.name.lower(), "音乐")
                        direct_audio_name = self._audio_attachment_filename(
                            result.title, audio_path, fallback
                        )
                    if use_local_audio_url and not (
                        isinstance(direct_audio_url, str)
                        and direct_audio_url.startswith(("http://", "https://"))
                    ):
                        direct_audio_url = await self._register_local_file_url(
                            audio_path,
                            platform=result.platform.name.lower(),
                            name=direct_audio_name,
                            file_size=direct_audio_size,
                        )
                    if direct_audio_url:
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
                    else:
                        # OneBot must be able to fetch the one-shot URL. Never
                        # fall back to AstrBot's native local-file component for
                        # Apple Music audio, which can copy/serialize large files.
                        direct_audio_content = None

        # OneBot-only local URL delivery must not fall back to AstrBot's
        # native media serialization if registration or the action fails.
        # QQ Official deliberately takes the native File/Record path instead.
        unsupported_audio_url_delivery = (
            audio_url_delivery_requested
            and delivery_mode not in {"onebot", "qq_official"}
        )
        if unsupported_audio_url_delivery:
            _log_info(
                f"Native audio send skipped: source={result.platform.name.lower()} "
                "reason=unsupported delivery mode"
            )
            groups = [
                SendGroup(
                    contents=[
                        content
                        for content in group.contents
                        if not isinstance(content, AudioContent)
                    ],
                    force_merge=group.force_merge,
                    render_card=group.render_card,
                )
                for group in groups
            ]
        if use_local_audio_url and direct_audio_content is None:
            groups = [
                SendGroup(
                    contents=[
                        content for content in group.contents
                        if not isinstance(content, AudioContent)
                    ],
                    force_merge=group.force_merge,
                    render_card=group.render_card,
                )
                for group in groups
            ]

        groups = self._prepare_compact_forward_groups(groups)

        send_items = await self._prepare_pixiv_image_groups(result, groups)

        sent = False
        for item in send_items:
            if isinstance(item, _PixivFileUpload):
                try:
                    item_sent = await self._send_pixiv_file_upload(
                        event, item, delivery_mode
                    )
                except Exception as exc:
                    logger.error(
                        f"Pixiv file send failed: name={item.name} "
                        f"error={_safe_log_error(exc)}"
                    )
                    # Upload was attempted; do not also encode the oversized
                    # original image as a normal message component.
                    item_sent = True
                sent = item_sent or sent
                continue

            group = item
            group_has_lyrics_card = lyrics_path is not None and any(
                isinstance(content, ImageContent)
                and content.path_task == Path(lyrics_path)
                for content in group.contents
            )
            group_sent = await self._send_group(event, result, group)
            if group_has_lyrics_card and result.platform.name.lower() == "kugou":
                _log_info(
                    "[酷狗音乐] 歌词卡片随紧凑转发消息发送成功"
                    if group_sent
                    else "[酷狗音乐] 歌词卡片所在紧凑转发消息发送失败或无可发送内容"
                )
            sent = group_sent or sent

        if direct_audio_content is not None:
            message_type = (
                "record"
                if (
                    result.extra.get("audio_as_voice") is True
                    or result.extra.get("audio_delivery_mode") == "voice"
                )
                else "file"
            )
            file_size = direct_audio_size
            try:
                sent = (
                    await self._send_url_via_onebot(
                        event,
                        direct_audio_url,
                        message_type=message_type,
                        name=direct_audio_name,
                        platform=result.platform.name.lower(),
                        file_size=file_size,
                        quality=result.extra.get("audio_quality"),
                    )
                    or sent
                )
            except Exception as exc:
                if message_type == "file":
                    logger.error(
                        f"OneBot 音乐文件上传失败：platform={result.platform.name.lower()} "
                        f"name={direct_audio_name} error={_safe_log_error(exc)}"
                    )
                else:
                    logger.error(
                        f"OneBot 音乐语音发送失败：platform={result.platform.name.lower()} "
                        f"error={_safe_log_error(exc)}"
                    )
                # 已经尝试发送音频，不再追加一条与原消息无关的文本兜底。
                sent = True

        if (
            not sent
            and pixiv_compact_card_sent
            and result.platform.name.lower() == "pixiv"
        ):
            # The compact Pixiv card used to count as its own merged media
            # group; after keeping it standalone, retain the same no-fallback
            # behavior when a result has no image pages to send.
            return

        if not sent and self._suppress_result_text(result):
            # Bilibili dynamic/Opus text is card-only. If no media was sent,
            # do not turn it into a duplicate standalone text message.
            return

        if not sent:
            if use_local_audio_url or unsupported_audio_url_delivery:
                # Apple Music's OneBot-only audio path may intentionally omit
                # the audio when the platform/file URL is unavailable. The
                # song card already contains useful information; avoid a
                # misleading generic fallback message in that case.
                return
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

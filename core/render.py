"""HTML card renderer backed by Jinja2 and Playwright Chrome Headless Shell."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Mapping
from html import escape as html_escape
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from .cache import get_active_cache_dir
from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    MediaContent,
    ParseResult,
    TextContent,
    VideoContent,
)
from .exception import DownloadException

try:
    from jinja2 import (
        ChoiceLoader,
        Environment,
        FileSystemLoader,
        TemplateNotFound,
        select_autoescape,
    )
    from markupsafe import Markup, escape

    _JINJA_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖应由 requirements.txt 提供
    ChoiceLoader = Environment = FileSystemLoader = TemplateNotFound = select_autoescape = None  # type: ignore[assignment,misc]
    Markup = str  # type: ignore[assignment,misc]
    _JINJA_AVAILABLE = False

try:
    from playwright.async_api import async_playwright

    _PLAYWRIGHT_AVAILABLE = True
    _PLAYWRIGHT_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - requirements.txt 提供依赖
    # Playwright 是可选的卡片能力。缺少依赖时保留原媒体发送流程，
    # 但不切换到其他图片渲染器。
    async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_AVAILABLE = False
    _PLAYWRIGHT_IMPORT_ERROR = exc

try:
    from apilmoji import EmojiCDNSource

    _APILMOJI_AVAILABLE = True
except ImportError:  # pragma: no cover - 可选的表情图片增强
    EmojiCDNSource = None  # type: ignore[assignment,misc]
    _APILMOJI_AVAILABLE = False


# Unicode Emoji（含 ZWJ 序列、变体选择器、肤色、键帽和旗帜 tag 序列）。
# HTML 模板中的 .emoji--apple 类会优先使用 Apple Color Emoji，系统缺少该
# 字体时再回退。键帽/©️/™️ 等是 iOS 分享文案中常见、但不在 1Fxxx 区间的表情。
_EMOJI_BASE = (
    r"(?:[\U0001F1E6-\U0001FAFF\u2600-\u27BF\u00A9\u00AE\u203C\u2049"
    r"\u2122\u2139\u3030\u303D\u3297\u3299]|[0-9#*]\uFE0F?\u20E3)"
)
_EMOJI_SUFFIX = r"(?:[\uFE0E\uFE0F\U000E0020-\U000E007F]|[\U0001F3FB-\U0001F3FF])*"
_EMOJI_CHARACTER = rf"{_EMOJI_BASE}{_EMOJI_SUFFIX}"
_EMOJI_RE = re.compile(
    rf"(?:[\U0001F1E6-\U0001F1FF]{{2}}|{_EMOJI_CHARACTER}(?:\u200d{_EMOJI_CHARACTER})*)"
)
_EMPTY_DESCRIPTION_RE = re.compile(r"^(?:简介\s*[:：]\s*)?[-—–]+$")
_LRC_LINE_RE = re.compile(
    r"^\[(?P<minutes>\d{1,3}):(?P<seconds>\d{2}(?:[.:]\d{1,3})?)\](?P<text>.*)$"
)
_LYRIC_CREDIT_PREFIXES = (
    "出品",
    "演唱",
    "作词",
    "作曲",
    "词曲",
    "编曲",
    "制作",
    "录音",
    "混音",
    "母带",
    "吉他",
    "监制",
    "人声",
)
# Some platforms (notably Xiaohongshu) serialize adjacent topics as
# ``#topic[话题]##next-topic[话题]#``.  Keep the generic extractor permissive
# enough to recognize the second ``#`` while consuming the optional closing
# delimiter so it does not leak into the card description.
_TOPIC_TAG_RE = re.compile(
    r"(?<![A-Za-z0-9_])#\s*([^#\s,\uFF0C;\uFF1B\u3001\u3002.!\uFF01?\uFF1F|\uFF5C/\\]+)"
)
_TOPIC_TRAILING_RE = re.compile(
    r"[\s,\uFF0C;\uFF1B\u3001\u3002.!\uFF01?\uFF1F|\uFF5C/\\]+$"
)
_LABELED_TOPIC_LINE_RE = re.compile(
    r"(?m)^[ \t]*标签\s*[:：][ \t]*(?P<topics>#[^\r\n]*?)[ \t]*(?:\r?\n|$)"
)
_LABELED_TOPIC_SEPARATOR_RE = re.compile(r"[,，]\s*(?=#)")
_TOPIC_PLATFORM_SUFFIX_RE = re.compile(r"\[话题\]\s*$")


class Renderer:
    """将 ``ParseResult`` 以选中的 HTML 模板渲染为 PNG。

    模板发现顺序（前者可覆盖后者）：

    1. ``<插件数据目录>/templates``，用于用户自定义模板；
    2. ``<插件目录>/templates``，用于随插件发布的模板；
    3. ``core/templates``，内置兜底模板。
    """

    BUILTIN_TEMPLATE_NAMES: ClassVar[tuple[str, ...]] = (
        "default",
        "compact",
        "apple",
    )
    _QQMUSIC_LOGIN_TEMPLATE_NAME: ClassVar[str] = "qqmusic_login.html"
    _LYRICS_TEMPLATE_NAME: ClassVar[str] = "lyrics_card.html"
    _TEMPLATES_DIR: ClassVar[Path] = Path(__file__).with_name("templates")
    _RESOURCES_DIR: ClassVar[Path] = Path(__file__).with_name("resources")
    _PLUGIN_LOGO_PATH: ClassVar[Path] = _TEMPLATES_DIR.parent.parent / "logo.png"
    _CARD_FONT_PATH: ClassVar[Path] = _RESOURCES_DIR / "douyin_sans.otf"
    _STAT_ICON_NAMES: ClassVar[dict[str, str]] = {
        "likes": "like.png",
        "comments": "comment.png",
        "favorites": "favorites.png",
        "shares": "share.png",
        "playlist_track_count": "song_count.png",
    }
    _PLATFORM_LOGO_NAMES: ClassVar[dict[str, str]] = {
        "bilibili": "bilibili.png",
        "douyin": "douyin.png",
        "xhs": "xhs.png",
        "pixiv": "pixiv.png",
        "qqmusic": "qqmusic.png",
        "netease": "cloudmusic.png",
        "kugou": "kugou.png",
        "qishui": "qishui.png",
        "kuwo": "kuwo.png",
        "applemusic": "applemusic.png",
    }
    _LYRICS_PLATFORM_THEMES: ClassVar[dict[str, dict[str, str]]] = {
        "netease": {
            "accent_color": "#e93345",
            "accent_deep": "#b81f32",
            "accent_soft": "#fff1f3",
        },
        "qqmusic": {
            "accent_color": "#13a866",
            "accent_deep": "#087a49",
            "accent_soft": "#effbf4",
        },
        "kugou": {
            "accent_color": "#1597e5",
            "accent_deep": "#0875b7",
            "accent_soft": "#edf8ff",
        },
    }
    _LIVE_PHOTO_ICON_NAME: ClassVar[str] = "livep.png"
    _LIVE_STREAM_ICON_NAME: ClassVar[str] = "live.png"
    _LIVE_PHOTO_HINT: ClassVar[str] = (
        "（对于除Apple、vivo机型以外的手机）可尝试点击“查看原图”后保存获取实况图~"
    )
    _ERROR_PREVIEW_IMAGE_NAMES: ClassVar[tuple[str, ...]] = ("error_preview.png",)
    _THEME_CONTENT_KINDS: ClassVar[set[str]] = {"image", "graphics", "video", "dynamic"}
    _EMOJI_FETCH_TIMEOUT_SECONDS: ClassVar[float] = 3.0
    _BROWSER_VIEWPORT_WIDTH: ClassVar[int] = 760
    # A short viewport keeps the browser from padding compact cards to a large
    # fixed canvas; built-in templates are cropped to their card root on export.
    _BROWSER_VIEWPORT_HEIGHT: ClassVar[int] = 100
    _BROWSER_START_TIMEOUT_MS: ClassVar[int] = 30_000
    _PAGE_LOAD_TIMEOUT_MS: ClassVar[int] = 30_000
    _ASSET_WAIT_TIMEOUT_MS: ClassVar[int] = 5_000

    @staticmethod
    def _log_exception(message: str) -> None:
        """兼容 AstrBot 不同版本 logger 的异常日志接口。"""
        log_exception = getattr(logger, "exception", None)
        if callable(log_exception):
            log_exception(message)
        else:
            logger.error(message)

    @staticmethod
    def _log_warning(message: str) -> None:
        log_warning = getattr(logger, "warning", None)
        if callable(log_warning):
            log_warning(message)
        else:
            logger.error(message)

    @staticmethod
    def _log_debug(message: str) -> None:
        log_debug = getattr(logger, "debug", None)
        if callable(log_debug):
            log_debug(message)

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.template_dirs = self._template_dirs()
        try:
            self.environment = self._build_environment()
        except Exception as exc:
            # 模板环境初始化属于可选卡片能力，不能阻断解析器和媒体发送。
            self._log_exception(f"Jinja2 卡片模板环境初始化失败: {exc}")
            self.environment = None
        self._emoji_uri_cache: dict[str, str | None] = {}
        self._emoji_path_cache: dict[str, Path | None] = {}
        self._emoji_source = None
        # Playwright browser objects are intentionally kept for the renderer
        # lifetime. Starting a headless shell for every card was the main
        # source of latency in the previous implementation.
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._browser_context: Any | None = None
        self._browser_lock: asyncio.Lock | None = None
        if _APILMOJI_AVAILABLE and EmojiCDNSource is not None:
            try:
                emoji_style = self._normalized_emoji_style()
                cache_dir = Path(getattr(self.cfg, "cache_dir", self._TEMPLATES_DIR)) / "emojis"
                self._emoji_source = EmojiCDNSource(
                    base_url=str(getattr(self.cfg, "emoji_cdn", "https://emojicdn.elk.sh")),
                    style=emoji_style,
                    cache_dir=cache_dir,
                )
            except Exception as exc:
                # 表情资源是增强项；初始化失败不能让解析插件无法加载。
                self._log_warning(
                    f"Apple Emoji 资源初始化失败，将保留 Unicode 表情: {exc}"
                )
                self._emoji_source = None

    @classmethod
    def load_resources(cls) -> None:
        """保留启动期资源加载接口，浏览器资源由实例生命周期管理。

        原实现会在插件初始化时预加载 Pillow 字体和图片；HTML 方案不再
        需要该 IO。保留方法可让旧的 ``initialize`` 调用保持兼容。
        """
        # 内置模板随插件发布，不在安装目录执行写操作；用户模板目录由
        # PluginConfig 在数据目录中创建。
        return

    @staticmethod
    def _browser_is_connected(browser: Any | None) -> bool:
        if browser is None:
            return False
        try:
            connected = getattr(browser, "is_connected", False)
            return bool(connected() if callable(connected) else connected)
        except Exception:
            return False

    async def _close_browser_unlocked(self) -> None:
        """关闭浏览器资源；调用方负责串行化。"""
        context, browser, playwright = (
            self._browser_context,
            self._browser,
            self._playwright,
        )
        self._browser_context = None
        self._browser = None
        self._playwright = None

        for resource, method_name in (
            (context, "close"),
            (browser, "close"),
            (playwright, "stop"),
        ):
            method = getattr(resource, method_name, None)
            if not callable(method):
                continue
            try:
                await method()
            except Exception as exc:
                self._log_warning(f"关闭 Playwright 资源失败: {exc}")

    async def start(self) -> bool:
        """启动并复用 Chrome Headless Shell。

        Playwright 在 ``headless=True`` 时会选择其安装的
        ``chromium-headless-shell``。浏览器只启动一次，后续卡片共用同一
        个进程和上下文，以避免每张卡片重复付出浏览器启动开销。
        """
        if not _PLAYWRIGHT_AVAILABLE or async_playwright is None:
            logger.error(
                "Playwright 不可用，跳过卡片渲染；请安装依赖并执行 "
                "python -m playwright install chromium-headless-shell: "
                f"{_PLAYWRIGHT_IMPORT_ERROR!r}"
            )
            return False

        if self._browser_is_connected(self._browser) and self._browser_context:
            return True

        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()

        async with self._browser_lock:
            if self._browser_is_connected(self._browser) and self._browser_context:
                return True
            if self._browser is not None or self._playwright is not None:
                await self._close_browser_unlocked()

            try:
                self._playwright = await async_playwright().start()
                launch_args = ["--disable-dev-shm-usage"]
                # Chromium's sandbox cannot start as root on most Linux images;
                # keep the safer default everywhere else.
                if (
                    os.name != "nt"
                    and hasattr(os, "geteuid")
                    and os.geteuid() == 0
                ):
                    launch_args.append("--no-sandbox")
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=launch_args,
                    timeout=self._BROWSER_START_TIMEOUT_MS,
                )
                self._browser_context = await self._browser.new_context(
                    viewport={
                        "width": self._BROWSER_VIEWPORT_WIDTH,
                        "height": self._BROWSER_VIEWPORT_HEIGHT,
                    },
                    device_scale_factor=1,
                    color_scheme="light",
                )
                logger.info("Playwright Chrome Headless Shell 已启动")
                return True
            except Exception as exc:
                await self._close_browser_unlocked()
                self._log_exception(
                    "Playwright Chrome Headless Shell 启动失败，跳过卡片渲染；"
                    "请执行 python -m playwright install chromium-headless-shell: "
                    f"{exc}"
                )
                return False

    async def close(self) -> None:
        """在插件卸载时释放复用的浏览器进程。"""
        if self._browser_lock is None:
            await self._close_browser_unlocked()
            return
        async with self._browser_lock:
            await self._close_browser_unlocked()

    @staticmethod
    def _inject_base_url(html: str, base_url: str | None) -> str:
        """让 ``file://`` 页面中的相对模板资源可被 Chrome 读取。"""
        if not base_url or re.search(r"<base\b", html, flags=re.IGNORECASE):
            return html
        try:
            if "://" in base_url:
                uri = base_url
            else:
                uri = Path(base_url).resolve().as_uri()
            if not uri.endswith("/"):
                uri += "/"
            tag = f'<base href="{html_escape(uri, quote=True)}">'
            head = re.search(r"<head\b[^>]*>", html, flags=re.IGNORECASE)
            if head:
                return f"{html[:head.end()]}{tag}{html[head.end():]}"
            return f"{tag}{html}"
        except (OSError, TypeError, ValueError):
            return html

    def _template_dirs(self) -> list[Path]:
        data_dir = getattr(self.cfg, "template_dir", None)
        if data_dir is None:
            data_dir = Path(getattr(self.cfg, "cache_dir", self._TEMPLATES_DIR)) / "templates"
        plugin_dir = Path(getattr(self.cfg, "plugin_dir", self._TEMPLATES_DIR))
        writable_data_dir = Path(data_dir)
        try:
            writable_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 仍保留该搜索项；若不可写，Jinja2 会继续尝试插件/内置模板。
            self._log_warning(f"用户卡片模板目录不可用: {writable_data_dir} ({exc})")
        candidates = [writable_data_dir, plugin_dir / "templates", self._TEMPLATES_DIR]
        result: list[Path] = []
        for directory in candidates:
            if directory not in result and (
                directory == writable_data_dir or directory.is_dir()
            ):
                result.append(directory)
        return result

    def _build_environment(self):
        if not _JINJA_AVAILABLE:
            return None
        assert ChoiceLoader is not None
        assert Environment is not None
        assert FileSystemLoader is not None
        assert select_autoescape is not None
        environment = Environment(
            loader=ChoiceLoader([FileSystemLoader(str(path)) for path in self.template_dirs]),
            autoescape=select_autoescape(("html", "xml")),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        environment.filters.update(
            format_count=self.format_count,
            emoji=self._emoji_markup,
            file_uri=self._file_uri,
        )
        return environment

    def available_templates(self) -> list[str]:
        """列出当前模板搜索路径中的模板名（不含扩展名）。"""
        names: set[str] = set(self.BUILTIN_TEMPLATE_NAMES)
        for directory in self.template_dirs:
            names.update(path.stem for path in directory.glob("*.html"))
        # The QR login template is an internal command surface, not a parser
        # result template that users should select in card configuration.
        names.discard(self._QQMUSIC_LOGIN_TEMPLATE_NAME.removesuffix(".html"))
        names.discard(self._LYRICS_TEMPLATE_NAME.removesuffix(".html"))
        return sorted(names)

    @staticmethod
    def format_count(value: int | None) -> str:
        """模板使用的紧凑数字格式化器。"""
        if value is None:
            return ""
        if value >= 100_000_000:
            return f"{value / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
        if value >= 10_000:
            return f"{value / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
        return str(value)

    def _normalized_emoji_style(self) -> str:
        """将配置值收敛为安全的 CDN 缓存目录/HTML 类名片段。"""
        return re.sub(
            r"[^a-z0-9_-]",
            "",
            str(getattr(self.cfg, "emoji_style", "APPLE") or "APPLE").lower(),
        ) or "apple"

    @staticmethod
    def _emoji_tokens(value: object) -> set[str]:
        return {matched.group(0) for matched in _EMOJI_RE.finditer(str(value or ""))}

    async def _prepare_emoji_assets(self, result: ParseResult) -> None:
        """预热本地表情图片缓存，失败时仍保留 Unicode 回退。

        网络下载设置了短超时，卡片渲染不会因为某个 CDN 不可达而阻塞
        主发送流程。已经下载过的 Apple/其他风格资源会直接复用。
        """
        if self._emoji_source is None:
            return
        values: list[object] = []

        def collect(item: ParseResult) -> None:
            values.extend((item.platform.display_name, item.title, item.text, item.extra_info))
            if item.author:
                values.extend((item.author.name, item.author.description))
            playlist_tracks = item.extra.get("playlist_tracks")
            if isinstance(playlist_tracks, list):
                for track in playlist_tracks:
                    if isinstance(track, Mapping):
                        values.extend(
                            (track.get("title"), track.get("artist"), track.get("album"))
                        )
            for content in item.contents:
                values.extend(
                    (getattr(content, "text", None), getattr(content, "alt", None))
                )

        collect(result)
        if result.repost:
            collect(result.repost)
        tokens: set[str] = set()
        for value in values:
            tokens.update(self._emoji_tokens(value))
        tokens = {token for token in tokens if token not in self._emoji_uri_cache}
        if not tokens:
            return

        async def resolve(token: str) -> tuple[str, Path | None]:
            try:
                path = await asyncio.wait_for(
                    self._emoji_source.get_emoji(token),
                    timeout=self._EMOJI_FETCH_TIMEOUT_SECONDS,
                )
                return token, Path(path) if path else None
            except Exception:
                return token, None

        for token, path in await asyncio.gather(*(resolve(token) for token in tokens)):
            self._emoji_path_cache[token] = path
            self._emoji_uri_cache[token] = self._file_uri(path)

    def _emoji_markup(self, value: object) -> Markup:
        """安全地保留 Unicode 表情，并赋予 Apple 风格的字体优先级。

        iOS 分享文本中的组合表情（国旗、肤色、ZWJ 家庭等）会被作为一个
        序列包裹，避免模板转义或 CSS 分词导致表情拆分。未安装 Apple 字体
        的服务器会依次回退到 Segoe/Noto Emoji，不会丢失原始 Unicode。
        """
        text = str(value or "")
        if not _JINJA_AVAILABLE:
            return Markup(text)

        pieces: list[Markup] = []
        start = 0
        style = self._normalized_emoji_style()
        for matched in _EMOJI_RE.finditer(text):
            pieces.append(escape(text[start : matched.start()]))
            classes = f"emoji emoji--{style}"
            token = matched.group(0)
            if uri := self._emoji_uri_cache.get(token):
                pieces.append(
                    Markup(
                        f'<img class="{classes} emoji-image" src="{escape(uri)}" '
                        f'alt="{escape(token)}">'
                    )
                )
            else:
                pieces.append(Markup(f'<span class="{classes}">{escape(token)}</span>'))
            start = matched.end()
        pieces.append(escape(text[start:]))
        return Markup("".join(str(piece) for piece in pieces))

    @staticmethod
    def _file_uri(value: object | None) -> str | None:
        if value is None:
            return None
        try:
            path = Path(value)
            return path.resolve().as_uri() if path.exists() else None
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _card_text(value: str | None) -> str | None:
        """过滤空白与解析器常见的“简介：-”占位描述。"""
        text = (value or "").strip()
        return None if not text or _EMPTY_DESCRIPTION_RE.fullmatch(text) else text

    @staticmethod
    def _dedupe_tags(tags: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for tag in tags:
            key = tag.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(tag)
        return result

    @classmethod
    def _split_topic_tags(cls, value: str | None) -> tuple[str | None, list[str]]:
        text = (value or "").strip()
        if not text:
            return None, []

        tags: list[str] = []

        def normalize_topic(value: str) -> str:
            value = _TOPIC_PLATFORM_SUFFIX_RE.sub("", value.strip())
            return _TOPIC_TRAILING_RE.sub("", value).strip()

        def remove_labeled_topic_line(matched: re.Match[str]) -> str:
            """按逗号拆分“标签:”行，并将完整标签从详情文字中移除。"""
            topics: list[str] = []
            for item in _LABELED_TOPIC_SEPARATOR_RE.split(matched.group("topics")):
                item = item.strip()
                if not item.startswith("#"):
                    return matched.group(0)
                topic = normalize_topic(item[1:])
                if topic:
                    topics.append(f"# {topic}")
            if not topics:
                return matched.group(0)
            tags.extend(topics)
            return ""

        text = _LABELED_TOPIC_LINE_RE.sub(remove_labeled_topic_line, text)

        spans: list[tuple[int, int]] = []
        for matched in _TOPIC_TAG_RE.finditer(text):
            topic = normalize_topic(matched.group(1))
            if not topic:
                continue
            start, end = matched.span()
            # ``[话题]#`` is Xiaohongshu's closing delimiter. Consume only
            # that delimiter; for ordinary ``#tag#next`` text, leave the
            # second hash available as the next topic's opening marker.
            if (
                _TOPIC_PLATFORM_SUFFIX_RE.search(matched.group(1))
                and text[end : end + 1] == "#"
            ):
                end += 1
            spans.append((start, end))
            tags.append(f"# {topic}")

        if spans:
            pieces: list[str] = []
            cursor = 0
            for start, end in spans:
                pieces.append(text[cursor:start])
                cursor = end
            pieces.append(text[cursor:])
            cleaned = "".join(pieces)
        else:
            cleaned = text

        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r" *\n *", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip(" \t\r\n,，;；、|｜/\\")
        return cls._card_text(cleaned), cls._dedupe_tags(tags)

    @staticmethod
    def _border_radius_px(value: object | None) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        match = re.match(r"^([0-9]*\.?[0-9]+)", text)
        return float(match.group(1)) if match else 0.0

    @classmethod
    def _round_png_corners(cls, path: Path, radius_px: float) -> bool:
        if radius_px <= 0:
            return False
        try:
            from PIL import Image, ImageDraw
        except ImportError as exc:  # pragma: no cover - Pillow is a runtime dependency
            cls._log_warning(f"Pillow 不可用，跳过卡片圆角裁切: {exc}")
            return False

        temporary = path.with_name(f".{path.stem}_{uuid.uuid4().hex}.png")
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
                width, height = image.size
                if width <= 0 or height <= 0:
                    return False

                scale = 4
                scaled_width = width * scale
                scaled_height = height * scale
                scaled_radius = max(
                    0,
                    min(
                        int(round(radius_px * scale)),
                        min(scaled_width, scaled_height) // 2,
                    ),
                )
                mask = Image.new("L", (scaled_width, scaled_height), 0)
                draw = ImageDraw.Draw(mask)
                draw.rounded_rectangle(
                    (0, 0, scaled_width - 1, scaled_height - 1),
                    radius=scaled_radius,
                    fill=255,
                )
                resample = getattr(Image, "Resampling", Image).LANCZOS
                mask = mask.resize((width, height), resample)
                image.putalpha(mask)
                image.save(temporary)
            temporary.replace(path)
            return True
        except Exception as exc:
            cls._log_warning(f"卡片圆角裁切失败，保留原图: {exc}")
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    @staticmethod
    def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    @staticmethod
    def _mix_rgb(
        rgb: tuple[int, int, int],
        target: tuple[int, int, int],
        amount: float,
    ) -> tuple[int, int, int]:
        amount = max(0.0, min(1.0, amount))
        return tuple(
            max(0, min(255, round(channel * amount + target_channel * (1 - amount))))
            for channel, target_channel in zip(rgb, target, strict=True)
        )

    @classmethod
    def _preview_theme(cls, path: Path) -> dict[str, str] | None:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - Pillow is a runtime dependency
            cls._log_warning(f"Pillow 不可用，跳过 Apple 卡片动态取色: {exc}")
            return None

        try:
            with Image.open(path) as source:
                source.thumbnail((72, 72))
                image = source.convert("RGBA")
                weighted = [0.0, 0.0, 0.0]
                total_weight = 0.0
                pixels = (
                    image.get_flattened_data()
                    if hasattr(image, "get_flattened_data")
                    else image.getdata()
                )
                for red, green, blue, alpha in pixels:
                    if alpha < 64:
                        continue
                    maximum = max(red, green, blue)
                    minimum = min(red, green, blue)
                    chroma = maximum - minimum
                    lightness = (maximum + minimum) / 510
                    saturation = 0.0 if maximum == 0 else chroma / maximum
                    if lightness < 0.04 or lightness > 0.98:
                        weight = 0.25
                    else:
                        weight = 0.55 + saturation * 1.35 + chroma / 255
                    weighted[0] += red * weight
                    weighted[1] += green * weight
                    weighted[2] += blue * weight
                    total_weight += weight
                if total_weight <= 0:
                    return None
        except Exception as exc:
            cls._log_warning(f"Apple 卡片动态取色失败，使用默认白底: {exc}")
            return None

        base = tuple(round(channel / total_weight) for channel in weighted)
        page = cls._rgb_to_hex(cls._mix_rgb(base, (245, 245, 247), 0.42))
        card_top = cls._rgb_to_hex(cls._mix_rgb(base, (255, 255, 255), 0.24))
        card_bottom = cls._rgb_to_hex(cls._mix_rgb(base, (255, 255, 255), 0.34))
        surface = cls._rgb_to_hex(cls._mix_rgb(base, (245, 245, 247), 0.34))
        subtle = cls._rgb_to_hex(cls._mix_rgb(base, (250, 250, 252), 0.26))
        border_rgb = cls._mix_rgb(base, (0, 0, 0), 0.74)
        muted_rgb = cls._mix_rgb(base, (32, 32, 36), 0.20)
        secondary_rgb = cls._mix_rgb(base, (24, 24, 28), 0.12)
        return {
            "base_color": cls._rgb_to_hex(base),
            "page_bg": page,
            "card_bg": f"linear-gradient(180deg, {card_top} 0%, {card_bottom} 100%)",
            "glow": (
                "radial-gradient(circle at 86% 5%, "
                f"rgba({base[0]}, {base[1]}, {base[2]}, 0.34) 0%, "
                f"rgba({base[0]}, {base[1]}, {base[2]}, 0.17) 30%, "
                f"rgba({base[0]}, {base[1]}, {base[2]}, 0) 64%)"
            ),
            "surface_bg": surface,
            "subtle_bg": subtle,
            "muted_text": cls._rgb_to_hex(muted_rgb),
            "secondary_text": cls._rgb_to_hex(secondary_rgb),
            "border": f"rgba({border_rgb[0]}, {border_rgb[1]}, {border_rgb[2]}, 0.18)",
            "divider": f"rgba({border_rgb[0]}, {border_rgb[1]}, {border_rgb[2]}, 0.17)",
            "shadow": f"rgba({base[0]}, {base[1]}, {base[2]}, 0.22)",
        }

    @classmethod
    def _dynamic_theme_from_contents(
        cls, contents: list[dict[str, Any]]
    ) -> dict[str, str] | None:
        error_preview = cls._error_preview_path()
        error_preview_path = error_preview.resolve() if error_preview else None
        for content in contents:
            if content.get("kind") not in cls._THEME_CONTENT_KINDS:
                continue
            path = content.get("path")
            if not isinstance(path, Path) or not path.is_file():
                continue
            try:
                if error_preview_path and path.resolve() == error_preview_path:
                    continue
            except OSError:
                continue
            if theme := cls._preview_theme(path):
                theme["source_path"] = str(path)
                return theme
        return None

    @classmethod
    def _error_preview_path(cls) -> Path | None:
        for filename in cls._ERROR_PREVIEW_IMAGE_NAMES:
            fallback = cls._RESOURCES_DIR / filename
            if fallback.is_file():
                return fallback
        return None

    @classmethod
    async def _media_path(cls, content: MediaContent) -> Path | None:
        """返回卡片可展示的本地预览图，视觉媒体失效时使用默认占位图。"""
        if isinstance(content, VideoContent):
            try:
                cover = await content.get_cover_path()
            except (DownloadException, OSError, RuntimeError) as exc:
                logger.debug(f"视频封面获取失败，使用默认预览图: {exc}")
                cover = None

            if cover and cover.is_file():
                return cover
            return cls._error_preview_path()

        try:
            if isinstance(content, (ImageContent, GraphicsContent)):
                path = await content.get_path()
                if path.is_file():
                    return path
                return cls._error_preview_path()
            if isinstance(content, DynamicContent):
                # 动态内容若已有 GIF/图片副本，优先用它作为静态卡片封面。
                if content.gif_path and content.gif_path.is_file():
                    return content.gif_path
                path = await content.get_path()
                if path.is_file() and path.suffix.lower() in {
                    ".apng",
                    ".avif",
                    ".gif",
                    ".jpeg",
                    ".jpg",
                    ".png",
                    ".webp",
                }:
                    return path
        except (DownloadException, OSError, RuntimeError):
            if isinstance(content, (ImageContent, GraphicsContent, DynamicContent)):
                return cls._error_preview_path()
            return None
        if isinstance(content, DynamicContent):
            return cls._error_preview_path()
        return None

    async def _content_context(self, content: MediaContent) -> dict[str, Any]:
        path = await self._media_path(content)
        kind = "media"
        if isinstance(content, VideoContent):
            kind = "video"
        elif isinstance(content, ImageContent):
            kind = "image"
        elif isinstance(content, GraphicsContent):
            kind = "graphics"
        elif isinstance(content, AudioContent):
            kind = "audio"
        elif isinstance(content, FileContent):
            kind = "file"
        elif isinstance(content, DynamicContent):
            kind = "dynamic"
        elif isinstance(content, TextContent):
            kind = "text"

        return {
            "kind": kind,
            "path": path,
            "uri": self._file_uri(path),
            "text": getattr(content, "text", None),
            "alt": getattr(content, "alt", None),
            "duration": getattr(content, "duration", None),
            "name": getattr(content, "name", None),
        }

    @staticmethod
    async def _optional_image_uri(content: object) -> str | None:
        """Resolve an auxiliary image without showing the card placeholder."""

        if not isinstance(content, ImageContent):
            return None
        try:
            path = await content.get_path()
        except (DownloadException, OSError, RuntimeError):
            return None
        try:
            return (
                Renderer._file_uri(path)
                if isinstance(path, Path) and path.is_file()
                else None
            )
        except OSError:
            return None

    async def _playlist_tracks_context(self, value: object) -> list[dict[str, Any]]:
        """Convert parser-owned song summaries into safe template data."""

        if not isinstance(value, list):
            return []
        tracks: list[dict[str, Any]] = []
        for raw_track in value:
            if not isinstance(raw_track, Mapping):
                continue
            title = str(raw_track.get("title") or "").strip()
            if not title:
                continue
            try:
                index = int(raw_track.get("index") or len(tracks) + 1)
            except (TypeError, ValueError):
                index = len(tracks) + 1
            item: dict[str, Any] = {
                "index": index,
                "title": title,
                "artist": str(raw_track.get("artist") or "").strip() or None,
                "album": str(raw_track.get("album") or "").strip() or None,
                "cover_uri": await self._optional_image_uri(
                    raw_track.get("cover_content")
                ),
            }
            tracks.append(item)
        return tracks

    async def _result_context(self, result: ParseResult, *, depth: int = 0) -> dict[str, Any]:
        if depth == 0:
            await self._prepare_emoji_assets(result)
        avatar_path = None
        if result.author:
            try:
                avatar_path = await result.author.get_avatar_path()
            except (DownloadException, OSError, RuntimeError):
                avatar_path = None

        contents = [await self._content_context(content) for content in result.contents]
        dynamic_color_enabled = bool(getattr(self.cfg, "card_dynamic_color", False))
        theme = self._dynamic_theme_from_contents(contents) if dynamic_color_enabled else None
        if dynamic_color_enabled:
            if theme:
                self._log_debug(
                    "Apple 卡片动态取色已应用: "
                    f"source={theme.get('source_path')}, base={theme.get('base_color')}"
                )
            else:
                self._log_debug(
                    "Apple 卡片动态取色未应用: 未找到可读取的真实本地预览图"
                )
        platform_name = result.platform.name.lower()
        platform_logo_name = self._PLATFORM_LOGO_NAMES.get(platform_name)
        platform_logo_uri = (
            self._file_uri(self._RESOURCES_DIR / "logos" / platform_logo_name)
            if platform_logo_name
            else None
        )
        has_live_photo = platform_name in {"douyin", "xhs"} and result.has_motion_photo
        has_live_stream = (
            platform_name in {"douyin", "bilibili"}
            and result.extra.get("is_live_stream") is True
        )
        stats = result.engagement.as_dict()
        playlist_track_count = result.extra.get("playlist_track_count")
        try:
            playlist_track_count = (
                int(playlist_track_count)
                if playlist_track_count is not None
                else None
            )
        except (TypeError, ValueError, OverflowError):
            playlist_track_count = None
        stat_items: list[dict[str, Any]] = []
        if playlist_track_count is not None:
            stat_items.append(
                {
                    "key": "playlist_track_count",
                    "label": "歌曲数",
                    "icon": "♫",
                    "icon_uri": self._file_uri(
                        self._RESOURCES_DIR
                        / self._STAT_ICON_NAMES["playlist_track_count"]
                    ),
                    "value": playlist_track_count,
                }
            )
        stat_items.extend(
            {
                "key": key,
                "label": label,
                "icon": fallback_icon,
                "icon_uri": self._file_uri(self._RESOURCES_DIR / icon_name),
                "value": stats[key],
            }
            for key, label, fallback_icon, icon_name in (
                ("likes", "点赞", "♡", self._STAT_ICON_NAMES["likes"]),
                ("comments", "评论", "◌", self._STAT_ICON_NAMES["comments"]),
                ("favorites", "收藏", "☆", self._STAT_ICON_NAMES["favorites"]),
                ("shares", "转发", "↗", self._STAT_ICON_NAMES["shares"]),
            )
        )
        title, title_topic_tags = self._split_topic_tags(self._card_text(result.title))
        text, text_topic_tags = self._split_topic_tags(self._card_text(result.text))
        topic_tags = self._dedupe_tags(title_topic_tags + text_topic_tags)
        playlist_tracks = await self._playlist_tracks_context(
            result.extra.get("playlist_tracks")
        )
        card_extra_info = result.extra.get("card_info")
        if card_extra_info is None:
            card_extra_info = result.extra_info
        card_extra = {
            key: value
            for key, value in result.extra.items()
            if key not in {"playlist_tracks", "lyrics"}
        }
        card_url = result.url
        if result.extra.get("show_playlist_url") is False:
            card_url = None
        card: dict[str, Any] = {
            "platform": {
                "name": result.platform.name,
                "display_name": result.platform.display_name,
                "logo_uri": platform_logo_uri,
            },
            "author": {
                "name": result.author.name,
                "description": result.author.description,
                "avatar": avatar_path,
                "avatar_uri": self._file_uri(avatar_path),
            }
            if result.author
            else None,
            "title": title,
            "text": text,
            "topic_tags": topic_tags,
            "timestamp": result.timestamp,
            "datetime": result.formatted_datetime(),
            "url": card_url,
            "extra": card_extra,
            "extra_info": card_extra_info,
            "playlist_tracks": playlist_tracks,
            "has_live_photo": has_live_photo,
            "has_live_stream": has_live_stream,
            "live_stream_uri": (
                self._file_uri(self._RESOURCES_DIR / self._LIVE_STREAM_ICON_NAME)
                if has_live_stream
                else None
            ),
            "live_photo_uri": (
                self._file_uri(self._RESOURCES_DIR / self._LIVE_PHOTO_ICON_NAME)
                if has_live_photo
                else None
            ),
            "live_photo_hint": self._LIVE_PHOTO_HINT if has_live_photo else None,
            "stats": stats,
            "stat_items": [item for item in stat_items if item["value"] is not None],
            "contents": contents,
            "theme": theme,
            "repost": None,
        }
        # 防止第三方解析器意外构造循环转发对象。
        if result.repost and depth < 1:
            card["repost"] = (
                await self._result_context(result.repost, depth=depth + 1)
            )["card"]
        return {"card": card}

    def _template_name(self) -> str:
        requested = str(getattr(self.cfg, "card_template", "apple") or "apple").strip()
        if requested == "custom":
            custom_name = str(getattr(self.cfg, "card_custom_template", "") or "").strip()
            # 兼容旧配置直接把 card_template 写成模板名的用法。
            requested = custom_name or requested
        # 禁止从配置注入目录路径；同名的用户模板依然可以被发现。
        requested = Path(requested).name.removesuffix(".html")
        return requested or "apple"

    def _template_base_dir(self) -> Path:
        """返回最终使用模板所在目录，供相对静态资源解析。

        ``ChoiceLoader`` 的第一个命中项会覆盖后续目录；这里沿用完全相同
        的顺序。这样用户模板除了 HTML 本身，还可以安全地引用同目录的
        图片、字体或 CSS，而不必把资源复制到数据目录的根部。
        """
        requested = f"{self._template_name()}.html"
        for directory in self.template_dirs:
            if (directory / requested).is_file():
                return directory
        for directory in self.template_dirs:
            if (directory / "default.html").is_file():
                return directory
        return self.template_dirs[0]

    def render_html(self, result: ParseResult, context: dict[str, Any]) -> str:
        """渲染 HTML，公开此方法方便模板开发和单元测试。"""
        if not self.environment:
            raise RuntimeError("Jinja2 不可用，无法渲染卡片模板")

        template_name = f"{self._template_name()}.html"
        try:
            template = self.environment.get_template(template_name)
        except TemplateNotFound as exc:
            logger.error(f"卡片模板不存在，跳过卡片渲染: {template_name}")
            raise RuntimeError(f"卡片模板不存在: {template_name}") from exc

        return template.render(
            result=result,
            card=context["card"],
            # 顶层别名便于用户模板迁移和快速编写。
            platform=context["card"]["platform"],
            author=context["card"]["author"],
            stats=context["card"]["stats"],
            contents=context["card"]["contents"],
            extra=context["card"]["extra"],
            playlist_tracks=context["card"].get("playlist_tracks", []),
            config=self.cfg,
            template_name=self._template_name(),
            emoji_style=str(getattr(self.cfg, "emoji_style", "APPLE") or "APPLE").lower(),
            card_font_uri=self._file_uri(self._CARD_FONT_PATH),
        )

    async def _render_playwright_png(
        self, html: str, target: Path, *, base_url: str | None = None
    ) -> bool:
        """用 Chrome Headless Shell 将渲染后的 HTML 截图为 PNG。

        临时 HTML 与最终 PNG 都位于插件缓存目录。临时 HTML 在本次渲染
        完成后立即删除，最终 PNG 则沿用 ``CacheCleaner`` 的原有清理周期。
        """
        if not await self.start():
            return False
        context = self._browser_context
        if context is None:
            return False

        # Use a hidden unique name so direct callers may keep their own rendered
        # HTML preview next to a PNG without it being removed by this renderer.
        temporary_html = target.with_name(f".{target.stem}_{uuid.uuid4().hex}.html")
        page: Any | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_html.write_text(
                self._inject_base_url(html, base_url),
                encoding="utf-8",
            )
            page = await context.new_page()
            await page.goto(
                temporary_html.resolve().as_uri(),
                wait_until="load",
                timeout=self._PAGE_LOAD_TIMEOUT_MS,
            )

            # 等待本地图片和字体完成解码，避免截图偶发出现空白封面。
            for expression, label in (
                (
                    "Array.from(document.images).every((image) => image.complete)",
                    "图片",
                ),
                (
                    "document.fonts ? document.fonts.status === 'loaded' : true",
                    "字体",
                ),
            ):
                try:
                    await page.wait_for_function(
                        expression,
                        timeout=self._ASSET_WAIT_TIMEOUT_MS,
                    )
                except Exception as exc:
                    # 资源超时不阻止截图；浏览器仍会按 HTML 的实际状态输出。
                    self._log_warning(f"等待卡片{label}资源超时，继续截图: {exc}")

            # Built-in templates mark the visual card root so exported PNGs do
            # not retain the page-level padding/background around the card.
            locator_factory = getattr(page, "locator", None)
            root = locator_factory("[data-card-root]") if callable(locator_factory) else None
            if root is not None and await root.count():
                radius_px = 0.0
                try:
                    radius_px = self._border_radius_px(
                        await root.first.evaluate(
                            "(element) => getComputedStyle(element).borderTopLeftRadius"
                        )
                    )
                except Exception as exc:
                    self._log_warning(f"读取卡片圆角半径失败，跳过圆角裁切: {exc}")
                await root.first.screenshot(
                    path=str(target.resolve()),
                    type="png",
                )
                if radius_px > 0:
                    self._round_png_corners(target.resolve(), radius_px)
            else:
                # Custom templates may not add the marker; crop to the body so
                # the browser viewport itself never becomes an exported border.
                body = locator_factory("body") if callable(locator_factory) else None
                if body is not None and await body.count():
                    await body.first.screenshot(
                        path=str(target.resolve()),
                        type="png",
                    )
                else:
                    # Keep compatibility with lightweight test/fallback page
                    # implementations that expose only page.screenshot().
                    await page.screenshot(
                        path=str(target.resolve()),
                        type="png",
                        full_page=True,
                    )
            return target.exists() and target.stat().st_size > 0
        except Exception as exc:
            self._log_exception(f"Playwright PNG 卡片渲染失败: {exc}")
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    self._log_warning(f"关闭卡片渲染页面失败: {exc}")
            try:
                temporary_html.unlink(missing_ok=True)
            except OSError as exc:
                self._log_warning(f"清理卡片临时 HTML 失败: {exc}")

    @staticmethod
    def _lyrics_lines(lyrics: str) -> list[dict[str, Any]]:
        """Normalize LRC text into the small, presentation-only line model."""

        lines: list[dict[str, Any]] = []
        previous_seconds: float | None = None
        for raw_line in str(lyrics or "").replace("\r\n", "\n").splitlines():
            raw_line = raw_line.strip().lstrip("\ufeff")
            if not raw_line:
                continue
            match = _LRC_LINE_RE.match(raw_line)
            if match:
                text = match.group("text").strip()
                if not text:
                    continue
                minutes = int(match.group("minutes"))
                seconds = float(match.group("seconds").replace(":", "."))
                absolute_seconds = minutes * 60 + seconds
                timestamp = f"{minutes:02d}:{int(seconds):02d}"
                stanza_break = (
                    previous_seconds is not None
                    and absolute_seconds - previous_seconds >= 6
                )
                previous_seconds = absolute_seconds
            else:
                # Ignore LRC metadata such as [ti:], [ar:] and [offset:].
                if raw_line.startswith("[") and "]" in raw_line:
                    continue
                text = raw_line
                timestamp = ""
                stanza_break = False

            lines.append(
                {
                    "timestamp": timestamp,
                    "text": text,
                    "stanza_break": stanza_break,
                    "is_credit": text.startswith(_LYRIC_CREDIT_PREFIXES),
                }
            )
        return lines

    async def render_lyrics_card(
        self,
        lyrics: str,
        *,
        platform_key: str,
        platform_display_name: str,
        song_title: str,
        artists: str | None = None,
        album: str | None = None,
        duration_seconds: float | None = None,
        release_text: str | None = None,
        cover_path: Path | None = None,
        source_label: str | None = None,
        target_path: Path | None = None,
    ) -> Path | None:
        """Render a platform-colored Apple-style lyrics card."""

        lyrics_lines = self._lyrics_lines(lyrics)
        if not lyrics_lines:
            self._log_warning("歌词为空，跳过歌词卡片渲染")
            return None
        if not self.environment:
            self._log_warning("Jinja2 不可用，跳过歌词卡片渲染")
            return None

        platform_key = str(platform_key or "").strip().lower()
        theme = self._LYRICS_PLATFORM_THEMES.get(
            platform_key,
            self._LYRICS_PLATFORM_THEMES["netease"],
        )
        duration_text = None
        if duration_seconds is not None:
            try:
                duration_value = max(0, int(float(duration_seconds)))
            except (TypeError, ValueError, OverflowError):
                duration_value = 0
            if duration_value:
                minutes, seconds = divmod(duration_value, 60)
                duration_text = f"{minutes}:{seconds:02d}"

        target = target_path
        try:
            if target is None:
                cache_dir = get_active_cache_dir(self.cfg.cache_dir)
                target = cache_dir / f"lyrics_{platform_key}_{uuid.uuid4().hex}.png"
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)

            try:
                template = self.environment.get_template(self._LYRICS_TEMPLATE_NAME)
            except TemplateNotFound as exc:
                self._log_exception(
                    f"歌词卡片模板不存在: {self._LYRICS_TEMPLATE_NAME}: {exc}"
                )
                return None

            logo_name = self._PLATFORM_LOGO_NAMES.get(platform_key)
            platform_logo_uri = (
                self._file_uri(self._RESOURCES_DIR / "logos" / logo_name)
                if logo_name
                else None
            )
            resolved_cover = (
                Path(cover_path)
                if cover_path is not None and Path(cover_path).is_file()
                else None
            )
            html = template.render(
                plugin_logo_uri=self._file_uri(self._PLUGIN_LOGO_PATH),
                platform_key=platform_key,
                platform_display_name=platform_display_name,
                platform_logo_uri=platform_logo_uri,
                song_title=song_title,
                artists=artists,
                album=album,
                duration_text=duration_text,
                release_text=release_text,
                cover_uri=self._file_uri(resolved_cover),
                lyrics_lines=lyrics_lines,
                lyric_count=len(lyrics_lines),
                source_label=source_label or f"歌词来源：{platform_display_name}",
                card_font_uri=self._file_uri(self._CARD_FONT_PATH),
                **theme,
            )
            template_base_dir = next(
                (
                    directory
                    for directory in self.template_dirs
                    if (directory / self._LYRICS_TEMPLATE_NAME).is_file()
                ),
                self._TEMPLATES_DIR,
            )
            rendered = await self._render_playwright_png(
                html,
                target,
                base_url=str(template_base_dir),
            )
            if not rendered:
                target.unlink(missing_ok=True)
                return None
            return target
        except Exception as exc:
            if target is not None:
                try:
                    Path(target).unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(f"清理歌词卡片失败: {cleanup_error}")
            self._log_exception(f"歌词卡片渲染失败: {exc}")
            return None

    async def render_qqmusic_login_card(self, qrcode: bytes) -> Path | None:
        """Render a styled QQ Music QR login card."""
        return await self._render_music_login_card(
            qrcode,
            platform_key="qqmusic",
            platform_display_name="QQ 音乐",
            scan_app_name="QQ 音乐或 QQ",
        )

    async def render_kugou_login_card(self, qrcode: bytes) -> Path | None:
        """Render a styled Kugou Music QR login card."""
        return await self._render_music_login_card(
            qrcode,
            platform_key="kugou",
            platform_display_name="酷狗音乐",
            scan_app_name="酷狗音乐 App",
        )

    async def _render_music_login_card(
        self,
        qrcode: bytes,
        *,
        platform_key: str,
        platform_display_name: str,
        scan_app_name: str,
    ) -> Path | None:
        if not qrcode:
            return None
        if not self.environment:
            self._log_warning("Jinja2 is unavailable; skipping music login card")
            return None

        qr_path: Path | None = None
        target: Path | None = None
        try:
            cache_dir = get_active_cache_dir(self.cfg.cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            qr_path = cache_dir / f".{platform_key}_login_qr_{token}.png"
            target = cache_dir / f"{platform_key}_login_{token}.png"
            qr_path.write_bytes(qrcode)

            try:
                template = self.environment.get_template(
                    self._QQMUSIC_LOGIN_TEMPLATE_NAME
                )
            except TemplateNotFound as exc:
                self._log_exception(
                    f"Music login card template is missing: "
                    f"{self._QQMUSIC_LOGIN_TEMPLATE_NAME}: {exc}"
                )
                return None

            platform_logo_uri = self._file_uri(
                self._RESOURCES_DIR / "logos" / f"{platform_key}.png"
            )
            html = template.render(
                qrcode_uri=self._file_uri(qr_path),
                plugin_logo_uri=self._file_uri(self._PLUGIN_LOGO_PATH),
                platform_key=platform_key,
                platform_display_name=platform_display_name,
                platform_logo_uri=platform_logo_uri,
                scan_app_name=scan_app_name,
                # Retain the previous context key for user-overridden QQ cards.
                qqmusic_logo_uri=platform_logo_uri,
                card_font_uri=self._file_uri(self._CARD_FONT_PATH),
            )
            template_base_dir = next(
                (
                    directory
                    for directory in self.template_dirs
                    if (directory / self._QQMUSIC_LOGIN_TEMPLATE_NAME).is_file()
                ),
                self._TEMPLATES_DIR,
            )
            rendered = await self._render_playwright_png(
                html,
                target,
                # Keep a concrete base URL so user-overridden copies can still
                # reference local assets alongside their template.
                base_url=str(template_base_dir),
            )
            if not rendered:
                if target is not None:
                    target.unlink(missing_ok=True)
                return None
            return target
        except Exception as exc:
            if target is not None:
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(
                        f"Failed to clean {platform_display_name} login card: "
                        f"{cleanup_error}"
                    )
            self._log_exception(
                f"{platform_display_name} login card rendering failed: {exc}"
            )
            return None
        finally:
            if qr_path is not None:
                try:
                    qr_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(
                        f"Failed to clean temporary {platform_display_name} QR image: "
                        f"{cleanup_error}"
                    )

    async def render_card(self, result: ParseResult) -> Path | None:
        """将解析实体渲染为缓存 PNG；失败只返回 ``None``，不影响媒体发送。"""
        if not bool(getattr(self.cfg, "card_enabled", True)):
            return None
        target: Path | None = None
        try:
            context = await self._result_context(result)
            html = self.render_html(result, context)
            cache_dir = (
                Path(result.cache_dir)
                if result.cache_dir is not None
                else get_active_cache_dir(self.cfg.cache_dir)
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            target = cache_dir / f"card_{uuid.uuid4().hex}.png"
            rendered = await self._render_playwright_png(
                html,
                target,
                base_url=str(self._template_base_dir()),
            )
            if not rendered:
                # 严格使用 Playwright。失败时不生成替代卡片，让 sender
                # 继续既有的原媒体发送路径。
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(f"清理失败的卡片文件失败: {cleanup_error}")
                logger.error("卡片渲染失败，已跳过卡片发送")
                result.render_image = None
                return None
            result.render_image = target
            return target
        except Exception as exc:
            if target is not None:
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(f"清理失败的卡片文件失败: {cleanup_error}")
            result.render_image = None
            self._log_exception(f"卡片渲染出现未处理异常，已跳过卡片发送: {exc}")
            return None

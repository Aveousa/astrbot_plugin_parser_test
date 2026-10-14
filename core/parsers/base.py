"""Parser 基类定义"""

import asyncio
from abc import ABC
from asyncio import Task, TimeoutError, sleep
from collections.abc import Callable, Coroutine, Mapping
from inspect import isawaitable
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from aiohttp import ClientError, ClientSession, ClientTimeout
from typing_extensions import Unpack

from ..config import ParserItem, PluginConfig
from ..constants import ANDROID_HEADER, COMMON_HEADER, IOS_HEADER
from ..data import (
    AudioContent,
    Author,
    DynamicContent,
    EngagementStats,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    ParseResultKwargs,
    Platform,
    VideoContent,
)
from ..download import Downloader
from ..exception import ParseException, RedirectException

T = TypeVar("T", bound="BaseParser")
HandlerFunc = Callable[[T, Match[str]], Coroutine[Any, Any, ParseResult]]
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"


class _LazyAvatarDownload:
    """Resolve an optional avatar only when the card actually needs it.

    Most parsers already have a concrete URL when they call ``create_author``.
    A few metadata enrichments (such as Apple Music artist portraits) need a
    small, best-effort lookup first.  Keeping that lookup lazy means an
    optional enhancement cannot delay or fail the normal media parse when a
    sender never renders a card.  The object is an awaitable rather than a
    scheduled task, so its network work starts from ``Author.get_avatar_path``.
    """

    # Artist lookup plus image transfer must remain bounded independently of
    # the global Downloader timeout.
    _TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        resolver: Callable[[], Any],
        downloader: Downloader,
        headers: dict[str, str],
        proxy: str | None,
        worker_proxy_url: str | None,
    ) -> None:
        self._resolver = resolver
        self._downloader = downloader
        self._headers = headers
        self._proxy = proxy
        self._worker_proxy_url = worker_proxy_url
        self._task: asyncio.Task[Path | None] | None = None

    def __await__(self):
        return self._get().__await__()

    def get_name(self) -> str:
        return "lazy_avatar"

    def done(self) -> bool:
        return self._task is not None and self._task.done()

    def cancel(self) -> bool:
        return self._task.cancel() if self._task is not None else False

    async def _get(self) -> Path | None:
        if self._task is None or (self._task.done() and self._task.cancelled()):
            self._task = asyncio.create_task(self._run())
        return await self._task

    async def _run(self) -> Path | None:
        try:
            async def resolve_and_download() -> Any:
                value = self._resolver()
                if isawaitable(value):
                    value = await value
                if not isinstance(value, str) or not value.strip():
                    return None
                downloaded = self._downloader.download_img(
                    value.strip(),
                    headers=self._headers,
                    proxy=self._proxy,
                    worker_proxy_url=self._worker_proxy_url,
                )
                return await downloaded if isawaitable(downloaded) else downloaded

            path = await asyncio.wait_for(
                resolve_and_download(), timeout=self._TIMEOUT_SECONDS
            )
            # Some test doubles and downloaders use ``Path()`` as a soft
            # failure marker.  Treat it as missing rather than letting the
            # renderer turn the current working directory into a file URI.
            return None if isinstance(path, Path) and path == Path() else path
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - avatar enrichment must be optional
            # Avatar lookup/download is deliberately an enhancement.  A
            # missing local path makes the renderer fall back to its placeholder.
            return None


# 注册处理器装饰器
def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""

    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])

        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))

        return func

    return decorator


class BaseParser:
    """所有平台 Parser 的抽象基类

    子类必须实现：
    - platform: 平台信息（包含名称和显示名称)
    """

    _registry: ClassVar[list[type["BaseParser"]]] = []
    """ 存储所有已注册的 Parser 类 """

    platform: ClassVar[Platform]
    """ 平台信息（包含名称和显示名称） """

    if TYPE_CHECKING:
        _key_patterns: ClassVar[KeyPatterns]
        _handlers: ClassVar[dict[str, HandlerFunc]]

    def __init__(self, config: PluginConfig, downloader: Downloader):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.cfg = config
        self.data_dir = self.cfg.data_dir
        self.downloader = downloader
        self._session: ClientSession | None = None

    @property
    def proxy(self) -> str | None:
        try:
            parser_cfg: ParserItem = getattr(
                self.cfg.parser, self.__class__.platform.name
            )
            return self.cfg.proxy if parser_cfg.use_proxy else None
        except AttributeError:
            return None

    @property
    def worker_proxy_url(self) -> str | None:
        """返回平台专用的 Worker 下载代理；默认不启用。"""
        return None

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:  # 跳过抽象类
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        # 获取所有被 handle 装饰的方法
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                handler = cast(HandlerFunc, attr)
                for keyword, pattern in key_patterns:
                    cls._handlers[keyword] = handler
                    cls._key_patterns.append((keyword, pattern))

        # 按关键字长度降序排序
        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        """获取所有已注册的 Parser 类"""
        return cls._registry

    @property
    def session(self) -> ClientSession:
        """获取当前实例的 session，惰性创建"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                proxy=self.proxy, timeout=ClientTimeout(total=self.cfg.common_timeout)
            )
        return self._session

    async def close_session(self) -> None:
        """关闭当前实例的 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        """解析 URL 提取信息

        Args:
            keyword: 关键词
            searched: 正则表达式匹配对象，由平台对应的模式匹配得到

        Returns:
            ParseResult: 解析结果

        Raises:
            ParseException: 解析失败时抛出
        """
        return await self._handlers[keyword](self, searched)

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> ParseResult:
        """先重定向再解析"""
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        """搜索 URL 匹配模式"""
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        """构建解析结果"""
        # 允许各平台解析器使用更贴近 API 的短字段名，同时把结果对象
        # 的公共字段固定为 like/comment/favorite/share_count。
        aliases = {
            "likes": "like_count",
            "comments": "comment_count",
            "favorites": "favorite_count",
            "shares": "share_count",
            "like": "like_count",
            "comment": "comment_count",
            "favorite": "favorite_count",
            "share": "share_count",
        }
        for source, target in aliases.items():
            if source in kwargs:
                if target not in kwargs:
                    kwargs[target] = kwargs[source]  # type: ignore[literal-required]
                # Do not leave an alias in **kwargs (especially when callers
                # provide both an alias and the canonical field).
                kwargs.pop(source, None)  # type: ignore[arg-type]
        return ParseResult(platform=cls.platform, **kwargs)

    @staticmethod
    def engagement_from_mapping(value: object) -> EngagementStats:
        """从平台 API 的任意映射中提取互动统计。

        解析器只需将对应的 ``stat``/``statistics`` 节点传入即可；未知或
        缺失字段会保留为 ``None``，不会影响原有解析流程。
        """
        if not isinstance(value, Mapping):
            return EngagementStats()
        return EngagementStats.from_mapping(value)

    async def get_redirect_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 单次重定向"""
        headers = headers or COMMON_HEADER.copy()
        retries = self.cfg.download_retry_times
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=False, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(f"redirect check {resp.status} {resp.reason}")
                    return resp.headers.get("Location", url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    async def get_final_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 允许多次重定向"""
        headers = headers or COMMON_HEADER.copy()
        retries = 2
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=True, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(
                            f"final url check {resp.status} {resp.reason}"
                        )
                    return str(resp.url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    def create_author(
        self,
        name: str,
        avatar_url: str | Callable[[], Any] | None = None,
        description: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建作者对象"""

        avatar_task = None
        if isinstance(avatar_url, str) and avatar_url:
            avatar_task = self.downloader.download_img(
                avatar_url,
                headers=headers or self.headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
        elif callable(avatar_url):
            avatar_task = _LazyAvatarDownload(
                avatar_url,
                self.downloader,
                headers or self.headers,
                self.proxy,
                self.worker_proxy_url,
            )
        return Author(name=name, avatar=avatar_task, description=description)

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
        *,
        cover_headers: dict[str, str] | None = None,
    ):
        """创建视频内容"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url,
                headers=(
                    cover_headers
                    if cover_headers is not None
                    else headers or self.headers
                ),
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_video(
                url_or_task,
                headers=headers or self.headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )

        return VideoContent(url_or_task, cover_task, duration)

    def create_video_content_by_task(
        self,
        path_task: Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
        *,
        cover_headers: dict[str, str] | None = None,
    ):
        """创建视频内容，允许调用方自行决定下载任务实现"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url,
                headers=(
                    cover_headers
                    if cover_headers is not None
                    else headers or self.headers
                ),
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
        return VideoContent(path_task, cover_task, duration)

    def create_image_contents(
        self,
        image_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建图片内容列表"""
        contents: list[ImageContent] = []
        for url in image_urls:
            task = self.downloader.download_img(
                url,
                headers=headers or self.headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
            contents.append(ImageContent(task))
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建动态图片内容列表"""
        contents: list[DynamicContent] = []
        for url in dynamic_urls:
            task = self.downloader.download_video(
                url,
                headers=headers or self.headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
            contents.append(DynamicContent(task))
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ):
        """创建音频内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_audio(
                url_or_task,
                headers=headers or self.headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )

        return AudioContent(url_or_task, duration)

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建图文内容 图片不能为空 文字可空 渲染时文字在前 图片在后"""
        image_task = self.downloader.download_img(
            image_url,
            headers=headers or self.headers,
            proxy=self.proxy,
            worker_proxy_url=self.worker_proxy_url,
        )
        return GraphicsContent(image_task, text, alt)

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建文件内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_file(
                url_or_task,
                headers=headers or self.headers,
                file_name=name,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )

        return FileContent(url_or_task)

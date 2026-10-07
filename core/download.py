import re
from asyncio import Task, TimeoutError, create_task, gather, sleep
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import aiofiles
from aiohttp import ClientError, ClientSession, ClientTimeout
from tqdm.asyncio import tqdm
from yarl import URL

from astrbot.api import logger

from .config import PluginConfig
from .cache import get_active_cache_dir
from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import generate_file_name, merge_av, safe_unlink

P = ParamSpec("P")
T = TypeVar("T")
_PERCENT_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_AUDIO_ERROR_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/json",
        "text/plain",
        "text/xml",
    }
)
_AUDIO_ERROR_PREFIXES = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"{",
    b"[",
)


def _looks_like_invalid_audio(content_type: str, chunk: bytes) -> bool:
    """Return whether a response is an error document instead of audio data.

    Media endpoints occasionally answer an expired signed URL with HTTP 200 and
    an HTML/JSON error page.  Status and Content-Length checks alone cannot
    detect that case, so audio downloads validate the response headers and a
    small prefix before creating the cached file.  ``video/mp4`` is deliberately
    accepted because some platforms expose an audio-only MP4 container.
    """

    media_type = content_type.partition(";")[0].strip().lower()
    if media_type in _AUDIO_ERROR_MEDIA_TYPES:
        return True
    prefix = chunk.lstrip()[:64].lower()
    return prefix.startswith(_AUDIO_ERROR_PREFIXES)


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class Downloader:
    """保留平台共用的流式媒体下载器。"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.max_size = self.cfg.source_max_size
        self.default_headers: dict[str, str] = COMMON_HEADER.copy()
        # 用于流式下载的客户端
        self.client = ClientSession(
            timeout=ClientTimeout(total=self.cfg.download_timeout)
        )

    async def close(self):
        """关闭网络客户端"""
        await self.client.close()

    @staticmethod
    def _request_url(url: str) -> str | URL:
        """Preserve already-encoded signed media URLs before aiohttp sends them."""
        if _PERCENT_ENCODED_RE.search(url):
            return URL(url, encoded=True)
        return url

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        worker_proxy_url: str | None = None,
        validate_audio: bool = False,
    ) -> Path:
        """流式下载"""
        if not file_name:
            file_name = generate_file_name(url)
        cache_dir = get_active_cache_dir(self.cfg.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_path = cache_dir / file_name
        # 如果文件存在，则直接返回
        if file_path.exists():
            return file_path
        headers = headers or self.default_headers
        request_url = self._request_url(url)
        proxy_kwargs = {} if proxy is ... else {"proxy": proxy}
        retries = self.cfg.download_retry_times
        for attempt in range(retries + 1):
            try:
                if worker_proxy_url:
                    request = self.client.post(
                        f"{worker_proxy_url.rstrip('/')}/download",
                        json={"url": url, "headers": headers},
                        allow_redirects=True,
                        **proxy_kwargs,
                    )
                else:
                    request = self.client.get(
                        request_url,
                        headers=headers,
                        allow_redirects=True,
                        **proxy_kwargs,
                    )
                async with request as response:
                    if response.status >= 400:
                        raise ClientError(f"HTTP {response.status} {response.reason}")
                    content_length = response.content_length
                    max_bytes = self.max_size * 1024 * 1024
                    response_headers = getattr(response, "headers", {}) or {}
                    content_type = str(response_headers.get("Content-Type", ""))

                    if content_length == 0:
                        logger.warning(f"媒体 url: {url}, 大小为 0, 取消下载")
                        raise ZeroSizeException
                    if content_length and content_length > max_bytes:
                        logger.warning(
                            f"媒体 url: {url} 大小 {content_length / 1024 / 1024:.2f} MB 超过 {self.max_size} MB, 取消下载"
                        )
                        raise SizeLimitException

                    downloaded = 0
                    first_chunk = True
                    with self.get_progress_bar(file_name, content_length) as bar:
                        async with aiofiles.open(file_path, "wb") as file:
                            async for chunk in response.content.iter_chunked(
                                1024 * 1024
                            ):
                                if validate_audio and first_chunk:
                                    first_chunk = False
                                    if _looks_like_invalid_audio(content_type, chunk):
                                        raise DownloadException(
                                            "音频地址返回的不是可用媒体文件"
                                        )
                                downloaded += len(chunk)
                                if downloaded > max_bytes:
                                    raise SizeLimitException
                                await file.write(chunk)
                                bar.update(len(chunk))

                    if downloaded == 0:
                        logger.warning(f"媒体 url: {url}, 实际大小为 0, 取消下载")
                        raise ZeroSizeException
                    if content_length and downloaded < content_length:
                        raise ClientError(
                            f"HTTP payload incomplete {downloaded}/{content_length}"
                        )

                return file_path
            except (ZeroSizeException, SizeLimitException):
                await safe_unlink(file_path)
                raise
            except DownloadException:
                await safe_unlink(file_path)
                raise
            except (ClientError, TimeoutError) as exc:
                await safe_unlink(file_path)
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                logger.exception(f"下载失败 | url: {url}, file_path: {file_path}")
                raise DownloadException("媒体下载失败") from exc
        raise DownloadException("媒体下载失败")

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        """获取进度条 bar

        Args:
            desc (str): 描述
            total (int | None): 总大小. Defaults to None.

        Returns:
            tqdm: 进度条
        """
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        worker_proxy_url: str | None = None,
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(
            url,
            file_name=video_name,
            headers=headers,
            proxy=proxy,
            worker_proxy_url=worker_proxy_url,
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        worker_proxy_url: str | None = None,
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url,
            file_name=audio_name,
            headers=headers,
            proxy=proxy,
            worker_proxy_url=worker_proxy_url,
            validate_audio=True,
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        worker_proxy_url: str | None = None,
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url,
            file_name=file_name,
            headers=headers,
            proxy=proxy,
            worker_proxy_url=worker_proxy_url,
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        worker_proxy_url: str | None = None,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(
            url,
            file_name=img_name,
            headers=headers,
            proxy=proxy,
            worker_proxy_url=worker_proxy_url,
        )

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        worker_proxy_url: str | None = None,
    ) -> list[Path]:
        paths_or_errs = await gather(
            *[
                self.download_img(
                    url,
                    headers=headers,
                    proxy=proxy,
                    worker_proxy_url=worker_proxy_url,
                )
                for url in urls
            ],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        worker_proxy_url: str | None = None,
    ) -> Path:
        """
        download video and audio file by url with stream and merge
        """
        v_path, a_path = await gather(
            self.download_video(
                v_url,
                headers=headers,
                proxy=proxy,
                worker_proxy_url=worker_proxy_url,
            ),
            self.download_audio(
                a_url,
                headers=headers,
                proxy=proxy,
                worker_proxy_url=worker_proxy_url,
            ),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path

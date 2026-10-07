from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from yarl import URL


@pytest.fixture
def download_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    config_pkg = types.ModuleType("astrbot.core.config")
    config_pkg.__path__ = []
    config_mod = types.ModuleType("astrbot.core.config.astrbot_config")
    config_mod.AstrBotConfig = dict
    star = types.ModuleType("astrbot.core.star")
    star.__path__ = []
    context_mod = types.ModuleType("astrbot.core.star.context")
    context_mod.Context = object
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: tempfile.gettempdir()
    path_mod.get_astrbot_plugin_path = lambda: tempfile.gettempdir()

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.config": config_pkg,
        "astrbot.core.config.astrbot_config": config_mod,
        "astrbot.core.star": star,
        "astrbot.core.star.context": context_mod,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_mod,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "core.download", raising=False)
    monkeypatch.delitem(sys.modules, "core.config", raising=False)
    monkeypatch.delitem(sys.modules, "core.utils", raising=False)
    return importlib.import_module("core.download")


def test_request_url_preserves_percent_encoded_signed_query(download_module):
    raw_url = (
        "https://p3-sign.douyinpic.com/tos-cn/example.webp?"
        "x-signature=DkHnjnMPzQbZ%2FkkFsDF2zzqjLzg%3D&from=327834062"
    )

    prepared = download_module.Downloader._request_url(raw_url)

    assert isinstance(prepared, URL)
    assert str(prepared) == raw_url
    assert "x-signature=DkHnjnMPzQbZ%2FkkFsDF2zzqjLzg%3D" in prepared.raw_query_string


def test_request_url_keeps_plain_url_as_string(download_module):
    raw_url = "https://example.com/media/image.webp?from=327834062"

    assert download_module.Downloader._request_url(raw_url) == raw_url


def test_streamd_posts_worker_download_contract(download_module, tmp_path: Path):
    calls: list[tuple[str, dict]] = []
    media = b"worker-media"

    class Content:
        async def iter_chunked(self, _size: int):
            yield media

    class Response:
        status = 200
        reason = "OK"
        content_length = len(media)
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Client:
        def post(self, url: str, **kwargs):
            calls.append((url, kwargs))
            return Response()

        def get(self, *_args, **_kwargs):
            raise AssertionError("Worker 模式不应直连媒体 URL")

    class Progress:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def update(self, _size: int):
            return None

    downloader = object.__new__(download_module.Downloader)
    downloader.cfg = SimpleNamespace(cache_dir=tmp_path, download_retry_times=0)
    downloader.max_size = 1
    downloader.default_headers = {"User-Agent": "default"}
    downloader.client = Client()
    downloader.get_progress_bar = lambda *_args, **_kwargs: Progress()

    async def download():
        return await downloader.streamd(
            "https://p3-sign.douyinpic.com/media.webp?x=1",
            file_name="media.webp",
            headers={"User-Agent": "douyin", "Referer": "https://douyin.com/"},
            proxy=None,
            worker_proxy_url="https://proxy.example/",
        )

    path = asyncio.run(download())

    assert path.read_bytes() == media
    assert calls == [
        (
            "https://proxy.example/download",
            {
                "json": {
                    "url": "https://p3-sign.douyinpic.com/media.webp?x=1",
                    "headers": {
                        "User-Agent": "douyin",
                        "Referer": "https://douyin.com/",
                    },
                },
                "allow_redirects": True,
                "proxy": None,
            },
        )
    ]


def test_download_audio_rejects_html_error_page(download_module, tmp_path: Path):
    """A successful HTTP status must not turn an HTML error page into audio."""

    body = b"<!doctype html><html><body>permission denied</body></html>"

    class Content:
        async def iter_chunked(self, _size: int):
            yield body

    class Response:
        status = 200
        reason = "OK"
        content_length = len(body)
        headers = {"Content-Type": "text/html; charset=utf-8"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Client:
        def get(self, *_args, **_kwargs):
            return Response()

    class Progress:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def update(self, _size: int):
            return None

    downloader = object.__new__(download_module.Downloader)
    downloader.cfg = SimpleNamespace(cache_dir=tmp_path, download_retry_times=0)
    downloader.max_size = 1
    downloader.default_headers = {"User-Agent": "default"}
    downloader.client = Client()
    downloader.get_progress_bar = lambda *_args, **_kwargs: Progress()

    async def download():
        return await downloader.download_audio(
            "https://media.example/song",
            audio_name="song.mp3",
            headers={"User-Agent": "music-parser"},
            proxy=None,
        )

    with pytest.raises(download_module.DownloadException):
        asyncio.run(download())

    assert not (tmp_path / "song.mp3").exists()


def test_download_audio_accepts_audio_only_mp4_container(
    download_module, tmp_path: Path
):
    """Qishui may expose an audio-only MP4 URL with a ``video/mp4`` type."""

    body = b"\x00\x00\x00\x1cftypM4A " + b"audio-data"

    class Content:
        async def iter_chunked(self, _size: int):
            yield body

    class Response:
        status = 200
        reason = "OK"
        content_length = len(body)
        headers = {"Content-Type": "video/mp4"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Client:
        def get(self, *_args, **_kwargs):
            return Response()

    class Progress:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def update(self, _size: int):
            return None

    downloader = object.__new__(download_module.Downloader)
    downloader.cfg = SimpleNamespace(cache_dir=tmp_path, download_retry_times=0)
    downloader.max_size = 1
    downloader.default_headers = {"User-Agent": "default"}
    downloader.client = Client()
    downloader.get_progress_bar = lambda *_args, **_kwargs: Progress()

    async def download():
        return await downloader.download_audio(
            "https://media.example/song",
            audio_name="song.mp3",
            headers={"User-Agent": "music-parser"},
            proxy=None,
        )

    path = asyncio.run(download())

    assert path.read_bytes() == body

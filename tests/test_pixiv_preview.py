import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import Author
from core.parsers.pixiv import PixivHelper, PixivParser


class _Downloader:
    def download_img(self, url: str, **kwargs):
        async def complete() -> Path:
            return Path(url.rsplit("/", 1)[-1])

        return asyncio.create_task(complete())


def test_missing_detail_cover_reuses_first_page_as_card_preview():
    async def parse():
        parser = PixivParser.__new__(PixivParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(pixiv=SimpleNamespace(use_proxy=False)),
        )
        parser.mycfg = SimpleNamespace(
            nsfw="send",
            multi_image_forward=False,
        )
        parser.downloader = _Downloader()

        async def get_author(_uid: str) -> Author:
            return Author("author")

        async def get_pages(_pid: str):
            return [
                {"urls": {"original": "https://i.pximg.net/first.jpg"}},
                {"urls": {"original": "https://i.pximg.net/second.jpg"}},
            ]

        parser._get_author = get_author
        parser.api = SimpleNamespace(get_pages=get_pages)

        return await parser._handle_illust(
            "143290358",
            {
                "id": "143290358",
                "title": "ordinary multi-image illustration",
                "userId": "26090072",
                "illustType": 0,
                "pageCount": 2,
                "xRestrict": 0,
                "urls": {"regular": None},
                "tags": {"tags": []},
            },
        )

    result = asyncio.run(parse())

    assert len(result.contents) == 1
    assert len(result.send_groups) == 1
    assert len(result.send_groups[0].contents) == 2
    assert result.contents[0] is result.send_groups[0].contents[0]
    assert result.contents[0].path_task.result() == Path("first.jpg")


def test_pixiv_cover_blur_runs_in_worker_thread(monkeypatch: pytest.MonkeyPatch):
    parser = PixivParser.__new__(PixivParser)
    parser.downloader = _Downloader()
    parser.cfg = SimpleNamespace(proxy=None)
    calls = []

    def blur(path: Path, radius: int = 20) -> Path:
        calls.append((path, radius))
        return path.with_name("blurred.jpg")

    async def run_in_thread(func, *args, **kwargs):
        assert func is PixivHelper.blur
        return func(*args, **kwargs)

    monkeypatch.setattr(PixivHelper, "blur", blur)
    monkeypatch.setattr(asyncio, "to_thread", run_in_thread)

    result = asyncio.run(
        parser._download_cover("https://i.pximg.net/cover.jpg", blur=True)
    )

    assert calls == [(Path("cover.jpg"), 5)]
    assert result[0].path_task == Path("blurred.jpg")

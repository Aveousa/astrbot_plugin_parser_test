import asyncio
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from core.parsers.xhs import XHSImage, XHSParser


def test_xhs_image_exposes_live_photo_stream_url():
    image = XHSImage(
        urlDefault="https://example.com/cover.webp",
        livePhoto=True,
        stream={"EF4": [{"masterUrl": "https://example.com/live.mp4"}]},
    )

    assert image.image_url.endswith("cover.webp")
    assert image.is_live_photo is True
    assert image.video_url.endswith("live.mp4")


def test_xhs_motion_photo_converts_webp_and_cleans_intermediates(tmp_path: Path):
    video = b"\x00\x00\x00\x18ftypmp42mp4-data"

    class FakeDownloader:
        async def download_img(self, _url: str, *, img_name: str, **_kwargs) -> Path:
            path = tmp_path / img_name
            Image.new("RGB", (2, 2), (255, 0, 0)).save(path, "WEBP")
            return path

        async def download_video(
            self, _url: str, *, video_name: str, **_kwargs
        ) -> Path:
            path = tmp_path / video_name
            path.write_bytes(video)
            return path

    parser = object.__new__(XHSParser)
    parser.cfg = SimpleNamespace(
        cache_dir=tmp_path,
        proxy=None,
        parser=SimpleNamespace(xhs=SimpleNamespace(use_proxy=False)),
    )
    parser.downloader = FakeDownloader()

    result = asyncio.run(
        parser._download_motion_photo(
            "https://example.com/cover.webp",
            "https://example.com/live.mp4",
            headers={"User-Agent": "test"},
            referer="https://www.xiaohongshu.com/explore/demo",
        )
    )

    assert result.name.startswith("xhs_motion_")
    assert result.suffix == ".jpg"
    assert result.read_bytes()[:2] == b"\xff\xd8"
    assert result.read_bytes().endswith(video)
    assert not list(tmp_path.glob(".xhs_motion_*"))

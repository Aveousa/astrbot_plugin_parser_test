import asyncio
from base64 import b64encode
import json
from pathlib import Path
from types import SimpleNamespace

import msgspec
import pytest

from core.parsers.douyin import DouyinParser, signature
from core.parsers.douyin.motion_photo import XMP_HEADER, build_motion_photo
from core.parsers.douyin.slides import Image as SlidesImage
from core.parsers.douyin.slides import PlayAddr as SlidesPlayAddr
from core.parsers.douyin.slides import Video as SlidesVideo
from core.parsers.douyin.video import AwemeDetailRes, Image, PlayAddr, Video


def test_douyin_live_url_matches_dedicated_handler():
    keyword, searched = DouyinParser.search_url(
        "https://live.douyin.com/1234567890"
    )

    assert keyword == "live.douyin"
    assert searched.group("web_rid") == "1234567890"


def test_douyin_reflow_live_url_matches_dedicated_handler():
    keyword, searched = DouyinParser.search_url(
        "https://webcast.amemv.com/douyin/webcast/reflow/1234567890123456789"
    )

    assert keyword == "webcast.amemv"
    assert searched.group("room_id") == "1234567890123456789"


def test_extract_live_room_from_rsc_payload():
    payload = (
        '2:{"roomStore":{"roomInfo":{"room":{'
        '"title":"直播标题",'
        '"owner":{"nickname":"主播", "avatar_thumb":{"url_list":["avatar"]}},'
        '"cover":{"url_list":["cover"]},'
        '"like_count":1234'
        "}}}}"
    )
    html = "self.__pace_f.push([1," + json.dumps(payload) + "])"

    room = DouyinParser._extract_live_room(html)

    assert room["title"] == "直播标题"
    assert room["owner"]["nickname"] == "主播"
    assert room["like_count"] == 1234
    assert DouyinParser._first_url(room["cover"]) == "cover"


def test_extract_live_room_from_reflow_rsc_payload():
    payload = (
        '5:["$","$L7",null,{"data":{"room":{'
        '"title":"回流直播",'
        '"owner":{"nickname":"主播","avatarThumb":{"urlList":["avatar"]}},'
        '"cover":{"urlList":["cover"]},'
        '"stats":{"userCountStr":"12"},"likeCount":34'
        "}}}]"
    )
    html = "self.__rsc_f.push([1," + json.dumps(payload) + "])"

    room = DouyinParser._extract_reflow_live_room(html)

    assert room["title"] == "回流直播"
    assert room["owner"]["nickname"] == "主播"
    assert DouyinParser._first_url(room["owner"]["avatarThumb"]) == "avatar"
    assert room["stats"]["userCountStr"] == "12"


def test_extract_live_room_skips_empty_room_store_state():
    payload = (
        '2:{"roomStore":{"roomInfo":{"room":{"title":"真实直播"}}}'
        ',"roomStore":{"roomInfo":{}}}'
    )
    html = "self.__pace_f.push([1," + json.dumps(payload) + "])"

    room = DouyinParser._extract_live_room(html)

    assert room["title"] == "真实直播"


def test_live_room_prefers_snapshot_as_separate_send_group(tmp_path: Path):
    parser = object.__new__(DouyinParser)
    parser.cfg = SimpleNamespace(cache_dir=tmp_path)
    parser.mycfg = SimpleNamespace(
        worker_proxy_enabled=False,
        worker_proxy_url="",
        use_proxy=False,
    )
    parser.downloader = SimpleNamespace(
        download_img=lambda _url, **_kwargs: tmp_path / "cover.jpg"
    )

    async def capture(_url, _fallback):
        return tmp_path / "snapshot.jpg"

    parser._capture_live_snapshot = capture
    async def exercise():
        result = parser._build_live_result(
            "https://live.douyin.com/1234567890",
            "1234567890",
            {
                "title": "直播标题",
                "owner": {"nickname": "主播"},
                "cover": {"url_list": ["cover"]},
                "like_count": 123,
                "stats": {"comment_count": 45},
                "stream_url": {"hls_pull_url": "https://stream.example/live.m3u8"},
            },
            {},
        )
        snapshot = result.send_groups[0].contents[0]
        snapshot_path = await snapshot.get_path()
        return result, snapshot_path

    result, snapshot_path = asyncio.run(exercise())

    assert [content.path_task for content in result.contents] == [
        tmp_path / "cover.jpg"
    ]
    assert len(result.send_groups) == 1
    assert snapshot_path == tmp_path / "snapshot.jpg"
    assert result.send_groups[0].force_merge is False
    assert result.like_count is None
    assert result.comment_count is None


def test_build_motion_photo_injects_xmp_and_appends_video(tmp_path: Path):
    jpeg = b"\xff\xd8\xff\xdbjpeg-data\xff\xd9"
    video = b"\x00\x00\x00\x18ftypmp42mp4-data"
    image_path = tmp_path / "cover.jpg"
    video_path = tmp_path / "live.mp4"
    output_path = tmp_path / "motion.jpg"
    image_path.write_bytes(jpeg)
    video_path.write_bytes(video)

    result = build_motion_photo(image_path, video_path, output_path)

    output = result.read_bytes()
    assert output[:4] == b"\xff\xd8\xff\xe1"
    app1_length = int.from_bytes(output[4:6], "big")
    assert output[6 : 6 + app1_length - 2].startswith(XMP_HEADER)
    assert b'GCamera:MotionPhoto="1"' in output
    assert f'Item:Length="{len(video)}"'.encode() in output
    assert output.endswith(video)


def test_build_motion_photo_rejects_non_jpeg_cover(tmp_path: Path):
    image_path = tmp_path / "cover.webp"
    video_path = tmp_path / "live.mp4"
    output_path = tmp_path / "motion.jpg"
    image_path.write_bytes(b"RIFF-not-a-jpeg")
    video_path.write_bytes(b"video")

    with pytest.raises(ValueError, match="not a JPEG"):
        build_motion_photo(image_path, video_path, output_path)

    assert not output_path.exists()


def test_motion_photo_video_url_prefers_h264_uri():
    parser = object.__new__(DouyinParser)
    image = Image(
        clip_type=5,
        url_list=["https://example.com/cover.jpeg"],
        video=Video(
            play_addr=PlayAddr(url_list=["https://example.com/fallback.mp4"]),
            play_addr_h264=PlayAddr(uri="live-video-id"),
        ),
    )

    assert parser._motion_photo_video_url(image) == (
        "https://aweme.snssdk.com/aweme/v1/play/"
        "?video_id=live-video-id&ratio=1080p&line=0"
    )


def test_douyin_image_headers_keep_ua_and_add_page_context():
    parser = object.__new__(DouyinParser)
    parser.ios_headers = {"User-Agent": "ios-agent", "Cookie": "page-cookie"}
    referer = "https://www.iesdouyin.com/share/note/1234567890123456789/"

    headers = parser._build_image_headers(
        referer,
        base_headers={
            "User-Agent": "android-agent",
            "cookie": "stale-cookie",
            "Accept": "old-accept",
            "Referer": "https://old.example/",
        },
    )

    assert headers["User-Agent"] == "android-agent"
    assert headers["Referer"] == referer
    assert headers["Accept"] == parser._IMAGE_ACCEPT
    assert headers["Accept-Language"] == "zh-CN,zh;q=0.9"
    assert not any(key.lower() == "cookie" for key in headers)


@pytest.mark.parametrize(
    ("enabled", "url", "expected"),
    [
        (False, "https://proxy.example", None),
        (True, "", None),
        (True, "not-a-url", None),
        (True, "https://proxy.example/?token=secret", None),
        (True, "  https://proxy.example/  ", "https://proxy.example"),
    ],
)
def test_worker_proxy_requires_enabled_switch_and_valid_address(
    enabled: bool, url: str, expected: str | None
):
    parser = object.__new__(DouyinParser)
    parser.mycfg = SimpleNamespace(
        worker_proxy_enabled=enabled,
        worker_proxy_url=url,
    )

    assert parser.worker_proxy_url == expected


def test_worker_api_url_matches_deployed_worker_routes():
    parser = object.__new__(DouyinParser)
    parser.mycfg = SimpleNamespace(
        worker_proxy_enabled=True,
        worker_proxy_url="https://proxy.example/",
    )
    target = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123"

    request_url, via_worker = parser._worker_api_request_url(target)

    assert via_worker is True
    assert request_url == (
        "https://proxy.example/douyin/aweme/v1/web/aweme/detail/?aweme_id=123"
    )
    untouched, via_worker = parser._worker_api_request_url(
        "https://www.iesdouyin.com/share/video/123/"
    )
    assert untouched == "https://www.iesdouyin.com/share/video/123/"
    assert via_worker is False


def test_worker_download_request_posts_target_and_headers():
    calls: list[tuple[str, dict]] = []
    sentinel = object()

    class Session:
        closed = False

        def post(self, url: str, **kwargs):
            calls.append((url, kwargs))
            return sentinel

        def get(self, *_args, **_kwargs):
            raise AssertionError("Worker 模式不应直接 GET 目标 URL")

    parser = object.__new__(DouyinParser)
    parser.mycfg = SimpleNamespace(
        worker_proxy_enabled=True,
        worker_proxy_url="https://proxy.example",
    )
    parser._session = Session()

    request, via_worker = parser._worker_download_request(
        "https://www.iesdouyin.com/share/video/123/",
        {"User-Agent": "test", "Cookie": "ttwid=value"},
        allow_redirects=False,
    )

    assert request is sentinel
    assert via_worker is True
    assert calls == [
        (
            "https://proxy.example/download",
            {
                "json": {
                    "url": "https://www.iesdouyin.com/share/video/123/",
                    "headers": {
                        "User-Agent": "test",
                        "Cookie": "ttwid=value",
                    },
                },
                "allow_redirects": False,
            },
        )
    ]


def test_worker_api_body_decodes_base64_wrapper():
    upstream = b'{"status_code":0,"aweme_detail":{"desc":"test"}}'
    wrapped = msgspec.json.encode(
        {"data": b64encode(upstream).decode(), "encoding": "base64"}
    )

    assert DouyinParser._decode_worker_api_body(wrapped) == upstream

    with pytest.raises(ValueError, match="Base64"):
        DouyinParser._decode_worker_api_body(
            msgspec.json.encode({"data": "not-base64", "encoding": "base64"})
        )


def test_video_content_uses_dedicated_cover_headers():
    requests: dict[str, dict] = {}

    class FakeDownloader:
        def download_img(self, _url: str, **kwargs):
            requests["cover"] = kwargs
            return Path("cover.jpg")

        def download_video(self, _url: str, **kwargs):
            requests["video"] = kwargs
            return Path("video.mp4")

    parser = object.__new__(DouyinParser)
    parser.cfg = SimpleNamespace(
        proxy=None,
        parser=SimpleNamespace(douyin=SimpleNamespace(use_proxy=False)),
    )
    parser.mycfg = SimpleNamespace(
        worker_proxy_enabled=True,
        worker_proxy_url="https://proxy.example/",
    )
    parser.headers = {"User-Agent": "default-agent"}
    parser.downloader = FakeDownloader()

    content = parser.create_video_content(
        "https://example.com/video.mp4",
        "https://example.com/cover.webp",
        headers={"User-Agent": "video-agent", "Accept": "*/*"},
        cover_headers={"User-Agent": "cover-agent", "Accept": "image/*"},
    )

    assert content.path_task == Path("video.mp4")
    assert content.cover == Path("cover.jpg")
    assert requests["video"]["headers"]["User-Agent"] == "video-agent"
    assert requests["cover"]["headers"]["User-Agent"] == "cover-agent"
    assert requests["cover"]["headers"]["Accept"] == "image/*"
    assert requests["video"]["worker_proxy_url"] == "https://proxy.example"
    assert requests["cover"]["worker_proxy_url"] == "https://proxy.example"


def test_aweme_detail_schema_decodes_motion_photo_metadata():
    response = msgspec.json.decode(
        msgspec.json.encode(
            {
                "status_code": 0,
                "aweme_detail": {
                    "create_time": 0,
                    "author": {"nickname": "tester"},
                    "desc": "live photo",
                    "images": [
                        {
                            "clip_type": 5,
                            "url_list": ["https://example.com/cover.jpeg"],
                            "video": {
                                "play_addr_h264": {"uri": "live-video-id"}
                            },
                        }
                    ],
                },
            }
        ),
        type=AwemeDetailRes,
    )

    assert response.aweme_detail is not None
    image = response.aweme_detail.images[0]
    assert image.clip_type == 5
    assert image.video.play_addr_h264.uri == "live-video-id"


def test_slides_dynamic_urls_exclude_motion_photo_clips():
    live = SlidesImage(
        clip_type=5,
        video=SlidesVideo(
            play_addr=SlidesPlayAddr(url_list=["https://example.com/live.mp4"])
        ),
    )
    dynamic = SlidesImage(
        clip_type=2,
        video=SlidesVideo(
            play_addr=SlidesPlayAddr(url_list=["https://example.com/dynamic.mp4"])
        ),
    )

    from core.parsers.douyin.slides import Author, Avatar, SlidesData

    slides = SlidesData(
        author=Author(nickname="tester", avatar_thumb=Avatar(url_list=[])),
        desc="demo",
        create_time=0,
        images=[live, dynamic],
    )

    assert slides.dynamic_urls == ["https://example.com/dynamic.mp4"]


def test_download_motion_photo_packages_and_cleans_intermediate_files(
    tmp_path: Path,
):
    jpeg = b"\xff\xd8\xff\xdbjpeg-data\xff\xd9"
    video = b"\x00\x00\x00\x18ftypmp42mp4-data"

    requests: dict[str, dict] = {}

    class FakeDownloader:
        async def download_img(self, _url: str, *, img_name: str, **kwargs) -> Path:
            requests["image"] = kwargs
            path = tmp_path / img_name
            path.write_bytes(jpeg)
            return path

        async def download_video(
            self, _url: str, *, video_name: str, **kwargs
        ) -> Path:
            requests["video"] = kwargs
            path = tmp_path / video_name
            path.write_bytes(video)
            return path

    parser = object.__new__(DouyinParser)
    parser.cfg = SimpleNamespace(
        cache_dir=tmp_path,
        proxy=None,
        parser=SimpleNamespace(douyin=SimpleNamespace(use_proxy=False)),
    )
    parser.downloader = FakeDownloader()
    parser.ios_headers = {"User-Agent": "test", "Cookie": "secret"}

    result = asyncio.run(
        parser._download_motion_photo(
            "https://example.com/cover.jpeg",
            "https://example.com/live.mp4",
            headers={"User-Agent": "test", "Cookie": "secret"},
            referer="https://www.iesdouyin.com/share/note/1234567890123456789/",
        )
    )

    assert result.name.startswith("motion_")
    assert result.suffix == ".jpg"
    assert result.read_bytes().endswith(video)
    assert not list(tmp_path.glob(".motion_*"))
    assert requests["image"]["headers"]["Referer"].endswith("1234567890123456789/")
    assert requests["image"]["headers"]["Accept"] == parser._IMAGE_ACCEPT
    assert "Cookie" not in requests["image"]["headers"]
    assert requests["video"]["headers"]["Referer"].endswith("1234567890123456789/")
    assert requests["video"]["headers"]["Accept"] == "*/*"
    assert "Cookie" not in requests["video"]["headers"]


def test_a_bogus_matches_reference_vector(monkeypatch: pytest.MonkeyPatch):
    query = (
        "device_platform=webapp&aid=6383&channel=channel_pc_web&"
        "aweme_id=7643801277823001529"
    )
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
        "Safari/537.36 Edg/130.0.0.0"
    )
    monkeypatch.setattr(signature.time, "time", lambda: 1786896000.123)
    signature.random.seed(20260816)

    result = signature.generate_a_bogus(query, user_agent)

    assert result == (
        "DfR0Mfu2p3jifjSt5RCLfY3q6lbVYQEC0SVkMD2fUBDPyL39HMTa9exoqk4v2FEj"
        "Fs/jIeSjy4hbO3KDrQAj8rmUHWwoWdQ2m6RdKl5Q5I0j53iruyR0nt8F4kG-FeeM-"
        "iA3xOvsy75nFbw0AoK75JIlO6ZCcHgOEisnO9W="
    )

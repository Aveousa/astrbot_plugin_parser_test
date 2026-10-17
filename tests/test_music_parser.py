import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from core.data import AudioContent
from core.exception import ParseException
from core.parsers.music import (
    AppleMusicParser,
    KugouMusicParser,
    KuwoMusicParser,
    NetEaseMusicParser,
    QishuiMusicParser,
    QQMusicParser,
    _duration_seconds,
    _format_release_text,
)
import core.parsers.music as music_parser_module


class _Downloader:
    def __init__(self):
        self.image_urls: list[str] = []
        self.audio_urls: list[str] = []
        self.audio_requests: list[dict[str, object]] = []

    def download_img(self, url: str, **_kwargs):
        self.image_urls.append(url)

        async def complete() -> Path:
            return Path(url.rsplit("/", 1)[-1] or "image.jpg")

        return asyncio.create_task(complete())

    def download_audio(self, url: str, **_kwargs):
        self.audio_urls.append(url)
        self.audio_requests.append({"url": url, **_kwargs})

        async def complete() -> Path:
            return Path(url.rsplit("/", 1)[-1] or "track.mp3")

        return asyncio.create_task(complete())


def test_qqmusic_account_status_requires_identity_and_auth_key():
    parser = QQMusicParser.__new__(QQMusicParser)
    cookie = "uin=12345; qqmusic_key=account-key"
    parser.cookiejar = SimpleNamespace(
        get_cookie_header_for_url=lambda _url: cookie
    )
    assert parser.has_cookie() is True
    assert parser.has_auth_credentials() is True

    cookie = "uin=12345; pgv_pvid=anonymous"
    assert parser.has_cookie() is True
    assert parser.has_auth_credentials() is False

    # Browser-exported cookies and QR login responses may expose the same
    # credentials through QQ's alternate names.
    cookie = "qqmusic_uin=12345; qm_keyst=account-key"
    assert parser.has_cookie() is True
    assert parser.has_auth_credentials() is True


def _build_result(show_playlist_cover: bool | None):
    async def build():
        parser = QishuiMusicParser.__new__(QishuiMusicParser)
        parser.cfg = SimpleNamespace(
            card_enabled=True,
            parser=SimpleNamespace(
                qishui=SimpleNamespace(
                    use_proxy=False,
                    **(
                        {"show_playlist_cover": show_playlist_cover}
                        if show_playlist_cover is not None
                        else {}
                    ),
                )
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        result = parser._playlist_result(
            title="测试歌单",
            author_name="测试作者",
            author_avatar=None,
            description=None,
            cover="https://example.com/playlist.jpg",
            track_count=1,
            timestamp=None,
            url="https://example.com/playlist",
            identifier="playlist-id",
            tracks=[
                {
                    "title": "测试歌曲",
                    "singer": "测试歌手",
                    "album": {"name": "测试专辑"},
                }
            ],
            stats={"comments": 2},
        )
        if result.contents:
            await result.contents[0].get_path()
        return result

    return asyncio.run(build())


@pytest.mark.parametrize(
    ("parser_cls", "text", "expected_keyword"),
    [
        (
            QQMusicParser,
            "我找到一份歌单 https://c6.y.qq.com/base/fcgi-bin/u?__=abc123",
            "c6.y.qq.com/base/fcgi-bin/u",
        ),
        (
            QQMusicParser,
            "QQ 音乐歌单 https://i2.y.qq.com/n3/other/pages/details/playlist.html?hosteuin=abc&id=9013740134&source=qq",
            "i2.y.qq.com/n3/other/pages/details/playlist",
        ),
        (
            NetEaseMusicParser,
            "网易云歌单：https://163cn.tv/AbC_123",
            "163cn.tv",
        ),
        (
            NetEaseMusicParser,
            "网易云歌单 https://music.163.com/m/playlist?app_version=9.5.15&id=123456&userid=1312543631",
            "music.163.com/m/playlist",
        ),
        (
            NetEaseMusicParser,
            "网易云歌单 https://music.163.com/#/playlist?id=123456",
            "music.163.com/#/playlist",
        ),
        (
            NetEaseMusicParser,
            "网易云歌单 https://y.music.163.com/m/playlist?app_version=9.5.15&id=12431056413&userid=1312543631",
            "y.music.163.com/m/playlist",
        ),
        (
            KugouMusicParser,
            "酷狗歌单 https://t1.kugou.com/zQfV86G6V3",
            "t1.kugou.com",
        ),
        (
            KugouMusicParser,
            "酷狗歌单 https://activity.kugou.com/share/v-abc/index.html?global_specialid=x",
            "activity.kugou.com/share",
        ),
        (
            KugouMusicParser,
            "酷狗歌单 https://m.kugou.com/songlist/gcid_3znb0r4xz2z03e/?src_cid=3znb0r4xz2z03e&uid=1224543248&iszlist=1",
            "m.kugou.com/songlist",
        ),
        (
            KugouMusicParser,
            "酷狗单曲 https://m.kugou.com/share/?album_id=1012787&hash=3F66F1BA3ADA9E30DD5C597438942741&action=single",
            "m.kugou.com/share",
        ),
        (
            KugouMusicParser,
            "酷狗单曲 https://h5.kugou.com/v2/v-5a15aeb1/index.html?hash=dec2bffc693efc0895809f0952244048&album_id=631501",
            "h5.kugou.com/v2/v-",
        ),
        (
            QishuiMusicParser,
            "汽水歌单：https://qishui.douyin.com/s/iXqUS9uU/",
            "qishui.douyin.com/s",
        ),
        (
            QishuiMusicParser,
            "汽水歌单 https://music.douyin.com/qishui/share/playlist?playlist_id=123456",
            "music.douyin.com/qishui/share/playlist",
        ),
        (
            KuwoMusicParser,
            "酷我单曲 https://m.kuwo.cn/yinyue/72057414?f=ip&t=qqfriend",
            "m.kuwo.cn/yinyue",
        ),
        (
            QQMusicParser,
            "QQ单曲 https://i.y.qq.com/v8/playsong.html?media_mid=002OvI0G0XlMaO&songid=453455745",
            "i.y.qq.com/v8/playsong",
        ),
        (
            NetEaseMusicParser,
            "网易云单曲 https://y.music.163.com/m/song?fx=x&id=3429744904&userid=1",
            "y.music.163.com/m/song",
        ),
    ],
)
def test_music_routes_match_links_embedded_in_text(
    parser_cls, text: str, expected_keyword: str
):
    keyword, searched = parser_cls.search_url(text)

    assert keyword == expected_keyword
    assert searched.group(0) in text


@pytest.mark.parametrize(
    ("parser_cls", "url", "expected_keyword", "expected_id"),
    [
        (
            KuwoMusicParser,
            "https://m.kuwo.cn/newh5app/playlist_detail/3567046051?from=ip&t=qqfriend",
            "m.kuwo.cn/newh5app/playlist_detail",
            "3567046051",
        ),
        (
            AppleMusicParser,
            "https://music.apple.com/cn/playlist/eng/pl.u-leyl0YAsMJgb1ro?l=en",
            "music.apple.com",
            "pl.u-leyl0YAsMJgb1ro",
        ),
    ],
)
def test_new_music_routes_match_direct_links(
    parser_cls, url: str, expected_keyword: str, expected_id: str
):
    keyword, searched = parser_cls.search_url(url)

    assert keyword == expected_keyword
    assert searched.group("playlist_id") == expected_id


def test_netease_playlist_route_extracts_playlist_id_not_user_ids():
    url = (
        "https://music.163.com/m/playlist?app_version=9.5.15&"
        "id=9605284231&userid=1312543631&dlt=0846&creatorId=1312543631"
    )

    keyword, searched = NetEaseMusicParser.search_url(url)

    assert keyword == "music.163.com/m/playlist"
    assert searched.group("playlist_id") == "9605284231"


def test_netease_track_route_extracts_song_id_not_user_id():
    url = (
        "https://y.music.163.com/m/song?fx-wechatnew=t1&fx-wxqd=&"
        "fx-wordtest=&id=3429744904&PlayerStyles_SynchronousSharing=t3&"
        "userid=1312543631&app_version=9.5.15"
    )

    keyword, searched = NetEaseMusicParser.search_url(url)

    assert keyword == "y.music.163.com/m/song"
    assert searched.group("song_id") == "3429744904"


@pytest.mark.parametrize(
    ("mp3_url", "resolved_url", "expected_url"),
    [
        (
            "",
            None,
            "https://music.163.com/song/media/outer/url?id=3395393731",
        ),
        (
            "https://example.com/song.mp3",
            None,
            "https://example.com/song.mp3",
        ),
        (
            "",
            "https://media.example/meting-song.mp3",
            "https://media.example/meting-song.mp3",
        ),
    ],
)
def test_netease_track_resolves_audio_url(
    mp3_url: str,
    resolved_url: str | None,
    expected_url: str,
):
    async def build():
        parser = NetEaseMusicParser.__new__(NetEaseMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(netease=SimpleNamespace(use_proxy=False)),
        )
        parser.downloader = _Downloader()
        parser.headers = {}

        async def request(_url, **_kwargs):
            return {
                "songs": [
                    {
                        "id": 3395393731,
                        "name": "Brand New Sky",
                        "duration": 238000,
                        "mp3Url": mp3_url,
                        "artists": [{"name": "测试歌手"}],
                        "album": {"name": "测试专辑"},
                    }
                ]
            }

        async def resolve_audio_url(_song_id):
            return resolved_url

        parser._json_request = request
        parser._resolve_netease_audio_url = resolve_audio_url
        _, searched = NetEaseMusicParser.search_url(
            "https://y.music.163.com/m/song?id=3395393731&userid=123"
        )
        result = await parser._handle_track(searched)
        audio = next(
            content
            for content in result.contents
            if isinstance(content, AudioContent)
        )
        await audio.get_path()
        return parser, result

    parser, result = asyncio.run(build())

    assert parser.downloader.audio_urls == [expected_url]
    assert result.title == "Brand New Sky"
    assert result.extra["card_preview_only"] is False
    assert result.extra["audio_as_voice"] is True
    assert result.extra["audio_send_url"] == expected_url


def test_netease_single_track_resolves_first_artist_avatar_from_artist_endpoint():
    async def build():
        parser = NetEaseMusicParser.__new__(NetEaseMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(
                netease=SimpleNamespace(use_proxy=False, return_audio=False)
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: ""
        )
        requested = []

        async def request(url, **_kwargs):
            requested.append(url)
            if "/api/artist/6452" in url:
                return {"artist": {"img1v1Url": "https://cdn.example/first.jpg"}}
            if "/api/artist/11127" in url:
                return {"artist": {"img1v1Url": "https://cdn.example/second.jpg"}}
            return {
                "songs": [
                    {
                        "id": 186016,
                        "name": "Song",
                        "artists": [
                            {
                                "id": 6452,
                                "name": "First",
                                "picId": 0,
                                "img1v1Id": 0,
                                "picUrl": "https://cdn.example/default.jpg",
                            },
                            {"id": 11127, "name": "Second"},
                        ],
                        "album": {"name": "Album"},
                        "duration": 180000,
                    }
                ]
            }

        parser._json_request = request
        _, searched = NetEaseMusicParser.search_url(
            "https://music.163.com/song?id=186016"
        )
        result = await parser._handle_track(searched)
        avatar_path = await result.author.get_avatar_path()
        return parser, result, avatar_path, requested

    parser, result, avatar_path, requested = asyncio.run(build())

    assert result.author.name == "First / Second"
    assert avatar_path == Path("first.jpg")
    assert parser.downloader.image_urls == ["https://cdn.example/first.jpg"]
    assert requested[-1] == "https://music.163.com/api/artist/6452"
    assert all("/api/artist/11127" not in url for url in requested)


@pytest.mark.parametrize(
    ("api_base", "body", "expected_url", "expected_params"),
    [
        (
            "https://api.qijieya.cn/meting/",
            "https://media.example/meting.mp3",
            "https://media.example/meting.mp3",
            {
                "server": "netease",
                "type": "url",
                "id": "3395393731",
                "br": "320",
            },
        ),
        (
            "https://api.ames.cc.cd",
            '{"data":[{"code":200,"url":"https://media.example/vip.mp3"}]}',
            "https://media.example/vip.mp3",
            {
                "id": "3395393731",
                "level": "exhigh",
                "cookie": "MUSIC_U=vip-session",
            },
        ),
    ],
)
def test_netease_audio_api_supports_meting_and_cookie_compatible_sources(
    api_base: str,
    body: str,
    expected_url: str,
    expected_params: dict[str, str],
):
    async def resolve():
        class _Response:
            def __init__(self):
                self.status = 200
                self.headers = {}
                self.url = "https://api.example/"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return body

        class _Session:
            closed = False

            def get(self, url, **kwargs):
                requests.append((url, kwargs))
                return _Response()

        requests = []
        parser = NetEaseMusicParser.__new__(NetEaseMusicParser)
        parser.cfg = SimpleNamespace(
            parser=SimpleNamespace(
                netease=SimpleNamespace(audio_api_base=api_base),
            ),
        )
        parser.headers = {}
        parser._session = _Session()
        parser.cookiejar = SimpleNamespace(cookies_str="MUSIC_U=vip-session")

        result = await parser._resolve_netease_audio_url("3395393731")
        return result, requests

    result, requests = asyncio.run(resolve())

    assert result == expected_url
    assert len(requests) == 1
    request_url, kwargs = requests[0]
    expected_request_url = (
        api_base.rstrip("/")
        if "meting" in api_base.lower()
        else f"{api_base}/song/url/v1"
    )
    assert request_url == expected_request_url
    assert kwargs["params"] == expected_params


def test_netease_cookie_is_applied_to_official_detail_and_media_requests():
    async def build():
        parser = NetEaseMusicParser.__new__(NetEaseMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(netease=SimpleNamespace(use_proxy=False)),
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda url: (
                "MUSIC_U=vip-session" if "music.163.com" in url else ""
            )
        )
        requested_headers = []

        async def request(_url, **kwargs):
            requested_headers.append(kwargs["headers"])
            return {
                "songs": [
                    {
                        "id": 3395393731,
                        "name": "VIP track",
                        "duration": 180000,
                        "artists": [],
                    }
                ]
            }

        async def no_api_url(_song_id):
            return None

        parser._json_request = request
        parser._resolve_netease_audio_url = no_api_url
        _, searched = NetEaseMusicParser.search_url(
            "https://music.163.com/song?id=3395393731"
        )
        result = await parser._handle_track(searched)
        await asyncio.gather(*(content.get_path() for content in result.contents))
        return parser, requested_headers

    parser, requested_headers = asyncio.run(build())

    assert requested_headers[0]["Cookie"] == "MUSIC_U=vip-session"
    assert parser.downloader.audio_requests[0]["headers"]["Cookie"] == (
        "MUSIC_U=vip-session"
    )


def test_qq_track_route_extracts_media_mid_from_share_url():
    url = (
        "https://i.y.qq.com/v8/playsong.html?media_mid=002OvI0G0XlMaO&"
        "songid=453455745&source=qq"
    )

    keyword, searched = QQMusicParser.search_url(url)

    assert keyword == "i.y.qq.com/v8/playsong"
    assert QQMusicParser._track_mid_from_match(searched) == "002OvI0G0XlMaO"


def test_qq_track_route_strips_trailing_punctuation_from_song_id():
    _, searched = QQMusicParser.search_url(
        "https://i.y.qq.com/v8/playsong.html?songid=453455745。"
    )

    assert QQMusicParser._track_mid_from_match(searched) == "453455745"


def test_qq_single_track_uses_first_singer_avatar():
    async def build():
        parser = QQMusicParser.__new__(QQMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(
                qqmusic=SimpleNamespace(use_proxy=False, return_audio=False)
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {}

        async def fetch_song_details(_song_ids):
            return [
                {
                    "mid": "song-mid",
                    "name": "Song",
                    "singer": [
                        {"mid": "first-mid", "name": "First"},
                        {"mid": "second-mid", "name": "Second"},
                    ],
                    "interval": 180,
                }
            ]

        parser._fetch_song_details = fetch_song_details
        _, searched = QQMusicParser.search_url(
            "https://y.qq.com/n/ryqq/songDetail/song-mid"
        )
        result = await parser._handle_track(searched)
        avatar_path = await result.author.get_avatar_path()
        return parser, result, avatar_path

    parser, result, avatar_path = asyncio.run(build())

    assert result.author.name == "First / Second"
    assert avatar_path == Path("T001R300x300M000first-mid.jpg")
    assert parser.downloader.image_urls == [
        "https://y.gtimg.cn/music/photo_new/T001R300x300M000first-mid.jpg"
    ]


def test_qq_track_uses_numeric_songid_when_media_mid_has_no_metadata():
    async def build():
        parser = QQMusicParser.__new__(QQMusicParser)
        parser.cfg = SimpleNamespace(
            parser=SimpleNamespace(
                qqmusic=SimpleNamespace(use_proxy=False, return_audio=False)
            )
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        requested = []

        async def fetch_song_details(song_ids):
            requested.extend(song_ids)
            if song_ids == ["002OvI0G0XlMaO"]:
                return [{"mid": "", "name": "", "singer": []}]
            return [
                {
                    "mid": "002ymWx23tnFk",
                    "name": "Beanie",
                    "singer": [{"name": "Test artist"}],
                    "interval": 180,
                }
            ]

        parser._fetch_song_details = fetch_song_details
        _, searched = QQMusicParser.search_url(
            "https://i.y.qq.com/v8/playsong.html?media_mid=002OvI0G0XlMaO&"
            "songid=453455745"
        )
        result = await parser._handle_track(searched)
        return requested, result

    requested, result = asyncio.run(build())

    assert requested == ["002OvI0G0XlMaO", "453455745"]
    assert result.title == "Beanie"
    assert result.extra["track_id"] == "002ymWx23tnFk"


def test_qq_vkey_audio_url_uses_cookie_and_expands_sip_prefix():
    async def resolve():
        requests = []

        class _Response:
            status = 200
            url = "https://u.y.qq.com/cgi-bin/musicu.fcg"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return json.dumps(
                    {
                        "req_1": {
                            "code": 0,
                            "data": {
                                "sip": ["https://dl.stream.qqmusic.qq.com/"],
                                "midurlinfo": [
                                    {
                                        "songmid": "002OvI0G0XlMaO",
                                        "filename": "M800002OvI0G0XlMaO.mp3",
                                        "purl": "M800002OvI0G0XlMaO.mp3?vkey=test",
                                    }
                                ],
                            },
                        }
                    }
                )

        class _Session:
            closed = False

            def request(self, method, url, **kwargs):
                requests.append((method, url, kwargs))
                return _Response()

        parser = QQMusicParser.__new__(QQMusicParser)
        parser.headers = {"User-Agent": "test"}
        parser._session = _Session()
        parser.cookiejar = SimpleNamespace(
            cookies_str="uin=12345; qqmusic_key=key; fqm_pvqid=pvid"
        )

        result = await parser._resolve_qq_audio_url("002OvI0G0XlMaO")
        return result, requests

    result, requests = asyncio.run(resolve())

    assert result == (
        "https://dl.stream.qqmusic.qq.com/"
        "M800002OvI0G0XlMaO.mp3?vkey=test"
    )
    assert len(requests) == 1
    method, url, kwargs = requests[0]
    assert method == "POST"
    assert url == "https://u.y.qq.com/cgi-bin/musicu.fcg"
    assert kwargs["headers"]["Cookie"] == (
        "uin=12345; qqmusic_key=key; fqm_pvqid=pvid"
    )
    payload = json.loads(kwargs["data"])
    assert payload["req_1"]["param"]["songmid"] == [
        "002OvI0G0XlMaO",
        "002OvI0G0XlMaO",
    ]
    assert payload["req_1"]["param"]["filename"] == [
        "M800002OvI0G0XlMaO.mp3",
        "M500002OvI0G0XlMaO.mp3",
    ]
    assert payload["comm"]["g_tk"] == QQMusicParser._qq_g_tk("key")


def test_qq_vkey_resolves_requested_quality_order_and_reports_actual_tier():
    async def resolve():
        requests = []

        class _Response:
            status = 200
            url = "https://u.y.qq.com/cgi-bin/musicu.fcg"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return json.dumps({
                    "req_1": {
                        "code": 0,
                        "data": {
                            "sip": ["https://dl.stream.qqmusic.qq.com/"],
                            # The response order differs from the preference order.
                            "midurlinfo": [
                                {"filename": "M500media.mp3", "purl": "M500media.mp3?vkey=std"},
                                {"filename": "F000media.flac", "purl": "F000media.flac?vkey=lossless"},
                                {"filename": "M800media.mp3", "purl": "M800media.mp3?vkey=high"},
                            ],
                        },
                    },
                })

        class _Session:
            closed = False

            def request(self, method, url, **kwargs):
                requests.append((method, url, kwargs))
                return _Response()

        parser = QQMusicParser.__new__(QQMusicParser)
        parser.headers = {"User-Agent": "test"}
        parser._session = _Session()
        parser.cookiejar = SimpleNamespace(cookies_str="uin=12345; qqmusic_key=key")
        source = await parser._resolve_qq_audio_source(
            "song", media_mid="media",
            quality_priority=("lossless", "high", "standard"),
        )
        return source, requests

    source, requests = asyncio.run(resolve())

    assert source == (
        "https://dl.stream.qqmusic.qq.com/F000media.flac?vkey=lossless",
        "lossless",
    )
    payload = json.loads(requests[0][2]["data"])
    assert payload["req_1"]["param"]["filename"] == [
        "F000media.flac", "M800media.mp3", "M500media.mp3",
    ]


def test_qq_track_uses_configured_file_quality_order_but_fixed_voice_order():
    async def build(send_format):
        parser = QQMusicParser.__new__(QQMusicParser)
        parser.cfg = SimpleNamespace(parser=SimpleNamespace(qqmusic=SimpleNamespace(
            use_proxy=False,
            return_audio=True,
            audio_send_format=send_format,
            file_quality_priority=["standard", "lossless"],
        )))
        parser.downloader = _Downloader()
        parser.headers = {}
        parser._fetch_song_details = lambda _song_ids: None
        requests = []

        async def fetch_song_details(_song_ids):
            return [{
                "mid": "song-mid",
                "name": "Test song",
                "singer": [{"name": "Test artist"}],
                "interval": 180,
                "file": {"media_mid": "media-mid"},
            }]

        async def resolve(song_mid, *, media_mid=None, quality_priority):
            requests.append((song_mid, media_mid, quality_priority))
            return (
                "https://dl.stream.qqmusic.qq.com/M500media-mid.mp3?vkey=test",
                "standard",
            )

        parser._fetch_song_details = fetch_song_details
        parser._resolve_qq_audio_source = resolve
        _, searched = QQMusicParser.search_url(
            "https://i.y.qq.com/v8/playsong.html?songmid=song-mid"
        )
        result = await parser._handle_track(searched)
        return requests, result

    file_requests, file_result = asyncio.run(build("file"))
    voice_requests, voice_result = asyncio.run(build("voice"))

    assert file_requests[0][2] == ("standard", "lossless")
    assert file_result.extra["audio_quality"] == "standard"
    assert file_result.extra["audio_as_file"] is True
    assert voice_requests[0][2] == ("high", "standard")
    assert voice_result.extra["audio_quality"] == "standard"
    assert voice_result.extra["audio_as_voice"] is True


def test_qq_single_track_audio_switch_can_disable_audio_resolution():
    async def build():
        parser = QQMusicParser.__new__(QQMusicParser)
        parser.cfg = SimpleNamespace(
            parser=SimpleNamespace(
                qqmusic=SimpleNamespace(use_proxy=False, return_audio=False)
            )
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        parser._fetch_song_details = lambda _song_ids: None

        async def fail_if_called(_song_mid):
            raise AssertionError("vkey resolution should be disabled")

        parser._resolve_qq_audio_url = fail_if_called
        async def fetch_song_details(_song_ids):
            return [
                {
                    "mid": "002OvI0G0XlMaO",
                    "name": "Test song",
                    "singer": [{"name": "Test artist"}],
                    "interval": 180,
                }
            ]

        parser._fetch_song_details = fetch_song_details
        _, searched = QQMusicParser.search_url(
            "https://i.y.qq.com/v8/playsong.html?media_mid=002OvI0G0XlMaO"
        )
        return await parser._handle_track(searched)

    result = asyncio.run(build())

    assert result.audio_contents == []
    assert result.extra.get("audio_as_voice") is not True


def test_qq_cookie_check_uses_profile_api_and_reports_authentication():
    async def check(status=200, payload=None):
        class _Response:
            def __init__(self):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return json.dumps(payload if payload is not None else {"code": 0})

        class _Session:
            closed = False

            def get(self, url, **kwargs):
                requests.append((url, kwargs))
                return _Response()

        requests = []
        parser = QQMusicParser.__new__(QQMusicParser)
        parser.headers = {"User-Agent": "test"}
        parser._session = _Session()
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: "uin=12345; qqmusic_key=key"
        )
        return await parser.check_cookie(), requests

    valid, valid_requests = asyncio.run(check(payload={"code": 0}))
    invalid, invalid_requests = asyncio.run(check(payload={"code": 1000}))

    assert valid is True
    assert invalid is False
    assert valid_requests[0][0] == QQMusicParser._QQ_COOKIE_CHECK_URL
    assert valid_requests[0][1]["params"]["loginUin"] == "12345"
    assert valid_requests[0][1]["params"]["g_tk"] == str(
        QQMusicParser._qq_g_tk("key")
    )
    assert valid_requests[0][1]["headers"]["Cookie"] == (
        "uin=12345; qqmusic_key=key"
    )
    assert invalid_requests[0][1]["allow_redirects"] is False


def test_qq_cookie_check_does_not_treat_transport_or_unknown_errors_as_invalid():
    async def check(*, error=None, payload=None, status=200):
        class _Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return json.dumps(payload)

        class _Session:
            closed = False

            def get(self, *_args, **_kwargs):
                if error:
                    raise error
                response = _Response()
                response.status = status
                return response

        parser = QQMusicParser.__new__(QQMusicParser)
        parser.headers = {"User-Agent": "test"}
        parser._session = _Session()
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: "uin=12345; qqmusic_key=key"
        )
        return await parser.check_cookie()

    assert asyncio.run(check(error=TimeoutError())) is None
    assert asyncio.run(check(payload={"code": 500001})) is None
    assert asyncio.run(check(payload={"unexpected": "shape"})) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (132, 132.0), (146000, 146.0), ("02:46", 166.0), ("3:58", 238.0),
        ("PT5M6S", 306.0), ("PT1H2M3.5S", 3723.5), ("PT12S", 12.0),
        ("PT3H", 10800.0), ("PTbad", 0.0),
    ],
)
def test_single_track_duration_is_normalized(value, expected):
    assert _duration_seconds(value) == expected


def test_qishui_track_route_extracts_track_id():
    url = (
        "https://music.douyin.com/qishui/share/track?"
        "track_id=7693571928527079458&sec_sharer_id=demo"
    )

    keyword, searched = QishuiMusicParser.search_url(url)

    assert keyword == "music.douyin.com/qishui/share/track"
    assert searched.group("track_id") == "7693571928527079458"


def test_qishui_track_payload_builds_card_and_audio():
    async def build():
        parser = QishuiMusicParser.__new__(QishuiMusicParser)
        parser.cfg = SimpleNamespace(
            card_enabled=True,
            proxy=None,
            parser=SimpleNamespace(
                qishui=SimpleNamespace(use_proxy=False),
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        payload = {
            "loaderData": {
                "track_page": {
                    "audioWithLyricsOption": {
                        "track_id": "7693571928527079458",
                        "trackName": "泥",
                        "artistName": "歌手",
                        "duration": 238.848,
                        "url": "https://example.com/audio.mp4",
                        "coverURL": "https://example.com/cover.jpg",
                        "trackInfo": {
                            "name": "泥",
                            "album": {"name": "专辑"},
                            "artists": [{"name": "歌手"}],
                            "stats": {
                                "count_collected": 4,
                                "count_comment": 2,
                                "count_shared": 1,
                            },
                        },
                    }
                }
            }
        }
        html = (
            "<script>_ROUTER_DATA = "
            + json.dumps(payload, ensure_ascii=False)
            + "\nfunction runWindowFn"
        )
        router_data = parser._decode_router_data(html)
        track = router_data["loaderData"]["track_page"]["audioWithLyricsOption"]
        result = parser._track_result(
            track,
            url="https://music.douyin.com/qishui/share/track?track_id=1",
            track_id="1",
        )
        await asyncio.gather(*(content.get_path() for content in result.contents))
        return result

    result = asyncio.run(build())

    assert result.title == "泥"
    assert result.author is not None and result.author.name == "歌手"
    assert result.text is None
    assert result.comment_count == 2
    assert result.favorite_count == 4
    assert result.share_count == 1
    assert any(isinstance(content, AudioContent) for content in result.contents)


def test_qishui_ugc_video_always_returns_complete_video():
    async def build():
        parser = QishuiMusicParser.__new__(QishuiMusicParser)
        parser.cfg = SimpleNamespace(
            card_enabled=True,
            proxy=None,
            parser=SimpleNamespace(
                qishui=SimpleNamespace(
                    use_proxy=False,
                    audio_send_format="file",
                    video_send_mode="audio",  # retired values are ignored
                ),
            ),
        )

        class VideoDownloader(_Downloader):
            def download_video(self, url, **_kwargs):
                async def complete() -> Path:
                    return Path(url.rsplit("/", 1)[-1] or "video.mp4")

                return asyncio.create_task(complete())

        downloader = VideoDownloader()
        parser.downloader = downloader
        parser.headers = {}
        result = parser._ugc_video_result(
            {
                "video_id": "ugc-1",
                "videoName": "用户视频",
                "artistName": "作者",
                "duration": 151.4,
                "url": "https://example.com/ugc.mp4",
                "coverURL": "https://example.com/cover.jpg",
            },
            url="https://music.douyin.com/qishui/share/ugc_video?ugc_video_id=1",
            video_id="1",
        )
        await asyncio.gather(*(content.get_path() for content in result.contents))
        return result, downloader

    result, downloader = asyncio.run(build())

    assert len(result.video_contents) == 1
    assert result.audio_contents == []
    assert result.extra["music_type"] == "ugc_video"
    assert "video_send_mode" not in result.extra
    assert downloader.image_urls == ["https://example.com/cover.jpg"]


@pytest.mark.parametrize(
    ("show_playlist_cover", "expected_contents"),
    [(True, 1), (False, 0), (None, 1)],
)
def test_playlist_cover_setting_controls_only_main_cover(
    show_playlist_cover: bool | None, expected_contents: int
):
    result = _build_result(show_playlist_cover)

    assert len(result.contents) == expected_contents
    assert len(result.extra["playlist_tracks"]) == 1
    assert "歌曲数: 1" in result.extra["info"]
    assert "歌曲数" not in result.extra["card_info"]
    assert result.extra["playlist_cover_only"] is (expected_contents == 1)


def test_kugou_mobile_songlist_payload_is_decoded():
    html = (
        '<script>window.$output = '
        '{"encode_src_gid":"gcid_demo","info":{"listinfo":'
        '{"name":"测试歌单","list_create_username":"测试作者","count":2},'
        '"songs":[{"name":"歌曲 - 歌手","singerinfo":[],"albuminfo":{"name":"专辑"}}]}};'
    )

    payload = KugouMusicParser._decode_mobile_songlist(html)

    assert payload["info"]["listinfo"]["name"] == "测试歌单"
    assert payload["encode_src_gid"] == "gcid_demo"


def test_kugou_track_uses_cookie_and_marks_audio_as_onebot_voice():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(kugou=SimpleNamespace(use_proxy=False)),
        )
        parser.downloader = _Downloader()
        parser.headers = {"User-Agent": "test"}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda url: (
                "KUGOUID=member-session" if "kugou.com" in url else ""
            )
        )
        requests = []

        async def get_song_info(url, **kwargs):
            requests.append((url, kwargs))
            return {
                "songName": "VIP track",
                "author_name": "Test artist",
                "url": "https://cdn.kugou.com/song.mp3",
                "timeLength": 180,
            }

        parser._json_request = get_song_info
        result = await parser._kugou_single_result(
            source_url="https://www.kugou.com/mixsong/test.html",
            song_hash="test-hash",
        )
        audio = next(
            content for content in result.contents if isinstance(content, AudioContent)
        )
        await audio.get_path()
        return parser, result, requests

    parser, result, requests = asyncio.run(build())

    assert result.title == "VIP track"
    assert result.extra["audio_as_voice"] is True
    assert result.extra["audio_send_url"] == "https://cdn.kugou.com/song.mp3"
    assert requests[0][1]["headers"]["Cookie"] == "KUGOUID=member-session"
    assert parser.downloader.audio_requests[0]["headers"]["Cookie"] == (
        "KUGOUID=member-session"
    )


def test_kugou_lyrics_params_use_h5_client_signature_and_duration():
    params = KugouMusicParser._kugou_lyrics_params(
        song_hash="abc123",
        keyword="Artist - Song",
        duration=255582,
        cookie="kg_mid=test-mid; kg_dfid=test-dfid",
    )

    assert params["clientver"] == "1000"
    assert params["appid"] == "1058"
    assert params["mid"] == "test-mid"
    assert params["uuid"] == "test-mid"
    assert params["dfid"] == "test-dfid"
    assert params["hash"] == "ABC123"
    assert params["timelength"] == "255000"
    unsigned = {key: value for key, value in params.items() if key != "signature"}
    assert params["signature"] == KugouMusicParser._signature(unsigned)


def test_kugou_fetches_lrc_from_public_h5_endpoint():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(kugou=SimpleNamespace(use_proxy=False)),
        )
        parser.headers = {"User-Agent": "test"}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: "kg_mid=test-mid"
        )
        requested = []

        async def request(url, **kwargs):
            requested.append((url, kwargs))
            return {"status": 1, "data": {"lrc": "[00:01.00]Hello"}}

        parser._json_request = request
        lyrics = await parser._fetch_kugou_lyrics(
            "song-hash",
            keyword="Artist - Song",
            duration=61,
        )
        return lyrics, requested

    lyrics, requested = asyncio.run(build())

    assert lyrics == "[00:01.00]Hello"
    assert len(requested) == 1
    request_url, request_kwargs = requested[0]
    assert request_url.startswith(
        "https://m3ws.kugou.com/api/v1/krc/get_lyrics?"
    )
    params = parse_qs(urlparse(request_url).query)
    assert params["hash"] == ["SONG-HASH"]
    assert params["keyword"] == ["Artist - Song"]
    assert params["timelength"] == ["61000"]
    assert request_kwargs["headers"]["Cookie"] == "kg_mid=test-mid"


def test_kugou_lyrics_fetch_logs_empty_response_without_lyric_text(
    monkeypatch: pytest.MonkeyPatch,
):
    infos = []
    monkeypatch.setattr(
        music_parser_module,
        "logger",
        SimpleNamespace(
            info=lambda message, *args: infos.append(
                message % args if args else message
            ),
            warning=lambda message, *args: infos.append(
                message % args if args else message
            ),
        ),
    )

    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: ""
        )

        async def request(_url, **_kwargs):
            return {"status": 1, "data": {}}

        parser._json_request = request
        return await parser._fetch_kugou_lyrics(
            "song-hash", keyword="Artist - Song", duration=61
        )

    assert asyncio.run(build()) is None
    assert any("开始请求单曲歌词接口" in message for message in infos)
    assert any("没有返回可用 LRC" in message for message in infos)
    assert all("Artist - Song" not in message for message in infos)


def test_kugou_single_track_attaches_lyrics_and_logs_progress(
    monkeypatch: pytest.MonkeyPatch,
):
    infos = []
    monkeypatch.setattr(
        music_parser_module,
        "logger",
        SimpleNamespace(
            info=lambda message, *args: infos.append(
                message % args if args else message
            ),
            debug=lambda message, *args: infos.append(
                message % args if args else message
            ),
            warning=lambda message, *args: infos.append(
                message % args if args else message
            ),
        ),
    )

    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(
                kugou=SimpleNamespace(
                    use_proxy=False, return_audio=False, parse_lyrics=True
                )
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: ""
        )

        async def fetch_song_info(*_args, **_kwargs):
            return {}

        async def fetch_lyrics(*_args, **_kwargs):
            return "[00:01.00]Secret lyric text"

        parser._fetch_kugou_song_info = fetch_song_info
        parser._fetch_kugou_lyrics = fetch_lyrics
        return await parser._kugou_single_result(
            source_url="https://www.kugou.com/mixsong/demo.html",
            song_hash="song-hash",
            fallback={
                "song_name": "Song",
                "author_name": "Artist",
                "timelength": 180000,
            },
        )

    result = asyncio.run(build())

    assert result.extra["lyrics"]["text"] == "[00:01.00]Secret lyric text"
    assert any("歌词已附加到解析结果" in message for message in infos)
    assert all("Secret lyric text" not in message for message in infos)


def test_kugou_return_audio_disabled_skips_playback_request_and_keeps_card():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(
                kugou=SimpleNamespace(
                    use_proxy=False, return_audio=False, audio_send_format="voice"
                )
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {"User-Agent": "test"}
        parser.cookiejar = SimpleNamespace(get_cookie_header_for_url=lambda _url: "")
        requests = []

        async def get_song_info(url, **kwargs):
            requests.append((url, kwargs))
            raise AssertionError("playback endpoint must not be requested")

        parser._json_request = get_song_info
        fallback = {
            "hash": "song-hash",
            "song_name": "卡片歌曲",
            "author_name": "卡片歌手",
            "album_img": "https://example.com/cover.jpg",
            "timelength": 185000,
        }
        result = await parser._kugou_single_result(
            source_url="https://www.kugou.com/mixsong/demo.html",
            song_hash="song-hash",
            fallback=fallback,
            album_name="卡片专辑",
        )
        return result, requests, parser.downloader

    result, requests, downloader = asyncio.run(build())

    assert result.title == "卡片歌曲"
    assert result.author.name == "卡片歌手"
    assert result.extra["duration"] == 185
    assert not any(isinstance(content, AudioContent) for content in result.contents)
    assert requests == []
    assert downloader.audio_urls == []


def test_kugou_single_track_uses_first_artist_avatar_lazily():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(
                kugou=SimpleNamespace(use_proxy=False, return_audio=False)
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: ""
        )
        requested = []

        async def request(url, **_kwargs):
            requested.append(url)
            return {
                "authors": [
                    {
                        "author_name": "First",
                        "avatar": (
                            "http://singerimg.kugou.com/uploadpic/softhead/"
                            "{size}/20260101/first.jpg"
                        ),
                    },
                    {
                        "author_name": "Second",
                        "avatar": (
                            "http://singerimg.kugou.com/uploadpic/softhead/"
                            "{size}/20260101/second.jpg"
                        ),
                    },
                ],
                "author_name": "First、Second",
            }

        parser._json_request = request
        result = await parser._kugou_single_result(
            source_url="https://www.kugou.com/mixsong/demo.html",
            song_hash="song-hash",
            fallback={
                "song_name": "Song",
                "author_name": "First、Second",
            },
        )
        avatar_path = await result.author.get_avatar_path()
        return parser, result, avatar_path, requested

    parser, result, avatar_path, requested = asyncio.run(build())

    assert result.author.name == "First、Second"
    assert avatar_path == Path("first.jpg")
    assert parser.downloader.image_urls == [
        "https://singerimg.kugou.com/uploadpic/softhead/165/20260101/first.jpg"
    ]
    assert requested == [
        "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=song-hash"
    ]


def test_kugou_activity_detail_route_resolves_song_and_audio():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(
                kugou=SimpleNamespace(
                    use_proxy=False, return_audio=True, audio_send_format="voice"
                )
            ),
        )
        parser.downloader = _Downloader()
        parser.headers = {"User-Agent": "test"}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: ""
        )
        requests = []

        async def json_request(url, **kwargs):
            requests.append((url, kwargs))
            return {
                "status": 1,
                "data": {
                    "hash": "activity-hash",
                    "ori_audio_name": "Activity song",
                    "author_name": "First、Second",
                    "authors": [
                        {
                            "author_name": "First",
                            "sizable_avatar": (
                                "http://singerimg.kugou.com/uploadpic/softhead/"
                                "{size}/20260101/first.jpg"
                            ),
                        },
                        {"author_name": "Second"},
                    ],
                    "album_info": {
                        "album_name": "Activity album",
                        "sizable_cover": "http://imge.kugou.com/stdmusic/{size}/cover.jpg",
                    },
                    "audio_info": {"timelength": "157000"},
                },
            }

        async def fetch_song_info(song_hash, **_kwargs):
            assert song_hash == "activity-hash"
            return {
                "songName": "Activity song",
                "author_name": "First、Second",
                "authors": [
                    {
                        "avatar": (
                            "http://singerimg.kugou.com/uploadpic/softhead/"
                            "{size}/20260101/first.jpg"
                        )
                    },
                    {"avatar": "http://example.com/second.jpg"},
                ],
                "album_name": "Activity album",
                "album_img": "http://imge.kugou.com/stdmusic/{size}/cover.jpg",
                "url": "https://audio.example/activity.mp3",
                "timeLength": 157,
            }

        parser._json_request = json_request
        parser._fetch_kugou_song_info = fetch_song_info
        url = (
            "https://activity.kugou.com/vo-activity/demo-activity/detail.html#/?"
            "channel_id=16752399542&file_id=1740901401759611&t_uid=2134613718"
        )
        _, searched = KugouMusicParser.search_url(url)
        result = await parser._handle_activity_track(searched)
        await asyncio.gather(*(content.get_path() for content in result.contents))
        return parser, result, requests

    parser, result, requests = asyncio.run(build())

    assert result.title == "Activity song"
    assert result.author.name == "First、Second"
    assert result.extra["audio_as_voice"] is True
    assert result.extra["audio_send_url"] == "https://audio.example/activity.mp3"
    assert parser.downloader.audio_urls == ["https://audio.example/activity.mp3"]
    assert parser.downloader.image_urls == [
        "https://imge.kugou.com/stdmusic/400/cover.jpg",
        "https://singerimg.kugou.com/uploadpic/softhead/165/20260101/first.jpg",
    ]
    assert len(requests) == 1
    request_url, request_kwargs = requests[0]
    assert request_url.startswith("https://gateway.kugou.com/v1/get_song_detail?")
    params = parse_qs(urlparse(request_url).query)
    assert params["fileid"] == ["1740901401759611"]
    assert params["global_collection_id"] == ["16752399542"]
    unsigned = {
        key: values[0] for key, values in params.items() if key != "signature"
    }
    assert params["signature"] == [KugouMusicParser._signature(unsigned)]
    assert request_kwargs["headers"]["x-router"] == "youth.kugou.com"


def test_kugou_member_cookie_uses_signed_web_player_endpoint():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(kugou=SimpleNamespace(use_proxy=False)),
        )
        parser.downloader = _Downloader()
        parser.headers = {"User-Agent": "test"}
        cookie = (
            "kg_mid=test-mid; kg_dfid=test-dfid; "
            "KuGoo=KugooID%3D12345%26t%3Dmember-token%26NickName%3D%ZZ"
        )
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: cookie
        )
        requests = []

        async def get_song_info(url, **kwargs):
            requests.append((url, kwargs))
            return {
                "status": 1,
                "err_code": 0,
                "data": {
                    "song_name": "Member track",
                    "author_name": "Member artist",
                    "play_url": "https://cdn.kugou.com/member-song.mp3",
                    "timelength": 180,
                },
            }

        parser._json_request = get_song_info
        source_url = "https://www.kugou.com/mixsong/test.html"
        result = await parser._kugou_single_result(
            source_url=source_url,
            song_hash="test-hash",
            fallback={
                "encode_album_audio_id": "encoded-member-id",
                "album_id": "test-album",
            },
        )
        return parser, cookie, source_url, result, requests

    parser, cookie, source_url, result, requests = asyncio.run(build())

    assert len(requests) == 1
    request_url, request_kwargs = requests[0]
    assert request_url.startswith("https://wwwapi.kugou.com/play/songinfo?")
    params = parse_qs(urlparse(request_url).query)
    assert params["appid"] == ["1014"]
    assert params["platid"] == ["4"]
    assert params["userid"] == ["12345"]
    assert params["token"] == ["member-token"]
    assert params["dfid"] == ["test-dfid"]
    assert params["mid"] == ["test-mid"]
    assert params["encode_album_audio_id"] == ["encoded-member-id"]
    assert "hash" not in params
    unsigned = {key: values[0] for key, values in params.items() if key != "signature"}
    assert params["signature"] == [KugouMusicParser._signature(unsigned)]
    assert request_kwargs["headers"]["Cookie"] == cookie
    assert request_kwargs["headers"]["Referer"] == source_url
    assert result.title == "Member track"
    assert result.extra["audio_as_voice"] is True
    assert result.extra["audio_send_url"] == "https://cdn.kugou.com/member-song.mp3"
    assert parser.downloader.audio_requests[0]["headers"]["Cookie"] == cookie


def test_kugou_cookie_check_uses_account_response_not_track_entitlement():
    async def check(payload):
        parser = KugouMusicParser.__new__(KugouMusicParser)
        cookie = (
            "kg_mid=test-mid; kg_dfid=test-dfid; "
            "KuGoo=KugooID%3D12345%26t%3Dmember-token"
        )
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: cookie
        )
        requests = []

        async def json_request(url, *, headers):
            requests.append((url, headers))
            return payload

        parser._json_request = json_request
        result = await parser.check_cookie()
        return parser, cookie, result, requests

    parser, cookie, accepted, requests = asyncio.run(
        check({"status": 1, "err_code": 0, "data": {"play_url": "ignored"}})
    )
    _, _, rejected, rejected_requests = asyncio.run(
        check({"status": 0, "err_code": 30020})
    )
    _, _, no_audio, _ = asyncio.run(
        check({"status": 1, "err_code": 0, "data": {"song_name": "Track"}})
    )

    assert parser.has_cookie() is True
    assert parser.has_auth_credentials() is True
    assert accepted is True
    assert rejected is False
    # The probe track is restricted, but the successful account response still
    # proves that the Cookie was accepted.  Playback entitlement is checked
    # separately when resolving an actual requested song.
    assert no_audio is True
    request_url, headers = requests[0]
    assert request_url.startswith("https://wwwapi.kugou.com/play/songinfo?")
    params = parse_qs(urlparse(request_url).query)
    assert params["userid"] == ["12345"]
    assert params["token"] == ["member-token"]
    assert params["hash"] == [KugouMusicParser._COOKIE_CHECK_TRACK["hash"]]
    unsigned = {key: values[0] for key, values in params.items() if key != "signature"}
    assert params["signature"] == [KugouMusicParser._signature(unsigned)]
    assert headers["Cookie"] == cookie
    assert rejected_requests[0][1]["Cookie"] == cookie


def test_kugou_cookie_check_does_not_treat_transport_failure_as_invalid_cookie():
    async def check():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: (
                "KuGoo=KugooID%3D12345%26t%3Dmember-token"
            )
        )

        async def fail_request(_url, *, headers):
            raise ParseException("offline")

        parser._json_request = fail_request
        return await parser.check_cookie()

    assert asyncio.run(check()) is None


def test_kugou_web_player_without_audio_falls_back_to_legacy_endpoint():
    async def build():
        parser = KugouMusicParser.__new__(KugouMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(kugou=SimpleNamespace(use_proxy=False)),
        )
        parser.downloader = _Downloader()
        parser.headers = {"User-Agent": "test"}
        parser.cookiejar = SimpleNamespace(
            get_cookie_header_for_url=lambda _url: (
                "KuGoo=KugooID%3D12345%26t%3Dmember-token"
            )
        )
        requests = []

        async def get_song_info(url, **kwargs):
            requests.append(url)
            if url.startswith("https://wwwapi.kugou.com/"):
                return {"status": 1, "err_code": 0, "data": {"song_name": "Track"}}
            return {
                "songName": "Track",
                "url": "https://cdn.kugou.com/fallback.mp3",
            }

        parser._json_request = get_song_info
        result = await parser._kugou_single_result(
            source_url="https://www.kugou.com/mixsong/test.html",
            song_hash="test-hash",
            fallback={"encode_album_audio_id": "encoded-id"},
        )
        return parser, result, requests

    parser, result, requests = asyncio.run(build())

    assert len(requests) == 2
    assert requests[0].startswith("https://wwwapi.kugou.com/play/songinfo?")
    assert requests[1].startswith("https://m.kugou.com/app/i/getSongInfo.php?")
    assert result.extra["audio_send_url"] == "https://cdn.kugou.com/fallback.mp3"
    assert parser.downloader.audio_requests[0]["url"] == (
        "https://cdn.kugou.com/fallback.mp3"
    )


def test_kuwo_nuxt_payload_is_decoded():
    html = (
        '<script>window.__NUXT__=(function(a,b){return '
        '{data:[{playlistId:"123",playListInfo:{name:"测试",total:2,'
        'musicList:[{name:"Song",artist:"Artist",album:"Album",'
        'albumpic:"https://example.com/a.jpg"}]}}]} '
        '}(null,"cover"));</script>'
    )

    payload = KuwoMusicParser._decode_nuxt_payload(html)

    info = payload["data"][0]["playListInfo"]
    assert info["name"] == "测试"
    assert info["total"] == 2
    assert info["musicList"][0]["name"] == "Song"


def test_apple_music_server_data_is_decoded():
    html = (
        '<script id="serialized-server-data" type="application/json">'
        '{"data":[{"data":{"sections":[{"id":"playlist-detail-header-section",'
        '"items":[{"title":"Mix","trackCount":1}]},{"id":"track-list",'
        '"items":[{"title":"Song","artistName":"Artist",'
        '"contentDescriptor":{"kind":"song"}}]}]}}]}'
        '</script>'
    )

    payload = AppleMusicParser._decode_server_data(html)

    assert payload["data"][0]["data"]["sections"][0]["items"][0]["title"] == "Mix"


_APPLE_TRACK_URL = (
    "https://music.apple.com/cn/album/"
    "%E8%90%BD%E6%97%A5/1514034441?i=1514034916"
)


def _apple_music_html(sections, structured_data=None):
    html = (
        '<script id="serialized-server-data" type="application/json">'
        + json.dumps({"data": [{"data": {"sections": sections}}]}, ensure_ascii=False)
        + "</script>"
    )
    if structured_data is not None:
        html += (
            '<script type="application/ld+json">'
            + json.dumps(structured_data, ensure_ascii=False)
            + "</script>"
        )
    return html


def _parse_apple_music_html(html, url=_APPLE_TRACK_URL, *, status=200, show_url=True,
                           backend=None, return_audio=False, audio_send_format="file",
                           metadata_only=False):
    async def build():
        requests = []

        class _Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return html

        class _Session:
            closed = False

            def get(self, url, **kwargs):
                requests.append((url, kwargs))
                response = _Response()
                response.status = status
                return response

        parser = AppleMusicParser.__new__(AppleMusicParser)
        parser.cfg = SimpleNamespace(
            card_enabled=True,
            parser=SimpleNamespace(
                applemusic=SimpleNamespace(use_proxy=False, show_playlist_url=show_url,
                                           return_audio=return_audio,
                                           audio_send_format=audio_send_format)
            ),
        )
        parser.headers = {"User-Agent": "test"}
        parser.downloader = _Downloader()
        parser._session = _Session()
        parser.apple_music_backend = backend
        keyword, searched = parser.search_url(url)
        result = (await parser._handle_track(searched, metadata_only=True)
                  if metadata_only else await parser.parse(keyword, searched))
        await asyncio.gather(*(content.get_path() for content in result.contents))
        return parser, result, requests

    return asyncio.run(build())


@pytest.mark.parametrize("enabled,return_audio,audio_send_format,metadata_only,expected_voice,expected_file", [
    (True, True, "voice", False, True, False),
    (True, True, "file", False, False, True),
    (False, True, "voice", False, False, False),
    (True, False, "voice", False, False, False),
    (True, True, "voice", True, False, False),
])
def test_apple_music_optional_backend_preserves_default_cards(
    enabled, return_audio, audio_send_format, metadata_only, expected_voice, expected_file
):
    calls = []
    def download_task(url, duration):
        calls.append((url, duration))
        async def complete():
            return Path("track.m4a")
        return asyncio.create_task(complete())
    backend = SimpleNamespace(settings=SimpleNamespace(enabled=enabled), download_task=download_task)
    _, result, _ = _parse_apple_music_html(
        _apple_music_html([], _apple_music_json_ld()),
        backend=backend, return_audio=return_audio,
        audio_send_format=audio_send_format, metadata_only=metadata_only,
    )
    expected_audio = expected_voice or expected_file
    assert bool(calls) is expected_audio
    assert any(isinstance(content, AudioContent) for content in result.contents) is expected_audio
    assert result.extra.get("audio_as_voice", False) is expected_voice
    assert result.extra.get("audio_as_file", False) is expected_file
    assert result.extra.get("audio_send_via_url", False) is expected_audio
    assert result.extra["duration"] == 306
    if metadata_only:
        assert result.contents == []


@pytest.mark.parametrize(
    "url",
    [
        _APPLE_TRACK_URL,
        "https://music.apple.com/cn/album/1514034441?i=1514034916",
        "https://music.apple.com/album/name/1514034441?l=en&i=1514034916&uo=4",
        "https://music.apple.com/us/album/name/1514034441/?i=1514034916#lyrics",
        "https://music.apple.com/cn/song/%E8%90%BD%E6%97%A5/1514034916",
        "https://music.apple.com/cn/song/1514034916?l=en",
        "https://music.apple.com/song/1514034916",
    ],
)
def test_apple_music_track_routes_match_links_embedded_in_text(url):
    keyword, searched = AppleMusicParser.search_url("歌曲：" + url + " 请解析")

    assert keyword == "music.apple.com"
    assert (searched.group("song_id") or searched.group("track_id")) == "1514034916"
    assert "https://" + searched.group(0) == url
    assert searched.group("playlist_id") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://music.apple.com/cn/album/name/1514034441",
        "https://music.apple.com/cn/album/1514034441?other_i=1514034916",
        "https://music.apple.com/cn/album/1514034441#?i=1514034916",
        "https://music.apple.com/cn/album/1514034441?i=1514034916bad",
        "https://music.apple.com/cn/album/1514034441?i=bad",
        "https://music.apple.com/cn/song/1514034916bad",
    ],
)
def test_apple_music_track_routes_do_not_parse_albums_or_invalid_ids(url):
    with pytest.raises(ParseException):
        AppleMusicParser.search_url(url)


@pytest.mark.parametrize("artist_name", ["大都会乐团", None])
@pytest.mark.parametrize("show_url", [True, False])
def test_apple_music_album_selects_requested_track_and_reuses_card(artist_name, show_url):
    sections = [
        {
            "id": "album-detail-header-section - 1514034441",
            "items": [{
                "title": "晴朗日 - Single",
                "subtitleLinks": [{"title": "大都会乐团"}],
                "artwork": {"dictionary": {"url": "https://example.com/{w}x{h}bb.{f}"}},
            }],
        },
        {
            "id": "track-list - 1514034441",
            "items": [
                {"title": "今日晴", "contentDescriptor": {
                    "kind": "song", "identifiers": {"storeAdamID": "1514034455"},
                }},
                {
                    "title": "落日",
                    "artistName": artist_name,
                    "duration": 306667,
                    "previewUrl": "https://example.com/preview.m4a",
                    "contentDescriptor": {
                        "kind": "song", "identifiers": {"storeAdamID": 1514034916},
                    },
                },
            ],
        },
        {"id": "you-might-also-like", "items": [{
            "title": "不能选中的推荐歌曲",
            "contentDescriptor": {"kind": "song", "identifiers": {"storeAdamID": "1514034916"}},
        }]},
    ]

    parser, result, requests = _parse_apple_music_html(_apple_music_html(sections), show_url=show_url)

    assert result.title == "落日"
    assert result.platform.name == "applemusic"
    assert result.author.name == "大都会乐团"
    assert result.extra["music_type"] == "track"
    assert result.extra["track_id"] == "1514034916"
    assert result.extra["card_info"] == "专辑: 晴朗日 - Single\n时长: 5:06"
    assert result.extra["card_preview_only"] is True
    assert result.extra["show_playlist_url"] is show_url
    assert parser.downloader.image_urls == ["https://example.com/500x500bb.jpg"]
    assert parser.downloader.audio_urls == []
    assert result.audio_contents == []
    assert len(requests) == 1
    assert requests[0][0] == _APPLE_TRACK_URL
    assert requests[0][1]["headers"]["Referer"] == "https://music.apple.com/"


def _apple_music_json_ld(track_id="1514034916"):
    return {
        "@type": "MusicComposition",
        "name": "落日",
        "url": "https://music.apple.com/cn/song/" + track_id,
        "datePublished": "2020-05-20",
        "timeRequired": "PT5M6S",
        "audio": {
            "@type": "MusicRecording",
            "name": "落日",
            "url": "https://music.apple.com/cn/song/" + track_id,
            "duration": "PT5M6S",
            "byArtist": [{"name": "大都会乐团"}, {"name": "嘉宾"}],
            "inAlbum": {"name": "晴朗日 - Single", "image": "https://example.com/square.jpg"},
            "audio": {"@type": "AudioObject", "contentUrl": "https://example.com/preview.m4a"},
        },
    }


def test_apple_music_song_detail_uses_json_ld_for_missing_duration():
    sections = [{
        "id": "songDetail",
        "itemKind": "songDetailHeader",
        "items": [{
            "title": "落日",
            "artists": "大都会乐团",
            "album": "晴朗日 - Single",
            "releaseDate": "2020年5月20日",
            "artwork": {"dictionary": {"url": "https://example.com/{w}x{h}bb.{f}"}},
            "contentDescriptor": {"kind": "song", "identifiers": {"storeAdamID": "1514034916"}},
        }],
    }]

    parser, result, _ = _parse_apple_music_html(
        _apple_music_html(sections, _apple_music_json_ld()),
        "https://music.apple.com/cn/song/1514034916",
    )

    assert result.title == "落日"
    assert result.author.name == "大都会乐团"
    assert result.extra["card_info"] == "专辑: 晴朗日 - Single\n时长: 5:06"
    assert result.formatted_datetime().startswith("2020-05-20")
    assert parser.downloader.image_urls == ["https://example.com/500x500bb.jpg"]
    assert parser.downloader.audio_urls == []


@pytest.mark.parametrize("server_data", ["", "null", "{bad json}", '{"data": []}'])
def test_apple_music_track_can_fall_back_to_matching_json_ld(server_data):
    html = (
        '<script id="serialized-server-data">' + server_data + "</script>"
        + '<script type="application/ld+json">{bad json}</script>'
        + '<script type="application/ld+json">'
        + json.dumps({"@graph": [_apple_music_json_ld("999"), _apple_music_json_ld()]})
        + "</script>"
    )

    parser, result, _ = _parse_apple_music_html(html)

    assert result.title == "落日"
    assert result.author.name == "大都会乐团 / 嘉宾"
    assert result.extra["card_info"] == "专辑: 晴朗日 - Single\n时长: 5:06"
    assert parser.downloader.image_urls == ["https://example.com/square.jpg"]
    assert parser.downloader.audio_urls == []


def test_apple_music_track_reads_exact_title_from_apple_meta_fallback():
    url = (
        "https://music.apple.com/cn/album/月声尽-心灯明/6817316824"
        "?i=6817316837&l=en-GB&qq_aio_chat_type=3"
    )
    html = (
        '<meta property="og:title" content="月声尽,心灯明 by Wuthering Waves">'
        '<meta name="apple:title" content="月声尽,心灯明">'
        '<meta name="apple:content_id" content="6817316837">'
    )

    _, result, _ = _parse_apple_music_html(html, url)

    assert result.title == "月声尽,心灯明"
    assert result.extra["track_id"] == "6817316837"


def test_apple_music_meta_title_is_ignored_for_different_track_id():
    html = (
        '<meta name="apple:title" content="不匹配的歌曲">'
        '<meta name="apple:content_id" content="999">'
    )

    with pytest.raises(ParseException, match="歌曲不存在或暂时无法访问"):
        _parse_apple_music_html(html)


@pytest.mark.parametrize(
    "html",
    [
        "<html>not found</html>",
        _apple_music_html(None),
        _apple_music_html([None, {"id": "track-list", "items": [None, {}]}]),
        _apple_music_html([], _apple_music_json_ld("999")),
    ],
)
def test_apple_music_missing_track_fails_instead_of_selecting_another_song(html):
    with pytest.raises(ParseException, match="歌曲不存在或暂时无法访问"):
        _parse_apple_music_html(html)


def test_apple_music_track_reports_http_errors():
    with pytest.raises(ParseException, match="歌曲请求失败: HTTP 404"):
        _parse_apple_music_html("not found", status=404)


def test_apple_music_track_short_duration_is_in_milliseconds():
    html = _apple_music_html([{
        "id": "track-list",
        "items": [{
            "title": "短歌",
            "duration": 5000,
            "contentDescriptor": {"kind": "song", "identifiers": {"storeAdamID": "1514034916"}},
        }],
    }])

    parser, result, _ = _parse_apple_music_html(html)

    assert result.extra["card_info"] == "时长: 0:05"
    assert result.extra["card_preview_only"] is False
    assert result.contents == []
    assert parser.downloader.audio_urls == []


def test_apple_music_playlist_still_dispatches_to_playlist_handler():
    html = _apple_music_html([
        {"id": "playlist-detail-header-section", "items": [{"title": "Mix", "trackCount": 1}]},
        {"id": "track-list", "items": [{
            "title": "Song", "artistName": "Artist", "contentDescriptor": {"kind": "song"},
        }]},
    ])
    _, result, _ = _parse_apple_music_html(
        html, "https://music.apple.com/cn/playlist/eng/pl.u-leyl0YAsMJgb1ro?l=en"
    )

    assert result.title == "Mix"
    assert result.extra["playlist_id"] == "pl.u-leyl0YAsMJgb1ro"
    assert result.extra["playlist_track_count"] == 1
    assert result.extra["playlist_tracks"][0]["title"] == "Song"
    assert "music_type" not in result.extra


def test_netease_lyrics_are_read_from_lyric_endpoint():
    async def run():
        parser = NetEaseMusicParser.__new__(NetEaseMusicParser)
        parser._netease_cookie_header = lambda _url: "MUSIC_U=test"
        requested = {}

        async def json_request(url, *, headers):
            requested["url"] = url
            requested["headers"] = headers
            return {"lrc": {"lyric": "[00:01.00]Hello\n[00:02.00]world"}}

        parser._json_request = json_request
        lyrics = await parser._fetch_netease_lyrics("3395393731")
        return lyrics, requested

    lyrics, request = asyncio.run(run())
    assert lyrics == "[00:01.00]Hello\n[00:02.00]world"
    assert "id=3395393731" in request["url"]
    assert request["headers"]["Cookie"] == "MUSIC_U=test"


def test_release_text_formats_netease_millisecond_timestamp():
    assert _format_release_text(1713888000000, milliseconds=True) == "2024-04-24"
    assert _format_release_text("2024-04-24", milliseconds=True) == "2024-04-24"
    assert _format_release_text("not-a-date", milliseconds=True) == "not-a-date"


def test_qqmusic_lyrics_decode_base64_from_official_response():
    async def run():
        parser = QQMusicParser.__new__(QQMusicParser)
        parser._qq_cookie_values = lambda: {"uin": "123"}
        parser._qq_cookie_header = lambda _url: "uin=123; qqmusic_key=key"
        requested = {}

        async def json_request(url, *, headers):
            requested["url"] = url
            requested["headers"] = headers
            lyric = base64.b64encode("[00:01.00]你好".encode()).decode()
            return {"req_1": {"data": {"lyric": lyric}}}

        parser._json_request = json_request
        lyrics = await parser._fetch_qq_lyrics("song-mid")
        return lyrics, requested

    lyrics, request = asyncio.run(run())
    assert lyrics == "[00:01.00]你好"
    assert "PlayLyricInfo" in request["url"]
    assert request["headers"]["Cookie"] == "uin=123; qqmusic_key=key"

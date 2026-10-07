import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import AudioContent
from core.parsers.music import (
    AppleMusicParser,
    KugouMusicParser,
    KuwoMusicParser,
    NetEaseMusicParser,
    QishuiMusicParser,
    QQMusicParser,
    _duration_seconds,
)


class _Downloader:
    def __init__(self):
        self.audio_urls: list[str] = []

    def download_img(self, url: str, **_kwargs):
        async def complete() -> Path:
            return Path(url.rsplit("/", 1)[-1] or "image.jpg")

        return asyncio.create_task(complete())

    def download_audio(self, url: str, **_kwargs):
        self.audio_urls.append(url)

        async def complete() -> Path:
            return Path(url.rsplit("/", 1)[-1] or "track.mp3")

        return asyncio.create_task(complete())


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
    ("mp3_url", "expected_url"),
    [
        (
            "",
            "https://music.163.com/song/media/outer/url?id=3395393731",
        ),
        ("https://example.com/song.mp3", "https://example.com/song.mp3"),
    ],
)
def test_netease_track_resolves_audio_url(mp3_url: str, expected_url: str):
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

        parser._json_request = request
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [(132, 132.0), (146000, 146.0), ("02:46", 166.0), ("3:58", 238.0)],
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

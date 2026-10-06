import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.parsers.music import (
    KugouMusicParser,
    NetEaseMusicParser,
    QQMusicParser,
    QishuiMusicParser,
)


class _Downloader:
    def download_img(self, url: str, **_kwargs):
        async def complete() -> Path:
            return Path(url.rsplit("/", 1)[-1] or "image.jpg")

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
            NetEaseMusicParser,
            "网易云歌单：https://163cn.tv/AbC_123",
            "163cn.tv",
        ),
        (
            NetEaseMusicParser,
            "网易云歌单 https://music.163.com/m/playlist?id=123456",
            "music.163.com/m/playlist",
        ),
        (
            NetEaseMusicParser,
            "网易云歌单 https://music.163.com/#/playlist?id=123456",
            "music.163.com/#/playlist",
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
            QishuiMusicParser,
            "汽水歌单：https://qishui.douyin.com/s/iXqUS9uU/",
            "qishui.douyin.com/s",
        ),
        (
            QishuiMusicParser,
            "汽水歌单 https://music.douyin.com/qishui/share/playlist?playlist_id=123456",
            "music.douyin.com/qishui/share/playlist",
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

from __future__ import annotations

import json
import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def utils_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    monkeypatch.delitem(sys.modules, "core.utils", raising=False)
    return importlib.import_module("core.utils")


def test_extract_json_url_keeps_existing_detail_qqdocurl_support(utils_module):
    data = {"meta": {"detail_1": {"qqdocurl": "https://example.com/doc"}}}
    assert utils_module.extract_json_url(data) == "https://example.com/doc"


def test_extract_json_url_reads_supported_miniapp_legacy_url(utils_module):
    data = {
        "meta": {
            "miniapp": {
                "legacyUrl": "https%3A%2F%2Fwww.douyin.com%2Fvideo%2F1234567890123456789"
            }
        }
    }
    assert utils_module.extract_json_url(data).startswith(
        "https://www.douyin.com/video/1234567890123456789"
    )


def test_extract_json_url_prefers_nested_supported_share_over_unrelated_url(utils_module):
    data = {
        "prompt": "https://example.com/landing",
        "meta": {
            "detail": {
                "nested": "open https:\\/\\/www.xiaohongshu.com\\/explore\\/abc?x=1 now"
            }
        },
    }
    assert utils_module.extract_json_url(data) == "https://www.xiaohongshu.com/explore/abc?x=1"


def test_extract_json_url_recognizes_netease_short_share(utils_module):
    data = {
        "prompt": "https://example.com/landing",
        "meta": {
            "detail": {
                "nested": "open https://163cn.tv/AbC_123 now",
            }
        },
    }

    assert utils_module.extract_json_url(data) == "https://163cn.tv/AbC_123"


def test_extract_json_url_prefers_qq_music_playlist_share(utils_module):
    playlist_url = (
        "https://i2.y.qq.com/n3/other/pages/details/playlist.html?"
        "hosteuin=abc&id=9013740134&source=qq"
    )
    data = {
        "prompt": "https://example.com/landing",
        "meta": {
            "detail": {
                "card": "open " + playlist_url,
            }
        },
    }

    assert utils_module.extract_json_url(data) == playlist_url


@pytest.mark.parametrize(
    "playlist_url",
    [
        "https://m.kuwo.cn/newh5app/playlist_detail/3567046051?from=ip&t=qqfriend",
        "https://music.apple.com/cn/playlist/eng/pl.u-leyl0YAsMJgb1ro?l=en",
    ],
)
def test_extract_json_url_recognizes_new_music_platforms(utils_module, playlist_url):
    data = {"meta": {"detail": {"url": "open " + playlist_url}}}

    assert utils_module.extract_json_url(data) == playlist_url


@pytest.mark.parametrize(
    "track_url",
    [
        "https://m.kugou.com/share/?album_id=1012787&hash=abc&action=single",
        "https://m.kuwo.cn/yinyue/72057414?f=ip",
        "https://i.y.qq.com/v8/playsong.html?media_mid=x&songid=453455745",
        "https://y.music.163.com/m/song?id=3429744904",
    ],
)
def test_extract_json_url_recognizes_music_single_tracks(utils_module, track_url):
    data = {"meta": {"detail": {"url": "open " + track_url}}}

    assert utils_module.extract_json_url(data) == track_url


def test_extract_json_url_prefers_netease_song_page_over_media_url(utils_module):
    song_page_url = (
        "https://y.music.163.com/m/song?fx-wechatnew=t1&fx-wxqd=&"
        "fx-wordtest=&id=3395393731&PlayerStyles_SynchronousSharing=t3&"
        "fx-listentest=t3&H5_DownloadVIPGift=&userid=1312543631&"
        "app_version=9.5.15&dlt=0846&ts-wakeup="
    )
    media_url = "http://music.163.com/song/media/outer/url?id=3395393731"
    card = {
        "app": "com.tencent.music.lua",
        "bizsrc": "qqconnect.sdkshare_music",
        "meta": {
            "music": {
                "desc": "鸣潮先约电台/飞行雪绒",
                "jumpUrl": song_page_url,
                "musicUrl": media_url,
                "preview": "https://pic.ugcimg.cn/example.jpg",
                "tag": "网易云音乐",
                "title": "Brand New Sky (新世界的天空)",
            }
        },
        "prompt": "[分享]Brand New Sky",
        "view": "music",
    }

    # Napcat/AstrBot may pass either the decoded card object or its serialized
    # value inside the outer Json.data field.
    assert utils_module.extract_json_url(card) == song_page_url
    assert utils_module.extract_json_url({"data": json.dumps(card)}) == song_page_url

"""Regressions for successful responses which silently downgrade audio quality."""

import asyncio
from types import SimpleNamespace

import pytest

from core.parsers.kugou_quality import KugouQualityClient, downloaded_quality
from core.parsers.music import KugouMusicParser

HASH = "747D7356E8952FCA50903F4194CE7296"
COOKIE = "KugooID=123; t=test; kg_mid=mid; kg_dfid=dfid"


@pytest.mark.parametrize("reported", [None, 128, 128000])
def test_lossless_mp3_response_continues_to_high(reported):
    client = KugouQualityClient(SimpleNamespace(proxy=None))
    calls = []

    async def request(_session, params):
        calls.append(params["quality"])
        data = {"url": "https://fsandroid.kugou.com/song.mp3"}
        if params["quality"] == "flac":
            data["bitrate"] = reported
        else:
            data["bitrate"] = 320000
        return {"status": 1, "data": data}

    client._request = request
    result = asyncio.run(client.resolve(COOKIE, HASH, ("lossless", "high", "standard")))
    assert calls == ["flac", "320"]
    assert result["requested_audio_quality"] == "high"


def test_matching_lossless_backup_is_used_before_downgrading():
    client = KugouQualityClient(SimpleNamespace(proxy=None))

    async def request(_session, _params):
        return {"status": 1, "data": {
            "url": "http://fsandroid.kugou.com/song.mp3",
            "backup_url": ["http://fsandroid.kugou.com/song.flac"],
        }}

    client._request = request
    result = asyncio.run(client.resolve(COOKIE, HASH, ("lossless", "high")))
    assert result["url"] == "https://fsandroid.kugou.com/song.flac"
    assert result["requested_audio_quality"] == "lossless"


@pytest.mark.parametrize("bitrate", [128, 128000])
def test_reported_standard_bitrate_does_not_end_high_request(bitrate):
    client = KugouQualityClient(SimpleNamespace(proxy=None))
    calls = []

    async def request(_session, params):
        calls.append(params["quality"])
        return {"status": 1, "data": {
            "url": "https://fsandroid.kugou.com/song.mp3", "bitrate": bitrate,
        }}

    client._request = request
    result = asyncio.run(client.resolve(COOKIE, HASH, ("high", "standard")))
    assert calls == ["320", "128"]
    assert result["requested_audio_quality"] == "standard"


def test_log_example_bytes_are_recognized_as_standard(tmp_path):
    path = tmp_path / "song.mp3"
    with path.open("wb") as stream:
        stream.write(b"ID3")
        stream.truncate(3323437)
    assert downloaded_quality(path, 207) == ("standard", 128)
    assert downloaded_quality(path, None) == (None, None)
    path = tmp_path / "lossless.flac"
    path.write_bytes(b"fLaC")
    assert downloaded_quality(path, 207) == ("lossless", None)


@pytest.mark.parametrize("final_success", [True, False])
def test_downloaded_high_downgrade_keeps_searching_and_preserves_playback(
    tmp_path, monkeypatch, final_success
):
    parser = KugouMusicParser.__new__(KugouMusicParser)
    parser.cfg = SimpleNamespace(parser=SimpleNamespace(kugou=SimpleNamespace(
        audio_send_format="file", file_quality_priority=["high", "lossless", "standard"],
    )))
    parser._kugou_cookie_header = lambda _url: COOKIE
    metadata = {"song_name": "Song", "timelength": 207000,
                "url": "https://fsandroid.kugou.com/old.mp3"}
    path = tmp_path / "downgraded.mp3"
    with path.open("wb") as stream:
        stream.write(b"ID3")
        stream.truncate(3323437)
    lossless = tmp_path / "real.flac"
    lossless.write_bytes(b"fLaC")
    calls = []

    async def legacy(*_args, **_kwargs):
        return metadata

    async def resolve(_self, _cookie, _hash, priorities, *_args):
        calls.append(priorities)
        if priorities[0] == "high":
            return {"url": "https://fsandroid.kugou.com/new.mp3",
                    "requested_audio_quality": "high"}
        if final_success:
            return {"url": "https://fsandroid.kugou.com/new.flac",
                    "requested_audio_quality": "lossless"}
        return {}

    async def download(_self, url):
        return lossless if url.endswith("flac") else path

    parser._fetch_kugou_legacy_song_info = legacy
    monkeypatch.setattr(KugouQualityClient, "resolve", resolve)
    monkeypatch.setattr(KugouQualityClient, "download", download)
    result = asyncio.run(parser._fetch_kugou_song_info(HASH))
    assert calls == [("high", "lossless", "standard"), ("lossless", "standard")]
    assert result["song_name"] == "Song"
    assert result["_local_audio_path"] == (lossless if final_success else path)
    assert "audio_quality" not in result

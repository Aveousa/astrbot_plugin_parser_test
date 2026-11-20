import asyncio
from types import SimpleNamespace

import pytest

from core.exception import DownloadException
from core.parsers.kugou_quality import KugouQualityClient, audio_url, session_fields
from core.parsers.music import KugouMusicParser

HASH = "1F7E7659B9FFF4FB6F71562A61BBC25F"


@pytest.mark.parametrize("kind", ["flac", "mp3", "html", "redirect", "oversize", "cancel"])
def test_isolated_download_validates_audio_and_cleans_failures(tmp_path, monkeypatch, kind):
    from core.parsers import kugou_quality as module

    body = b"fLaC" + b"metadata" * 100 if kind == "flac" else b"ID3" + b"audio" * 100
    if kind == "html":
        body = b"<html>expired URL</html>"
    suffix = ".flac" if kind == "flac" else ".mp3"
    captured = {}

    class Content:
        async def iter_chunked(self, size):
            yield body[:2]
            if kind == "cancel":
                raise asyncio.CancelledError
            yield body[2:]

    class Response:
        status = 302 if kind == "redirect" else 200
        content_length = 2 * 1024 * 1024 if kind == "oversize" else len(body)
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class Session:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def get(self, url, **kwargs):
            captured["request"] = kwargs
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    parser = SimpleNamespace(
        cfg=SimpleNamespace(cache_dir=tmp_path, source_max_size=1, download_timeout=10),
        proxy=None,
    )

    async def run():
        client = KugouQualityClient(parser)
        if kind in {"flac", "mp3"}:
            path = await client.download("https://webfs.tx.kugou.com/song" + suffix)
            assert path.read_bytes() == body
            assert path.suffix == suffix
        else:
            error = asyncio.CancelledError if kind == "cancel" else DownloadException
            with pytest.raises(error):
                await client.download("https://webfs.tx.kugou.com/song" + suffix)
            assert not list(tmp_path.iterdir())
        assert captured["request"]["allow_redirects"] is False
        assert "Cookie" not in captured["request"]["headers"]
        assert "cookies" not in captured["request"]
        assert not list(tmp_path.glob("*.part"))

    asyncio.run(run())


def test_resolver_rejects_private_answers_in_connection(monkeypatch):
    from core.parsers.kugou_quality import _PublicResolver

    async def run():
        resolver = _PublicResolver()

        async def private(*_args):
            return [{"host": "127.0.0.1"}]

        monkeypatch.setattr(resolver._resolver, "resolve", private)
        try:
            with pytest.raises(OSError, match="non-public"):
                await resolver.resolve("webfs.tx.kugou.com", 443)
        finally:
            await resolver.close()

    asyncio.run(run())


def test_download_failure_preserves_old_playback_and_metadata(monkeypatch):
    parser = KugouMusicParser.__new__(KugouMusicParser)
    parser.cfg = SimpleNamespace(
        parser=SimpleNamespace(kugou=SimpleNamespace(audio_send_format="file"))
    )
    parser._kugou_cookie_header = lambda _url: ""
    metadata = {
        "url": "https://webfs.tx.kugou.com/old.mp3",
        "song_name": "Original",
        "author_name": "Artist",
        "timelength": 299000,
    }

    async def legacy(*_args, **_kwargs):
        return metadata

    async def resolve(*_args, **_kwargs):
        return {
            "url": "https://webfs.tx.kugou.com/new.flac",
            "requested_audio_quality": "lossless",
        }

    async def download(*_args, **_kwargs):
        raise DownloadException("unavailable")

    parser._fetch_kugou_legacy_song_info = legacy
    monkeypatch.setattr(KugouQualityClient, "resolve", resolve)
    monkeypatch.setattr(KugouQualityClient, "download", download)
    assert asyncio.run(parser._fetch_kugou_song_info(HASH)) == metadata


def test_prepared_audio_keeps_metadata_and_does_not_download_twice(
    monkeypatch, tmp_path
):
    from core.data import AudioContent, ParseResult, Platform

    parser = KugouMusicParser.__new__(KugouMusicParser)
    parser.cfg = SimpleNamespace(
        parser=SimpleNamespace(kugou=SimpleNamespace(audio_send_format="file"))
    )
    parser.headers = {}
    parser._kugou_cookie_header = lambda _url: ""
    path = tmp_path / "prepared.flac"
    path.write_bytes(b"fLaC")
    metadata = {
        "url": "https://webfs.tx.kugou.com/old.mp3",
        "song_name": "Original",
        "author_name": "Artist",
        "timelength": 299000,
        "img": "https://imge.kugou.com/cover.jpg",
    }

    async def legacy(*_args, **_kwargs):
        return metadata

    async def resolve(*_args, **_kwargs):
        return {
            "url": "https://webfs.tx.kugou.com/new.flac",
            "requested_audio_quality": "lossless",
        }

    async def download(*_args, **_kwargs):
        return path

    def result_builder(**kwargs):
        assert kwargs["audio_url"] is None
        assert kwargs["cover"] == metadata["img"]
        assert kwargs["artist_name"] == "Artist"
        return ParseResult(
            platform=Platform("kugou", "酷狗音乐"), title=kwargs["title"]
        )

    parser._fetch_kugou_legacy_song_info = legacy
    parser._single_track_result = result_builder
    parser._kugou_artist_avatar_url = lambda _info: "https://imge.kugou.com/artist.jpg"
    monkeypatch.setattr(KugouQualityClient, "resolve", resolve)
    monkeypatch.setattr(KugouQualityClient, "download", download)
    result = asyncio.run(
        parser._kugou_single_result(
            source_url="https://www.kugou.com/mixsong/test.html", song_hash=HASH
        )
    )
    assert result.title == "Original"
    assert len(result.contents) == 1 and isinstance(result.contents[0], AudioContent)
    assert asyncio.run(result.contents[0].get_path()) == path
    assert result.extra["requested_audio_quality"] == "lossless"
    assert "audio_quality" not in result.extra
    assert result.extra["audio_as_file"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "https://webfs.tx.kugou.com:8080/a.mp3",
        "https://kugou.com/a.mp3",
        "https://webfs.tx.kugou.com.evil.test/a.mp3",
        "https://127.0.0.1/a.mp3",
    ],
)
def test_media_url_rejects_untrusted_targets(bad):
    assert audio_url({"url": bad}) is None


def test_browser_cookie_precedence_does_not_mix_lite_identity():
    cookie = "KugooID=123; t=web; kg_mid=web-mid; kg_dfid=web-dfid; userid=456; token=lite; KUGOU_API_MID=lite-mid; mid=old-mid; dfid=old-dfid"
    assert session_fields(cookie) == {
        "userid": "123",
        "token": "web",
        "mid": "web-mid",
        "dfid": "web-dfid",
    }


@pytest.mark.parametrize("status", [None, 0, 2])
def test_non_success_body_cannot_supply_audio_url(status):
    client = KugouQualityClient(SimpleNamespace(proxy=None))

    async def request(*_args):
        return {
            "status": status,
            "data": {"url": "https://webfs.tx.kugou.com/audio.mp3"},
        }

    client._request = request
    cookie = "KugooID=123; t=web; kg_mid=web-mid; kg_dfid=web-dfid"
    assert asyncio.run(client.resolve(cookie, HASH, ("high",))) == {}

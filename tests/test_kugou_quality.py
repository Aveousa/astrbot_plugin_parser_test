import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from core.parsers.kugou_quality import (
    KugouQualityClient,
    audio_url,
    build_params,
    quality_priority,
    session_fields,
)
from core.parsers.music import KugouMusicParser

COOKIE = "userid=12345; token=test-token; KUGOU_API_MID=123456; dfid=test-device"
HASH = "1F7E7659B9FFF4FB6F71562A61BBC25F"


def test_h5_cookie_extracts_mineradio_web_account_fields():
    fields = session_fields(
        "KuGoo=KugooID=123&t=web-token; kg_mid=abcdefabcdefabcdefabcdefabcdefab; kg_dfid=device"
    )
    assert fields == {
        "userid": "123",
        "token": "web-token",
        "mid": "abcdefabcdefabcdefabcdefabcdefab",
        "dfid": "device",
    }


def test_missing_fields_reject_song_requests():
    assert session_fields("userid=123; token=test") is None
    assert session_fields("KuGoo=KugooID=0&t=web; kg_mid=mid; kg_dfid=dfid") is None
    assert session_fields(COOKIE)["userid"] == "12345"


def test_mineradio_web_cookie_fields_support_h5_not_lite():
    cookie = (
        "KuGoo=KugooID=889875332&t=web-token&NickName=name; "
        "KugooID=889875332; t=web-token; mid=abcdef0123456789abcdef0123456789; dfid=device-id"
    )
    fields = session_fields(cookie)
    assert fields == {
        "userid": "889875332",
        "token": "web-token",
        "mid": "abcdef0123456789abcdef0123456789",
        "dfid": "device-id",
    }
    params = build_params(fields, HASH, "lossless", "958909", "j2i4p51")
    assert params["appid"] == 1014
    assert params["quality"] == "flac"
    assert params["IsFreePart"] == 0


def test_wire_parameters_match_fixed_h5_contract(monkeypatch):
    monkeypatch.setattr("core.parsers.kugou_quality.time.time", lambda: 1800000000)
    fields = session_fields(COOKIE)
    params = build_params(fields, HASH, "high", "958909", "j2i4p51")
    assert params["appid"] == 1014
    assert params["clientver"] == 20000
    assert params["srcappid"] == 2919
    assert params["quality"] == "320"
    assert params["album_id"] == 958909
    assert params["album_audio_id"] == 0
    assert params["hash"] == HASH.lower()
    assert "page_id" not in params and "ppage_id" not in params
    expected_key = hashlib.md5(
        (
            HASH.lower() + "57ae12eb6890223e355ccfcb74edf70d1014" + "123456" + "12345"
        ).encode()
    ).hexdigest()
    assert params["key"] == expected_key
    signature = params.pop("signature")
    salt = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    assert (
        signature
        == hashlib.md5(
            (salt + "".join(f"{k}={params[k]}" for k in sorted(params)) + salt).encode()
        ).hexdigest()
    )


def test_h5_does_not_use_redirects_or_accept_injected_cookie_fields():
    assert (
        session_fields("KugooID=123; t=token\\r\\nCookie: evil; mid=mid; dfid=dfid")
        is None
    )

    client = KugouQualityClient(SimpleNamespace(proxy=None))
    calls = []

    class Response:
        status = 302

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def get(self, _url, **kwargs):
            calls.append(kwargs)
            return Response()

    with pytest.raises(ValueError):
        asyncio.run(
            client._request(
                Session(),
                build_params(session_fields(COOKIE), HASH, "high", None, None),
            )
        )
    assert calls[0]["allow_redirects"] is False

    assert quality_priority(["lossless", "bad", "lossless", "high"]) == (
        "lossless",
        "high",
    )
    assert quality_priority(None) == ("high", "lossless", "standard")
    assert (
        audio_url({"url": ["", "https://webfs.tx.kugou.com/song.flac"]})
        == "https://webfs.tx.kugou.com/song.flac"
    )
    assert (
        audio_url(
            {
                "play_url": "https://webfs.tx.kugou.com/a.mp3,https://webfs.tx.kugou.com/b.mp3"
            }
        )
        == "https://webfs.tx.kugou.com/a.mp3"
    )
    assert audio_url({"url": "https://user:secret@webfs.tx.kugou.com/song.mp3"}) is None
    assert audio_url({"url": "file:///private/song.mp3"}) is None
    assert audio_url({"url": "http://webfs.tx.kugou.com/song.mp3"}) == "https://webfs.tx.kugou.com/song.mp3"
    assert audio_url({"url": "https://example.com/song.mp3"}) is None
    assert audio_url({"url": "https://webfs.tx.kugou.com/song.mp3"})


@pytest.mark.parametrize("outcome", ["selected", "filtered", "rejected", "timeout"])
def test_quality_diagnostics_never_log_credentials_or_signed_urls(monkeypatch, outcome):
    import json

    messages = []
    monkeypatch.setattr("core.parsers.kugou_quality.logger.debug", messages.append)
    client = KugouQualityClient(SimpleNamespace(proxy=None))
    sensitive_url = "https://webfs.tx.kugou.com/private-name.flac?token=secret-query"

    async def request(*_args):
        if outcome == "timeout":
            raise TimeoutError(sensitive_url)
        if outcome == "rejected":
            return {"status": "secret-response", "error_code": "secret-error"}
        return {
            "status": 1,
            "data": {
                "url": sensitive_url
                if outcome == "selected"
                else sensitive_url.replace("webfs.tx.kugou.com", "untrusted.example"),
                "bitrate": "secret-bitrate",
                "filesize": 123,
            },
        }

    client._request = request
    asyncio.run(client.resolve(COOKIE, HASH, ("lossless",)))
    text = "\n".join(messages)
    assert messages
    for secret in (
        "test-token",
        "secret-query",
        "private-name",
        "secret-response",
        "secret-error",
        "secret-bitrate",
        HASH,
    ):
        assert secret not in text
    rows = [json.loads(message.removeprefix("[酷狗音质] ")) for message in messages]
    assert len({row["trace"] for row in rows}) == 1
    if outcome == "filtered":
        assert any(row.get("reason") == "cdn_host_not_allowed" and row.get("cdn_host") == "untrusted.example" for row in rows)
    if outcome == "timeout":
        assert any(row.get("reason") == "timeout" for row in rows)


def test_http_cdn_upgrade_preserves_signed_bytes_and_quality_order():
    source = "http://webfs.tx.kugou.com/path%2fsong.flac?sig=a%2Bb%2fC&x=1&x=2"
    expected = "https" + source[4:]
    assert audio_url({"url": source}) == expected
    client = KugouQualityClient(SimpleNamespace(proxy=None))
    calls = []

    async def request(_session, params):
        calls.append(params["quality"])
        return {"status": 1, "data": {"url": source}}

    client._request = request
    result = asyncio.run(client.resolve(COOKIE, HASH, ("lossless", "high", "standard")))
    assert calls == ["flac"]
    assert result["url"] == expected
    assert result["requested_audio_quality"] == "lossless"


@pytest.mark.parametrize("url", [
    "http://untrusted.example/a.flac",
    "http://webfs.tx.kugou.com.evil.example/a.flac",
    "http://webfs.tx.kugou.com:8080/a.flac",
    "https://webfs.tx.kugou.com:80/a.flac",
    "ftp://webfs.tx.kugou.com/a.flac",
])
def test_upgrade_does_not_expand_cdn_or_port_allowlist(url):
    assert audio_url({"url": url}) is None


def test_http_default_port_upgrade_and_precise_rejections():
    from core.parsers.kugou_quality import _url_diagnostic

    suffix = "/p%2Fsong.flac?sig=test%2b%2F&x=1&x=2"
    source = "http://webfs.tx.kugou.com:80" + suffix
    assert audio_url({"url": source}) == "https://webfs.tx.kugou.com" + suffix
    assert _url_diagnostic({"url": source})["reason"] == "http_upgraded"
    for url, reason in (
        ("http://other.kugou.com/song.flac?sig=private", "cdn_host_not_allowed"),
        ("https://webfs.tx.kugou.com:80/song.flac", "cdn_port_not_allowed"),
        ("http://webfs.tx.kugou.com:bad/song.flac", "malformed_url"),
        ("file:///secret", "unsupported_scheme"),
    ):
        assert audio_url({"url": url}) is None
        details = _url_diagnostic({"url": url})
        assert details["reason"] == reason
        assert "private" not in str(details) and "song.flac" not in str(details)


def test_quality_chain_skips_explicit_preview_and_empty_results():
    client = KugouQualityClient(SimpleNamespace(proxy=None))
    calls = []

    async def request(_session, params):
        calls.append(params["quality"])
        if params["quality"] == "320":
            return {
                "status": 1,
                "data": {
                    "url": "https://webfs.tx.kugou.com/preview.mp3",
                    "preview": True,
                },
            }
        if params["quality"] == "flac":
            return {"status": 1, "data": {"url": []}}
        return {"status": 1, "data": {"url": "https://webfs.tx.kugou.com/full.mp3"}}

    client._request = request
    result = asyncio.run(client.resolve(COOKIE, HASH, quality_priority(None)))
    assert calls == ["320", "flac", "128"]
    assert result["url"] == "https://webfs.tx.kugou.com/full.mp3"
    assert result["requested_audio_quality"] == "standard"


@pytest.mark.parametrize("failure", ["device", "auth", "timeout"])
def test_auth_device_or_transport_failure_stops_chain(failure):
    client = KugouQualityClient(SimpleNamespace(proxy=None))
    calls = []

    async def request(_session, _params):
        calls.append(1)
        if failure == "timeout":
            raise TimeoutError("signed-url-must-not-be-logged")
        return {"status": 0, "error_code": 20028 if failure == "device" else 152}

    client._request = request
    assert asyncio.run(client.resolve(COOKIE, HASH, quality_priority(None))) == {}
    assert calls == [1]


def test_web_only_cookie_makes_no_lite_request():
    client = KugouQualityClient(SimpleNamespace(proxy=None))

    async def request(*_args):
        pytest.fail("web credentials must not be sent to lite API")

    client._request = request
    assert (
        asyncio.run(
            client.resolve("KuGoo=KugooID=123&t=web", HASH, quality_priority(None))
        )
        == {}
    )


@pytest.mark.parametrize(
    "mode,lite_success", [("voice", True), ("file", True), ("file", False)]
)
def test_parser_preserves_web_fallback_and_voice_behavior(
    monkeypatch, mode, lite_success
):
    parser = KugouMusicParser.__new__(KugouMusicParser)
    parser.cfg = SimpleNamespace(
        parser=SimpleNamespace(
            kugou=SimpleNamespace(
                audio_send_format=mode,
                file_quality_priority=["lossless", "high"],
            )
        )
    )
    parser._kugou_cookie_header = lambda _url: COOKIE
    calls = []

    async def lite(_self, cookie, song_hash, priorities, album_id, album_audio_id):
        calls.append("lite")
        assert priorities == ("lossless", "high")
        return {"url": "https://webfs.tx.kugou.com/lite.flac"} if lite_success else {}

    async def web(*_args, **_kwargs):
        calls.append("web")
        return {"url": "https://webfs.tx.kugou.com/existing.mp3"}

    async def downloaded(_self, url):
        from pathlib import Path

        return Path("tested.flac")

    monkeypatch.setattr(KugouQualityClient, "download", downloaded)
    monkeypatch.setattr(KugouQualityClient, "resolve", lite)
    parser._fetch_kugou_web_song_info = web
    result = asyncio.run(parser._fetch_kugou_song_info(HASH))
    if mode == "file" and lite_success:
        assert calls == ["web", "lite"]
        assert result["url"].endswith("lite.flac")
        assert result["_local_audio_path"].name == "tested.flac"
    else:
        assert calls == (["web", "lite"] if mode == "file" else ["web"])
        assert result["url"].endswith("existing.mp3")

import asyncio
import time
from types import SimpleNamespace

import pytest

from core.cookie import Cookie, CookieJar
from core.parsers.qqmusic_session import QQMusicSession


def make_parser(tmp_path):
    jar = object.__new__(CookieJar)
    jar.domain = "y.qq.com"
    jar.cookies = [
        Cookie(".y.qq.com", "/", n, v, True, 0)
        for n, v in {
            "uin": "12345",
            "qqmusic_uin": "12345",
            "qqmusic_key": "old-key",
            "qm_keyst": "old-key",
            "other": "keep",
        }.items()
    ]
    jar.cookie_file = tmp_path / "cookies.txt"
    jar.persistence_enabled = True
    jar._sync_cookies_str()
    return SimpleNamespace(cookiejar=jar, cfg=SimpleNamespace(cookie_dir=tmp_path))


def credential(**changes):
    data = {
        "musicid": "12345",
        "str_musicid": "12345",
        "musickey": "old-key",
        "refresh_key": "secret-refresh",
        "musickeyCreateTime": int(time.time()) - 10000,
        "keyExpiresIn": 11000,
    }
    data.update(changes)
    return data


def success(**changes):
    return {
        "code": 0,
        "req_0": {
            "code": 0,
            "data": credential(
                musickey="new-key",
                refresh_key="new-refresh",
                musickeyCreateTime=int(time.time()),
                keyExpiresIn=86400,
                **changes,
            ),
        },
    }


def run(coro):
    return asyncio.run(coro)


def test_encrypted_restart_and_success(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential())
    assert b"secret-refresh" not in (tmp_path / "qqmusic_session.enc").read_bytes()
    assert b"old-key" not in (tmp_path / "qqmusic_session.enc").read_bytes()
    restored = QQMusicSession(parser)

    async def request(*args):
        return success()

    restored._request_refresh = request
    assert run(restored.refresh_if_due())
    assert parser.cookiejar.get(domain="u.y.qq.com", path="/cgi-bin/musicu.fcg") == {
        "uin": "12345",
        "qqmusic_uin": "12345",
        "qqmusic_key": "new-key",
        "qm_keyst": "new-key",
        "other": "keep",
    }
    assert QQMusicSession(parser)._state["credentials"]["musickey"] == "new-key"


def test_not_due_missing_refresh_and_incomplete_clears(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential(keyExpiresIn=100000))

    async def request(*args):
        pytest.fail("unexpected request")

    session._request_refresh = request
    assert not run(session.refresh_if_due())
    session.capture_login(credential(refresh_key=""))
    assert not run(session.refresh_if_due())
    assert QQMusicSession(parser)._state is None


def test_failure_cooldown_and_manual_change(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential())
    calls = []

    async def request(*args):
        calls.append(1)
        return {"code": 1}

    session._request_refresh = request
    assert not run(session.refresh_if_due())
    assert not run(session.refresh_if_due())
    assert len(calls) == 1
    session.capture_login(credential())
    parser.cookiejar.cookies[0].value = "999"
    assert not run(session.refresh_if_due())
    assert len(calls) == 1


def test_concurrency_disabled_and_cancel(tmp_path):
    async def check():
        parser = make_parser(tmp_path)
        session = QQMusicSession(parser)
        session.capture_login(credential())
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def request(*args):
            calls.append(1)
            entered.set()
            await release.wait()
            return success()

        session._request_refresh = request
        a = asyncio.create_task(session.refresh_if_due())
        await entered.wait()
        b = asyncio.create_task(session.refresh_if_due())
        release.set()
        assert await a
        assert not await b
        assert len(calls) == 1
        session.capture_login(credential(musickey="new-key"))
        parser.cookiejar.persistence_enabled = False
        assert not await session.refresh_if_due()
        parser.cookiejar.persistence_enabled = True
        session._last_attempt = 0
        entered.clear()
        release.clear()
        task = asyncio.create_task(session.refresh_if_due())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert parser.cookiejar.get()["qqmusic_key"] == "new-key"
        session.start()
        session.start()
        session.stop()
        await session.close()

    run(check())


def test_corrupt_state_not_overwritten(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential())
    path = tmp_path / "qqmusic_session.enc"
    path.write_bytes(b"corrupt")
    session = QQMusicSession(parser)
    session.capture_login(credential())
    assert path.read_bytes() == b"corrupt"


def test_bad_response_and_save_rollback(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential())

    async def bad(*args):
        result = success()
        result["req_0"]["data"]["musicid"] = "999"
        return result

    session._request_refresh = bad
    assert not run(session.refresh_if_due())
    session._last_attempt = 0
    session._state["last_attempt"] = 0

    async def good(*args):
        return success()

    session._request_refresh = good

    def fail():
        raise OSError("private")

    parser.cookiejar.save_to_file = fail
    assert not run(session.refresh_if_due())
    assert parser.cookiejar.get()["qqmusic_key"] == "old-key"
    assert not run(QQMusicSession(parser).refresh_if_due())


def test_inflight_retirement_and_new_login(tmp_path):
    async def check(change):
        parser = make_parser(tmp_path)
        session = QQMusicSession(parser)
        session.capture_login(credential())
        entered, release = asyncio.Event(), asyncio.Event()

        async def request(*args):
            entered.set()
            await release.wait()
            return success()

        session._request_refresh = request
        task = asyncio.create_task(session.refresh_if_due())
        await entered.wait()
        change(parser, session)
        release.set()
        assert not await task
        assert parser.cookiejar.get()["qqmusic_key"] == "old-key"

    run(
        check(
            lambda parser, session: setattr(
                parser.cookiejar, "persistence_enabled", False
            )
        )
    )
    run(check(lambda parser, session: session.capture_login({})))
    run(check(lambda parser, session: session.stop()))


def test_malicious_key_and_expired_response_are_rejected(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential())

    async def request(*args):
        result = success()
        result["req_0"]["data"]["musickey"] = "bad;injected=1"
        return result

    session._request_refresh = request
    assert not run(session.refresh_if_due())
    assert parser.cookiejar.get()["qqmusic_key"] == "old-key"


def test_retired_capture_cannot_replace_active_refresh_material(tmp_path):
    parser = make_parser(tmp_path)
    retired = QQMusicSession(parser)
    retired.capture_login(credential())
    retired.stop()
    current = QQMusicSession(parser)
    current.capture_login(credential(refresh_key="active-refresh"))
    saved = (tmp_path / "qqmusic_session.enc").read_bytes()

    retired.capture_login(credential(refresh_key="stale-refresh"))

    assert (tmp_path / "qqmusic_session.enc").read_bytes() == saved
    assert (
        QQMusicSession(parser)._state["credentials"]["refresh_key"] == "active-refresh"
    )


def test_disabled_capture_cannot_remove_active_state(tmp_path):
    parser = make_parser(tmp_path)
    session = QQMusicSession(parser)
    session.capture_login(credential())
    saved = (tmp_path / "qqmusic_session.enc").read_bytes()
    parser.cookiejar.persistence_enabled = False

    session.capture_login({})

    assert (tmp_path / "qqmusic_session.enc").read_bytes() == saved


def test_finite_cookie_expiry_tracks_refreshed_credential(tmp_path, monkeypatch):
    parser = make_parser(tmp_path)
    original_expiry = int(time.time()) + 100
    for cookie in parser.cookiejar.cookies:
        if cookie.name != "other":
            cookie.expires = original_expiry
    session = QQMusicSession(parser)
    session.capture_login(credential())
    response = success()

    async def request(*_args):
        return response

    session._request_refresh = request
    assert run(session.refresh_if_due())
    monkeypatch.setattr("core.cookie.time.time", lambda: original_expiry + 1)
    cookies = parser.cookiejar.get(domain="u.y.qq.com", path="/cgi-bin/musicu.fcg")
    assert cookies["qqmusic_key"] == "new-key"
    assert cookies["uin"] == "12345"
    assert parser.cookiejar.cookies[-1].expires == 0


def test_refresh_does_not_overwrite_external_cookie_file_change(tmp_path):
    from core.parsers.qqmusic_cookie import QQMusicCookieJar

    async def check():
        parser = make_parser(tmp_path)
        parser.cookiejar = QQMusicCookieJar(
            parser.cfg,
            SimpleNamespace(
                name="qqmusic",
                cookies="uin=12345; qqmusic_uin=12345; qqmusic_key=old-key; qm_keyst=old-key",
            ),
        )
        session = QQMusicSession(parser)
        session.capture_login(credential())

        async def request(*_args):
            parser.cookiejar.cookie_file.write_bytes(b"administrator-edited")
            return success()

        session._request_refresh = request
        assert not await session.refresh_if_due()
        assert parser.cookiejar.cookie_file.read_bytes() == b"administrator-edited"
        assert parser.cookiejar.get()["qqmusic_key"] == "old-key"

    run(check())


def test_cookie_partial_write_failure_preserves_old_disk_and_refresh_state(
    tmp_path, monkeypatch
):
    from core.parsers.qqmusic_cookie import QQMusicCookieJar

    parser = make_parser(tmp_path)
    parser.cookiejar = QQMusicCookieJar(
        parser.cfg,
        SimpleNamespace(
            name="qqmusic",
            cookies="uin=12345; qqmusic_uin=12345; qqmusic_key=old-key; qm_keyst=old-key",
        ),
    )
    session = QQMusicSession(parser)
    session.capture_login(credential())
    saved = parser.cookiejar.cookie_file.read_bytes()

    def partial_write(jar):
        jar.cookie_file.write_bytes(b"truncated")
        raise OSError("disk full")

    async def request(*_args):
        return success()

    monkeypatch.setattr(CookieJar, "save_to_file", partial_write)
    session._request_refresh = request
    assert not run(session.refresh_if_due())
    assert parser.cookiejar.cookie_file.read_bytes() == saved
    assert parser.cookiejar.get()["qqmusic_key"] == "old-key"
    assert QQMusicSession(parser)._state["credentials"]["musickey"] == "old-key"
    assert list(tmp_path.glob(".qq-cookie-save-*")) == []


@pytest.mark.parametrize("failure_target", ["record", "cookie"])
def test_refresh_commit_failure_keeps_pair_consistent_after_restart(
    tmp_path, monkeypatch, failure_target
):
    import os

    from core.parsers.qqmusic_cookie import QQMusicCookieJar

    configured = "uin=12345; qqmusic_uin=12345; qqmusic_key=old-key; qm_keyst=old-key"
    parser = make_parser(tmp_path)
    parser.cookiejar = QQMusicCookieJar(
        parser.cfg, SimpleNamespace(name="qqmusic", cookies=configured)
    )
    session = QQMusicSession(parser)
    session.capture_login(credential())
    original = parser.cookiejar.cookie_file.read_bytes()
    replace = os.replace
    target = (
        parser.cookiejar._import_record
        if failure_target == "record"
        else parser.cookiejar.cookie_file
    )

    def fail_commit(source, destination):
        if destination == target:
            raise OSError("injected commit failure")
        return replace(source, destination)

    async def request(*_args):
        return success()

    monkeypatch.setattr("core.parsers.qqmusic_cookie.os.replace", fail_commit)
    session._request_refresh = request
    assert not run(session.refresh_if_due())
    assert parser.cookiejar.get()["qqmusic_key"] == "old-key"
    assert parser.cookiejar.cookie_file.read_bytes() == original
    parser.cookiejar = QQMusicCookieJar(
        parser.cfg, SimpleNamespace(name="qqmusic", cookies=configured)
    )
    assert parser.cookiejar.get()["qqmusic_key"] == "old-key"
    assert QQMusicSession(parser)._state["credentials"]["musickey"] == "old-key"


@pytest.mark.parametrize("oversized", [False, True])
def test_refresh_http_request_is_isolated_bounded_and_uses_existing_identity(
    tmp_path, monkeypatch, oversized
):
    import json

    parser = make_parser(tmp_path)
    parser.proxy = "http://localhost:8080"
    session = QQMusicSession(parser)
    session.capture_login(credential())
    captured = {}
    body = b"x" * 65537 if oversized else json.dumps(success()).encode()

    class Content:
        async def iter_chunked(self, size):
            for start in range(0, len(body), size):
                yield body[start : start + size]

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["request"] = kwargs
            return Response()

    monkeypatch.setattr("core.parsers.qqmusic_session.aiohttp.ClientSession", Client)
    result = run(session.refresh_if_due())
    assert result is (not oversized)
    assert captured["client"]["proxy"] == parser.proxy
    assert captured["client"]["timeout"].total == 15
    assert captured["request"]["allow_redirects"] is False
    request = captured["request"]["json"]
    assert request["req_0"]["param"]["loginMode"] == 2
    assert request["req_0"]["param"]["refresh_key"] == "secret-refresh"
    assert request["comm"]["tmeLoginType"] == 2
    assert captured["request"]["cookies"]["qqmusic_key"] == "old-key"
    assert parser.cookiejar.get()["qqmusic_key"] == (
        "old-key" if oversized else "new-key"
    )


def test_storage_symlink_is_not_followed(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    try:
        (tmp_path / "qqmusic_session.enc").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    session = QQMusicSession(make_parser(tmp_path))
    session.capture_login(credential())
    assert target.read_bytes() == b"untouched"

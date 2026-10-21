from __future__ import annotations

import asyncio
import json
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest

from core.parsers.qqmusic_login import QQMusicLogin


class _Headers(dict):
    def __init__(self, set_cookie: list[str] | None = None):
        super().__init__()
        self._set_cookie = set_cookie or []

    def getall(self, name: str, default=None):
        if name.lower() == "set-cookie":
            return list(self._set_cookie)
        return default


class _SessionCookieJar:
    def __init__(self, values: dict[str, str]):
        self.values = values

    def filter_cookies(self, _url):
        cookies = SimpleCookie()
        for name, value in self.values.items():
            cookies[name] = value
        return cookies


def _login(*, session_cookies: dict[str, str] | None = None) -> QQMusicLogin:
    session = SimpleNamespace(
        cookie_jar=_SessionCookieJar(session_cookies or {})
    )
    return QQMusicLogin(SimpleNamespace(session=session))


def _response(*set_cookie: str):
    return SimpleNamespace(cookies={}, headers=_Headers(list(set_cookie)))


@pytest.mark.parametrize("name", ["p_skey", "p-skey", "pskey", "skey"])
def test_p_skey_accepts_known_cookie_names(name: str):
    login = _login()

    cookies = login._authorization_cookies(_response(f"{name}=token; Path=/"))

    assert login._p_skey_from_cookies(cookies) == "token"


def test_authorization_cookies_include_session_cookie_jar():
    login = _login(session_cookies={"p_skey": "session-token", "uin": "12345"})

    cookies = login._authorization_cookies(_response("skey=response-token; Path=/"))

    assert cookies == {
        "p_skey": "session-token",
        "uin": "12345",
        "skey": "response-token",
    }
    assert login._p_skey_from_cookies(cookies) == "session-token"


def test_response_cookie_takes_priority_over_stale_session_cookie():
    login = _login(session_cookies={"p_skey": "stale-token"})

    cookies = login._authorization_cookies(
        _response("p_skey=fresh-token; Path=/")
    )

    assert login._p_skey_from_cookies(cookies) == "fresh-token"


def test_p_skey_returns_none_when_no_compatible_cookie_exists():
    login = _login(session_cookies={"uin": "12345"})

    cookies = login._authorization_cookies(_response())

    assert login._p_skey_from_cookies(cookies) is None


def test_credential_field_diagnostics_report_names_and_presence_without_values():
    diagnostics = QQMusicLogin._credential_field_diagnostics(
        {
            "musicid": "account-id-must-not-appear",
            "musickey": "music-key-must-not-appear",
            "refresh_key": "refresh-key-must-not-appear",
            "refresh_token": "  ",
            "openid": "open-id-must-not-appear",
            "keyExpiresIn": 259200,
            "unexpected secret value!": "must-not-appear",
        }
    )

    assert diagnostics == {
        "fields": [
            "keyExpiresIn",
            "musicid",
            "musickey",
            "openid",
            "refresh_key",
            "refresh_token",
        ],
        "refresh_fields_nonempty": {
            "refresh_key": True,
            "refresh_token": False,
            "openid": True,
            "unionid": False,
            "access_token": False,
            "musickeyCreateTime": False,
            "keyExpiresIn": True,
        },
    }
    assert "must-not-appear" not in repr(diagnostics)


def test_music_credential_exchange_logs_only_fields_and_keeps_refresh_data_out_of_cookie(
    monkeypatch,
):
    secret_values = {
        "musicid": "account-id-secret",
        "str_musicid": "account-id-secret",
        "musickey": "music-key-secret",
        "refresh_key": "refresh-key-secret",
        "refresh_token": "refresh-token-secret",
        "openid": "open-id-secret",
        "unionid": "union-id-secret",
        "access_token": "access-token-secret",
        "musickeyCreateTime": 1_800_000_000,
        "keyExpiresIn": 259200,
    }
    body = json.dumps({"req_0": {"code": 0, "data": secret_values}})

    class _Response:
        status = 200
        headers = {}

        async def text(self):
            return body

    class _RequestContext:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def post(self, *_args, **_kwargs):
            return _RequestContext()

    class _CookieJar:
        saved = ""

        def replace_from_cookies_str(self, value):
            self.saved = value

    cookiejar = _CookieJar()
    login = QQMusicLogin(
        SimpleNamespace(session=_Session(), cookiejar=cookiejar)
    )
    log_messages: list[str] = []
    monkeypatch.setattr("core.parsers.qqmusic_login.logger.info", log_messages.append)

    credentials = asyncio.run(login._get_music_credential("authorization-code"))

    assert credentials == {
        "uin": "account-id-secret",
        "qqmusic_uin": "account-id-secret",
        "qqmusic_key": "music-key-secret",
        "qm_keyst": "music-key-secret",
    }
    assert cookiejar.saved == (
        "uin=account-id-secret; qqmusic_uin=account-id-secret; "
        "qqmusic_key=music-key-secret; qm_keyst=music-key-secret"
    )
    assert len(log_messages) == 1
    assert "refresh_key" in log_messages[0]
    assert "refresh-key-secret" not in log_messages[0]
    assert "musickeyCreateTime" in log_messages[0]
    assert all(value not in log_messages[0] for value in secret_values.values() if isinstance(value, str))


@pytest.mark.parametrize(
    ("check_result", "expected_message"),
    [
        (True, "QQ 音乐登录成功，Cookie 已保存并通过有效性检验"),
        (False, "QQ 音乐登录凭据已保存，但有效性检验未通过，请重新扫码登录"),
        (None, "QQ 音乐登录成功，Cookie 已保存；暂时无法确认有效性，请稍后检查"),
    ],
)
def test_qq_qr_login_checks_saved_cookie_and_reports_result(
    monkeypatch, check_result, expected_message
):
    login = _login()
    calls = {"exchange": 0, "check": 0}

    async def poll():
        return "0", "user-id", "signature"

    async def exchange(_uin, _signature):
        calls["exchange"] += 1
        return {}

    async def check_cookie():
        calls["check"] += 1
        return check_result

    login.parser.check_cookie = check_cookie
    monkeypatch.setattr(login, "_poll_qrcode", poll)
    monkeypatch.setattr(login, "_exchange_authorization", exchange)
    monkeypatch.setattr("core.parsers.qqmusic_login.asyncio.sleep", _no_sleep)

    messages = asyncio.run(_collect_messages(login.check_qr_state()))

    assert calls == {"exchange": 1, "check": 1}
    assert messages == [expected_message]


def test_qq_qr_login_treats_cookie_check_exception_as_unknown(monkeypatch):
    login = _login()
    calls = {"exchange": 0, "check": 0}

    async def poll():
        return "0", "user-id", "signature"

    async def exchange(_uin, _signature):
        calls["exchange"] += 1
        return {}

    async def check_cookie():
        calls["check"] += 1
        raise TimeoutError("private diagnostic")

    login.parser.check_cookie = check_cookie
    monkeypatch.setattr(login, "_poll_qrcode", poll)
    monkeypatch.setattr(login, "_exchange_authorization", exchange)
    monkeypatch.setattr("core.parsers.qqmusic_login.asyncio.sleep", _no_sleep)

    messages = asyncio.run(_collect_messages(login.check_qr_state()))

    assert calls == {"exchange": 1, "check": 1}
    assert messages == [
        "QQ 音乐登录成功，Cookie 已保存；暂时无法确认有效性，请稍后检查"
    ]


async def _collect_messages(generator):
    return [message async for message in generator]


async def _no_sleep(_seconds):
    return None

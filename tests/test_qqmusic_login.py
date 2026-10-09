from __future__ import annotations

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

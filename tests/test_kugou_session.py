import asyncio
from types import SimpleNamespace

import pytest

from core.cookie import Cookie
from core.parsers.kugou_session import KugouCookieJar, KugouSession
from core.parsers.music import KugouMusicParser


def parser_at(tmp_path):
    cfg = SimpleNamespace(cookie_dir=tmp_path)
    jar = KugouCookieJar(
        cfg,
        SimpleNamespace(
            name="kugou", cookies="KugooID=123; t=old; kg_mid=mid; kg_dfid=dfid"
        ),
    )
    return SimpleNamespace(
        cookiejar=jar,
        _WEB_PLAY_URL=KugouMusicParser._WEB_PLAY_URL,
        _kugou_account_credentials=KugouMusicParser._kugou_account_credentials,
    )


def candidates():
    return [
        Cookie(".kugou.com", "/", name, value, True, 0)
        for name, value in {
            "KugooID": "456",
            "t": "new",
            "kg_mid": "mid",
            "kg_dfid": "dfid",
        }.items()
    ]


@pytest.mark.parametrize("compound", ["KugooID=789&t=another", "KugooID=456", "t=new"])
def test_conflicting_or_incomplete_compound_is_not_committed(tmp_path, compound):
    from core.parsers.kugou_quality import session_fields

    parser = parser_at(tmp_path)
    session = KugouSession(parser)
    ticket = session.begin()
    candidate = candidates() + [Cookie(".kugou.com", "/", "KuGoo", compound, True, 0)]

    async def validate(_cookie):
        pytest.fail("conflicting candidates must not reach network validation")

    session._validate = validate
    with pytest.raises(RuntimeError):
        asyncio.run(session.commit(candidate, ticket))
    header = "; ".join(f"{c.name}={c.value}" for c in candidate)
    assert session_fields(header) is None
    assert parser.cookiejar.get()["t"] == "old"


@pytest.mark.parametrize("valid", [True, False, None])
def test_candidate_validation_preserves_previous_on_failure(tmp_path, valid):
    parser = parser_at(tmp_path)
    session = KugouSession(parser)
    ticket = session.begin()
    saved = parser.cookiejar.cookie_file.read_bytes()

    async def validate(cookie):
        assert "new" in cookie
        assert parser.cookiejar.get()["t"] == "old"
        return valid

    session._validate = validate
    if valid:
        asyncio.run(session.commit(candidates(), ticket))
        assert parser.cookiejar.get()["t"] == "new"
        restored = parser_at(tmp_path)
        assert restored.cookiejar.get()["t"] == "new"
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(session.commit(candidates(), ticket))
        assert parser.cookiejar.get()["t"] == "old"
        assert parser.cookiejar.cookie_file.read_bytes() == saved


@pytest.mark.parametrize("change", ["retire", "new_login", "manual_file", "memory"])
def test_inflight_login_cannot_overwrite_changed_account(tmp_path, change):
    async def run():
        parser = parser_at(tmp_path)
        session = KugouSession(parser)
        ticket = session.begin()

        async def validate(_cookie):
            if change == "retire":
                session.stop()
            elif change == "new_login":
                session.begin()
            elif change == "manual_file":
                parser.cookiejar.cookie_file.write_bytes(b"changed externally")
            else:
                parser.cookiejar.replace_from_cookies_str("KugooID=789; t=manual")
            return True

        session._validate = validate
        with pytest.raises(RuntimeError):
            await session.commit(candidates(), ticket)
        assert parser.cookiejar.get()["t"] != "new"

    asyncio.run(run())


def test_commit_failure_rolls_back_and_preserves_scopes(tmp_path, monkeypatch):
    parser = parser_at(tmp_path)
    session = KugouSession(parser)
    ticket = session.begin()

    async def validate(_cookie):
        return True

    def fail():
        raise OSError("private")

    session._validate = validate
    monkeypatch.setattr(parser.cookiejar, "save_to_file", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(session.commit(candidates(), ticket))
    assert parser.cookiejar.get()["t"] == "old"


def test_browser_candidate_scopes_are_not_flattened(tmp_path):
    parser = parser_at(tmp_path)
    session = KugouSession(parser)
    ticket = session.begin()
    extra = Cookie(
        "login-user.kugou.com", "/login", "vip_token", "scoped", True, 2000000000
    )

    async def validate(_cookie):
        return True

    session._validate = validate
    asyncio.run(session.commit(candidates() + [extra], ticket))
    assert any(c == extra for c in parser.cookiejar.cookies)
    assert "scoped" not in parser.cookiejar.get_cookie_header_for_url(
        parser._WEB_PLAY_URL
    )

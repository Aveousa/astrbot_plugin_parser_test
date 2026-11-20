from __future__ import annotations

import asyncio
import hashlib
import json
from http.cookies import SimpleCookie

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from core.parsers.kugou_login import KugouMusicLogin


class _Headers(dict):
    def __init__(self, set_cookie: list[str] | None = None):
        super().__init__()
        self._set_cookie = set_cookie or []

    def getall(self, name: str, default=None):
        if name.lower() == "set-cookie":
            return list(self._set_cookie)
        return default


class _Response:
    status = 200

    def __init__(self, payload: dict, *, set_cookie: list[str] | None = None):
        self._payload = payload
        self.headers = _Headers(set_cookie)
        self.cookies: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return json.dumps(self._payload)

    async def read(self):
        return b"qr-image"


class _Session:
    cookie_jar = None

    def __init__(self, *, get_responses: list[_Response], post_response: _Response):
        self.get_responses = list(get_responses)
        self.post_response = post_response
        self.get_calls: list[tuple[str, dict, dict]] = []
        self.post_calls: list[tuple[str, dict, dict]] = []

    def get(self, url: str, *, params=None, headers=None, allow_redirects=False):
        self.get_calls.append((url, params or {}, headers or {}))
        return self.get_responses.pop(0)

    def post(self, url: str, *, params=None, headers=None, data=None, allow_redirects=False):
        self.post_calls.append((url, params or {}, headers or {}))
        return self.post_response


class _CookieJar:
    def __init__(self):
        self.cookies_str = ""

    def replace_from_cookies_str(self, value: str):
        self.cookies_str = value


class _Parser:
    _KUGOU_SECRET = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"

    def __init__(self, session: _Session):
        self.session = session
        self.cookiejar = _CookieJar()

    @classmethod
    def _signature(cls, params, body=""):
        ordered = "".join(f"{key}={params[key]}" for key in sorted(params))
        raw = f"{cls._KUGOU_SECRET}{ordered}{body}{cls._KUGOU_SECRET}"
        return hashlib.md5(raw.encode()).hexdigest()

    @classmethod
    def _signed_params(cls, extra=None):
        now = "1780000000000"
        params = {
            "srcappid": "2919",
            "clientver": "20000",
            "clienttime": now,
            "mid": now,
            "uuid": now,
            "dfid": "-",
        }
        params.update({key: str(value) for key, value in (extra or {}).items()})
        params["signature"] = cls._signature(params)
        return params


def _response(status: str, data: dict, *, set_cookie: list[str] | None = None):
    return _Response({"status": status, "data": data}, set_cookie=set_cookie)


def test_kugou_aes_token_encryption_matches_web_format():
    key, encrypted = KugouMusicLogin._aes_encrypt_token("secret-token", "0123456789ABCDEF")
    key_digest = hashlib.md5(key.encode("ascii")).hexdigest()

    decryptor = Cipher(
        algorithms.AES(key_digest.encode("ascii")),
        modes.CBC(key_digest[-16:].encode("ascii")),
    ).decryptor()
    padded = decryptor.update(bytes.fromhex(encrypted)) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    assert json.loads(plaintext) == {"token": "secret-token"}
    assert encrypted == "c2700fbdc232e744c23768c2e5f80ff594b5996e084cce1fbb8e1519b184d0f2"


def test_kugou_rsa_uses_web_clients_no_padding_byte_layout():
    plaintext = b'{"clienttime_ms":1,"key":"0123456789ABCDEF"}'
    encrypted = KugouMusicLogin._rsa_encrypt_no_padding(plaintext)
    expected_input = int.from_bytes(
        plaintext + bytes(KugouMusicLogin._RSA_BLOCK_SIZE - len(plaintext)), "big"
    )

    assert int(encrypted, 16) == pow(
        expected_input,
        KugouMusicLogin._RSA_PUBLIC_EXPONENT,
        KugouMusicLogin._RSA_PUBLIC_MODULUS,
    )
    assert len(encrypted) % 4 == 0


def test_kugou_qr_login_persists_exchange_cookies(monkeypatch):
    session = _Session(
        get_responses=[
            _response("1", {"qrcode": "one-time-qr-token"}),
            _response("1", {"status": 2, "nickname": "tester"}),
            _response("1", {"status": 4, "userid": 12345, "token": "authorized-token"}),
        ],
        post_response=_response(
            "1",
            {},
            set_cookie=[
                "KuGoo=KugooID%3D12345; Domain=.kugou.com; Path=/; Secure",
                "kg_mid=music-mid; Domain=.kugou.com; Path=/; Secure",
            ],
        ),
    )
    parser = _Parser(session)
    validity_checks = []

    async def check_cookie():
        validity_checks.append("checked")
        return True

    parser.check_cookie = check_cookie
    login = KugouMusicLogin(parser)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("core.parsers.kugou_login.asyncio.sleep", no_sleep)

    async def run_login():
        image = await login.login_with_qrcode()
        messages = [message async for message in login.check_qr_state()]
        return image, messages

    image, messages = asyncio.run(run_login())

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert messages == [
        "二维码已扫描，请在手机上确认登录",
        "酷狗音乐登录成功，Cookie 已保存并通过有效性检验",
    ]
    assert validity_checks == ["checked"]
    assert "KuGoo=KugooID%3D12345" in parser.cookiejar.cookies_str
    assert "kg_mid=music-mid" in parser.cookiejar.cookies_str
    assert session.post_calls
    exchange_params = session.post_calls[0][1]
    assert exchange_params["userid"] == "12345"
    assert exchange_params["dev"] == "web"
    assert exchange_params["plat"] == "4"
    assert exchange_params["params"]
    assert exchange_params["pk"]
    unsigned_params = dict(exchange_params)
    signature = unsigned_params.pop("signature")
    assert signature == _Parser._signature(unsigned_params)


def test_kugou_qr_login_reports_cookie_check_failure_as_unknown(monkeypatch):
    session = _Session(
        get_responses=[
            _response("1", {"qrcode": "one-time-qr-token"}),
            _response("1", {"status": 4, "userid": 12345, "token": "authorized-token"}),
        ],
        post_response=_response(
            "1",
            {},
            set_cookie=[
                "KuGoo=KugooID%3D12345; Domain=.kugou.com; Path=/; Secure",
            ],
        ),
    )
    parser = _Parser(session)
    validity_checks = []

    async def check_cookie():
        validity_checks.append("checked")
        raise TimeoutError("private diagnostic")

    parser.check_cookie = check_cookie
    login = KugouMusicLogin(parser)
    monkeypatch.setattr("core.parsers.kugou_login.asyncio.sleep", _no_sleep)

    messages = asyncio.run(_collect_login_messages(login.check_qr_state(), login))

    assert messages == [
        "酷狗音乐登录成功，Cookie 已保存；暂时无法确认有效性，请稍后检查"
    ]
    assert validity_checks == ["checked"]
    assert "KuGoo=KugooID%3D12345" in parser.cookiejar.cookies_str


def test_retired_login_cannot_recreate_http_or_poll():
    from types import SimpleNamespace
    import pytest

    login = KugouMusicLogin(SimpleNamespace(kugou_session=SimpleNamespace(stop=lambda: None)))
    login.stop()
    with pytest.raises(RuntimeError, match="已关闭"):
        _ = login.session

    async def messages():
        return [m async for m in login.check_qr_state()]

    assert asyncio.run(messages()) == ["酷狗解析器已重载，请重新登录"]
    assert login._http is None


def test_retirement_cancels_sleeping_login_and_closes_resources():
    from types import SimpleNamespace

    async def run():
        login = KugouMusicLogin(SimpleNamespace(kugou_session=SimpleNamespace(
            begin=lambda: (1, "snapshot"), stop=lambda: None,
        )))
        entered = asyncio.Event()
        closed = []

        class Http:
            closed = False

            async def close(self):
                self.closed = True
                closed.append(True)

        async def flow():
            await login.begin_login()
            login._http = Http()
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await login.end_login()

        task = asyncio.create_task(flow())
        await entered.wait()
        await login.close()
        assert task.cancelled()
        assert closed == [True]
        assert not login._flow_lock.locked()
        assert login._flow_task is None

    asyncio.run(run())


def test_production_api_login_validates_before_persisting(tmp_path):
    import aiohttp
    from types import SimpleNamespace
    from yarl import URL
    from core.parsers.kugou_session import KugouCookieJar, KugouSession
    from core.parsers.music import KugouMusicParser

    async def run():
        jar = KugouCookieJar(SimpleNamespace(cookie_dir=tmp_path), SimpleNamespace(name="kugou", cookies="KugooID=123; t=old"))
        parser = SimpleNamespace(cookiejar=jar, proxy=None,
            _WEB_PLAY_URL=KugouMusicParser._WEB_PLAY_URL,
            _kugou_account_credentials=KugouMusicParser._kugou_account_credentials,
            _signed_params=KugouMusicParser._signed_params)
        parser.kugou_session = KugouSession(parser)
        seen = []

        async def validate(cookie):
            seen.append(cookie)
            assert jar.get()["t"] == "old"
            return True

        parser.kugou_session._validate = validate
        login = KugouMusicLogin(parser)
        await login.begin_login()
        session_jar = aiohttp.CookieJar()
        cookies = SimpleCookie()
        for name, value in {"KuGoo": "KugooID=456&t=new", "kg_mid": "mid", "kg_dfid": "dfid"}.items():
            cookies[name] = value
            cookies[name]["domain"] = ".kugou.com"
            cookies[name]["path"] = "/"
        session_jar.update_cookies(cookies, URL("https://loginservice.kugou.com/"))
        fake = _Session(get_responses=[], post_response=_response("1", {}))
        fake.cookie_jar = session_jar
        fake.closed = True
        # Keep the test double open during exchange and restore before cleanup.
        fake.closed = False
        login._http = fake
        try:
            await login._exchange_authorization("456", "qr-only")
            assert seen and jar.get()["KuGoo"] == "KugooID=456&t=new"
            assert all(c.domain == ".kugou.com" for c in jar.cookies)
        finally:
            login._http = None
            await login.end_login()

    asyncio.run(run())


async def _collect_login_messages(generator, login):
    await login.login_with_qrcode()
    return [message async for message in generator]


async def _no_sleep(_seconds):
    return None

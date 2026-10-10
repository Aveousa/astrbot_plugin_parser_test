from __future__ import annotations

import asyncio
import hashlib
import json

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

    def get(self, url: str, *, params=None, headers=None):
        self.get_calls.append((url, params or {}, headers or {}))
        return self.get_responses.pop(0)

    def post(self, url: str, *, params=None, headers=None, data=None):
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
        "酷狗音乐登录成功，Cookie 已保存",
    ]
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

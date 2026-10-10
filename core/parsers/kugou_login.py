from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import re
import secrets
import time
from collections.abc import AsyncGenerator, Mapping
from http.cookies import SimpleCookie
from urllib.parse import quote, urlencode
from uuid import uuid4

from aiohttp import ClientError
from astrbot.api import logger
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from yarl import URL

from ..cookie import CookieJar


class KugouMusicLogin:
    """Kugou Music QR login, including the web client token exchange."""

    _QR_URL = "https://login-user.kugou.com/v2/qrcode"
    _POLL_URL = "https://login-user.kugou.com/v2/get_userinfo_qrcode"
    _EXCHANGE_URL = "https://loginservice.kugou.com/v1/login_by_token_get"
    _QR_CODE_URL = "https://h5.kugou.com/apps/loginQRCode/html/index.html"
    _APP_ID = "1058"
    _QR_CLIENT_VERSION = "8131"
    _LOGIN_CLIENT_VERSION = "1000"
    _PLATFORM = "4"
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    _LOGIN_REFERER = "https://login-user.kugou.com/login/?appid=1058"
    _RSA_PUBLIC_MODULUS = int(
        "B1B1EC76A1BBDBF0D18E8CD9A87E53FA3881E2F004C67C9DDA2CA677DBEFA3D"
        "61DF8463FE12D84FF4B4699E02C9D41CAB917F5A8FB9E35580C4BDF97763A042"
        "0A476295D763EE10174E6F9EBF7DF8A77BA5B20CDA4EE705DEF5BBA3C88567B"
        "9656E52C9CD5CD95CA735FF2D25F762B133273EEEB7B4F3EA8B6DA29040F3B67CD",
        16,
    )
    _RSA_PUBLIC_EXPONENT = 65537
    _RSA_BLOCK_SIZE = 128
    _KEY_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, parser):
        self.parser = parser
        self._qrcode: str | None = None
        self._mid = hashlib.md5(uuid4().bytes).hexdigest()

    def _signed_params(self, extra: Mapping[str, object]) -> dict[str, str]:
        now_ms = int(time.time() * 1000)
        params = self.parser._signed_params(
            {
                "appid": self._APP_ID,
                "clientver": self._QR_CLIENT_VERSION,
                "clienttime": now_ms,
                "mid": self._mid,
                "uuid": self._mid,
                "dfid": "-",
                "plat": self._PLATFORM,
                **extra,
            }
        )
        return {str(name): str(value) for name, value in params.items()}

    @staticmethod
    def _ensure_success(response) -> None:
        status = getattr(response, "status", 200)
        if isinstance(status, int) and status >= 400:
            raise ClientError(f"酷狗登录接口返回 HTTP {status}")

    @staticmethod
    async def _response_json(response) -> Mapping[str, object]:
        try:
            payload = json.loads(await response.text())
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("酷狗登录接口返回了无法识别的数据") from exc
        if not isinstance(payload, Mapping):
            raise TypeError("酷狗登录接口返回的数据格式无效")
        return payload

    async def login_with_qrcode(self) -> bytes:
        """Request a QR image and remember the one-time QR token."""

        self._qrcode = None
        qrcode_text = quote(f"{self._QR_CODE_URL}?appid={self._APP_ID}&", safe="-_.!~*'()")
        params = self._signed_params({"type": "1", "qrcode_txt": qrcode_text})
        async with self.parser.session.get(
            self._QR_URL,
            params=params,
            headers={"User-Agent": self._USER_AGENT, "Referer": self._LOGIN_REFERER},
        ) as response:
            self._ensure_success(response)
            payload = await self._response_json(response)

        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        if str(payload.get("status")) != "1":
            raise RuntimeError(f"酷狗登录二维码获取失败 (code={payload.get('error_code')})")
        qrcode = str(data.get("qrcode") or "").strip()
        if not qrcode:
            raise RuntimeError("酷狗登录接口没有返回二维码标识")

        image_data = str(data.get("qrcode_img") or "").strip()
        image_match = re.match(r"^data:image/[^;,]+;base64,(.*)$", image_data, re.DOTALL)
        if image_match:
            try:
                image = base64.b64decode(image_match.group(1), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise RuntimeError("酷狗登录二维码图片格式无效") from exc
        elif image_data.startswith(("https://", "http://")):
            async with self.parser.session.get(
                image_data,
                headers={"User-Agent": self._USER_AGENT, "Referer": self._LOGIN_REFERER},
            ) as response:
                self._ensure_success(response)
                image = await response.read()
        elif image_data:
            try:
                image = base64.b64decode(image_data, validate=True)
            except (ValueError, binascii.Error):
                image = self._render_qrcode_png(qrcode)
        else:
            # The current web endpoint often returns only the QR token. The
            # official login page builds the QR locally from qrcode_txt and
            # that token, so do the same when qrcode_img is omitted.
            image = self._render_qrcode_png(qrcode)

        if not image:
            raise RuntimeError("酷狗登录二维码图片为空")
        self._qrcode = qrcode
        return image

    @classmethod
    def _render_qrcode_png(cls, qrcode: str) -> bytes:
        try:
            import qrcode as qrcode_lib
            from qrcode.constants import ERROR_CORRECT_L
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError("缺少酷狗登录二维码生成依赖") from exc

        target = f"{cls._QR_CODE_URL}?{urlencode({'appid': cls._APP_ID, 'qrcode': qrcode})}"
        qr = qrcode_lib.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_L,
            box_size=6,
            border=4,
        )
        qr.add_data(target)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#000000", back_color="#ffffff")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    async def _poll_qrcode(self) -> tuple[str, str | None, str | None, Mapping[str, object]]:
        if not self._qrcode:
            raise RuntimeError("请先生成酷狗音乐登录二维码")
        params = self._signed_params({"qrcode": self._qrcode})
        async with self.parser.session.get(
            self._POLL_URL,
            params=params,
            headers={"User-Agent": self._USER_AGENT, "Referer": self._LOGIN_REFERER},
        ) as response:
            self._ensure_success(response)
            payload = await self._response_json(response)

        if str(payload.get("status")) != "1":
            raise RuntimeError(f"酷狗登录状态查询失败 (code={payload.get('error_code')})")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise TypeError("酷狗登录状态响应格式无效")
        state = str(data.get("status") or "")
        if state == "4":
            user_id = str(data.get("userid") or data.get("user_id") or "").strip()
            token = str(data.get("token") or "").strip()
            if not user_id or not token:
                raise RuntimeError("酷狗扫码登录成功，但响应缺少 userid 或 token")
            return state, user_id, token, data
        return state, None, None, data

    @classmethod
    def _aes_encrypt_token(cls, token: str, key: str | None = None) -> tuple[str, str]:
        """Match Kugou's AES-CBC/PKCS7 token wrapper (key is also the IV)."""

        key = key or "".join(secrets.choice(cls._KEY_ALPHABET) for _ in range(16))
        key_bytes = key.encode("ascii")
        if len(key_bytes) != 16:
            raise ValueError("酷狗登录 AES key 必须是 16 个 ASCII 字符")
        plaintext = json.dumps({"token": token}, separators=(",", ":")).encode("utf-8")
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(key_bytes)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return key, ciphertext.hex()

    @classmethod
    def _rsa_encrypt_no_padding(cls, plaintext: bytes) -> str:
        """Replicate Kugou's JS RSA NoPadding byte layout and hex encoding."""

        if len(plaintext) >= cls._RSA_BLOCK_SIZE:
            raise ValueError("酷狗登录 RSA 参数超出支持长度")
        # The web implementation writes the input bytes backwards into a
        # little-endian byte array, then interprets the result as a big-endian
        # integer. This is equivalent to appending zero bytes to the plaintext.
        block = plaintext + bytes(cls._RSA_BLOCK_SIZE - len(plaintext))
        encrypted = pow(
            int.from_bytes(block, "big"),
            cls._RSA_PUBLIC_EXPONENT,
            cls._RSA_PUBLIC_MODULUS,
        )
        hexadecimal = format(encrypted, "x")
        return hexadecimal.zfill((len(hexadecimal) + 3) // 4 * 4)

    @classmethod
    def _rsa_payload(cls, clienttime_ms: int, key: str) -> str:
        payload = json.dumps(
            {"clienttime_ms": clienttime_ms, "key": key}, separators=(",", ":")
        ).encode("ascii")
        return cls._rsa_encrypt_no_padding(payload)

    @classmethod
    def _response_cookies(cls, response) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for name, morsel in (getattr(response, "cookies", {}) or {}).items():
            value = str(getattr(morsel, "value", morsel)).strip()
            if value:
                cookies[str(name)] = value
        headers = getattr(response, "headers", {})
        getall = getattr(headers, "getall", None)
        raw_headers = getall("Set-Cookie", []) if callable(getall) else []
        for header in raw_headers:
            parsed = SimpleCookie()
            parsed.load(header)
            cookies.update(
                {
                    name: morsel.value
                    for name, morsel in parsed.items()
                    if morsel.value
                }
            )
        return cookies

    def _collect_login_cookies(self, response) -> dict[str, str]:
        cookies: dict[str, str] = {}
        cookie_jar = getattr(self.parser.session, "cookie_jar", None)
        filter_cookies = getattr(cookie_jar, "filter_cookies", None)
        if callable(filter_cookies):
            for host in (
                "https://loginservice.kugou.com/",
                "https://login-user.kugou.com/",
                "https://www.kugou.com/",
                "https://m.kugou.com/",
            ):
                try:
                    stored = filter_cookies(URL(host))
                except (TypeError, ValueError):
                    continue
                for name, morsel in (stored or {}).items():
                    value = str(getattr(morsel, "value", morsel)).strip()
                    if value:
                        cookies[str(name)] = value
        # The current exchange response takes priority over any older values
        # that the shared session may already contain.
        cookies.update(self._response_cookies(response))
        return cookies

    async def _exchange_authorization(self, user_id: str, token: str) -> dict[str, str]:
        now_ms = int(time.time() * 1000)
        aes_key, encrypted_token = self._aes_encrypt_token(token)
        rsa_payload = self._rsa_payload(now_ms, aes_key)
        params = self._signed_params(
            {
                "clientver": self._LOGIN_CLIENT_VERSION,
                "clienttime": now_ms // 1000,
                "dev": "web",
                "userid": user_id,
                "plat": self._PLATFORM,
                "clienttime_ms": now_ms,
                "pk": rsa_payload,
                "params": encrypted_token,
            }
        )
        async with self.parser.session.post(
            self._EXCHANGE_URL,
            params=params,
            headers={
                "User-Agent": self._USER_AGENT,
                "Referer": self._LOGIN_REFERER,
                "Origin": "https://login-user.kugou.com",
            },
            data=None,
        ) as response:
            self._ensure_success(response)
            result = await self._response_json(response)
            cookies = self._collect_login_cookies(response)

        if str(result.get("status")) != "1":
            raise RuntimeError(
                f"酷狗登录凭据获取失败 (code={result.get('error_code')})"
            )
        if not cookies:
            raise RuntimeError("酷狗登录接口未返回可保存的 Cookie")
        cookiejar: CookieJar | None = getattr(self.parser, "cookiejar", None)
        if cookiejar is None:
            raise RuntimeError("酷狗音乐 Cookie 存储未初始化")
        cookiejar.replace_from_cookies_str(
            "; ".join(f"{name}={value}" for name, value in cookies.items())
        )
        return cookies

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """Poll the QR state and persist the authorization cookies on success."""

        scanned_tip_pending = True
        for _ in range(90):
            try:
                state, user_id, token, _data = await self._poll_qrcode()
            except (ClientError, TimeoutError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(f"[酷狗音乐] 登录状态查询失败: {exc}")
                yield "酷狗音乐登录状态查询失败，请重新生成二维码"
                return

            if state == "4":
                try:
                    await self._exchange_authorization(user_id or "", token or "")
                except (
                    ClientError,
                    TimeoutError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    OSError,
                ) as exc:
                    logger.warning(f"[酷狗音乐] 登录凭据获取失败: {exc}")
                    yield f"酷狗音乐登录失败：{exc}"
                    return
                yield "酷狗音乐登录成功，Cookie 已保存"
                return
            if state == "2" and scanned_tip_pending:
                yield "二维码已扫描，请在手机上确认登录"
                scanned_tip_pending = False
            elif state == "0":
                yield "酷狗音乐二维码已过期，请重新生成"
                return
            await asyncio.sleep(2)
        yield "酷狗音乐二维码登录超时，请重新生成"

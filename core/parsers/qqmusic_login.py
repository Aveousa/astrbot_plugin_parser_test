from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncGenerator, Mapping
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from aiohttp import ClientError
from astrbot.api import logger

from ..cookie import CookieJar


class QQMusicLogin:
    """QQ Music QR login, following the same command flow as BilibiliLogin."""

    _QR_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
    _POLL_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
    _CHECK_SIG_URL = "https://ssl.ptlogin2.graph.qq.com/check_sig"
    _OAUTH_URL = "https://graph.qq.com/oauth2.0/authorize"
    _CGI_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    _APP_ID = "716027609"
    _THIRD_PARTY_APP_ID = "100497308"
    _DAID = "383"
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    _REFERER = "https://y.qq.com/"
    _STATUS_RE = re.compile(r"ptuiCB\((.*?)\)")
    _ARGS_RE = re.compile(r"'((?:\\.|[^'])*)'")
    _SIGN_PART_1 = (23, 14, 6, 36, 16, 7, 19)
    _SIGN_PART_2 = (16, 1, 32, 12, 19, 27, 8, 5)
    _SIGN_SCRAMBLE = (
        89, 39, 179, 150, 218, 82, 58, 252, 177, 52,
        186, 123, 120, 64, 242, 133, 143, 161, 121, 179,
    )

    def __init__(self, parser):
        self.parser = parser
        self._qrsig: str | None = None

    @staticmethod
    def _hash33(value: str, initial: int = 0) -> int:
        for char in value:
            initial = (initial << 5) + initial + ord(char)
        return initial & 2_147_483_647

    @classmethod
    def _zzc_sign(cls, payload: str) -> str:
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest().upper()
        first = "".join(digest[index] for index in cls._SIGN_PART_1)
        second = "".join(digest[index] for index in cls._SIGN_PART_2)
        masked = bytes(
            scramble ^ int(digest[index * 2 : index * 2 + 2], 16)
            for index, scramble in enumerate(cls._SIGN_SCRAMBLE)
        )
        middle = base64.b64encode(masked).decode("ascii").translate(
            str.maketrans("", "", "/+=")
        )
        return f"zzc{first}{middle}{second}".lower()

    @staticmethod
    def _response_cookies(response) -> dict[str, str]:
        cookies: dict[str, str] = {}
        response_cookies = getattr(response, "cookies", {}) or {}
        for name, morsel in response_cookies.items():
            cookies[name] = str(getattr(morsel, "value", morsel))
        # Some aiohttp-compatible test doubles expose only raw Set-Cookie headers.
        headers = getattr(response, "headers", {})
        raw_headers = getattr(headers, "getall", lambda *_args, **_kwargs: [])(
            "Set-Cookie", []
        )
        for header in raw_headers:
            parsed = SimpleCookie()
            parsed.load(header)
            cookies.update({name: morsel.value for name, morsel in parsed.items()})
        return cookies

    @staticmethod
    def _ensure_success(response) -> None:
        status = getattr(response, "status", 200)
        if isinstance(status, int) and status >= 400:
            raise ClientError(f"QQ 音乐登录接口返回 HTTP {status}")

    async def login_with_qrcode(self) -> bytes:
        """Generate a QQ authorization QR image."""
        self._qrsig = None
        params = {
            "appid": self._APP_ID,
            "e": "2",
            "l": "M",
            "s": "3",
            "d": "72",
            "v": "4",
            "t": str(time.time()),
            "daid": self._DAID,
            "pt_3rd_aid": self._THIRD_PARTY_APP_ID,
        }
        async with self.parser.session.get(
            self._QR_URL,
            params=params,
            headers={"User-Agent": self._USER_AGENT, "Referer": "https://xui.ptlogin2.qq.com/"},
            cookies={},
        ) as response:
            self._ensure_success(response)
            cookies = self._response_cookies(response)
            self._qrsig = cookies.get("qrsig")
            image = await response.read()
        if not self._qrsig or not image:
            raise RuntimeError("QQ 音乐二维码获取失败")
        return image

    async def _poll_qrcode(self) -> tuple[str, str | None, str | None]:
        qrsig = self._qrsig
        if not qrsig:
            raise RuntimeError("请先生成 QQ 音乐登录二维码")
        params = {
            "u1": "https://graph.qq.com/oauth2.0/login_jump",
            "ptqrtoken": str(self._hash33(qrsig)),
            "ptredirect": "0",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": "20102616",
            "js_type": "1",
            "pt_uistyle": "40",
            "aid": self._APP_ID,
            "daid": self._DAID,
            "pt_3rd_aid": self._THIRD_PARTY_APP_ID,
            "has_onekey": "1",
        }
        async with self.parser.session.get(
            self._POLL_URL,
            params=params,
            headers={"User-Agent": self._USER_AGENT, "Referer": "https://xui.ptlogin2.qq.com/"},
            cookies={"qrsig": qrsig},
        ) as response:
            self._ensure_success(response)
            body = await response.text()

        match = self._STATUS_RE.search(body)
        if not match:
            raise RuntimeError("QQ 音乐登录状态响应无法解析")
        args = self._ARGS_RE.findall(match.group(1))
        if not args or not args[0].isdigit():
            raise RuntimeError("QQ 音乐登录状态响应无效")
        status = args[0]
        if status != "0":
            return status, None, None
        if len(args) < 3:
            raise RuntimeError("QQ 音乐登录响应缺少授权信息")
        query = parse_qs(urlparse(args[2]).query)
        uin = next(iter(query.get("uin", ())), None)
        sigx = next(iter(query.get("ptsigx", ())), None)
        if not uin or not sigx:
            raise RuntimeError("无法解析 QQ 音乐授权参数")
        return status, uin, sigx

    async def _exchange_authorization(self, uin: str, sigx: str) -> dict[str, str]:
        headers = {"User-Agent": self._USER_AGENT, "Referer": "https://xui.ptlogin2.qq.com/"}
        check_params = {
            "uin": uin,
            "pttype": "1",
            "service": "ptqrlogin",
            "nodirect": "0",
            "ptsigx": sigx,
            "s_url": "https://graph.qq.com/oauth2.0/login_jump",
            "ptlang": "2052",
            "ptredirect": "100",
            "aid": self._APP_ID,
            "daid": self._DAID,
            "j_later": "0",
            "low_login_hour": "0",
            "regmaster": "0",
            "pt_login_type": "3",
            "pt_aid": "0",
            "pt_aaid": "16",
            "pt_light": "0",
            "pt_3rd_aid": self._THIRD_PARTY_APP_ID,
        }
        async with self.parser.session.get(
            self._CHECK_SIG_URL,
            params=check_params,
            headers=headers,
            cookies={},
            allow_redirects=False,
        ) as response:
            self._ensure_success(response)
            cookies = self._response_cookies(response)
        p_skey = cookies.get("p_skey")
        if not p_skey:
            raise RuntimeError("QQ 授权未返回 p_skey")

        oauth_params = {
            "response_type": "code",
            "client_id": self._THIRD_PARTY_APP_ID,
            "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
            "scope": "get_user_info,get_app_friends",
            "state": "state",
            "switch": "",
            "from_ptlogin": "1",
            "src": "1",
            "update_auth": "1",
            "openapi": "1010_1030",
            "g_tk": str(self._hash33(p_skey, 5381)),
            "auth_time": str(int(time.time()) * 1000),
            "ui": str(uuid4()),
        }
        async with self.parser.session.post(
            self._OAUTH_URL,
            data=oauth_params,
            cookies=cookies,
            headers={"User-Agent": self._USER_AGENT, "Referer": "https://graph.qq.com/"},
            allow_redirects=False,
        ) as response:
            self._ensure_success(response)
            location = response.headers.get("Location", "")
        code = next(iter(parse_qs(urlparse(location).query).get("code", ())), None)
        if not code:
            raise RuntimeError("QQ 音乐授权未返回登录 code")
        return await self._get_music_credential(code)

    async def _get_music_credential(self, code: str) -> dict[str, str]:
        payload = {
            "comm": {
                "ct": "24",
                "cv": "4747474",
                "platform": "yqq.json",
                "chid": "0",
                "g_tk": "5381",
                "g_tk_new_20200303": "5381",
                "format": "json",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "notice": "0",
                "needNewCode": "1",
                "tmeLoginType": "2",
            },
            "req_0": {
                "module": "QQConnectLogin.LoginServer",
                "method": "QQLogin",
                "param": {"code": code},
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "User-Agent": self._USER_AGENT,
            "Referer": self._REFERER,
            "Content-Type": "application/json",
        }
        async with self.parser.session.post(
            self._CGI_URL,
            data=body,
            headers=headers,
        ) as response:
            self._ensure_success(response)
            result = json.loads(await response.text())

        request = result.get("req_0") if isinstance(result, Mapping) else None
        if not isinstance(request, Mapping) or request.get("code") != 0:
            code = (
                request.get("code")
                if isinstance(request, Mapping)
                else result.get("code") if isinstance(result, Mapping) else None
            )
            raise RuntimeError(f"QQ 音乐凭据获取失败 (code={code})")
        data = request.get("data")
        if not isinstance(data, Mapping):
            raise TypeError("QQ 音乐凭据响应格式无效")
        music_id = str(data.get("musicid") or data.get("str_musicid") or "").strip()
        music_key = str(data.get("musickey") or "").strip()
        if not music_id or not music_key:
            raise RuntimeError("QQ 音乐凭据缺少 musicid 或 musickey")

        cookie_values = {
            "uin": music_id,
            "qqmusic_uin": music_id,
            "qqmusic_key": music_key,
            "qm_keyst": music_key,
        }
        cookiejar: CookieJar | None = getattr(self.parser, "cookiejar", None)
        if cookiejar is None:
            raise RuntimeError("QQ 音乐 Cookie 存储未初始化")
        cookiejar.replace_from_cookies_str(
            "; ".join(f"{name}={value}" for name, value in cookie_values.items())
        )
        return cookie_values

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """Poll QQ's QR authorization and persist the resulting playback cookies."""
        scanned_tip_pending = True
        for _ in range(60):
            try:
                status, uin, sigx = await self._poll_qrcode()
            except (ClientError, TimeoutError, RuntimeError) as exc:
                logger.warning(f"[QQ 音乐] 登录状态查询失败: {exc}")
                yield "QQ 音乐登录状态查询失败，请重新生成二维码"
                return

            if status == "0":
                try:
                    await self._exchange_authorization(uin or "", sigx or "")
                except (ClientError, TimeoutError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning(f"[QQ 音乐] 登录凭据获取失败: {exc}")
                    yield f"QQ 音乐登录失败：{exc}"
                    return
                yield "QQ 音乐登录成功，Cookie 已保存"
                return
            if status == "67" and scanned_tip_pending:
                yield "二维码已扫描，请在手机上确认登录"
                scanned_tip_pending = False
            elif status == "65":
                yield "二维码已过期，请重新生成"
                return
            elif status == "68":
                yield "已取消 QQ 音乐登录"
                return
            await asyncio.sleep(2)
        yield "QQ 音乐二维码登录超时，请重新生成"

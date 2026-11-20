from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from ..cookie import Cookie

_LOGIN_URL = "https://login-user.kugou.com/login/?appid=1001"
_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE = 512 * 1024
_NAMES = frozenset(
    {
        "kugoo",
        "kugooid",
        "userid",
        "t",
        "kugounickname",
        "nickname",
        "vip_type",
        "vip_token",
        "a_id",
        "kg_mid",
        "kg_dfid",
        "mid",
        "dfid",
    }
)
_SELECTORS = (
    "img#qrcode",
    "img.qrcode",
    "img.qr-code",
    "img[src*='qrcode' i]",
    "#qrcode canvas",
    ".qrcode canvas",
    ".qr-code canvas",
    "canvas#qrcode",
    "canvas.qrcode",
    "canvas.qr-code",
    "#qrcode img",
    ".qrcode img",
    ".qr-code img",
)


def _trusted_url(url: str, *, login_only: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (
                host == "login-user.kugou.com"
                if login_only
                else host == "kugou.com" or host.endswith(".kugou.com")
            )
        )
    except ValueError:
        return False


def _valid_png(image: bytes) -> bool:
    return len(_PNG) < len(image) <= _MAX_IMAGE and image.startswith(_PNG)


def _proxy_options(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
            raise ValueError
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        server = f"{parsed.scheme}://{host}"
        if parsed.port is not None:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username is not None:
            result["username"] = unquote(parsed.username)
        if parsed.password is not None:
            result["password"] = unquote(parsed.password)
        return result
    except (ValueError, TypeError):
        raise ValueError("Invalid browser proxy") from None


class KugouBrowserLogin:
    """One isolated, caller-owned browser session. Returned cookies are candidates."""

    def __init__(self, parser):
        self.parser = parser
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._closed = False
        self._started = False
        self._create_task = None
        self._response_tasks = set()
        self._close_lock = asyncio.Lock()
        self._qr = None

    def stop(self) -> None:
        """Retire this instance immediately and schedule resource release."""
        self._closed = True
        task = self._create_task
        if task is not None and not task.done():
            task.cancel()
        for task in tuple(self._response_tasks):
            task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.close())

    async def close(self) -> None:
        self._closed = True
        task = self._create_task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        async with self._close_lock:
            for pending in tuple(self._response_tasks):
                pending.cancel()
            if self._response_tasks:
                await asyncio.gather(
                    *tuple(self._response_tasks), return_exceptions=True
                )
            for attr, method in (
                ("_context", "close"),
                ("_browser", "close"),
                ("_playwright", "stop"),
            ):
                obj = getattr(self, attr)
                setattr(self, attr, None)
                if obj is not None:
                    try:
                        await asyncio.wait_for(getattr(obj, method)(), 3)
                    except (Exception, asyncio.CancelledError):
                        pass
            self._page = None

    def _on_response(self, response) -> None:
        if self._closed or self._qr is not None:
            return
        try:
            url = urlsplit(response.url)
            if (
                not _trusted_url(response.url, login_only=True)
                or url.path != "/v2/qrcode"
            ):
                return
            if response.status != 200:
                return
        except (AttributeError, ValueError):
            return
        task = asyncio.create_task(self._capture_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _capture_response(self, response) -> None:
        try:
            headers = response.headers
            length = headers.get("content-length")
            if length is not None and int(length) > _MAX_IMAGE * 2:
                return
            body = await asyncio.wait_for(response.body(), 3)
            if len(body) > _MAX_IMAGE * 2:
                return
            payload = json.loads(body)
            raw = payload.get("data", {}).get("qrcode_img", "")
            if not isinstance(raw, str) or len(raw) > _MAX_IMAGE * 2:
                return
            prefix = "data:image/png;base64,"
            raw = raw.removeprefix(prefix)
            image = base64.b64decode(raw, validate=True)
            if _valid_png(image) and not self._closed:
                self._qr = image
        except (Exception, asyncio.CancelledError):
            return

    async def create_qr(self) -> bytes:
        if self._closed or self._started:
            raise RuntimeError("Browser login is closed or already started")
        self._started = True
        self._create_task = asyncio.current_task()
        try:
            return await asyncio.wait_for(self._create(), 45)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise RuntimeError("Browser QR creation failed") from None
        finally:
            self._create_task = None

    async def _create(self) -> bytes:
        from playwright.async_api import async_playwright

        self._playwright = await asyncio.wait_for(async_playwright().start(), 10)
        options = {"headless": True}
        if (
            sys.platform.startswith("linux")
            and hasattr(os, "geteuid")
            and os.geteuid() == 0
        ):
            options["args"] = ["--no-sandbox"]
        proxy = _proxy_options(getattr(self.parser, "proxy", None))
        if proxy:
            options["proxy"] = proxy
        self._browser = await asyncio.wait_for(
            self._playwright.chromium.launch(**options), 15
        )
        self._context = await asyncio.wait_for(self._browser.new_context(), 5)
        self._page = await asyncio.wait_for(self._context.new_page(), 5)
        self._page.on("response", self._on_response)
        await asyncio.wait_for(
            self._page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=15000),
            16,
        )
        while not self._closed:
            if self._qr:
                return self._qr
            for frame in self._page.frames:
                if not _trusted_url(frame.url):
                    continue
                for selector in _SELECTORS:
                    try:
                        locator = frame.locator(selector).first
                        if not await asyncio.wait_for(
                            locator.is_visible(timeout=500), 1
                        ):
                            continue
                        image = await asyncio.wait_for(
                            locator.screenshot(timeout=1000), 2
                        )
                        if _valid_png(image):
                            return image
                    except Exception:
                        continue
            await asyncio.sleep(0.25)
        raise RuntimeError("Browser login closed")

    async def poll(self) -> list[Cookie] | None:
        if self._closed or self._context is None or self._create_task is not None:
            return None
        try:
            items = await asyncio.wait_for(self._context.cookies(), 3)
            cookies = []
            for item in items:
                domain = item.get("domain", "").lower()
                host = domain.removeprefix(".")
                name = item.get("name", "")
                value = item.get("value", "")
                path = item.get("path", "")
                if not (host == "kugou.com" or host.endswith(".kugou.com")):
                    continue
                if (
                    name.casefold() not in _NAMES
                    or not value
                    or not path.startswith("/")
                ):
                    continue
                expiry = item.get("expires", -1)
                expires = (
                    int(expiry)
                    if isinstance(expiry, (int, float)) and expiry > 0
                    else 0
                )
                cookie = Cookie(
                    domain, path, name, value, bool(item.get("secure")), expires
                )
                if not cookie.is_expired():
                    cookies.append(cookie)
            if self._closed or not self._has_account(cookies):
                return None
            return cookies
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("Browser cookie query failed") from None

    @staticmethod
    def _has_account(cookies: list[Cookie]) -> bool:
        scopes = {}
        for cookie in cookies:
            name = cookie.name.casefold()
            if name == "kugoo":
                value = cookie.value
                for _ in range(3):
                    value = unquote(value)
                fields = {k.casefold(): v for k, v in parse_qs(value).items()}
                uid = fields.get("kugooid") or fields.get("userid")
                token = fields.get("t")
                if (
                    uid
                    and token
                    and uid[0].strip() not in ("", "0")
                    and token[0].strip()
                ):
                    return True
            scope = scopes.setdefault((cookie.domain, cookie.path), {})
            scope[name] = cookie.value.strip()
        return any(
            (values.get("kugooid") or values.get("userid")) not in (None, "", "0")
            and values.get("t")
            for values in scopes.values()
        )

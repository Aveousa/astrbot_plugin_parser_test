"""Validate and commit fresh Kugou login candidates without disturbing live playback."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import asdict
from urllib.parse import urlsplit

import aiohttp

from ..cookie import Cookie
from .qqmusic_cookie import QQMusicCookieJar


class KugouCookieJar(QQMusicCookieJar):
    """Use the established atomic cookie/import-record protocol for Kugou as well."""

    def __init__(self, config, parser_cfg):
        super().__init__(config, parser_cfg, domain="kugou.com")


def cookie_snapshot(jar):
    return hashlib.sha256(
        json.dumps(
            [asdict(c) for c in jar.cookies], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class KugouSession:
    """Only an explicitly validated candidate may replace the current cookie file."""

    def __init__(self, parser):
        self.parser = parser
        self._generation = 0
        self._closed = False
        self._lock = asyncio.Lock()

    def begin(self):
        if self._closed or not getattr(
            self.parser.cookiejar, "persistence_enabled", True
        ):
            raise RuntimeError("酷狗解析器已重载，请重新发起登录")
        self._generation += 1
        return self._generation, cookie_snapshot(self.parser.cookiejar)

    def stop(self):
        self._closed = True
        self._generation += 1

    def _current(self, ticket):
        jar = self.parser.cookiejar
        checker = getattr(jar, "file_unchanged", None)
        return (
            not self._closed
            and getattr(jar, "persistence_enabled", True)
            and ticket == (self._generation, cookie_snapshot(jar))
            and (checker is None or checker())
        )

    async def _validate(self, cookie):
        """Isolate candidate probes from the parser's automatic response cookie jar."""
        params = self.parser._kugou_web_play_params(
            cookie,
            song_hash=self.parser._COOKIE_CHECK_TRACK["hash"],
            album_id=self.parser._COOKIE_CHECK_TRACK["album_id"],
            album_audio_id=self.parser._COOKIE_CHECK_TRACK["album_audio_id"],
        )
        if not params:
            return False
        async with (
            aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=12),
            ) as session,
            session.get(
                self.parser._WEB_PLAY_URL,
                params=params,
                headers={"Cookie": cookie, "Referer": "https://www.kugou.com/"},
                proxy=getattr(self.parser, "proxy", None),
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > 65536:
                    return None
            data = json.loads(body)
            if not isinstance(data, dict):
                return None
            return str(data.get("status")) == "1" and str(data.get("err_code")) == "0"

    def _identity(self, header):
        from .kugou_identity import account_credentials, cookie_values
        from .kugou_quality import session_fields

        try:
            values = cookie_values(header)
        except ValueError:
            raise RuntimeError("酷狗候选 Cookie 账号字段冲突") from None
        identity = account_credentials(header)
        if not all(identity) and any(
            values.get(k) for k in ("KuGoo", "KugooID", "t", "userid", "token")
        ):
            raise RuntimeError("酷狗候选 Cookie 账号字段不完整或冲突")
        gateway = session_fields(header)
        if gateway is not None and (gateway["userid"], gateway["token"]) != identity:
            return "", ""
        return identity

    async def commit(self, candidates: list[Cookie], ticket) -> None:
        async with self._lock:
            if not self._current(ticket):
                raise RuntimeError(
                    "酷狗登录期间会话已变化，未覆盖当前 Cookie，请重新登录"
                )
            allowed = []
            for c in candidates:
                domain = c.domain.lstrip(".")
                if domain != "kugou.com" and not domain.endswith(".kugou.com"):
                    continue
                if not c.name or not c.path.startswith("/") or c.is_expired():
                    continue
                if any(x in c.name for x in "=;\r\n") or any(
                    ord(x) < 32 or ord(x) == 127 for x in c.value
                ):
                    raise RuntimeError("酷狗登录候选 Cookie 格式无效，已保留当前会话")
                allowed.append(copy.copy(c))
            headers = {}
            for target in (
                self.parser._WEB_PLAY_URL,
                "https://gateway.kugou.com/v5/url",
            ):
                parsed = urlsplit(target)
                matching = sorted(
                    [c for c in allowed if c.match(parsed.hostname, parsed.path, True)],
                    key=lambda c: len(c.path),
                    reverse=True,
                )
                headers[target] = "; ".join(f"{c.name}={c.value}" for c in matching)
            web = headers[self.parser._WEB_PLAY_URL]
            expected = self._identity(web)
            if not all(expected):
                raise RuntimeError("酷狗登录缺少网页账号凭据，已保留当前 Cookie")
            # Both playback faces must see the same identity when present.
            gateway_account = self._identity(
                headers["https://gateway.kugou.com/v5/url"]
            )
            if any(gateway_account) and gateway_account != expected:
                raise RuntimeError("酷狗候选 Cookie 账号不一致，已保留当前会话")
            try:
                accepted = await asyncio.wait_for(self._validate(web), timeout=15)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never publish unvalidated credentials
                accepted = None
            if not self._current(ticket):
                raise RuntimeError("酷狗登录期间会话已变化，已保留当前 Cookie")
            if accepted is not True:
                raise RuntimeError(
                    "酷狗候选登录态未通过校验，已保留当前 Cookie，请稍后重新登录"
                )
            jar = self.parser.cookiejar
            previous = jar.cookies, jar.cookies_str, jar.raw_cookies
            try:
                jar.cookies = allowed
                jar._sync_cookies_str()
                jar.raw_cookies = None
                jar.save_to_file()
            except Exception:  # noqa: BLE001 - restore live memory on pre-commit failure
                jar.cookies, jar.cookies_str, jar.raw_cookies = previous
                raise RuntimeError("酷狗 Cookie 保存失败，已保留当前会话") from None

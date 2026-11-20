"""Best-effort QQ Music refresh; playback cookies remain the source of truth."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

import aiohttp
from astrbot.api import logger
from cryptography.fernet import Fernet, InvalidToken

_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_PATH = "/cgi-bin/musicu.fcg"
_NAMES = ("uin", "qqmusic_uin", "qqmusic_key", "qm_keyst")
_FIELDS = (
    "musicid",
    "str_musicid",
    "musickey",
    "refresh_key",
    "refresh_token",
    "openid",
    "unionid",
    "musickeyCreateTime",
    "keyExpiresIn",
    "loginType",
)
_LIMIT = 65536


def _safe(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and not any(ord(c) < 33 or ord(c) == 127 or c in ';,\\"' for c in value)
    )


def _account(data):
    if (
        data.get("str_musicid")
        and data.get("musicid")
        and str(data["str_musicid"]) != str(data["musicid"])
    ):
        return ""
    value = data.get("str_musicid") or data.get("musicid") or ""
    value = (
        str(value)
        if isinstance(value, (str, int)) and not isinstance(value, bool)
        else ""
    )
    return value if _safe(value) and value.isdigit() and int(value) > 0 else ""


def _times(data, now):
    try:
        create, lifetime = data["musickeyCreateTime"], data["keyExpiresIn"]
        if isinstance(create, bool) or isinstance(lifetime, bool):
            return None
        create, lifetime = int(create), int(lifetime)
        if create <= 0 or create > now + 300 or not 0 < lifetime <= 366 * 86400:
            return None
        return create, lifetime
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


class QQMusicSession:
    def __init__(self, parser):
        self.parser = parser
        self._state = None
        self._generation = 0
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()
        self._task = None
        self._closed = False
        self._storage_ok = True
        directory = getattr(getattr(parser, "cfg", None), "cookie_dir", None)
        self._dir = Path(directory) if directory else None
        self._key_path = self._dir / "qqmusic_session.key" if self._dir else None
        self._state_path = self._dir / "qqmusic_session.enc" if self._dir else None
        self._load()

    def _read(self, path, limit):
        if path.is_symlink():
            raise ValueError("symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("size")
        return data

    def _atomic(self, path, data):
        if path.is_symlink():
            raise ValueError("symlink")
        fd, tmp = tempfile.mkstemp(prefix=".qqmusic-", dir=self._dir)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink():
                raise ValueError("symlink")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load(self):
        if not self._dir:
            return
        try:
            if self._key_path.is_symlink() or self._state_path.is_symlink():
                raise ValueError("symlink")
            if not self._state_path.exists():
                if self._key_path.exists():
                    Fernet(self._read(self._key_path, 128))
                return
            key = self._read(self._key_path, 128)
            payload = json.loads(
                Fernet(key).decrypt(self._read(self._state_path, _LIMIT))
            )
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or not isinstance(payload.get("credentials"), dict)
                or not isinstance(payload.get("device"), dict)
            ):
                raise ValueError("format")
            credentials, device = payload["credentials"], payload["device"]
            if not (
                _account(credentials)
                and _safe(credentials.get("musickey"))
                and _safe(credentials.get("refresh_key"))
                and _times(credentials, time.time())
                and str(credentials.get("loginType", 2)) in ("1", "2", "6")
            ):
                raise ValueError("credentials")
            if any(
                not _safe(credentials[k])
                for k in ("refresh_token", "openid", "unionid")
                if credentials.get(k)
            ):
                raise ValueError("credentials")
            if not all(
                isinstance(device.get(k), str) and re.fullmatch(pattern, device[k])
                for k, pattern in (
                    ("id", r"[0-9a-f]{32}"),
                    ("aid", r"[0-9a-f]{16}"),
                    ("rom", r"[0-9a-f]{32}"),
                )
            ):
                raise ValueError("device")
            self._state = payload
        except (OSError, ValueError, TypeError, InvalidToken):
            self._storage_ok = False
            self._state = None
            logger.warning("[QQ 音乐] 刷新状态不可用，降级为内存存储")

    def _persist(self, state):
        if not self._dir or not self._storage_ok:
            return False
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            if self._key_path.is_symlink() or self._state_path.is_symlink():
                raise ValueError("symlink")
            if self._key_path.exists():
                key = self._read(self._key_path, 128)
            else:
                if self._state_path.exists():
                    raise ValueError("missing key")
                key = Fernet.generate_key()
                self._atomic(self._key_path, key)
            encrypted = Fernet(key).encrypt(
                json.dumps(state, separators=(",", ":")).encode()
            )
            if len(encrypted) > _LIMIT:
                raise ValueError("size")
            self._atomic(self._state_path, encrypted)
            return True
        except (OSError, ValueError, TypeError):
            self._storage_ok = False
            logger.warning("[QQ 音乐] 刷新状态持久化失败，降级为内存存储")
            return False

    def _snapshot_matches(self, old):
        jar = self.parser.cookiejar
        checker = getattr(jar, "file_unchanged", None)
        if checker is not None and not checker():
            return False
        account, key = _account(old), old.get("musickey")
        if not account or not _safe(key):
            return False
        found = set()
        for cookie in jar.cookies:
            if cookie.name in _NAMES and cookie.match("u.y.qq.com", _PATH, True):
                expected = account if cookie.name in _NAMES[:2] else key
                if cookie.value != expected:
                    return False
                found.add(cookie.name)
        return bool(found.intersection(_NAMES[:2])) and bool(
            found.intersection(_NAMES[2:])
        )

    def capture_login(self, data: Mapping) -> None:
        if self._closed or not getattr(
            self.parser.cookiejar, "persistence_enabled", True
        ):
            return
        self._generation += 1
        self._state = None
        self._last_attempt = 0.0
        try:
            # Invalidate prior valid on-disk state even for incomplete logins.
            if self._dir and self._storage_ok and self._state_path.exists():
                if self._state_path.is_symlink():
                    raise ValueError("symlink")
                self._state_path.unlink()
            old = {
                k: data[k]
                for k in _FIELDS
                if k in data
                and isinstance(data[k], (str, int))
                and not isinstance(data[k], bool)
            }
            if not (
                _account(old)
                and _safe(old.get("musickey"))
                and _safe(old.get("refresh_key"))
                and _times(old, time.time())
                and self._snapshot_matches(old)
            ):
                return
            if any(
                not _safe(old[k])
                for k in ("refresh_token", "openid", "unionid")
                if old.get(k)
            ):
                return
            mode = old.get("loginType", 2)
            if str(mode) not in ("1", "2", "6"):
                return
            state = {
                "version": 1,
                "credentials": old,
                "device": {
                    "id": secrets.token_hex(16),
                    "aid": secrets.token_hex(8),
                    "rom": secrets.token_hex(16),
                },
            }
            self._state = state
            self._persist(state)
        except (OSError, ValueError, TypeError):
            self._storage_ok = False
            logger.warning("[QQ 音乐] 刷新状态持久化失败，降级为内存存储")
        finally:
            logger.info(
                "[QQ 音乐] 登录凭据可刷新"
                if self._state
                else "[QQ 音乐] 登录凭据不可刷新"
            )

    async def _request_refresh(self, old, device):
        account = _account(old)
        comm = {
            "ct": 11,
            "cv": 14090008,
            "v": 14090008,
            "chid": 10003505,
            "tmeAppID": "qqmusic",
            "QIMEI": "",
            "QIMEI36": "",
            "OpenUDID": device["id"],
            "udid": device["id"],
            "OpenUDID2": device["id"],
            "aid": device["aid"],
            "os_ver": "10",
            "phonetype": "MI 6",
            "devicelevel": "29",
            "newdevicelevel": "29",
            "rom": device["rom"],
            "qq": account,
            "authst": old["musickey"],
            "tmeLoginType": int(old.get("loginType", 2)),
        }
        param = {
            "openid": old.get("openid", ""),
            "refresh_token": old.get("refresh_token", ""),
            "str_musicid": account,
            "musickey": old["musickey"],
            "unionid": old.get("unionid", ""),
            "refresh_key": old["refresh_key"],
            "loginMode": 2,
        }
        cookies = {
            name: account if name in _NAMES[:2] else old["musickey"] for name in _NAMES
        }
        headers = {
            "User-Agent": "QQMusic 14090008(android 10)",
            "Content-Type": "application/json",
        }
        async with (
            aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=15),
                proxy=getattr(self.parser, "proxy", None),
            ) as session,
            session.post(
                _URL,
                json={
                    "comm": comm,
                    "req_0": {
                        "module": "music.login.LoginServer",
                        "method": "Login",
                        "param": param,
                    },
                },
                headers=headers,
                cookies=cookies,
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > _LIMIT:
                    return None
            return json.loads(body)

    async def refresh_if_due(self) -> bool:
        attempted = False
        succeeded = False
        try:
            async with self._lock:
                state = self._state
                if (
                    self._closed
                    or not state
                    or not getattr(self.parser.cookiejar, "persistence_enabled", True)
                ):
                    return False
                old = state["credentials"]
                now = time.time()
                times = _times(old, now)
                if (
                    not times
                    or not _safe(old.get("refresh_key"))
                    or not self._snapshot_matches(old)
                ):
                    return False
                expiry = sum(times)
                last_wall = state.get("last_attempt", 0)
                if not isinstance(last_wall, (int, float)) or isinstance(
                    last_wall, bool
                ):
                    return False
                if (
                    now < expiry - 21600
                    or now > expiry + 30 * 86400
                    or (last_wall and now < last_wall + 3600)
                    or (
                        self._last_attempt
                        and time.monotonic() - self._last_attempt < 3600
                    )
                ):
                    return False
                attempted = True
                self._last_attempt = time.monotonic()
                state["last_attempt"] = now
                self._persist(state)
                generation = self._generation
                result = await self._request_refresh(dict(old), dict(state["device"]))
                current = asyncio.current_task()
                if (
                    (current and current.cancelling())
                    or self._closed
                    or generation != self._generation
                    or not getattr(self.parser.cookiejar, "persistence_enabled", True)
                    or not self._snapshot_matches(old)
                ):
                    return False
                if (
                    not isinstance(result, Mapping)
                    or result.get("code") != 0
                    or not isinstance(result.get("req_0"), Mapping)
                    or result["req_0"].get("code") != 0
                ):
                    return False
                data = result["req_0"].get("data")
                if (
                    not isinstance(data, Mapping)
                    or _account(data) != _account(old)
                    or not _safe(data.get("musickey"))
                    or not _safe(data.get("refresh_key"))
                ):
                    return False
                new_times = _times(data, time.time())
                if (
                    not new_times
                    or new_times[0] <= times[0]
                    or sum(new_times) <= expiry
                    or sum(new_times) <= time.time()
                ):
                    return False
                new = dict(old)
                for k in _FIELDS:
                    if k in data:
                        new[k] = data[k]
                if str(new.get("loginType", 2)) != str(old.get("loginType", 2)) or any(
                    not _safe(new[k])
                    for k in ("refresh_token", "openid", "unionid")
                    if new.get(k)
                ):
                    return False
                new_state = {
                    "version": 1,
                    "credentials": new,
                    "device": state["device"],
                    "last_attempt": now,
                }
                jar = self.parser.cookiejar
                changes = [
                    (c, c.value, c.expires)
                    for c in jar.cookies
                    if c.name in _NAMES and c.match("u.y.qq.com", _PATH, True)
                ]
                try:
                    for cookie, _, previous_expiry in changes:
                        cookie.value = (
                            _account(new)
                            if cookie.name in _NAMES[:2]
                            else new["musickey"]
                        )
                        if previous_expiry:
                            cookie.expires = sum(new_times)
                    jar._sync_cookies_str()
                    jar.save_to_file()
                except Exception:  # noqa: BLE001 - roll back any CookieJar save failure
                    for cookie, value, previous_expiry in changes:
                        cookie.value = value
                        cookie.expires = previous_expiry
                    jar._sync_cookies_str()
                    # Keep the previous refresh material paired with the unchanged cookie file.
                    self._persist(state)
                    return False
                self._persist(new_state)
                self._state = new_state
                succeeded = True
                return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - optional renewal must never break playback
            return False
        finally:
            if attempted:
                logger.info(
                    "[QQ 音乐] 凭据刷新成功"
                    if succeeded
                    else "[QQ 音乐] 凭据刷新未完成"
                )

    def start(self) -> None:
        if self._closed or (self._task and not self._task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    async def _run(self):
        while True:
            await self.refresh_if_due()
            await asyncio.sleep(3600)

    def stop(self) -> None:
        """Retire this instance immediately; close() subsequently joins its task."""
        self._closed = True
        self._generation += 1
        if self._task:
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        task, self._task = self._task, None
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

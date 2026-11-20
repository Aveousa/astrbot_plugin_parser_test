"""Optional H5 playback quality resolver, isolated from the existing web resolver."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from ..cache import cache_dir_activity, get_active_cache_dir
from ..exception import DownloadException, SizeLimitException

# Wire contract: Mineradio-paused H5 /v5/url path and the matching Folia H5 signature.
# Public client constants, not account credentials.
_URL = "https://gateway.kugou.com/v5/url"
_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
_KEY_SALT = "185672dd44712f60bb1736df5a377e82"
QUALITIES = {"high": "320", "lossless": "flac", "standard": "128"}
_H5_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/[IP] Safari/537.36"
DEFAULT_PRIORITY = ("high", "lossless", "standard")


def quality_priority(value: object) -> tuple[str, ...]:
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return DEFAULT_PRIORITY
    result = tuple(
        dict.fromkeys(v for v in values if isinstance(v, str) and v in QUALITIES)
    )
    return result or DEFAULT_PRIORITY


def _safe(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 4096
        and not any(char in value for char in (";", ",", "\\", '"', "\\r", "\\n"))
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
    )


def session_fields(cookie: str) -> dict[str, str] | None:
    values = {}
    for entry in cookie.split(";"):
        name, sep, value = entry.strip().partition("=")
        if sep:
            values.setdefault(name, value)
    # Prefer a complete browser identity; do not mix a stale lite identity with it.
    fields = {
        "userid": values.get("KugooID", ""),
        "token": values.get("t", ""),
        "mid": values.get("kg_mid") or values.get("mid", ""),
        "dfid": values.get("kg_dfid") or values.get("dfid", ""),
    }
    compound = values.get("KuGoo") or values.get("kugou") or values.get("Kugou") or ""
    for _ in range(4):
        from urllib.parse import unquote

        decoded = unquote(compound)
        if decoded == compound:
            break
        compound = decoded
    nested = {}
    for part in compound.split("&"):
        name, sep, value = part.partition("=")
        if sep:
            nested.setdefault(name.strip(), value.strip())
    fields["userid"] = (
        fields["userid"]
        or nested.get("KugooID")
        or nested.get("userid")
        or nested.get("uid", "")
    )
    fields["token"] = fields["token"] or nested.get("t") or nested.get("token", "")
    if not fields["userid"] and not fields["token"]:
        fields["userid"] = values.get("userid") or values.get("UserId", "")
        fields["token"] = values.get("token") or values.get("Token", "")
        fields["mid"] = values.get("KUGOU_API_MID") or fields["mid"]
    if not all(_safe(v) for v in fields.values()):
        return None
    if not re.fullmatch(r"[0-9]{1,20}", fields["userid"]) or int(fields["userid"]) == 0:
        return None
    return fields


def build_params(
    fields: Mapping[str, str],
    song_hash: str,
    quality: str,
    album_id: str | None,
    album_audio_id: str | None,
) -> dict:
    def numeric(value):
        text = str(value or "")
        return int(text) if re.fullmatch(r"[0-9]{1,20}", text) else 0

    timestamp_ms = int(time.time() * 1000)
    file_hash = song_hash.lower()
    params = {
        "srcappid": 2919,
        "clientver": 20000,
        "clienttime": timestamp_ms,
        "mid": fields["mid"],
        "uuid": timestamp_ms,
        "dfid": fields["dfid"] or "-",
        "appid": 1014,
        "token": fields["token"],
        "userid": int(fields["userid"]),
        "album_id": numeric(album_id),
        "area_code": 1,
        "hash": file_hash,
        "ssa_flag": "is_fromtrack",
        "version": 11430,
        "quality": QUALITIES[quality],
        "album_audio_id": numeric(album_audio_id),
        "behavior": "play",
        "pid": 2,
        "cmd": 26,
        "pidversion": 3001,
        "IsFreePart": 0,
        "cdnBackup": 1,
        "module": "",
    }
    params["key"] = hashlib.md5(
        f"{file_hash}57ae12eb6890223e355ccfcb74edf70d1014{fields['mid']}{fields['userid']}".encode()
    ).hexdigest()
    encoded = "".join(f"{k}={params[k]}" for k in sorted(params))
    params["signature"] = hashlib.md5(f"{_SALT}{encoded}{_SALT}".encode()).hexdigest()
    return params


def audio_url(payload: Mapping) -> str | None:
    for field in ("play_url", "playUrl", "url"):
        value = payload.get(field)
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if not isinstance(entry, str):
                continue
            for candidate in re.split(r",(?=https?://)", entry):
                candidate = candidate.strip()
                try:
                    parsed = urlsplit(candidate)
                    if (
                        parsed.scheme == "https"
                        and parsed.hostname
                        and parsed.hostname.lower().startswith("webfs.")
                        and parsed.hostname.lower().endswith(".kugou.com")
                        and parsed.port in (None, 443)
                        and not any(c.isspace() for c in candidate)
                        and not parsed.username
                        and not parsed.password
                    ):
                        return candidate
                except ValueError:
                    continue
    return None


class _PublicResolver(AbstractResolver):
    """Reject private DNS answers in the connection that actually downloads media."""

    def __init__(self):
        self._resolver = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=0):
        answers = await self._resolver.resolve(host, port, family)
        if not answers or any(
            not ipaddress.ip_address(a["host"]).is_global for a in answers
        ):
            raise OSError("non-public media address")
        return answers

    async def close(self):
        await self._resolver.close()


class KugouQualityClient:
    """Resolve URLs using the browser-session H5 contract from Mineradio."""

    def __init__(self, parser):
        self.parser = parser

    async def _request(self, session, params):
        headers = {
            "User-Agent": _H5_USER_AGENT,
            "x-router": "trackercdn.kugou.com",
            "Cookie": "; ".join(
                f"{name}={value}"
                for name, value in (
                    ("KugooID", params["userid"]),
                    ("t", params["token"]),
                    ("mid", params["mid"]),
                    ("dfid", params["dfid"]),
                )
            ),
        }
        async with session.get(
            _URL, params=params, headers=headers, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise ValueError("upstream unavailable")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("response too large")
            return json.loads(body)

    async def download(self, url: str) -> Path:
        """Use the shared download limit without changing other platforms' requests."""
        downloader = getattr(self.parser, "downloader", None)
        semaphore = getattr(downloader, "_download_semaphore", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(1)
        async with semaphore:
            return await self._download(url)

    async def _download(self, url: str) -> Path:
        """Download the selected CDN audio without credentials or redirects."""
        if audio_url({"url": url}) != url:
            raise DownloadException("酷狗音频地址不可用")
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in {".mp3", ".flac"}:
            raise DownloadException("酷狗音频格式无法确认")
        cfg = self.parser.cfg
        directory = get_active_cache_dir(cfg.cache_dir)
        maximum = int(getattr(cfg, "source_max_size", 100)) * 1024 * 1024
        timeout = min(120, max(1, float(getattr(cfg, "download_timeout", 60))))
        resolver = _PublicResolver()
        connector = aiohttp.TCPConnector(resolver=resolver)
        temporary = None
        try:
            with cache_dir_activity(directory) as active:
                target = active / (uuid.uuid4().hex + suffix)
                temporary = target.with_suffix(suffix + ".part")
                async with (
                    aiohttp.ClientSession(
                        connector=connector,
                        cookie_jar=aiohttp.DummyCookieJar(),
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as session,
                    session.get(
                        url,
                        allow_redirects=False,
                        headers={"User-Agent": _H5_USER_AGENT},
                        proxy=getattr(self.parser, "proxy", None),
                    ) as response,
                ):
                    if response.status != 200:
                        raise DownloadException("酷狗音频下载未完成")
                    if response.content_length and response.content_length > maximum:
                        raise SizeLimitException
                    count = 0
                    prefix = bytearray()
                    with temporary.open("wb") as stream:
                        async for chunk in response.content.iter_chunked(65536):
                            count += len(chunk)
                            if count > maximum:
                                raise SizeLimitException
                            if len(prefix) < 4096:
                                prefix.extend(chunk[: 4096 - len(prefix)])
                            stream.write(chunk)
                    if not count or (
                        response.content_length and count != response.content_length
                    ):
                        raise DownloadException("酷狗音频下载不完整")
                    media = bytes(prefix)
                    valid = (
                        media.startswith(b"fLaC")
                        if suffix == ".flac"
                        else (
                            media.startswith(b"ID3")
                            or any(
                                media[i] == 255 and media[i + 1] & 0xE0 == 0xE0
                                for i in range(min(4095, len(media) - 1))
                            )
                        )
                    )
                    if not valid:
                        raise DownloadException("酷狗音频内容无法确认")
                os.replace(temporary, target)
                return target
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            raise DownloadException("酷狗音频下载未完成") from None
        finally:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Cleanup failure must not replace the sanitized download error.
            await connector.close()
            await resolver.close()

    async def resolve(
        self,
        cookie: str,
        song_hash: str,
        priorities: tuple[str, ...],
        album_id=None,
        album_audio_id=None,
    ) -> dict:
        fields = session_fields(cookie)
        if fields is None or not re.fullmatch(r"[a-fA-F0-9]{32}", song_hash):
            return {}
        try:
            # One budget for the whole quality chain, so fallback is never
            # delayed by a timeout for every quality. No shared cookie mutation.
            async with (
                asyncio.timeout(8),
                aiohttp.ClientSession(
                    cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=aiohttp.ClientTimeout(total=4),
                    proxy=getattr(self.parser, "proxy", None),
                ) as session,
            ):
                for quality in priorities:
                    params = build_params(
                        fields, song_hash, quality, album_id, album_audio_id
                    )
                    response = await self._request(session, params)
                    if not isinstance(response, Mapping):
                        return {}
                    if str(response.get("status")) != "1":
                        return {}
                    code = str(response.get("error_code", response.get("errcode", 0)))
                    if (
                        code not in {"0", "None"}
                        or str(response.get("status", 1)) == "0"
                    ):
                        return {}
                    data = response.get("data", response)
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    if not isinstance(data, Mapping):
                        continue
                    if any(
                        source.get(k) not in (None, False, 0, "0", "")
                        for source in (response, data)
                        for k in (
                            "preview",
                            "is_free_part",
                            "isFreePart",
                            "IsFreePart",
                            "trial",
                        )
                    ):
                        continue
                    url = audio_url(data)
                    if url:
                        result = {"url": url, "requested_audio_quality": quality}
                        return result
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            # Exceptions may contain signed URLs and account tokens; don't log them.
            return {}
        return {}

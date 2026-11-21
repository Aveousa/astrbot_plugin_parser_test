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
from astrbot.api import logger
from yarl import URL

from ..cache import cache_dir_activity, get_active_cache_dir
from ..exception import DownloadException, SizeLimitException
from .kugou_identity import account_credentials, cookie_values

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
    try:
        values = cookie_values(cookie)
    except ValueError:
        return None
    userid, token = account_credentials(cookie)
    if not userid and not token and not any(values.get(k) for k in ("KuGoo", "KugooID", "t")):
        userid = values.get("userid") or values.get("UserId", "")
        token = values.get("token") or values.get("Token", "")
    fields = {
        "userid": userid, "token": token,
        "mid": values.get("kg_mid") or values.get("mid") or values.get("KUGOU_API_MID", ""),
        "dfid": values.get("kg_dfid") or values.get("dfid", ""),
    }
    if not all(_safe(v) for v in fields.values()):
        return None
    if not re.fullmatch(r"[0-9]{1,20}", userid) or int(userid) == 0:
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


def _url_candidates(payload):
    """Read the primary and backup URL shapes used by Mineradio's resolver."""
    def entries(value, depth=0):
        if isinstance(value, list) and depth < 4:
            for item in value:
                yield from entries(item, depth + 1)
        elif isinstance(value, str):
            # Some upstream fields contain JSON-style escaped slashes.
            value = value.replace("\\/", "/")
            for candidate in re.split(r",\s*(?=https?://)", value):
                if candidate.strip():
                    yield candidate.strip()

    for field in (
        "play_url", "playUrl", "url", "backupUrl", "backup_url", "play_backup_url",
    ):
        yield from entries(payload.get(field))


def _is_kugou_media_host(host: str) -> bool:
    # Mineradio has no complete CDN list. Trust the provider-owned DNS boundary
    # rather than a guessed CDN prefix; the connecting resolver still rejects
    # private addresses and downloads never follow redirects or send cookies.
    host = host.lower()
    return (
        len(host) <= 253
        and host.endswith(".kugou.com")
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split("."))
    )


def _inspect_audio_url(candidate):
    """Use identical rules for selection and diagnostics; never expose signed paths."""
    details = {"reason": "malformed_url"}
    if any(c.isspace() or ord(c) < 32 for c in candidate) or "\\" in candidate:
        return None, {"reason": "invalid_url_characters"}
    try:
        parsed = urlsplit(candidate)
        if parsed.username is not None or parsed.password is not None:
            return None, {"reason": "url_credentials_rejected"}
        host = parsed.hostname or ""
        port = parsed.port
        # DNS hostname only, never netloc, URL path, query, userinfo or fragment.
        if re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
            details["cdn_host"] = host.lower()
        details["scheme"] = parsed.scheme if parsed.scheme in {"http", "https"} else "other"
        details["port"] = port
        if parsed.scheme not in {"http", "https"}:
            details["reason"] = "unsupported_scheme"
        elif not _is_kugou_media_host(host):
            details["reason"] = "cdn_host_not_allowed"
        elif port not in ((None, 80, 443) if parsed.scheme == "http" else (None, 443)):
            details["reason"] = "cdn_port_not_allowed"
        else:
            if parsed.scheme == "http":
                # Remove HTTP's explicit default port while preserving all signed
                # path/query bytes (urlunsplit/requoting could change them).
                suffix = candidate[len(parsed.scheme) + 3 + len(parsed.netloc):]
                authority = parsed.netloc
                if port == 80:
                    authority = authority.rsplit(":", 1)[0]
                return "https://" + authority + suffix, {**details, "reason": "http_upgraded"}
            return candidate, {**details, "reason": "accepted"}
    except ValueError:
        return None, {"reason": "malformed_url"}
    return None, details


def audio_url(payload: Mapping) -> str | None:
    for candidate in _url_candidates(payload):
        url, _ = _inspect_audio_url(candidate)
        if url:
            return url
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


def _number(value):
    """Only numeric upstream diagnostics may enter logs, never arbitrary text."""
    if isinstance(value, bool):
        return None
    text = str(value)
    return int(text) if re.fullmatch(r"-?[0-9]{1,12}", text) else None


def _url_diagnostic(data):
    for candidate in _url_candidates(data):
        _, details = _inspect_audio_url(candidate)
        return details
    return {"reason": "empty_url"}


class KugouMediaError(DownloadException):
    def __init__(self, reason):
        super().__init__("酷狗高音质下载未完成")
        self.reason = reason


class KugouQualityClient:
    """Resolve URLs using the browser-session H5 contract from Mineradio."""

    def __init__(self, parser):
        self.parser = parser
        self.trace = uuid.uuid4().hex[:8]

    def report(self, event, **details):
        # Call sites pass fixed enums, validated numbers and booleans only.
        try:
            logger.debug(
                "[酷狗音质] "
                + json.dumps(
                    {"trace": self.trace, "event": event, **details}, ensure_ascii=False
                )
            )
        except Exception:  # noqa: BLE001 - optional diagnostics must not break playback
            return

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
            self.report("api_http", status=_number(response.status))
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
        try:
            async with semaphore:
                path = await self._download(url)
            self.report("download_ok", format=path.suffix.lstrip("."))
            return path
        except KugouMediaError as exc:
            self.report("download_failed", reason=exc.reason)
            raise
        except SizeLimitException:
            self.report("download_failed", reason="size_limit")
            raise
        except DownloadException:
            self.report("download_failed", reason="download_error")
            raise

    async def _download(self, url: str) -> Path:
        """Download the selected CDN audio without credentials or redirects."""
        if audio_url({"url": url}) != url:
            raise KugouMediaError("url_policy_rejected")
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in {".mp3", ".flac"}:
            raise KugouMediaError("unsupported_extension")
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
                        URL(url, encoded=True),
                        allow_redirects=False,
                        headers={"User-Agent": _H5_USER_AGENT},
                        proxy=getattr(self.parser, "proxy", None),
                    ) as response,
                ):
                    self.report(
                        "media_http",
                        status=_number(response.status),
                        bytes=_number(response.content_length),
                    )
                    if response.status != 200:
                        raise KugouMediaError(
                            "redirect" if 300 <= response.status < 400 else "http_error"
                        )
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
                        raise KugouMediaError("incomplete_audio")
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
                        raise KugouMediaError("invalid_audio_header")
                os.replace(temporary, target)
                return target
        except TimeoutError:
            raise KugouMediaError("download_timeout") from None
        except (aiohttp.ClientError, OSError, ValueError):
            raise KugouMediaError("network_or_io") from None
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
        if fields is None:
            self.report("skip", reason="incomplete_or_invalid_session")
            return {}
        if not re.fullmatch(r"[a-fA-F0-9]{32}", song_hash):
            self.report("skip", reason="invalid_song_hash")
            return {}
        priorities = quality_priority(priorities)
        self.report(
            "resolve_start",
            priority=list(priorities),
            proxy=bool(getattr(self.parser, "proxy", None)),
        )
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
                    self.report("request", quality=quality)
                    response = await self._request(session, params)
                    if not isinstance(response, Mapping):
                        self.report("api_rejected", reason="invalid_response")
                        return {}
                    self.report(
                        "api_result",
                        quality=quality,
                        status=_number(response.get("status")),
                        code=_number(
                            response.get("error_code", response.get("errcode", 0))
                        ),
                    )
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
                        self.report(
                            "quality_skipped", quality=quality, reason="preview"
                        )
                        continue
                    url = audio_url(data) or audio_url(response)
                    if url:
                        extension = Path(urlsplit(url).path).suffix.lower()
                        self.report(
                            "url_selected",
                            quality=quality,
                            format=extension
                            if extension in {".mp3", ".flac"}
                            else "other",
                            reported_bitrate=_number(data.get("bitrate")),
                            reported_size=_number(data.get("filesize")),
                        )
                        result = {"url": url, "requested_audio_quality": quality}
                        return result
                    self.report(
                        "quality_skipped", quality=quality, **_url_diagnostic(data)
                    )
        except TimeoutError:
            self.report("resolve_failed", reason="timeout")
            return {}
        except (aiohttp.ClientError, ValueError, TypeError):
            # Exceptions may contain signed URLs and account tokens; don't log them.
            self.report("resolve_failed", reason="network_or_response")
            return {}
        self.report("resolve_failed", reason="no_usable_url")
        return {}

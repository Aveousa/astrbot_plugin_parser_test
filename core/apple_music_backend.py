"""Optional wrapper-v2/gamdl adapter. Never imports gamdl into AstrBot.

Only the trusted, administrator-configured Python executable is launched;
account secrets never appear in argv, configuration or subprocess output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

from .cache import (
    clear_cache_dir,
    create_parse_cache_dir,
    get_active_cache_dir,
    get_cache_size,
)
from .exception import DownloadException
from .music_quality import (
    is_valid_qq_file_quality_priority,
    normalize_qq_file_quality_priority,
)

WRAPPER_VERSION = "0.0.2"
GAMDL_VERSION = "3.9.1"

# Keep the quality policy tied to the delivery format. AAC Web is small and
# broadly accepted by voice adapters; files prefer lossless ALAC and fall back
# through the user-configured gamdl codec order.
VOICE_CODEC_PRIORITY = ("aac-web",)
FILE_CODEC_PRIORITY = ("alac", "aac-web")
SUPPORTED_FILE_CODECS = frozenset({
    "alac", "aac-web", "aac-he-web", "aac", "aac-he",
    "aac-binaural", "aac-downmix", "aac-he-binaural", "aac-he-downmix",
    "atmos", "ac3",
})
MANAGED_BOOLEAN_SETTINGS = (
    "backend_enabled",
    "return_audio",
    "show_playlist_cover",
    "show_playlist_url",
    "force_chinese_language",
)
MANAGED_SETTINGS = frozenset({
    *MANAGED_BOOLEAN_SETTINGS,
    "audio_send_format",
    "file_codec_priority",
})
MUSIC_SETTINGS = {
    "netease": frozenset({"enable", "return_audio", "parse_lyrics", "audio_send_format", "show_playlist_cover", "show_playlist_url"}),
    "qqmusic": frozenset({"enable", "return_audio", "parse_lyrics", "audio_send_format", "file_quality_priority", "show_playlist_cover", "show_playlist_url"}),
    "kugou": frozenset({"enable", "return_audio", "parse_lyrics", "audio_send_format", "show_playlist_cover", "show_playlist_url"}),
}
MUSIC_BOOLEAN_SETTINGS = frozenset({"return_audio", "parse_lyrics", "show_playlist_cover", "show_playlist_url"})
PARSER_LABELS = {
    "netease": "网易云音乐",
    "qqmusic": "QQ 音乐",
    "kugou": "酷狗音乐",
}


def codec_priority_for_send_format(send_format: object) -> tuple[str, ...]:
    """Return the gamdl codec order for the configured delivery format."""
    if str(send_format or "").strip().lower() == "voice":
        return VOICE_CODEC_PRIORITY
    return FILE_CODEC_PRIORITY


def normalize_file_codec_priority(value: object) -> tuple[str, ...]:
    """Normalize the configured file codec order without passing arbitrary CLI values."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = ()
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        codec = item.strip().lower()
        if codec in SUPPORTED_FILE_CODECS and codec not in result:
            result.append(codec)
    return tuple(result) or FILE_CODEC_PRIORITY


class AppleMusicBackendError(DownloadException):
    """A deliberately sanitized error safe to show in the dashboard/chat."""


def normalize_track_url(value: str) -> tuple[str, str]:
    """Accept one Apple Music song, never an album/playlist/arbitrary URL."""
    if not isinstance(value, str) or len(value) > 2048:
        raise AppleMusicBackendError("请输入 Apple Music 单曲链接")
    try:
        parsed = urlsplit(value.strip())
        valid_host = (
            parsed.scheme == "https"
            and parsed.netloc == "music.apple.com"
            and not parsed.username
        )
    except ValueError:
        valid_host = False
    if not valid_host:
        raise AppleMusicBackendError("只接受 https://music.apple.com 的单曲链接")
    match = re.fullmatch(r"/([a-z]{2})/(song|album)/(?:[^/]+/)?([1-9]\d{0,19})/?", parsed.path)
    if not match or any(unquote(part) in (".", "..") for part in parsed.path.split("/")):
        raise AppleMusicBackendError("链接不是可识别的 Apple Music 单曲")
    country, kind, identifier = match.groups()
    if kind == "album":
        values = parse_qs(parsed.query).get("i", [])
        if len(values) != 1 or not re.fullmatch(r"[1-9]\d{0,19}", values[0]):
            raise AppleMusicBackendError("专辑链接必须用 i 参数指定一首歌曲")
        identifier = values[0]
    return f"https://music.apple.com/{country}/song/{identifier}", identifier


@dataclass(frozen=True)
class BackendSettings:
    enabled: bool = False
    wrapper_url: str = "http://127.0.0.1:80"
    decrypt_host: str = "127.0.0.1"
    decrypt_port: int = 10020
    python: str = ""
    timeout: int = 300

    @classmethod
    def from_config(cls, config) -> BackendSettings:
        try:
            item = config.parser.applemusic
        except (AttributeError, TypeError):
            return cls()

        def integer(value, default, invalid):
            if value is None:
                return default
            try:
                if isinstance(value, bool):
                    raise ValueError
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return invalid

        return cls(
            enabled=getattr(item, "backend_enabled", False) is True,
            wrapper_url=str(getattr(item, "wrapper_url", None) or cls.wrapper_url).strip(),
            decrypt_host=str(getattr(item, "wrapper_decrypt_host", None) or cls.decrypt_host).strip(),
            # Bad optional config must not break card parsing/plugin loading.
            # An invalid port stays invalid, so status reports a useful error.
            decrypt_port=integer(getattr(item, "wrapper_decrypt_port", None), cls.decrypt_port, 0),
            python=str(getattr(item, "gamdl_python", None) or "").strip(),
            timeout=max(30, min(integer(getattr(config, "download_timeout", None), 300, 300), 1800)),
        )

    def validate(self) -> str:
        try:
            parsed = urlsplit(self.wrapper_url)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise AppleMusicBackendError("wrapper 地址须为无账号、参数或路径的 HTTP(S) 地址")
        if not self.decrypt_host or any(c in self.decrypt_host for c in "/\\\r\n "):
            raise AppleMusicBackendError("wrapper 解密主机配置不正确")
        if not 1 <= self.decrypt_port <= 65535:
            raise AppleMusicBackendError("wrapper 解密端口须在 1–65535 之间")
        return self.wrapper_url.rstrip("/")


class AppleMusicBackend:
    # Lyrics are an optional enrichment.  Keep their subprocess deadline
    # bounded independently from the much longer audio-download timeout so a
    # missing native lyric falls back to LRCLIB quickly.
    _LYRICS_TIMEOUT = 30.0
    _MAX_LYRICS_SIZE = 2 * 1024 * 1024

    def __init__(
        self,
        config,
        settings: BackendSettings | None = None,
        *,
        platform_status_provider: Callable[[], object] | None = None,
        runtime_parser_provider: Callable[[], object] | None = None,
    ):
        self.cfg = config
        self.settings = settings or BackendSettings.from_config(config)
        self._platform_status_provider = platform_status_provider
        self._runtime_parser_provider = runtime_parser_provider
        # Cookie validity is process-local and tied to a digest of the current
        # value. Never persist or return Cookie contents from this registry.
        self._music_cookie_checks: dict[str, dict[str, str | bool]] = {}
        self._last_auth_state = "unknown"
        self._session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()
        self.pending_owner: str | None = None
        self.pending_until = 0.0
        self._probe_cache: tuple[float, dict] | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._tasks: set[asyncio.Task] = set()
        self.closed = False

    def platform_accounts(self, apple_auth_state: str = "unknown") -> list[dict]:
        """Return non-secret local credential and Apple login states for the page."""

        return self._status_platform_accounts(apple_auth_state)

    def _require_enabled(self) -> str:
        if self.closed:
            raise AppleMusicBackendError("Apple Music 后端已停止，请重载插件")
        if not self.settings.enabled:
            raise AppleMusicBackendError("请先在插件配置中启用 Apple Music 可选后端")
        return self.settings.validate()

    def management_settings(self) -> dict:
        """Return the small, non-secret configuration exposed to this page."""

        item = self._parser_item("applemusic")
        if item is None:
            return {
                "backend_enabled": False, "return_audio": False,
                "audio_send_format": "file",
                "file_codec_priority": list(FILE_CODEC_PRIORITY),
                "show_playlist_cover": True, "show_playlist_url": True,
                "force_chinese_language": False,
            }
        return {
            "backend_enabled": getattr(item, "backend_enabled", False) is True,
            "return_audio": getattr(item, "return_audio", False) is True,
            "audio_send_format": (
                "voice"
                if str(getattr(item, "audio_send_format", "file")).lower() == "voice"
                else "file"
            ),
            "file_codec_priority": list(
                normalize_file_codec_priority(
                    getattr(item, "file_codec_priority", None)
                )
            ),
            "show_playlist_cover": getattr(item, "show_playlist_cover", True) is not False,
            "show_playlist_url": getattr(item, "show_playlist_url", True) is not False,
            "force_chinese_language": (
                getattr(item, "force_chinese_language", False) is True
            ),
        }

    def _parser_item(self, key: str):
        parsers = getattr(self.cfg, "parser", None)
        getter = getattr(parsers, "get", None)
        if callable(getter):
            return getter(key)
        return getattr(parsers, key, None)

    def _music_cookie_value(self, parser_key: str) -> str:
        """Read the effective Cookie for internal fingerprinting only."""

        parser = next(
            (
                candidate
                for candidate in self._runtime_parsers()
                if getattr(getattr(candidate, "platform", None), "name", None)
                == parser_key
            ),
            None,
        )
        if parser is not None:
            header_method = (
                getattr(parser, "_qq_cookie_header", None)
                if parser_key == "qqmusic"
                else getattr(parser, "_kugou_cookie_header", None)
            )
            target_url = (
                getattr(parser, "_QQ_COOKIE_CHECK_URL", "")
                if parser_key == "qqmusic"
                else getattr(parser, "_WEB_PLAY_URL", "")
            )
            if callable(header_method) and target_url:
                try:
                    value = header_method(target_url)
                    if isinstance(value, str) and value:
                        return value
                except Exception:
                    pass
            cookiejar = getattr(parser, "cookiejar", None)
            value = getattr(cookiejar, "cookies_str", None)
            if isinstance(value, str) and value:
                return value
        item = self._parser_item(parser_key)
        value = getattr(item, "cookies", None) if item is not None else None
        return value if isinstance(value, str) else ""

    @staticmethod
    def _cookie_fingerprint(cookie: object) -> str:
        if not isinstance(cookie, str) or not cookie.strip():
            return ""
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    def record_music_cookie_check(
        self, parser_key: str, cookie: object, available: bool
    ) -> None:
        """Cache one definite validation result for the exact current Cookie."""

        if parser_key not in {"qqmusic", "kugou"}:
            return
        fingerprint = self._cookie_fingerprint(cookie)
        if not fingerprint:
            self._music_cookie_checks.pop(parser_key, None)
            return
        self._music_cookie_checks[parser_key] = {
            "fingerprint": fingerprint,
            "available": available is True,
        }

    def _music_cookie_check_status(
        self, parser_key: str, cookie: object
    ) -> bool | None:
        """Return a cached result only while the configured Cookie is unchanged."""

        fingerprint = self._cookie_fingerprint(cookie)
        result = self._music_cookie_checks.get(parser_key)
        if not fingerprint or not isinstance(result, dict):
            return None
        if result.get("fingerprint") != fingerprint:
            self._music_cookie_checks.pop(parser_key, None)
            return None
        available = result.get("available")
        return available if isinstance(available, bool) else None

    def music_cookie_status(
        self,
        parser_key: str,
        cookie: object,
        *,
        has_credentials: bool | None = None,
    ) -> dict[str, object]:
        """Return the sanitized three-state view plus a checking state."""

        configured = bool(self._cookie_fingerprint(cookie))
        if not configured:
            return {
                "available": False,
                "cookie_configured": False,
                "cookie_validity": "unconfigured",
                "cookie_status": "Cookie 未配置",
            }
        checked = self._music_cookie_check_status(parser_key, cookie)
        if checked is True:
            validity, label, available = "valid", "Cookie 有效", True
        elif checked is False or has_credentials is False:
            validity, label, available = "invalid", "Cookie 已配置但失效", False
        else:
            validity, label, available = "unknown", "Cookie 待检查", False
        return {
            "available": available,
            "cookie_configured": True,
            "cookie_validity": validity,
            "cookie_status": label,
        }

    def _music_parser(self, parser_key: str):
        return next(
            (
                candidate
                for candidate in self._runtime_parsers()
                if getattr(getattr(candidate, "platform", None), "name", None)
                == parser_key
            ),
            None,
        )

    async def check_music_cookie(self, owner: str, parser_key: object) -> dict:
        """Run one QQ Music/Kugou cookie check and return no credential data."""

        if parser_key not in {"qqmusic", "kugou"}:
            raise AppleMusicBackendError("仅支持检查 QQ 音乐或酷狗音乐 Cookie")
        parser_key = str(parser_key)
        if self._parser_item(parser_key) is None:
            raise AppleMusicBackendError(
                f"{PARSER_LABELS[parser_key]}解析器未配置，请先恢复解析器配置"
            )
        parser = self._music_parser(parser_key)
        if parser is None:
            raise AppleMusicBackendError(
                f"{PARSER_LABELS[parser_key]}解析器未启用，无法检查 Cookie"
            )

        async with self.lock:
            self.check_owner(owner)
            cookie = self._music_cookie_value(parser_key)
            if not cookie:
                self._music_cookie_checks.pop(parser_key, None)
                state = self.music_cookie_status(parser_key, cookie)
                return {
                    "parser_key": parser_key,
                    **state,
                    "message": f"{PARSER_LABELS[parser_key]} Cookie 未配置",
                }

            try:
                has_credentials = bool(parser.has_auth_credentials())
            except (AttributeError, TypeError, ValueError):
                has_credentials = False
            try:
                result = await parser.check_cookie()
            except Exception:
                # Third-party exceptions can contain account tokens or signed URLs.
                result = None

            if isinstance(result, bool):
                self.record_music_cookie_check(parser_key, cookie, result)
            state = self.music_cookie_status(
                parser_key, cookie, has_credentials=has_credentials
            )
            if result is True:
                message = (
                    f"{PARSER_LABELS[parser_key]} 账号 Cookie 有效；"
                    "具体歌曲播放权限由平台决定"
                )
            elif result is False:
                message = f"{PARSER_LABELS[parser_key]} 账号 Cookie 无效或已过期"
            else:
                message = "检查暂时无法完成；Cookie 状态未更新"
            return {"parser_key": parser_key, **state, "message": message}

    def _parser_config_entries(self) -> dict[str, dict]:
        """Describe three music parsers without returning cookie values."""

        entries: dict[str, dict] = {}
        defaults = self._default_parser_templates()
        history = self._parser_management_history()
        runtime_parsers = self._runtime_parsers()
        provided_statuses: dict[str, dict] = {}
        if self._platform_status_provider is not None:
            try:
                raw_statuses = self._platform_status_provider()
            except Exception:
                raw_statuses = ()
            if isinstance(raw_statuses, (list, tuple)):
                for status in raw_statuses:
                    if isinstance(status, dict) and status.get("key") in MUSIC_SETTINGS:
                        text = status.get("text")
                        provided_statuses[status["key"]] = {
                            "available": status.get("available") is True,
                            "cookie_configured": status.get("cookie_configured") is True
                                or status.get("available") is True,
                            "cookie_status": text.strip()[:64] if isinstance(text, str) else "Cookie 状态未确认",
                        }
        for key, fields in MUSIC_SETTINGS.items():
            item = self._parser_item(key)
            history_entry = history.get(key)
            history_entry = history_entry if isinstance(history_entry, dict) else {}
            if item is None:
                default = defaults.get(key)
                prior = history_entry
                previous_settings = prior.get("settings")
                previous_settings = previous_settings if isinstance(previous_settings, dict) else {}
                has_configuration = key in self._known_parser_keys() or bool(prior)
                known = has_configuration or default is not None
                configured = has_configuration
                cookie_configured = prior.get("cookie_configured") is True
                if not known:
                    settings = {}
                else:
                    settings = {}
                    for field in fields:
                        fallback = default.get(field) if default else None
                        value = previous_settings.get(field, fallback)
                        if field == "audio_send_format":
                            value = "file" if str(value or "voice").lower() == "file" else "voice"
                        elif field == "file_quality_priority":
                            value = list(normalize_qq_file_quality_priority(value))
                        elif field == "parse_lyrics":
                            value = False if value is None else bool(value)
                        elif field in MUSIC_BOOLEAN_SETTINGS | {"enable"}:
                            value = True if value is None else bool(value)
                        settings[field] = value
                entries[key] = {
                    "configured": False, "known": known,
                    "enabled": False,
                    "available": False,
                    "cookie_configured": cookie_configured,
                    "cookie_status": (
                        "Cookie 待检查" if cookie_configured and key in {"qqmusic", "kugou"} else
                        "Cookie 已配置（解析器未启用）" if cookie_configured else
                        "解析器未启用" if known else "未配置"
                    ),
                    "cookie_validity": (
                        "unknown" if cookie_configured and key in {"qqmusic", "kugou"} else
                        "unconfigured" if not cookie_configured else "unknown"
                    ),
                    "settings": settings,
                }
                continue
            raw_cookie = getattr(item, "cookies", None)
            cookiejar = next(
                (
                    getattr(parser, "cookiejar", None)
                    for parser in runtime_parsers
                    if getattr(getattr(parser, "platform", None), "name", None) == key
                    and getattr(parser, "cookiejar", None) is not None
                ),
                None,
            )
            cookie_value = (
                self._music_cookie_value(key)
                if key in {"qqmusic", "kugou"}
                else (getattr(cookiejar, "cookies_str", None) or raw_cookie)
            )
            cookie_configured = bool(cookie_value)
            provided = provided_statuses.get(key)
            parser = next((
                candidate for candidate in runtime_parsers
                if getattr(getattr(candidate, "platform", None), "name", None) == key
            ), None)
            if key in {"qqmusic", "kugou"}:
                checked = self._music_cookie_check_status(key, cookie_value)
                if not cookie_configured:
                    cookie_status, available = "Cookie 未配置", False
                if checked is None:
                    try:
                        has_credentials = (
                            parser.has_auth_credentials() if parser is not None else None
                        )
                    except (AttributeError, TypeError, ValueError):
                        has_credentials = None
                    if cookie_configured:
                        cookie_status = (
                            "Cookie 待检查" if has_credentials is not False else "Cookie 已配置但失效"
                        )
                    available = False
                elif checked:
                    cookie_status, available = "Cookie 有效", True
                else:
                    cookie_status, available = "Cookie 已配置但失效", False
            elif not getattr(item, "enable", True):
                cookie_status = "解析器未启用"
                available = False
            elif provided is not None:
                cookie_status = provided["cookie_status"]
                available = provided["available"]
                cookie_configured = cookie_configured or provided["cookie_configured"]
            elif key == "netease":
                cookie_status = "Cookie 已配置" if cookie_configured else "Cookie 未配置（可选）"
                available = cookie_configured
            elif key in {"qqmusic", "kugou"}:
                if not cookie_configured:
                    cookie_status, available = "Cookie 未配置（可选）", False
                elif parser is None:
                    cookie_status, available = "Cookie 已配置，状态未就绪", False
                else:
                    try:
                        available = parser.has_auth_credentials()
                    except (AttributeError, TypeError, ValueError):
                        available = False
                    cookie_status = "账号 Cookie 已配置" if available else "账号 Cookie 不完整"
            else:
                cookie_status, available = (
                    ("Cookie 已配置", True) if cookie_configured
                    else ("Cookie 未配置", False)
                )
            settings = {}
            for field in fields:
                value = getattr(item, field, None)
                if field == "audio_send_format":
                    value = "file" if str(value or "voice").strip().lower() == "file" else "voice"
                elif field == "file_quality_priority":
                    value = list(normalize_qq_file_quality_priority(value))
                elif field == "parse_lyrics":
                    value = False if value is None else bool(value)
                elif field in MUSIC_BOOLEAN_SETTINGS:
                    value = True if value is None else bool(value)
                settings[field] = value
            entries[key] = {
                "configured": True,
                "known": True,
                "enabled": getattr(item, "enable", True) is True,
                "available": available,
                "cookie_configured": cookie_configured,
                "cookie_status": cookie_status,
                "cookie_validity": (
                    "valid" if cookie_status == "Cookie 有效" else
                    "invalid" if cookie_status == "Cookie 已配置但失效" else
                    "unconfigured" if not cookie_configured else "unknown"
                ),
                "settings": settings,
            }
        return entries

    def _known_parser_keys(self) -> set[str]:
        parser_config = getattr(self.cfg, "parser", None)
        raw = getattr(parser_config, "_nodes", None)
        if isinstance(raw, dict):
            return set(raw)
        value = getattr(self.cfg, "parsers_template", None)
        if isinstance(value, list):
            return {
                str(item.get("__template_key"))
                for item in value
                if isinstance(item, dict) and item.get("__template_key")
            }
        return set()

    def _parser_management_history(self) -> dict:
        value = getattr(self.cfg, "_parser_management_history", None)
        if isinstance(value, dict):
            return value
        raw_data = getattr(self.cfg, "_data", None)
        value = raw_data.get("__parser_management_history_v1") if isinstance(raw_data, dict) else None
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _default_parser_templates() -> dict[str, dict]:
        try:
            path = Path(__file__).parent.parent / "default_template.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, list):
            return {}
        return {
            item.get("__template_key"): item
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("__template_key"), str)
        }

    def _runtime_parsers(self) -> list[object]:
        if self._runtime_parser_provider is None:
            return []
        try:
            values = self._runtime_parser_provider()
        except Exception:
            return []
        return list(values) if isinstance(values, (list, tuple, set)) else []

    def _parser_statuses(self) -> list[dict]:
        """Return non-secret enablement and Cookie status for all known parsers."""

        labels = {
            "bilibili": "Bilibili", "douyin": "抖音", "xhs": "小红书",
            "pixiv": "Pixiv", "qqmusic": "QQ 音乐",
            "netease": "网易云音乐", "kugou": "酷狗音乐",
            "qishui": "汽水音乐", "kuwo": "酷我音乐",
            "applemusic": "Apple Music",
        }
        default_templates = self._default_parser_templates()
        rows: dict[str, dict] = {}
        if self._platform_status_provider is not None:
            try:
                provided = self._platform_status_provider()
            except Exception:
                provided = ()
            if isinstance(provided, (list, tuple)):
                for status in provided:
                    if isinstance(status, dict) and status.get("key") in labels:
                        text = status.get("text")
                        rows[status["key"]] = {
                            "available": status.get("available") is True,
                            "cookie_configured": status.get("cookie_configured") is True
                                or status.get("available") is True,
                            "cookie_status": text.strip()[:64] if isinstance(text, str) else "状态未确认",
                        }
        runtime = {
            getattr(getattr(parser, "platform", None), "name", ""): parser
            for parser in self._runtime_parsers()
        }
        entries = []
        default_keys = set(default_templates) | self._known_parser_keys()
        for key, label in labels.items():
            item = self._parser_item(key)
            known = key in default_keys or item is not None
            parser = runtime.get(key)
            state = rows.get(key, {})
            available = state.get("available", False)
            cookie_configured = state.get("cookie_configured", False)
            cookie_status = state.get("cookie_status", "Cookie 未配置")
            if item is None:
                configured = False
                enabled = False
                cookie_configured = bool(self._parser_management_history().get(key, {}).get("cookie_configured"))
                cookie_status = (
                    "Cookie 已配置（解析器未启用）" if cookie_configured else
                    "解析器未启用" if known else "未配置"
                )
            elif key == "applemusic":
                configured = item is not None
                enabled = configured and getattr(item, "enable", True) is True
                cookie_configured = False
                cookie_status = (
                    "账号后端未启用" if not self.settings.enabled else
                    "已登录" if self._last_auth_state == "authenticated" else
                    "未登录"
                )
                available = self.settings.enabled and self._last_auth_state == "authenticated"
            else:
                configured = True
                enabled = getattr(item, "enable", True) is True
                if key in {"xhs", "pixiv", "netease"} and not state:
                    cookiejar = getattr(parser, "cookiejar", None)
                    cookie_configured = bool(
                        getattr(cookiejar, "cookies_str", None)
                        or getattr(item, "cookies", None)
                    )
                    cookie_status = ("Cookie 已配置" if cookie_configured else "Cookie 未配置（可选）")
                    available = cookie_configured
                if key in {"qishui", "kuwo"}:
                    cookie_status = "无需 Cookie"
                    available = True
            entries.append({
                "key": key, "label": label, "known": known,
                "configured": configured, "enabled": enabled,
                "cookie_configured": cookie_configured,
                "cookie_status": cookie_status,
            })
            if entries[-1]["known"]:
                if key == "applemusic":
                    entries[-1]["cookie_status"] = cookie_status
                elif key in rows:
                    entries[-1]["cookie_status"] = cookie_status
                    entries[-1]["available"] = state.get("available", False)
                elif item is None:
                    pass
                elif key in {"qqmusic", "kugou"} and item is not None:
                    cookiejar = getattr(parser, "cookiejar", None)
                    cookie_configured = bool(
                        getattr(cookiejar, "cookies_str", None)
                        or getattr(item, "cookies", None)
                    )
                    if not enabled:
                        cookie_status = "解析器未启用"
                    elif not cookie_configured:
                        cookie_status = "Cookie 未配置（可选）"
                    elif parser is None:
                        cookie_status = "Cookie 已配置，状态未就绪"
                    elif parser.has_auth_credentials():
                        cookie_status = "账号 Cookie 已配置"
                        available = True
                    else:
                        cookie_status = "账号 Cookie 不完整"
                        available = False
                    if not cookie_configured:
                        available = False
                    entries[-1]["cookie_configured"] = cookie_configured
                    entries[-1]["cookie_status"] = cookie_status
                    entries[-1]["available"] = available
                elif key in {"qishui", "kuwo"} and item is not None:
                    entries[-1]["cookie_status"] = "无需 Cookie"
                    entries[-1]["available"] = True
                elif key in {"xhs", "pixiv", "netease"} and item is not None:
                    cookiejar = getattr(parser, "cookiejar", None)
                    cookie_configured = bool(
                        getattr(cookiejar, "cookies_str", None)
                        or getattr(item, "cookies", None)
                    )
                    entries[-1]["cookie_configured"] = cookie_configured
                    entries[-1]["cookie_status"] = "Cookie 已配置" if cookie_configured else "Cookie 未配置（可选）"
                    entries[-1]["available"] = cookie_configured
                elif key == "douyin" and item is not None and parser is not None:
                    cookie_configured = bool(parser.has_login_cookie())
                    entries[-1]["cookie_configured"] = cookie_configured
                    entries[-1]["cookie_status"] = "登录 Cookie 已配置" if cookie_configured else "登录 Cookie 未配置"
                    entries[-1]["available"] = cookie_configured
                elif key == "bilibili" and item is not None and parser is not None:
                    cookie_configured = bool(parser.login.has_cookie())
                    entries[-1]["cookie_configured"] = cookie_configured
                    entries[-1]["cookie_status"] = "登录 Cookie 已配置" if cookie_configured else "登录 Cookie 未配置"
                    entries[-1]["available"] = cookie_configured
                if not enabled and item is not None:
                    entries[-1]["cookie_status"] = "解析器未启用"
                    entries[-1]["available"] = False
        return entries

    def _status_platform_accounts(self, apple_auth_state: str = "unknown") -> list[dict]:
        """Build fixed platform badges, with configured vs missing distinction."""

        provider_statuses: dict[str, dict] = {}
        if self._platform_status_provider is not None:
            try:
                raw_statuses = self._platform_status_provider()
            except Exception:
                raw_statuses = ()
            if isinstance(raw_statuses, (list, tuple)):
                for status in raw_statuses:
                    if isinstance(status, dict) and status.get("key") in {"douyin", "bilibili", *MUSIC_SETTINGS.keys()}:
                        text = status.get("text")
                        provider_statuses[status["key"]] = {
                            "available": status.get("available") is True,
                            "text": text.strip()[:64] if isinstance(text, str) else "状态未确认",
                        }

        accounts = []
        for key, label in (("douyin", "抖音"), ("bilibili", "Bilibili"), ("qqmusic", "QQ 音乐"), ("kugou", "酷狗音乐"), ("applemusic", "Apple Music")):
            if key in {"qqmusic", "kugou"}:
                parser_status = provider_statuses.get(key)
                entry = self._parser_item(key)
                if entry is None:
                    known = key in self._known_parser_keys() or key in self._default_parser_templates()
                    if parser_status is not None:
                        available, text = parser_status["available"], parser_status["text"]
                    else:
                        available, text = False, "解析器未启用" if known else "未配置"
                elif getattr(entry, "enable", True) is not True:
                    available, text = False, "解析器未启用"
                elif parser_status is None:
                    has_cookie = bool(getattr(entry, "cookies", None))
                    available, text = has_cookie, "状态未就绪" if has_cookie else "Cookie 未配置"
                elif entry is not None and parser_status is not None:
                    available, text = parser_status["available"], parser_status["text"]
                else:
                    has_cookie = bool(getattr(entry, "cookies", None))
                    available, text = has_cookie, "Cookie 已配置，状态未就绪" if has_cookie else "Cookie 未配置"
            elif key in {"douyin", "bilibili"}:
                parser_status = provider_statuses.get(key)
                available, text = (parser_status["available"], parser_status["text"]) if parser_status else (False, "状态未就绪")
            else:
                if not self.settings.enabled:
                    available, text = False, "账号后端未启用"
                elif self.closed:
                    available, text = False, "账号后端已停止"
                else:
                    available = apple_auth_state == "authenticated"
                    text = {
                        "authenticated": "已登录", "logged_out": "未登录",
                        "awaiting_2fa": "等待验证码", "in_progress": "正在登录",
                        "failed": "登录失败",
                    }.get(apple_auth_state, "状态未确认")
            accounts.append({"key": key, "label": label, "available": available, "text": text})
        return accounts

    async def update_music_parser_settings(self, owner: str, values: object) -> dict:
        if not isinstance(values, dict) or not values:
            raise AppleMusicBackendError("没有可保存的音乐解析设置")
        if set(values) - MUSIC_SETTINGS.keys():
            raise AppleMusicBackendError("设置中包含不支持的音乐解析器")
        updates: list[tuple[object, str, object]] = []
        responses: dict[str, dict] = {}
        for parser_key, settings in values.items():
            if not isinstance(settings, dict) or not settings:
                raise AppleMusicBackendError("音乐解析器设置无效")
            allowed = MUSIC_SETTINGS[parser_key]
            if set(settings) - allowed:
                raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]}设置中包含不支持的字段")
            for field, value in settings.items():
                if field in MUSIC_BOOLEAN_SETTINGS | {"enable"} and not isinstance(value, bool):
                    raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]}开关设置无效")
                if field == "audio_send_format" and value not in {"voice", "file"}:
                    raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]}音频发送模式无效")
                if field == "file_quality_priority" and not is_valid_qq_file_quality_priority(value):
                    raise AppleMusicBackendError("QQ 音乐文件音质优先级无效")
            item = self._parser_item(parser_key)
            if item is None:
                raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]}解析器未配置，请先在插件配置页恢复该解析器配置")
            for field, value in settings.items():
                updates.append((item, field, list(value) if field == "file_quality_priority" else value))

        async with self.lock:
            self.check_owner(owner)
            previous = [(item, field, getattr(item, field, None)) for item, field, _ in updates]
            raw_data = getattr(self.cfg, "_data", None)
            history_store = raw_data.get("__parser_management_history_v1") if isinstance(raw_data, dict) else None
            history_snapshot = (
                json.loads(json.dumps(history_store))
                if isinstance(history_store, dict)
                else None
            )
            if isinstance(raw_data, dict) and not isinstance(history_store, dict):
                history_store = {}
                raw_data["__parser_management_history_v1"] = history_store
            try:
                for item, field, value in updates:
                    setattr(item, field, value)
                if isinstance(history_store, dict):
                    for parser_key, settings in values.items():
                        saved = history_store.get(parser_key)
                        saved = dict(saved) if isinstance(saved, dict) else {}
                        saved_settings = saved.get("settings")
                        saved_settings = dict(saved_settings) if isinstance(saved_settings, dict) else {}
                        saved_settings.update(settings)
                        saved.update(settings=saved_settings, cookie_configured=bool(saved.get("cookie_configured")))
                        history_store[parser_key] = saved
                save = getattr(self.cfg, "save_config", None)
                if not callable(save):
                    raise RuntimeError("configuration is not persistent")
                await asyncio.get_running_loop().run_in_executor(None, save)
            except Exception:
                for item, field, value in previous:
                    setattr(item, field, value)
                if isinstance(history_store, dict) and history_snapshot is not None:
                    history_store.clear()
                    history_store.update(history_snapshot)
                raise AppleMusicBackendError("音乐解析设置保存失败，请稍后重试") from None
            all_entries = self._parser_config_entries()
            for key in values:
                responses[key] = all_entries[key]
            return {"message": "音乐解析设置已保存并立即生效", "parsers": responses}

    async def update_music_cookie(self, owner: str, parser_key: object, cookie: object, *, clear: bool = False) -> dict:
        if parser_key not in MUSIC_SETTINGS:
            raise AppleMusicBackendError("未知的音乐解析器")
        if not isinstance(clear, bool):
            raise AppleMusicBackendError("清除 Cookie 参数无效")
        if clear is True:
            cookie = ""
        if not isinstance(cookie, str) or len(cookie) > 16384:
            raise AppleMusicBackendError("Cookie 无效或超过长度限制")
        from .cookie import CookieJar

        cookie = CookieJar.clean_cookies_str(cookie)
        item = self._parser_item(parser_key)
        if item is None:
            raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]}解析器未配置，请先在插件配置页恢复该解析器配置")
        async with self.lock:
            self.check_owner(owner)
            old_cookie = getattr(item, "cookies", None)
            cookiejars = [
                getattr(parser, "cookiejar", None) for parser in self._runtime_parsers()
                if getattr(getattr(parser, "platform", None), "name", None) == parser_key
                and getattr(parser, "cookiejar", None) is not None
            ]
            cookie_states = [(jar, jar.raw_cookies, list(jar.cookies), jar.cookies_str) for jar in cookiejars]
            raw_data = getattr(self.cfg, "_data", None)
            history_store = None
            if isinstance(raw_data, dict):
                history_store = raw_data.get("__parser_management_history_v1")
                if not isinstance(history_store, dict):
                    history_store = {}
                    raw_data["__parser_management_history_v1"] = history_store
            previous_check = self._music_cookie_checks.pop(parser_key, None)
            history_snapshot = None
            if history_store is not None:
                history_snapshot = json.loads(json.dumps(history_store))
            try:
                item.cookies = cookie
                for jar in cookiejars:
                    jar.replace_from_cookies_str(cookie)
                if history_store is not None:
                    saved = history_store.get(parser_key)
                    saved = dict(saved) if isinstance(saved, dict) else {}
                    saved["cookie_configured"] = bool(cookie)
                    history_store[parser_key] = saved
                save = getattr(self.cfg, "save_config", None)
                if not callable(save):
                    raise RuntimeError("configuration is not persistent")
                await asyncio.get_running_loop().run_in_executor(None, save)
            except Exception:
                item.cookies = old_cookie
                if history_store is not None and history_snapshot is not None:
                    history_store.clear()
                    history_store.update(history_snapshot)
                if previous_check is not None:
                    self._music_cookie_checks[parser_key] = previous_check
                for jar, raw, cookies, cookies_str in cookie_states:
                    jar.raw_cookies, jar.cookies, jar.cookies_str = raw, cookies, cookies_str
                    try:
                        jar.save_to_file()
                    except Exception:
                        pass
                raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]} Cookie 保存失败") from None
            entry = self._parser_config_entries()[parser_key]
            return {
                "message": f"{PARSER_LABELS[parser_key]} Cookie 已{ '清除' if clear or not cookie else '保存' }",
                "parser_key": parser_key,
                "parser": entry,
            }

    async def update_management_settings(self, owner: str, values: object) -> dict:
        """Validate and persist only the Apple Music fields exposed by the page."""

        if not isinstance(values, dict) or not values:
            raise AppleMusicBackendError("没有可保存的 Apple Music 设置")
        unknown = set(values) - MANAGED_SETTINGS
        if unknown:
            raise AppleMusicBackendError("设置中包含不支持的字段")
        for field in MANAGED_BOOLEAN_SETTINGS:
            if field in values and not isinstance(values[field], bool):
                raise AppleMusicBackendError("Apple Music 开关设置无效")
        if "audio_send_format" in values and values["audio_send_format"] not in {
            "file", "voice"
        }:
            raise AppleMusicBackendError("Apple Music 音频发送格式无效")
        if "file_codec_priority" in values:
            codecs = values["file_codec_priority"]
            if (
                not isinstance(codecs, list)
                or not 1 <= len(codecs) <= len(SUPPORTED_FILE_CODECS)
                or any(not isinstance(codec, str) for codec in codecs)
                or len(codecs) != len(set(codecs))
                or any(codec not in SUPPORTED_FILE_CODECS for codec in codecs)
            ):
                raise AppleMusicBackendError("Apple Music 文件音质优先级无效")

        async with self.lock:
            self.check_owner(owner)
            item = self._parser_item("applemusic")
            if item is None:
                raise AppleMusicBackendError("Apple Music 解析器未配置，请先在插件配置页恢复该解析器配置")
            previous = {key: getattr(item, key, None) for key in values}
            try:
                for key, value in values.items():
                    setattr(item, key, list(value) if key == "file_codec_priority" else value)
                save = getattr(self.cfg, "save_config", None)
                if not callable(save):
                    raise RuntimeError("configuration is not persistent")
                await asyncio.get_running_loop().run_in_executor(None, save)
            except Exception:
                for key, value in previous.items():
                    setattr(item, key, value)
                raise AppleMusicBackendError("Apple Music 设置保存失败，请稍后重试") from None

            # BackendSettings is immutable and normally created at plugin load.
            # Refresh it so backend_enabled takes effect without a plugin reload.
            self.settings = BackendSettings.from_config(self.cfg)
            self._probe_cache = None
            return {
                "message": "Apple Music 解析设置已保存并立即生效",
                "settings": self.management_settings(),
            }

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict:
        # read(n) may return only the first available HTTP chunk. Read to EOF
        # with an upper bound instead of parsing a truncated JSON document.
        raw = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise ValueError("response exceeds size limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("response is not an object")
        return payload

    async def _http(self, method: str, path: str, *, body=None, timeout=12) -> tuple[int, dict]:
        base = self._require_enabled()
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            )
        try:
            async with self._session.request(
                method, base + path, json=body, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                # Ignore error bodies: wrapper/Apple errors can contain tokens.
                if response.status not in (200, 202):
                    return response.status, {}
                return response.status, await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError):
            raise AppleMusicBackendError("无法连接 wrapper，或请求超时；请检查独立服务地址") from None
        except (ValueError, UnicodeError):
            raise AppleMusicBackendError("wrapper 返回了无效响应") from None

    async def _me(self) -> dict:
        status, payload = await self._http("GET", "/me")
        if status != 200:
            raise AppleMusicBackendError(f"wrapper 状态检查失败（HTTP {status}）")
        if payload.get("version") != WRAPPER_VERSION:
            raise AppleMusicBackendError(f"需要 wrapper-v2 API {WRAPPER_VERSION}，当前版本不兼容")
        if not isinstance(payload.get("auth"), dict):
            raise AppleMusicBackendError("wrapper 未提供可识别的登录状态")
        if not isinstance(payload.get("runtime", {}), dict):
            raise AppleMusicBackendError("wrapper 未提供可识别的运行状态")
        return payload

    async def _subscription(self, me: dict) -> tuple[bool | None, str | None, str]:
        auth = me["auth"]
        token, user_token = auth.get("dev_token"), auth.get("music_user_token")
        if not all(
            isinstance(value, str) and 0 < len(value) <= 32768
            and all(33 <= ord(character) <= 126 for character in value)
            and ";" not in value
            for value in (token, user_token)
        ):
            return None, None, "未取得账号令牌，尚不能检查订阅"
        try:
            assert self._session is not None
            async with self._session.get(
                "https://amp-api.music.apple.com/v1/me/account",
                params={"meta": "subscription"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://music.apple.com",
                    "Cookie": f"media-user-token={user_token}",
                },
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None, None, f"Apple 订阅检查失败（HTTP {response.status}），不代表没有订阅"
                payload = await self._read_json(response)
            subscription = payload.get("meta", {}).get("subscription", {})
            active = subscription.get("active")
            storefront = subscription.get("storefront")
            return (
                active if isinstance(active, bool) else None,
                storefront if isinstance(storefront, str) and re.fullmatch(r"[a-z]{2}", storefront) else None,
                "订阅已核验" if isinstance(active, bool) else "Apple 未返回订阅状态",
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
            return None, None, "暂时无法核验订阅，请稍后刷新"

    async def probe_gamdl(self) -> dict:
        if self._probe_cache and time.monotonic() - self._probe_cache[0] < 60:
            return dict(self._probe_cache[1])
        result = {"available": False, "version": None, "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
        if self.settings.python:
            try:
                data = await self._run(
                    [self.settings.python, "-c", "from gamdl.cli.cli import main; from importlib.metadata import version; print(version('gamdl'))"],
                    timeout=15, capture=True,
                )
                version = data.decode().strip()
                if re.fullmatch(r"\d+\.\d+\.\d+", version):
                    result.update(version=version, available=version == GAMDL_VERSION)
            except AppleMusicBackendError:
                pass
        self._probe_cache = time.monotonic(), result
        return dict(result)

    async def status(self, owner: str) -> dict:
        loop = asyncio.get_running_loop()
        cache_bytes, cache_files = await loop.run_in_executor(
            None, get_cache_size, self.cfg.cache_dir
        )
        result = {
            "enabled": self.settings.enabled,
            "connected": False,
            "version": None,
            "auth_state": "unknown",
            "subscription_active": None,
            "storefront": None,
            "playback_ready": False,
            "can_submit_2fa": False,
            "message": "请在插件配置中启用可选后端并配置 wrapper / gamdl Python",
            "subscription_message": "尚未核验订阅",
            "gamdl": {"available": False, "version": None, "ffmpeg": False, "ffprobe": False},
            "cache": {"bytes": cache_bytes, "files": cache_files},
            "settings": self.management_settings(),
            "platform_accounts": self.platform_accounts(),
            "parsers": self._parser_statuses(),
            "music_parsers": self._parser_config_entries(),
        }
        if not self.settings.enabled or self.closed:
            return result
        try:
            me, probe = await asyncio.gather(self._me(), self.probe_gamdl(), return_exceptions=True)
            if isinstance(probe, dict):
                result["gamdl"] = probe
            if isinstance(me, Exception):
                raise me
            auth = me["auth"]
            state = auth.get("state")
            result.update(
                connected=True, version=WRAPPER_VERSION,
                auth_state=state if state in ("authenticated", "logged_out", "awaiting_2fa", "in_progress", "failed") else "unknown",
                playback_ready=me.get("runtime", {}).get("playback_ready") is True,
                can_submit_2fa=self.pending_owner == owner and time.monotonic() < self.pending_until,
                message="wrapper 已连接",
            )
            if state == "authenticated":
                active, storefront, message = await self._subscription(me)
                result.update(subscription_active=active, storefront=storefront, subscription_message=message)
        except AppleMusicBackendError as exc:
            result["message"] = exc.message
        self._last_auth_state = result["auth_state"]
        result["platform_accounts"] = self.platform_accounts(result["auth_state"])
        result["parsers"] = self._parser_statuses()
        return result

    async def clear_cache(self, owner: str) -> dict:
        """Clear generated media/cards while keeping the configured root."""

        async with self.lock:
            self.check_owner(owner)
            loop = asyncio.get_running_loop()
            removed_files, removed_bytes = await loop.run_in_executor(
                None, clear_cache_dir, self.cfg.cache_dir
            )
            return {
                "message": "缓存已清理，账号登录状态未受影响",
                "removed_files": removed_files,
                "removed_bytes": removed_bytes,
            }

    def check_owner(self, owner: str, *, require_pending=False) -> None:
        if time.monotonic() >= self.pending_until:
            self.pending_owner = None
        if self.pending_owner and self.pending_owner != owner:
            raise AppleMusicBackendError("另一位管理员正在完成双重认证，请稍后再试")
        if require_pending and self.pending_owner != owner:
            raise AppleMusicBackendError("没有属于当前管理员的验证码会话，请重新登录")

    async def login(self, owner: str, username: str, password: str) -> dict:
        async with self.lock:
            self.check_owner(owner)
            await self._me()  # Verify protocol BEFORE sending credentials.
            status, _ = await self._http("POST", "/login", body={"username": username, "password": password}, timeout=90)
            if status == 202:
                self.pending_owner, self.pending_until = owner, time.monotonic() + 600
                return {"needs_2fa": True, "message": "请输入受信任设备收到的六位验证码（10 分钟内）"}
            if status != 200:
                raise AppleMusicBackendError(f"Apple 登录失败（HTTP {status}）；请检查账号或稍后重试")
            self.pending_owner = None
            return {"needs_2fa": False, "message": "登录请求已完成，请刷新核验登录和订阅状态"}

    async def two_factor(self, owner: str, code: str) -> dict:
        async with self.lock:
            self.check_owner(owner, require_pending=True)
            status, _ = await self._http("POST", "/login/2fa", body={"code": code}, timeout=90)
            if status not in (200, 202):
                raise AppleMusicBackendError(f"验证码提交失败（HTTP {status}），请重试或重新登录")
            if status == 202:
                return {"needs_2fa": True, "message": "仍需双重认证，请检查验证码后重试"}
            self.pending_owner = None
            return {"needs_2fa": False, "message": "验证码已提交，请刷新核验登录和订阅状态"}

    async def clear_session(self, owner: str) -> dict:
        async with self.lock:
            self.check_owner(owner)
            # This is NOT a full logout: Apple's on-disk mpl_db is unchanged.
            status, _ = await self._http("DELETE", "/login", timeout=20)
            if status != 200:
                raise AppleMusicBackendError(f"清除内存会话失败（HTTP {status}）")
            self.pending_owner = None
            return {"needs_2fa": False, "message": "已清除内存会话；wrapper 磁盘登录缓存仍在，重启可能恢复登录"}

    async def _run(self, argv: list[str], *, timeout: int, capture=False, cwd=None) -> bytes:
        if self.closed:
            raise AppleMusicBackendError("Apple Music 后端已停止")
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x08000000}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, **kwargs,
            )
        except (OSError, ValueError):
            raise AppleMusicBackendError("无法启动可选工具，请检查 gamdl Python / FFmpeg 配置") from None
        self._processes.add(process)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise AppleMusicBackendError("可选工具执行失败；请确认 gamdl 版本、登录状态和运行依赖")
            return (stdout or b"")[:65536]
        except TimeoutError:
            raise AppleMusicBackendError("Apple Music 操作超时，已终止任务；可稍后重试") from None
        finally:
            await self._stop_process(process)
            self._processes.discard(process)

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                # gamdl can launch FFmpeg. Terminate the bounded process tree.
                child = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                await asyncio.wait_for(child.wait(), 10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, TimeoutError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    def download_task(
        self, url: str, duration: float, *, codec_priority: tuple[str, ...] | None = None
    ) -> asyncio.Task[Path]:
        task = asyncio.create_task(
            self.download(url, duration=duration, codec_priority=codec_priority)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    def _valid_synced_lyrics(text: object) -> bool:
        """Return whether *text* contains at least one real LRC line.

        gamdl can finish successfully when a track has no lyrics (or when a
        provider returned metadata only), so the subprocess exit status is not
        enough.  Requiring a timestamp and non-empty lyric text keeps those
        cases on the LRCLIB fallback path.
        """

        if not isinstance(text, str) or not text.strip():
            return False
        return bool(
            re.search(
                r"(?m)^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s*\S.*$",
                text,
            )
        )

    async def fetch_synced_lyrics(self, url: str) -> str:
        """Fetch one song's native Apple Music synced lyrics through gamdl.

        This deliberately shares the wrapper/account and subprocess plumbing
        with :meth:`download`, but invokes gamdl in ``--synced-lyrics-only``
        mode.  No audio codec, FFmpeg, or ``return_audio`` setting is involved.
        The method raises :class:`AppleMusicBackendError` for every unavailable
        or invalid result; callers treat that as an optional-enrichment miss.
        """

        canonical, identifier = normalize_track_url(url)
        self._require_enabled()
        if not self.settings.python:
            raise AppleMusicBackendError("未配置独立 gamdl Python 环境")

        # Probe before contacting wrapper so an unconfigured/broken optional
        # backend falls back without an avoidable account request.
        probe = await self.probe_gamdl()
        if not isinstance(probe, dict) or probe.get("available") is not True:
            raise AppleMusicBackendError("gamdl 不可用或版本不兼容")

        me = await self._me()
        auth = me.get("auth") if isinstance(me, dict) else None
        if not isinstance(auth, dict) or auth.get("state") != "authenticated":
            raise AppleMusicBackendError("Apple Music 账号尚未完成认证")
        active, _storefront, _message = await self._subscription(me)
        if active is not True:
            raise AppleMusicBackendError("Apple Music 订阅无效或尚未核验")

        try:
            cache_root = get_active_cache_dir(self.cfg.cache_dir)
            directory = create_parse_cache_dir(cache_root, "applemusic_lyrics")
            output = directory / "output"
            temporary = directory / "temporary"
            output.mkdir()
            temporary.mkdir()
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise AppleMusicBackendError("无法创建 Apple Music 歌词临时目录") from exc

        # Use the same safe, explicit wrapper arguments as audio downloads.
        # The unique output directory prevents concurrent parses from seeing
        # each other's LRC files.
        try:
            configured_timeout = float(self.settings.timeout)
        except (TypeError, ValueError, OverflowError):
            configured_timeout = self._LYRICS_TIMEOUT
        await self._run(
            [
                self.settings.python,
                "-m",
                "gamdl",
                "--no-config-file",
                "--no-exceptions",
                "--log-level",
                "ERROR",
                "--use-wrapper",
                "--wrapper-url",
                self.settings.validate(),
                "--wrapper-decrypt-host",
                self.settings.decrypt_host,
                "--wrapper-decrypt-port",
                str(self.settings.decrypt_port),
                "--synced-lyrics-only",
                "--synced-lyrics-format",
                "lrc",
                "--output-path",
                str(output),
                "--temp-path",
                str(temporary),
                # Make the generated path deterministic, while still
                # validating it by walking the isolated output directory below.
                "--album-folder-template",
                "album",
                "--compilation-folder-template",
                "album",
                "--no-album-folder-template",
                "album",
                "--single-disc-file-template",
                identifier,
                "--multi-disc-file-template",
                identifier,
                "--no-album-file-template",
                identifier,
                canonical,
            ],
            timeout=min(self._LYRICS_TIMEOUT, max(1.0, configured_timeout)),
            cwd=str(directory),
        )

        candidates: list[tuple[Path, str]] = []
        try:
            output_root = output.resolve()
            for path in output.rglob("*"):
                if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".lrc":
                    continue
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(output_root):
                        continue
                    if path.stat().st_size <= 0 or path.stat().st_size > self._MAX_LYRICS_SIZE:
                        continue
                    text = path.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError):
                    continue
                if self._valid_synced_lyrics(text):
                    candidates.append((path, text.strip()))
        except OSError:
            candidates = []

        # A direct single-track request should produce exactly one LRC file.
        # If gamdl emitted none or an ambiguous set, do not risk displaying a
        # lyric belonging to another track.
        if len(candidates) != 1:
            raise AppleMusicBackendError("gamdl 未生成有效的同步歌词文件")
        return candidates[0][1]

    async def download(
        self,
        url: str,
        *,
        duration: float = 0,
        codec_priority: tuple[str, ...] | None = None,
    ) -> Path:
        canonical, identifier = normalize_track_url(url)
        try:
            if isinstance(duration, bool):
                raise ValueError
            duration = float(duration)
        except (TypeError, ValueError, OverflowError):
            duration = 0
        if not math.isfinite(duration) or duration <= 0:
            raise AppleMusicBackendError("未取得完整歌曲时长，暂不获取音频")
        if duration > self.cfg.max_duration:
            raise AppleMusicBackendError("歌曲时长超过插件配置限制")
        async with self.lock:
            self._require_enabled()
            if self.pending_owner and time.monotonic() < self.pending_until:
                raise AppleMusicBackendError("请先在虚质解离器管理页的音乐管理中完成双重认证")
            me = await self._me()
            if me["auth"].get("state") != "authenticated":
                raise AppleMusicBackendError("请先在虚质解离器管理页的音乐管理中登录")
            active, _, _ = await self._subscription(me)
            if active is not True:
                raise AppleMusicBackendError("未能确认有效 Apple Music 订阅，请先在管理页检查")
            probe = await self.probe_gamdl()
            if not probe["available"] or not probe["ffmpeg"] or not probe["ffprobe"]:
                raise AppleMusicBackendError(f"请配置独立环境中的 gamdl {GAMDL_VERSION}，并安装 FFmpeg / FFprobe")
            if codec_priority is None:
                try:
                    send_format = getattr(self.cfg.parser.applemusic, "audio_send_format", "file")
                except (AttributeError, TypeError):
                    send_format = "file"
                if str(send_format or "").strip().lower() == "voice":
                    codec_priority = VOICE_CODEC_PRIORITY
                else:
                    try:
                        configured_priority = getattr(
                            self.cfg.parser.applemusic, "file_codec_priority", None
                        )
                    except (AttributeError, TypeError):
                        configured_priority = None
                    codec_priority = normalize_file_codec_priority(configured_priority)
            elif not isinstance(codec_priority, (list, tuple)):
                raise AppleMusicBackendError("Apple Music 音质配置无效")
            codec_priority = tuple(codec_priority)
            if not codec_priority or any(
                not isinstance(codec, str) or codec not in SUPPORTED_FILE_CODECS
                for codec in codec_priority
            ):
                raise AppleMusicBackendError("Apple Music 音质配置无效")
            codec_arg = ",".join(codec_priority)
            directory = create_parse_cache_dir(get_active_cache_dir(self.cfg.cache_dir), "applemusic")
            output, temporary = directory / "output", directory / "temporary"
            output.mkdir()
            temporary.mkdir()
            await self._run([
                self.settings.python, "-m", "gamdl", "--no-config-file", "--no-exceptions",
                "--log-level", "ERROR", "--use-wrapper", "--wrapper-url", self.settings.validate(),
                "--wrapper-decrypt-host", self.settings.decrypt_host,
                "--wrapper-decrypt-port", str(self.settings.decrypt_port),
                "--song-codec-priority", codec_arg, "--no-synced-lyrics",
                "--output-path", str(output), "--temp-path", str(temporary),
                "--album-folder-template", "album", "--compilation-folder-template", "album",
                "--no-album-folder-template", "album",
                "--single-disc-file-template", identifier,
                "--multi-disc-file-template", identifier,
                "--no-album-file-template", identifier, canonical,
            ], timeout=self.settings.timeout, cwd=str(directory))
            # gamdl can exit zero after a track error, so exit code is not proof.
            files = [p for p in output.rglob("*.m4a") if p.is_file() and not p.is_symlink()]
            if len(files) != 1 or not files[0].resolve().is_relative_to(output.resolve()):
                raise AppleMusicBackendError("gamdl 未生成唯一的歌曲音频；登录不代表下载可用")
            path = files[0]
            if not 0 < path.stat().st_size <= self.cfg.max_size:
                raise AppleMusicBackendError("生成的音频为空或超过插件大小限制")
            raw = await self._run([
                shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,codec_tag_string", "-of", "json", str(path),
            ], timeout=20, capture=True)
            try:
                info = json.loads(raw)
                actual = float(info["format"]["duration"])
                playable = any(
                    isinstance(s, dict) and s.get("codec_type") == "audio"
                    and s.get("codec_name") in {
                        "aac", "alac", "ac3", "eac3", "ec-3",
                    }
                    and s.get("codec_tag_string") != "enca"
                    for s in info["streams"]
                )
                # Check the decoded file, not a preview URL or a mere file name.
                if not playable or not math.isfinite(actual) or actual <= 0 or actual > self.cfg.max_duration:
                    raise ValueError
                if duration and abs(actual - duration) > max(5, duration * 0.05):
                    raise AppleMusicBackendError("音频时长与歌曲不一致，可能只有试听，已拒绝发送")
            except (KeyError, TypeError, ValueError):
                raise AppleMusicBackendError("输出文件未通过音频格式/时长检查") from None
            return path

    async def close(self) -> None:
        self.closed = True
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for process in tuple(self._processes):
            await self._stop_process(process)
        if self._session:
            await self._session.close()

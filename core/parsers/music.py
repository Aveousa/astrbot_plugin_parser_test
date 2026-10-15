"""Music playlist and single-track parsers for supported music platforms.

Playlist links produce a compact summary instead of downloading every track;
single-track links expose the same card fields and attach audio when a playable
media URL can be resolved.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from aiohttp import ClientError

from ..cookie import CookieJar
from ..data import AudioContent, ImageContent, ParseResult, Platform
from ..exception import DownloadException, ParseException, RedirectException
from ..music_quality import (
    QQ_QUALITY_FILENAMES,
    QQ_VOICE_QUALITY_PRIORITY,
    normalize_qq_file_quality_priority,
)
from .base import BaseParser, handle
from .kugou_login import KugouMusicLogin
from .qqmusic_login import QQMusicLogin


class _JavaScriptReference:
    """A reference used by Nuxt's compact server-state serializer."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class _NuxtValueParser:
    """Parse the JSON-like literal format emitted by Nuxt SSR.

    Kuwo serializes ``window.__NUXT__`` as ``function(a, b, ...){return
    {...}}(value_a, value_b, ...)``.  It is deliberately not valid JSON, but
    the payload itself only uses literals, arrays, objects and references to
    the function arguments.  Keeping this tiny parser local avoids requiring
    a JavaScript runtime just to read playlist metadata.
    """

    _IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")
    _NUMBER_RE = re.compile(r"-?(?:\d+\.\d*|\d+)(?:[eE][+-]?\d+)?")

    def __init__(self, source: str, position: int = 0):
        self.source = source
        self.position = position

    def _skip_space(self) -> None:
        while self.position < len(self.source) and self.source[self.position].isspace():
            self.position += 1

    def parse(self) -> Any:
        self._skip_space()
        if self.position >= len(self.source):
            raise ValueError("unexpected end of JavaScript value")
        char = self.source[self.position]
        if char in "'\"":
            return self._parse_string()
        if char == "{":
            return self._parse_object()
        if char == "[":
            return self._parse_array()
        if char in "-0123456789":
            return self._parse_number()
        if self.source.startswith("!0", self.position):
            self.position += 2
            return True
        if self.source.startswith("!1", self.position):
            self.position += 2
            return False
        match = self._IDENTIFIER_RE.match(self.source, self.position)
        if match:
            self.position = match.end()
            name = match.group(0)
            if name == "true":
                return True
            if name == "false":
                return False
            if name in {"null", "undefined"}:
                return None
            return _JavaScriptReference(name)
        raise ValueError(f"unsupported JavaScript value near {self.source[self.position:self.position + 40]!r}")

    def _parse_string(self) -> str:
        quote = self.source[self.position]
        self.position += 1
        result: list[str] = []
        while self.position < len(self.source):
            char = self.source[self.position]
            self.position += 1
            if char == quote:
                return "".join(result)
            if char != "\\":
                result.append(char)
                continue
            if self.position >= len(self.source):
                break
            escaped = self.source[self.position]
            self.position += 1
            simple_escapes = {
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "b": "\b",
                "f": "\f",
                "v": "\v",
                "0": "\0",
            }
            if escaped in simple_escapes:
                result.append(simple_escapes[escaped])
            elif escaped == "u":
                code = self.source[self.position:self.position + 4]
                if len(code) != 4:
                    raise ValueError("invalid Unicode escape in JavaScript string")
                self.position += 4
                result.append(chr(int(code, 16)))
            elif escaped == "x":
                code = self.source[self.position:self.position + 2]
                if len(code) != 2:
                    raise ValueError("invalid hex escape in JavaScript string")
                self.position += 2
                result.append(chr(int(code, 16)))
            elif escaped in "\r\n":
                if escaped == "\r" and self.position < len(self.source) and self.source[self.position] == "\n":
                    self.position += 1
            else:
                result.append(escaped)
        raise ValueError("unterminated JavaScript string")

    def _parse_number(self) -> int | float:
        match = self._NUMBER_RE.match(self.source, self.position)
        if not match:
            raise ValueError("invalid JavaScript number")
        text = match.group(0)
        self.position = match.end()
        return float(text) if any(char in text for char in ".eE") else int(text)

    def _parse_key(self) -> str:
        self._skip_space()
        if self.position >= len(self.source):
            raise ValueError("missing object key")
        if self.source[self.position] in "'\"":
            return self._parse_string()
        match = self._IDENTIFIER_RE.match(self.source, self.position)
        if not match:
            match = self._NUMBER_RE.match(self.source, self.position)
        if not match:
            raise ValueError("invalid object key")
        self.position = match.end()
        return match.group(0)

    def _parse_object(self) -> dict[str, Any]:
        self.position += 1
        result: dict[str, Any] = {}
        self._skip_space()
        while self.position < len(self.source) and self.source[self.position] != "}":
            key = self._parse_key()
            self._skip_space()
            if self.position >= len(self.source) or self.source[self.position] != ":":
                raise ValueError("missing object value separator")
            self.position += 1
            result[key] = self.parse()
            self._skip_space()
            if self.position < len(self.source) and self.source[self.position] == ",":
                self.position += 1
                self._skip_space()
                continue
            if self.position >= len(self.source) or self.source[self.position] != "}":
                raise ValueError("missing object item separator")
        if self.position >= len(self.source):
            raise ValueError("unterminated JavaScript object")
        self.position += 1
        return result

    def _parse_array(self) -> list[Any]:
        self.position += 1
        result: list[Any] = []
        self._skip_space()
        while self.position < len(self.source) and self.source[self.position] != "]":
            result.append(self.parse())
            self._skip_space()
            if self.position < len(self.source) and self.source[self.position] == ",":
                self.position += 1
                self._skip_space()
                continue
            if self.position >= len(self.source) or self.source[self.position] != "]":
                raise ValueError("missing array item separator")
        if self.position >= len(self.source):
            raise ValueError("unterminated JavaScript array")
        self.position += 1
        return result


def _resolve_nuxt_references(value: Any, arguments: Mapping[str, Any]) -> Any:
    if isinstance(value, _JavaScriptReference):
        return arguments.get(value.name)
    if isinstance(value, list):
        return [_resolve_nuxt_references(item, arguments) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_nuxt_references(item, arguments)
            for key, item in value.items()
        }
    return value


def _first_text(value: object, *keys: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        item = value.get(key)
        if item is not None and str(item).strip():
            return str(item).strip()
    return None


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value: object, *, milliseconds: bool = False) -> int | None:
    number = _as_int(value)
    if number is None:
        return None
    if milliseconds or number > 10_000_000_000:
        number //= 1000
    return number if number > 0 else None


def _date_timestamp(value: object, *, milliseconds: bool = False) -> int | None:
    """Normalize numeric and ISO date values exposed by music platforms."""

    if timestamp := _timestamp(value, milliseconds=milliseconds):
        return timestamp
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp())


def _duration_seconds(value: object) -> float:
    """Normalize the duration formats returned by music platforms.

    The APIs are not consistent here: QQ Music and Qishui normally return
    seconds, NetEase returns milliseconds, while some Kuwo pages expose a
    display value such as ``02:46`` and Apple Music uses ISO 8601 in JSON-LD.
    Keeping the conversion in one place
    makes the single-track card and any optional audio content use the same
    duration regardless of the source platform.
    """

    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        # Apple Music's JSON-LD uses ISO 8601 durations rather than milliseconds.
        iso_duration = re.fullmatch(
            r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
            text,
        )
        if iso_duration:
            hours, minutes, seconds = (
                float(part or 0) for part in iso_duration.groups()
            )
            return hours * 3600 + minutes * 60 + seconds
        if ":" in text:
            parts = text.split(":")
            try:
                seconds = 0.0
                for part in parts:
                    seconds = seconds * 60 + float(part.strip())
                return max(0.0, seconds)
            except (TypeError, ValueError, OverflowError):
                return 0.0
        value = text.replace(",", "")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if seconds > 10_000:
        seconds /= 1000
    return max(0.0, seconds)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = unescape(str(value)).replace("\r\n", "\n").strip()
    return text or None


def _cover_url(value: object, *, size: int = 400) -> str | None:
    """Normalize the cover formats used by the supported music APIs."""

    if isinstance(value, Mapping):
        # Qishui uses a URI plus one or more image hosts and a template prefix.
        uri = _first_text(value, "uri")
        hosts = value.get("urls")
        host = hosts[0] if isinstance(hosts, list) and hosts else None
        prefix = _first_text(value, "template_prefix")
        if uri and isinstance(host, str):
            # Owner avatars already contain a complete URL, while covers use
            # a host prefix plus a URI/template pair.
            if "." in host.rsplit("/", 1)[-1] or "?" in host:
                return host
            uri = uri.lstrip("/")
            if prefix:
                return (
                    f"{host.rstrip('/')}/{uri}~{prefix}-crop-center:"
                    f"{size}:{size}.jpg"
                )
            return f"{host.rstrip('/')}/{uri}"
        value = uri
    if not value:
        return None
    url = str(value).strip()
    if not url:
        return None
    url = (
        url.replace("{size}", str(size))
        .replace("{w}", str(size))
        .replace("{h}", str(size))
        .replace("{f}", "jpg")
    )
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def _format_count(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value:,}"


def _track_title(track: Mapping[str, Any]) -> str | None:
    return _first_text(track, "title", "songname", "remark", "name")


def _track_artist_names(track: Mapping[str, Any]) -> list[str]:
    artists: object | None = None
    for key in (
        "artists",
        "ar",  # NetEase Cloud Music
        "singer",
        "singers",
        "artist",
        "singerinfo",
    ):
        value = track.get(key)
        if value:
            artists = value
            break

    if isinstance(artists, Mapping):
        artists = [artists]

    names: list[str] = []
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, Mapping):
                name = _first_text(
                    artist, "name", "nickname", "artistname", "singername"
                )
            else:
                name = str(artist).strip()
            if name:
                names.append(name)
    elif isinstance(artists, str) and artists.strip():
        names.append(artists.strip())

    if not names:
        name = _first_text(track, "artistname", "singername", "artist_name")
        if name:
            names.append(name)
    return names


def _track_album_name(track: Mapping[str, Any]) -> str | None:
    album = track.get("album") or track.get("al") or track.get("albuminfo")
    if isinstance(album, Mapping):
        return _first_text(album, "name", "title", "albumname")
    if isinstance(album, str) and album.strip():
        return album.strip()
    return _first_text(track, "albumname", "album_name")


def _track_cover_url(track: Mapping[str, Any]) -> str | None:
    album = track.get("album") or track.get("al") or track.get("albuminfo")
    if isinstance(album, Mapping):
        for key in ("picUrl", "picurl", "cover", "url_cover"):
            if (value := album.get(key)) and (url := _cover_url(value, size=300)):
                return url
        # QQ Music's detail endpoint exposes an album mid instead of a URL.
        if pmid := _first_text(album, "pmid"):
            return f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{pmid}.jpg"

    for key in ("cover", "picUrl", "picurl", "url_cover"):
        if (value := track.get(key)) and (url := _cover_url(value, size=300)):
            return url
    return None


def _qq_artist_avatar_url(track: Mapping[str, Any], *, size: int = 300) -> str | None:
    """Build QQ Music's artist portrait URL from the first singer MID.

    The QQ song-detail response exposes singer identities in ``singer`` but
    does not include a portrait URL.  QQ's public image CDN uses the singer
    MID in the ``T001`` path.  Keep this helper limited to the first singer so
    collaborations use the primary artist's avatar consistently.
    """

    singers = track.get("singer")
    if isinstance(singers, Mapping):
        singers = [singers]
    if not isinstance(singers, list) or not singers:
        return None
    first_singer = singers[0]
    if not isinstance(first_singer, Mapping):
        return None
    singer_mid = _first_text(first_singer, "mid", "singer_mid")
    if not singer_mid:
        return None
    return f"https://y.gtimg.cn/music/photo_new/T001R{size}x{size}M000{singer_mid}.jpg"


def _track_summary(index: int, track: Mapping[str, Any]) -> dict[str, Any] | None:
    title = _track_title(track)
    if not title:
        return None
    artist_names = _track_artist_names(track)
    return {
        "index": index,
        "title": title,
        "artist": " / ".join(artist_names) if artist_names else None,
        "album": _track_album_name(track),
        "cover_url": _track_cover_url(track),
    }


class PlaylistParserBase(BaseParser):
    """Shared card mapping and HTTP helpers for playlist parsers."""

    platform = Platform(name="music_base", display_name="音乐")
    _MAX_PREVIEW_TRACKS = 5

    def _music_parser_config(self):
        """Read this parser's settings without assuming its config row exists.

        Parser rows can be intentionally removed from the AstrBot parser list.
        Read paths should then use safe defaults instead of raising and
        breaking the management page or an already-created parser instance.
        """

        parsers = getattr(self.cfg, "parser", None)
        getter = getattr(parsers, "get", None)
        if callable(getter):
            return getter(self.platform.name)
        return getattr(parsers, self.platform.name, None)

    def _return_audio_enabled(self) -> bool:
        value = getattr(self._music_parser_config(), "return_audio", None)
        return True if value is None else bool(value)

    def _audio_send_format(self) -> str:
        value = getattr(self._music_parser_config(), "audio_send_format", None)
        return "file" if str(value or "voice").strip().lower() == "file" else "voice"

    def _set_audio_delivery(self, result: ParseResult, audio_url: str) -> None:
        """Choose OneBot record vs file delivery for this music track."""

        if self._audio_send_format() == "file":
            # Download into the active parse cache, expose it through the
            # same short-lived AstrBot token URL as Apple Music, and have the
            # OneBot adapter send a file segment.
            result.extra["audio_as_file"] = True
            result.extra["audio_send_via_url"] = True
            result.extra["audio_send_url"] = None
            # Extra data survives subsequent downloader-task completion. Keep
            # the chosen mode there because create_audio_content begins with
            # legacy voice defaults.
            result.extra["audio_delivery_mode"] = "file"
            return
        result.extra["audio_as_voice"] = True
        result.extra["audio_send_url"] = audio_url
        result.extra["audio_delivery_mode"] = "voice"

    async def _json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        data: str | None = None,
    ) -> Any:
        request_headers = dict(self.headers)
        if headers:
            request_headers.update(headers)
        try:
            async with self.session.request(
                method,
                url,
                headers=request_headers,
                data=data,
            ) as response:
                if response.status >= 400:
                    raise ParseException(
                        f"请求音乐平台接口失败: HTTP {response.status}"
                    )
                text = await response.text()
        except ParseException:
            raise
        except (ClientError, TimeoutError) as exc:
            raise ParseException(f"请求音乐平台接口失败: {exc}") from exc

        # QQ Music's legacy endpoint defaults to JSONP even when format=json.
        text = text.strip()
        if text.startswith("jsonCallback(") and text.endswith(")"):
            text = text[len("jsonCallback(") : -1]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseException("音乐平台接口返回了无法识别的数据") from exc

    async def _optional_image_path(self, url: str) -> Path:
        """Download an auxiliary album cover without surfacing a card-only error."""

        try:
            return await self.downloader.download_img(
                url,
                headers=self.headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
        except (DownloadException, OSError, RuntimeError, TimeoutError, TypeError):
            # Album art is an enhancement.  Returning a non-file path lets the
            # renderer omit just that thumbnail while keeping the card usable.
            return Path()

    def _optional_image_content(self, url: str) -> ImageContent:
        return ImageContent(asyncio.create_task(self._optional_image_path(url)))

    async def _redirect_and_parse(self, url: str) -> ParseResult:
        redirected = await self.get_redirect_url(url, headers=self.headers)
        if redirected == url:
            raise ParseException("分享链接没有返回可识别的歌单地址")
        keyword, searched = self.search_url(redirected)
        return await self.parse(keyword, searched)

    @staticmethod
    def _track_line(index: int, track: Mapping[str, Any]) -> str | None:
        summary = _track_summary(index, track)
        if not summary:
            return None
        suffix = f" — {summary['artist']}" if summary["artist"] else ""
        return f"{index}. {summary['title']}{suffix}"

    def _show_playlist_cover(self) -> bool:
        """Return the per-platform preference for the main playlist cover.

        Older configurations do not have this field yet; treating a missing
        value as enabled keeps their existing card appearance unchanged.
        """

        parser_config = self._music_parser_config()
        value = getattr(parser_config, "show_playlist_cover", None)
        return True if value is None else bool(value)

    def _show_playlist_url(self) -> bool:
        """Return whether the card should expose the source playlist URL."""

        parser_config = self._music_parser_config()
        value = getattr(parser_config, "show_playlist_url", None)
        return True if value is None else bool(value)

    def _playlist_result(
        self,
        *,
        title: str,
        author_name: str,
        author_avatar: str | None,
        description: str | None,
        cover: str | None,
        track_count: int | None,
        timestamp: int | None,
        url: str,
        identifier: str,
        tracks: list[Mapping[str, Any]],
        stats: Mapping[str, object] | None = None,
        extra_lines: list[str] | None = None,
    ) -> ParseResult:
        stats = stats or {}
        preview_lines: list[str] = []
        playlist_tracks: list[dict[str, Any]] = []
        for index, track in enumerate(tracks[: self._MAX_PREVIEW_TRACKS], start=1):
            summary = _track_summary(index, track)
            if not summary:
                continue
            cover_url = summary.pop("cover_url", None)
            if cover_url and bool(getattr(self.cfg, "card_enabled", True)):
                summary["cover_content"] = self._optional_image_content(cover_url)
            playlist_tracks.append(summary)
            if line := self._track_line(index, track):
                preview_lines.append(line)

        info_lines: list[str] = []
        card_info_lines: list[str] = []
        if track_count is not None:
            info_lines.append(f"歌曲数: {_format_count(track_count)}")
        for label, key in (
            ("播放", "plays"),
            ("访问", "visits"),
        ):
            if value := _as_int(stats.get(key)):
                line = f"{label}: {_format_count(value)}"
                info_lines.append(line)
                card_info_lines.append(line)
            elif value == 0:
                # Preserve an explicit zero, but do not show absent values.
                info_lines.append(f"{label}: 0")
                card_info_lines.append(f"{label}: 0")
        if extra_lines:
            extra_info_lines = [line for line in extra_lines if line]
            info_lines.extend(extra_info_lines)
            card_info_lines.extend(extra_info_lines)
        if preview_lines:
            info_lines.append("部分歌曲:")
            info_lines.extend(preview_lines)
            if track_count and track_count > len(preview_lines):
                info_lines.append("…")

        contents = (
            self.create_image_contents([cover], headers=self.headers)
            if cover and self._show_playlist_cover()
            else []
        )
        return self.result(
            author=self.create_author(author_name or "未知用户", author_avatar),
            title=title or "未命名歌单",
            text=description,
            timestamp=timestamp,
            url=url,
            contents=contents,
            comment_count=_as_int(stats.get("comments")),
            favorite_count=_as_int(stats.get("favorites")),
            share_count=_as_int(stats.get("shares")),
            extra={
                "info": "\n".join(info_lines),
                # Sender 仍可使用完整的纯文本回退；卡片使用结构化歌曲行，
                # 因此不在卡片底部重复渲染这段列表。
                "card_info": "\n".join(card_info_lines),
                "playlist_tracks": playlist_tracks,
                "playlist_id": identifier,
                "playlist_track_count": track_count,
                # 这张封面用于信息卡片预览；发送器在卡片成功发送后会
                # 将它从独立媒体消息中排除，避免同一封面重复发送。
                "playlist_cover_only": bool(contents),
                "show_playlist_url": self._show_playlist_url(),
            },
        )

    def _single_track_result(
        self,
        *,
        title: str,
        artist_name: str,
        album_name: str | None,
        cover: str | None,
        duration: object,
        url: str,
        identifier: str,
        author_avatar: str | Callable[[], Any] | None = None,
        timestamp: int | None = None,
        audio_url: str | None = None,
        audio_headers: Mapping[str, str] | None = None,
        stats: Mapping[str, object] | None = None,
        download_cover: bool = True,
    ) -> ParseResult:
        """Build the common card/audio representation for a single song."""

        duration_value = _duration_seconds(duration)

        contents = (
            self.create_image_contents([cover], headers=self.headers) if cover and download_cover else []
        )
        if audio_url and audio_url.startswith(("http://", "https://")):
            contents.append(
                self.create_audio_content(
                    audio_url,
                    duration=duration_value,
                    headers=dict(audio_headers or self.headers),
                )
            )

        info_lines: list[str] = []
        if album_name:
            info_lines.append(f"专辑: {album_name}")
        if duration_value:
            minutes, seconds = divmod(int(duration_value), 60)
            info_lines.append(f"时长: {minutes}:{seconds:02d}")

        stats = stats if isinstance(stats, Mapping) else {}

        def stat_value(*keys: str) -> int | None:
            for key in keys:
                if key in stats:
                    value = _as_int(stats.get(key))
                    if value is not None:
                        return value
            return None

        return self.result(
            author=self.create_author(artist_name or "未知艺术家", author_avatar),
            title=title or "未命名歌曲",
            timestamp=timestamp,
            url=url,
            contents=contents,
            comment_count=stat_value("comments", "comment_count", "count_comment"),
            favorite_count=stat_value(
                "favorites", "favorite_count", "count_collected"
            ),
            share_count=stat_value("shares", "share_count", "count_shared"),
            extra={
                "info": "\n".join(info_lines),
                "card_info": "\n".join(info_lines),
                "music_type": "track",
                "track_id": identifier,
                "duration": duration_value,
                "card_preview_only": bool(cover),
                "show_playlist_url": self._show_playlist_url(),
            },
        )


class KuwoMusicParser(PlaylistParserBase):
    platform = Platform(name="kuwo", display_name="酷我音乐")

    @staticmethod
    def _decode_nuxt_payload(html: str) -> Mapping[str, Any]:
        marker = re.search(
            r"window\.__NUXT__\s*=\s*\(function\((.*?)\)\s*\{\s*return\s*",
            html,
            flags=re.DOTALL,
        )
        if not marker:
            raise ParseException("酷我音乐页面没有返回可识别的歌单数据")

        parameters = [item.strip() for item in marker.group(1).split(",")]
        parser = _NuxtValueParser(html, marker.end())
        try:
            serialized = parser.parse()
            parser._skip_space()
            if parser.position >= len(html) or html[parser.position] != "}":
                raise ValueError("missing Nuxt function terminator")
            parser.position += 1
            parser._skip_space()
            if parser.position >= len(html) or html[parser.position] != "(":
                raise ValueError("missing Nuxt argument list")
            parser.position += 1
            arguments: list[Any] = []
            parser._skip_space()
            while parser.position < len(html) and html[parser.position] != ")":
                arguments.append(parser.parse())
                parser._skip_space()
                if parser.position < len(html) and html[parser.position] == ",":
                    parser.position += 1
                    parser._skip_space()
                    continue
                if parser.position >= len(html) or html[parser.position] != ")":
                    raise ValueError("invalid Nuxt argument separator")
            if len(arguments) != len(parameters):
                raise ValueError("Nuxt parameter and argument counts differ")
        except (ValueError, TypeError, OverflowError) as exc:
            raise ParseException("酷我音乐页面数据格式无法识别") from exc

        resolved = _resolve_nuxt_references(serialized, dict(zip(parameters, arguments)))
        if not isinstance(resolved, Mapping):
            raise ParseException("酷我音乐页面没有返回可识别的歌单数据")
        return resolved

    @handle(
        "m.kuwo.cn/newh5app/playlist_detail",
        r"m\.kuwo\.cn/newh5app/playlist_detail/(?P<playlist_id>\d+)(?:\?[^\s<>]*)?",
    )
    @handle(
        "www.kuwo.cn/playlist_detail",
        r"www\.kuwo\.cn/playlist_detail/(?P<playlist_id>\d+)(?:\?[^\s<>]*)?",
    )
    @handle(
        "kuwo.cn/playlist_detail",
        r"(?<!www\.)kuwo\.cn/playlist_detail/(?P<playlist_id>\d+)(?:\?[^\s<>]*)?",
    )
    async def _handle_playlist(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.headers, "Referer": "https://www.kuwo.cn/"},
        ) as response:
            # Kuwo may return HTTP 430 while embedding complete SSR data.
            html = await response.text()

        payload = self._decode_nuxt_payload(html)
        data = payload.get("data")
        page = data[0] if isinstance(data, list) and data else {}
        page = page if isinstance(page, Mapping) else {}
        playlist = page.get("playListInfo")
        playlist = playlist if isinstance(playlist, Mapping) else None
        if not playlist:
            raise ParseException("酷我音乐歌单不存在或暂时无法访问")

        tracks = playlist.get("musicList")
        tracks = tracks if isinstance(tracks, list) else []
        page_data = page.get("pageData")
        page_data = page_data if isinstance(page_data, Mapping) else {}
        playlist_id = _first_text(page, "playlistId") or searched.group("playlist_id")
        return self._playlist_result(
            title=_first_text(playlist, "name") or "酷我音乐歌单",
            author_name=_first_text(playlist, "userName", "uname") or "未知用户",
            author_avatar=_cover_url(playlist.get("uPic"), size=165),
            description=_clean_text(playlist.get("desc")),
            cover=_cover_url(
                playlist.get("img700")
                or playlist.get("img500")
                or playlist.get("img300")
                or playlist.get("img")
            ),
            track_count=_as_int(playlist.get("total") or page_data.get("total")),
            timestamp=_timestamp(
                playlist.get("createTime")
                or playlist.get("create_time")
                or playlist.get("ctime")
            ),
            url=url,
            identifier=playlist_id,
            tracks=[item for item in tracks if isinstance(item, Mapping)],
            stats={"visits": playlist.get("listencnt")},
        )


    @handle(
        "m.kuwo.cn/yinyue",
        r"m\.kuwo\.cn/yinyue/(?P<track_id>\d+)(?:\?[^\s<>]*)?",
    )
    @handle(
        "m.kuwo.cn/h5app/single",
        r"m\.kuwo\.cn/h5app/single/(?P<track_id>\d+)(?:\?[^\s<>]*)?",
    )
    @handle(
        "www.kuwo.cn/play_detail",
        r"www\.kuwo\.cn/play_detail/(?P<track_id>\d+)(?:\?[^\s<>]*)?",
    )
    async def _handle_track(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.headers, "Referer": "https://www.kuwo.cn/"},
        ) as response:
            if response.status >= 400:
                raise ParseException(f"酷我音乐歌曲请求失败：HTTP {response.status}")
            html = await response.text()

        payload = self._decode_nuxt_payload(html)
        data = payload.get("data")
        page = data[0] if isinstance(data, list) and data else {}
        page = page if isinstance(page, Mapping) else {}
        song = page.get("songinfo")
        song = song if isinstance(song, Mapping) else None
        if not song:
            raise ParseException("酷我音乐歌曲不存在或暂时无法访问")

        track_id = str(song.get("rid") or searched.group("track_id"))
        return self._single_track_result(
            title=_first_text(song, "name", "songname") or "未命名歌曲",
            artist_name=_first_text(song, "artist", "artistname") or "未知艺术家",
            album_name=_first_text(song, "album", "albumname"),
            cover=_cover_url(song.get("albumpic") or song.get("pic")),
            duration=song.get("duration") or song.get("songTimeMinutes"),
            url=url,
            identifier=track_id,
            timestamp=_date_timestamp(song.get("releaseDate")),
        )


class AppleMusicParser(PlaylistParserBase):
    platform = Platform(name="applemusic", display_name="Apple Music")
    # Apple exposes a ``hasLyrics`` flag in the public catalog response, but
    # the actual lyric body is served through a MusicKit endpoint that needs a
    # short-lived Apple token/subscription.  Keep lyric lookup optional and
    # use LRCLIB's metadata-matched endpoint so the normal Apple card never
    # depends on that private API.
    _LYRICS_API_URL = "https://lrclib.net/api/get"

    # Artist metadata is deliberately separate from the short-lived media
    # files created for one parse.  The JSON file lives in the configured cache
    # root and is written atomically, while a per-parser in-memory fallback is
    # used by lightweight parser instances (and tests) without a cache path.
    _ARTIST_CACHE_FILENAME = "apple_music_artist_meta.json"
    _ARTIST_CACHE_SUCCESS_TTL = 7 * 24 * 60 * 60
    _ARTIST_CACHE_MISS_TTL = 2 * 60 * 60
    _ARTIST_TOTAL_TIMEOUT = 5.0
    _ARTIST_MAX_REGIONS = 3
    _ARTIST_RATE_BACKOFF = 45.0
    _artist_cache_files: ClassVar[dict[str, dict[str, dict[str, Any]]]] = {}
    _artist_cache_loaded: ClassVar[set[str]] = set()
    _artist_cache_lock = threading.RLock()
    _artist_rate_lock = threading.Lock()
    _artist_rate_limit_until: ClassVar[dict[str, float]] = {}

    @staticmethod
    def _normalize_artist_name(value: object) -> str:
        """Normalize names for exact (not substring) iTunes matching."""

        if value is None:
            return ""
        text = unicodedata.normalize("NFKC", str(value)).casefold()
        text = "".join(
            char
            for char in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(char)
        )
        # Keep word boundaries while making punctuation/case/width variants
        # comparable.  We intentionally do not remove all punctuation without
        # a boundary: ``AB`` must not match ``A&B`` by accident.
        text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
        return " ".join(text.split())

    @classmethod
    def _artist_name_parts(cls, value: object) -> list[str]:
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                if isinstance(item, Mapping):
                    item_name = _first_text(item, "name", "title", "artistName")
                else:
                    item_name = str(item).strip()
                if item_name:
                    names.append(item_name)
            return names
        if isinstance(value, Mapping):
            name = _first_text(value, "name", "title", "artistName")
            return [name] if name else []
        if not isinstance(value, str) or not value.strip():
            return []
        # Artist links are preferred for collaborations.  This fallback keeps
        # the first primary artist when only a display string is available.
        parts = re.split(
            r"\s+(?:feat\.?|ft\.?|featuring|with)\s+|[/\uFF0F|\uFF5C|\u3001]",
            value.strip(),
            flags=re.IGNORECASE,
        )
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _is_apple_host(host: str) -> bool:
        host = host.lower().rstrip(".")
        return (
            host in {"music.apple.com", "itunes.apple.com"}
            or host.endswith((".music.apple.com", ".itunes.apple.com", ".mzstatic.com"))
        )

    @classmethod
    def _is_apple_artist_url(cls, value: object) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            return False
        return cls._is_apple_host(parsed.hostname or "") and bool(
            re.search(
                r"/(?:[a-z]{2}/)?artists?/(?:[^/?#]+/)?(?:id)?\d+(?:[/?#]|$)",
                parsed.path,
                re.IGNORECASE,
            )
            or re.search(r"/(?:[a-z]{2}/)?artists?(?:/|$)", parsed.path, re.IGNORECASE)
        )

    @classmethod
    def _artist_id_from_url(cls, value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            return None
        if not cls._is_apple_host(parsed.hostname or ""):
            return None
        match = re.search(
            r"/(?:[a-z]{2}/)?artists?/(?:[^/?#]+/)?(?:id)?(\d+)(?:[/?#]|$)",
            parsed.path,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
        # Do not interpret a generic Apple Music ``?id=...`` URL (for
        # example a song or album link) as an artist identity.  Query IDs are
        # accepted only on an explicitly artist-shaped path.
        if not cls._is_apple_artist_url(value):
            return None
        query = parse_qs(parsed.query)
        for key in ("id", "artistId", "artist_id"):
            values = query.get(key)
            if values and str(values[0]).strip().isdigit():
                return str(values[0]).strip()
        return None

    @staticmethod
    def _artist_url_region(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            parts = [part for part in urlparse(value).path.split("/") if part]
        except ValueError:
            return None
        if parts and re.fullmatch(r"[a-zA-Z]{2}", parts[0]):
            return parts[0].lower()
        return None

    @classmethod
    def _artist_candidate_from_mapping(
        cls, value: object, *, allow_generic_id: bool = False
    ) -> dict[str, str] | None:
        if not isinstance(value, Mapping):
            return None
        name = _first_text(value, "title", "name", "artistName", "artistname", "label")
        url: str | None = None
        for key in (
            "url",
            "@id",
            "href",
            "link",
            "target",
            "uri",
            "artistLinkUrl",
        ):
            raw = value.get(key)
            if isinstance(raw, Mapping):
                raw = _first_text(raw, "url", "href", "link", "target")
            if isinstance(raw, str) and raw.strip():
                url = urljoin("https://music.apple.com/", raw.strip())
                break

        if url:
            if not cls._is_apple_artist_url(url):
                url = None

        artist_id = cls._artist_id_from_url(url)
        artist_id_keys = {
            "artistid",
            "artist_id",
            "artistadamid",
            "artistadam_id",
        }
        for key, raw in value.items():
            normalized_key = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if normalized_key in artist_id_keys:
                candidate = str(raw).strip()
                if candidate.isdigit():
                    artist_id = candidate
                    break
        descriptor = value.get("contentDescriptor")
        if isinstance(descriptor, Mapping):
            descriptor_kind = str(descriptor.get("kind") or "").lower()
            for key in ("artistId", "artistAdamID", "artistAdamId", "artist_id"):
                raw = descriptor.get(key)
                if str(raw).strip().isdigit():
                    artist_id = str(raw).strip()
                    break
            identifiers = descriptor.get("identifiers")
            if isinstance(identifiers, Mapping):
                identifier_keys = (
                    "artistId",
                    "artistAdamID",
                    "artistAdamId",
                    "artist_id",
                )
                if descriptor_kind in {"artist", "musicartist", "music-artist"}:
                    identifier_keys += ("storeAdamID",)
                for key in identifier_keys:
                    raw = identifiers.get(key)
                    if str(raw).strip().isdigit():
                        artist_id = str(raw).strip()
                        break
        value_kind = str(value.get("kind") or "").lower()
        value_identifiers = value.get("identifiers")
        if value_kind in {"artist", "musicartist", "music-artist"} and isinstance(
            value_identifiers, Mapping
        ):
            raw = value_identifiers.get("storeAdamID")
            if str(raw).strip().isdigit():
                artist_id = str(raw).strip()
        if not artist_id and allow_generic_id:
            raw = value.get("id")
            if str(raw).strip().isdigit():
                artist_id = str(raw).strip()
        if not (name or artist_id or url):
            return None
        result: dict[str, str] = {}
        if name:
            result["name"] = name
        if artist_id:
            result["id"] = artist_id
        if url:
            result["url"] = url
        return result

    @classmethod
    def _collect_artist_candidates(
        cls,
        value: object,
        *,
        allow_generic_id: bool = False,
        _depth: int = 0,
    ) -> list[dict[str, str]]:
        if _depth > 4:
            return []
        result: list[dict[str, str]] = []
        if isinstance(value, Mapping):
            candidate = cls._artist_candidate_from_mapping(
                value, allow_generic_id=allow_generic_id
            )
            if candidate:
                result.append(candidate)
            for key, child in value.items():
                key_text = str(key).lower()
                if key_text in {
                    "artistlinks",
                    "subtitlelinks",
                    "artists",
                    "artist",
                    "byartist",
                    "artistdata",
                    "contentdescriptor",
                    "identifiers",
                    # Apple wraps subtitle/artist links in
                    # ``segue.destination.contentDescriptor``.  The old
                    # walker stopped at ``segue``, leaving the first artist
                    # as name-only while a later JSON-LD artist supplied the
                    # first usable ID.
                    "segue",
                    "destination",
                } or "artist" in key_text:
                    child_candidates = cls._collect_artist_candidates(
                        child,
                        allow_generic_id=allow_generic_id
                        or "link" in key_text
                        or key_text
                        in {
                            "artist",
                            "artists",
                            "byartist",
                            "segue",
                            "destination",
                        },
                        _depth=_depth + 1,
                    )
                    # A subtitle link keeps the visible name on the outer
                    # mapping and the Adam ID/URL in its nested descriptor.
                    # Carry that name onto the nested identity so callers can
                    # match the first displayed artist without a name search.
                    if candidate and candidate.get("name"):
                        for child_candidate in child_candidates:
                            if not child_candidate.get("name") and (
                                child_candidate.get("id")
                                or child_candidate.get("url")
                            ):
                                child_candidate["name"] = candidate["name"]
                    result.extend(child_candidates)
        elif isinstance(value, list):
            for child in value:
                result.extend(
                    cls._collect_artist_candidates(
                        child,
                        allow_generic_id=allow_generic_id,
                        _depth=_depth + 1,
                    )
                )
        return result

    @classmethod
    def _artist_candidates(
        cls,
        track: Mapping[str, Any],
        album_header: Mapping[str, Any],
        fallback: Mapping[str, Any],
        artist_name: str | None,
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        # Ordered from the most song-specific fields to page-level fallbacks.
        for source in (track, album_header):
            for key in (
                "artistLinks",
                "subtitleLinks",
                "artists",
                "artist",
                "byArtist",
                "contentDescriptor",
            ):
                candidates.extend(
                    cls._collect_artist_candidates(
                        source.get(key), allow_generic_id="link" in key.lower()
                    )
                )
        for key in ("artists", "artist", "byArtist"):
            candidates.extend(cls._collect_artist_candidates(fallback.get(key)))

        display_names = cls._artist_name_parts(artist_name)
        if not display_names:
            display_names = cls._artist_name_parts(
                _first_text(track, "artistName", "artists")
                or cls._first_link_title(track.get("subtitleLinks"))
                or cls._first_link_title(track.get("artistLinks"))
                or cls._first_link_title(album_header.get("subtitleLinks"))
                or _first_text(fallback, "artist")
            )
        for name in display_names:
            candidates.append({"name": name})

        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            name = candidate.get("name", "").strip()
            artist_id = candidate.get("id", "").strip()
            url = candidate.get("url", "").strip()
            if not (name or artist_id or url):
                continue

            # The nested Apple descriptor is visited both as a whole and via
            # its ``identifiers`` child.  Merge those representations by Adam
            # ID so a later URL is not lost behind an earlier ID-only entry.
            if artist_id:
                existing = next(
                    (item for item in unique if item.get("id") == artist_id),
                    None,
                )
                if existing is not None:
                    if name and not existing.get("name"):
                        existing["name"] = name
                    if url and not existing.get("url"):
                        existing["url"] = url
                    continue
            key = (artist_id or cls._normalize_artist_name(name), url)
            if key in seen:
                continue
            seen.add(key)
            if name:
                candidate["name"] = name
            unique.append(candidate)
        return unique

    @classmethod
    def _primary_artist_name(
        cls,
        artist_name: object,
        candidates: list[Mapping[str, str]],
    ) -> str | None:
        """Return the displayed collaboration's first artist when known.

        Apple commonly renders collaborations as ``A & B`` while exposing
        ``A`` and ``B`` as separate subtitle links.  Splitting every ampersand
        would break legitimate names such as ``Earth, Wind & Fire``.  Only use
        a punctuation split when the first piece is corroborated by an
        extracted artist candidate; otherwise retain the complete display
        name for an exact-name lookup.
        """

        parts = cls._artist_name_parts(artist_name)
        if not parts:
            return None
        primary = parts[0]
        if len(parts) != 1 or not isinstance(primary, str):
            return primary

        for piece in re.split(
            r"\s*(?:&|＆|・|,|，)\s*", primary
        ):
            piece = piece.strip()
            if not piece or cls._normalize_artist_name(piece) == cls._normalize_artist_name(
                primary
            ):
                continue
            piece_key = cls._normalize_artist_name(piece)
            if any(
                piece_key
                and cls._normalize_artist_name(item.get("name")) == piece_key
                for item in candidates
            ):
                return piece
        return primary

    @classmethod
    def _artist_cache_key(cls, artist_id: str, region: str) -> str:
        return f"{artist_id}:{region}"

    @classmethod
    def _artist_search_cache_key(cls, artist_name: str, region: str) -> str:
        return f"search:{cls._normalize_artist_name(artist_name)}:{region}"

    def _artist_cache_path(self) -> Path | None:
        # ``cache_dir`` is scoped to one parse and is periodically removed by
        # CacheCleaner.  Keep metadata beside it under the plugin data root so
        # the seven-day/two-hour TTL can actually span songs and messages;
        # lightweight test/config objects without ``data_dir`` still fall back
        # to their supplied cache directory.
        root = getattr(self.cfg, "data_dir", None) or getattr(self.cfg, "cache_dir", None)
        if root is None:
            return None
        try:
            data_root = Path(getattr(self.cfg, "data_dir", None) or root)
            cache_root = Path(getattr(self.cfg, "cache_dir", None) or root)
            # Keep the metadata persistent across the ordinary generated-media
            # cache clear.  If both configured roots are identical, retain the
            # metadata in its own subdirectory rather than the cleaned root.
            metadata_root = (
                data_root
                if data_root != cache_root and data_root.name.lower() != "cache"
                else data_root / "metadata"
            )
            return metadata_root / self._ARTIST_CACHE_FILENAME
        except (TypeError, ValueError):
            return None

    @classmethod
    def _load_artist_cache_file(
        cls, path: Path, path_key: str
    ) -> dict[str, dict[str, Any]]:
        with cls._artist_cache_lock:
            with cls._artist_cache_lock:
                if path_key in cls._artist_cache_loaded:
                    return cls._artist_cache_files.setdefault(path_key, {})
            cache: dict[str, dict[str, Any]] = {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                raw_entries = payload.get("entries") if isinstance(payload, Mapping) else payload
                if isinstance(raw_entries, Mapping):
                    cache = {
                        str(key): dict(value)
                        for key, value in raw_entries.items()
                        if isinstance(value, Mapping)
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                cache = {}
            with cls._artist_cache_lock:
                cls._artist_cache_files[path_key] = cache
                cls._artist_cache_loaded.add(path_key)
                return cache

    @classmethod
    def _write_artist_cache_file(
        cls, path: Path, cache: Mapping[str, Mapping[str, Any]]
    ) -> None:
        path_key = str(path)
        with cls._artist_cache_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(
                    f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                temp.write_text(
                    json.dumps({"version": 1, "entries": cache}, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(temp, path)
            except (OSError, TypeError, ValueError):
                try:
                    temp.unlink(missing_ok=True)
                except (UnboundLocalError, OSError):
                    pass
            finally:
                with cls._artist_cache_lock:
                    cls._artist_cache_files[path_key] = dict(cache)

    def _artist_cache_get(self, key: str) -> dict[str, Any] | None:
        path = self._artist_cache_path()
        path_key = str(path) if path is not None else f"memory:{id(self)}"
        if path is None:
            with self._artist_cache_lock:
                cache = type(self)._artist_cache_files.setdefault(path_key, {})
        else:
            path_key = str(path)
            cache = self._load_artist_cache_file(path, path_key)
        now = time.time()
        with self._artist_cache_lock:
            entry = cache.get(key)
            if not isinstance(entry, Mapping):
                return None
            try:
                if float(entry.get("expires_at", 0)) <= now:
                    cache.pop(key, None)
                    if path is not None:
                        self._write_artist_cache_file(path, cache)
                    return None
            except (TypeError, ValueError):
                cache.pop(key, None)
                if path is not None:
                    self._write_artist_cache_file(path, cache)
                return None
            return dict(entry)

    def _artist_cache_put(
        self,
        key: str,
        *,
        found: bool,
        image_url: str | None = None,
        artist_id: str | None = None,
        artist_url: str | None = None,
        ttl: int | None = None,
    ) -> None:
        path = self._artist_cache_path()
        path_key = str(path) if path is not None else f"memory:{id(self)}"
        if path is None:
            with self._artist_cache_lock:
                cache = type(self)._artist_cache_files.setdefault(path_key, {})
        else:
            path_key = str(path)
            cache = self._load_artist_cache_file(path, path_key)
        entry: dict[str, Any] = {
            "found": bool(found),
            "image_url": image_url if found else None,
            "artist_id": artist_id,
            "artist_url": artist_url,
            "expires_at": time.time()
            + (ttl if ttl is not None else (self._ARTIST_CACHE_SUCCESS_TTL if found else self._ARTIST_CACHE_MISS_TTL)),
        }
        with self._artist_cache_lock:
            cache[key] = entry
            if path is not None:
                self._write_artist_cache_file(path, cache)

    def _artist_rate_limited(self) -> bool:
        scope = self._artist_rate_scope()
        with self._artist_rate_lock:
            return time.monotonic() < type(self)._artist_rate_limit_until.get(
                scope, 0.0
            )

    def _artist_rate_scope(self) -> str:
        path = self._artist_cache_path()
        return str(path.parent) if path is not None else f"memory:{id(self)}"

    def _trip_artist_rate_limit(self) -> None:
        scope = self._artist_rate_scope()
        with self._artist_rate_lock:
            limits = type(self)._artist_rate_limit_until
            limits[scope] = max(
                limits.get(scope, 0.0),
                time.monotonic() + self._ARTIST_RATE_BACKOFF,
            )

    @classmethod
    def _regions_for_storefront(cls, storefront: str | None) -> list[str]:
        preferred = str(storefront or "").strip().lower()
        regions: list[str] = []
        for region in (preferred, "us", "cn", "jp"):
            if re.fullmatch(r"[a-z]{2}", region) and region not in regions:
                regions.append(region)
            if len(regions) >= cls._ARTIST_MAX_REGIONS:
                break
        return regions or ["us"]

    async def _artist_get_text(self, url: str) -> tuple[int | None, str | None]:
        if self._artist_rate_limited():
            return None, None
        try:
            async with self.session.get(
                url,
                headers={
                    **getattr(self, "headers", {}),
                    "Referer": "https://music.apple.com/",
                    "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
                },
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status in (403, 429):
                    self._trip_artist_rate_limit()
                    return status, None
                if status >= 400:
                    return status, None
                return status, await response.text()
        except (ClientError, TimeoutError, OSError, RuntimeError, ValueError):
            return None, None

    async def _search_artist(
        self, artist_name: str, region: str
    ) -> dict[str, str] | None:
        self._artist_search_ambiguous = False
        self._artist_search_incomplete = False
        if not artist_name or self._artist_rate_limited():
            return None
        query = urlencode(
            {
                "term": artist_name,
                "entity": "musicArtist",
                "limit": "10",
                "country": region,
            }
        )
        status, text = await self._artist_get_text(
            f"https://itunes.apple.com/search?{query}"
        )
        if status is None or not text:
            self._artist_search_incomplete = True
            return None
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._artist_search_incomplete = True
            return None
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            self._artist_search_incomplete = True
            return None
        target = self._normalize_artist_name(artist_name)
        matches: list[dict[str, str]] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            result_kind = str(item.get("wrapperType") or item.get("kind") or "").lower()
            if result_kind and result_kind not in {"artist", "musicartist"}:
                continue
            result_name = _first_text(item, "artistName", "name")
            if not result_name or self._normalize_artist_name(result_name) != target:
                continue
            result_url = _first_text(item, "artistLinkUrl", "url")
            if result_url:
                if not self._is_apple_artist_url(result_url):
                    result_url = None
            result_id = self._artist_id_from_url(result_url)
            if not result_id:
                for key in ("artistId", "artistID", "id"):
                    raw = item.get(key)
                    if str(raw).strip().isdigit():
                        result_id = str(raw).strip()
                        break
            if result_id or result_url:
                candidate: dict[str, str] = {"name": result_name}
                if result_id:
                    candidate["id"] = result_id
                if result_url:
                    candidate["url"] = result_url
                matches.append(candidate)
        # More than one exact-name artist is inherently ambiguous.  A direct
        # Apple page link/ID from the track is handled before this fallback.
        unique_ids = {item.get("id") or item.get("url") for item in matches}
        if len(unique_ids) != 1:
            self._artist_search_ambiguous = bool(matches)
            return None
        return matches[0]

    @staticmethod
    def _json_ld_nodes(html: str) -> list[Mapping[str, Any]]:
        nodes: list[Mapping[str, Any]] = []
        for match in re.finditer(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            try:
                value = json.loads(match.group(1).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            pending = [value]
            while pending:
                current = pending.pop()
                if isinstance(current, list):
                    pending.extend(reversed(current))
                elif isinstance(current, Mapping):
                    nodes.append(current)
                    for key in ("@graph", "mainEntity", "subjectOf"):
                        if key in current:
                            pending.append(current[key])
        return nodes

    @classmethod
    def _image_urls_from_value(cls, value: object, *, _depth: int = 0) -> list[str]:
        if _depth > 4:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                result.extend(cls._image_urls_from_value(item, _depth=_depth + 1))
            return result
        if isinstance(value, Mapping):
            result: list[str] = []
            for key in ("url", "contentUrl", "thumbnailUrl", "src", "href", "@id"):
                if key in value:
                    result.extend(
                        cls._image_urls_from_value(value[key], _depth=_depth + 1)
                    )
            return result
        return []

    @classmethod
    def _valid_artist_image_url(
        cls, value: object, *, cover_url: str | None = None
    ) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        url = urljoin("https://music.apple.com/", unescape(value.strip()))
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or not parsed.netloc
        ):
            return None
        lowered = f"{parsed.path}?{parsed.query}".lower()
        if any(
            token in lowered
            for token in (
                "logo",
                "placeholder",
                "no-image",
                "noimage",
                "default-image",
                "blank-image",
                "default-artist",
                "default_artist",
                "generic-artist",
                "generic_artist",
                "artist-art-placeholder",
                "artist_art_placeholder",
                "artist-placeholder",
                "no_artwork",
                "no-artwork",
                "apple-music-logo",
                "apple_music_logo",
                "apple-music.png",
                "apple_music.png",
                "music-logo",
                "music_logo",
                "default_avatar",
                "default-avatar",
                "transparent",
                "spacer",
                "favicon",
            )
        ):
            return None
        if re.search(r"(?:^|[/_-])(?:0|1)x(?:0|1)(?:[a-z0-9._-]|$)", lowered):
            return None

        def canonical(raw: str) -> str:
            parsed_raw = urlparse(raw)
            path = re.sub(r"\d{2,5}x\d{2,5}", "", parsed_raw.path.lower())
            return f"{parsed_raw.netloc.lower()}{path}"

        if cover_url:
            cover = urljoin("https://music.apple.com/", cover_url)
            if url == cover or canonical(url) == canonical(cover):
                return None
        return url

    @classmethod
    def _artist_image_from_html(
        cls,
        html: str,
        *,
        artist_id: str | None = None,
        artist_name: str | None = None,
        cover_url: str | None = None,
    ) -> str | None:
        target_name = cls._normalize_artist_name(artist_name)
        target_id = str(artist_id or "").strip()
        for node in cls._json_ld_nodes(html):
            raw_type = node.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            type_names = {str(item).replace(" ", "").lower() for item in types}
            if not any(
                item in {"person", "musicgroup", "musicartist", "artist", "organization"}
                or "musicartist" in item
                for item in type_names
            ):
                continue
            node_url = _first_text(node, "url", "@id")
            node_id = cls._artist_id_from_url(node_url)
            node_name = _first_text(node, "name")
            if node_url:
                if not cls._is_apple_artist_url(node_url):
                    continue
            if target_id:
                # A known Adam ID is the strongest identity signal.  If a
                # page omits ``url``/``@id``, only accept a node whose name
                # still exactly identifies the requested artist.
                if node_id and node_id != target_id:
                    continue
                if not node_id and (
                    not target_name
                    or not node_name
                    or cls._normalize_artist_name(node_name) != target_name
                ):
                    continue
            elif target_name and (
                not node_name
                or cls._normalize_artist_name(node_name) != target_name
            ):
                continue
            for image in cls._image_urls_from_value(node.get("image")):
                if valid := cls._valid_artist_image_url(image, cover_url=cover_url):
                    return valid

        # Apple has changed its server-state shape several times.  Only walk
        # image fields under an explicitly artist-named branch so an album
        # artwork object cannot silently become an author portrait.
        try:
            payload = cls._decode_server_data(html)
        except ParseException:
            payload = {}

        def structured_images(value: object, artist_context: bool = False) -> list[str]:
            if isinstance(value, list):
                result: list[str] = []
                for item in value:
                    result.extend(structured_images(item, artist_context))
                return result
            if not isinstance(value, Mapping):
                return []
            result: list[str] = []
            artist_context_keys = {
                "artist",
                "artists",
                "artistdata",
                "artistdetail",
                "artistprofile",
                "artistpage",
                "musicartist",
                "performer",
                "profile",
            }
            if artist_context:
                for key in (
                    "image",
                    "artwork",
                    "portrait",
                    "photo",
                    "profileImage",
                    "avatar",
                ):
                    if key in value:
                        result.extend(cls._image_urls_from_value(value[key]))
            for key, child in value.items():
                child_context = artist_context or str(key).lower() in artist_context_keys
                result.extend(structured_images(child, child_context))
            return result

        for image in structured_images(payload):
            if valid := cls._valid_artist_image_url(image, cover_url=cover_url):
                return valid

        # Finally use Open Graph/Twitter image metadata.  It is common on
        # public artist pages, but still passes the same placeholder/cover
        # checks as JSON-LD candidates.
        for match in re.finditer(r"<meta\b[^>]*>", html, flags=re.IGNORECASE):
            attributes = {
                key.lower(): unescape(value)
                for key, _quote, value in re.findall(
                    r"([\w:-]+)\s*=\s*([\"'])(.*?)\2",
                    match.group(0),
                    flags=re.DOTALL,
                )
            }
            property_name = (
                attributes.get("property", "") or attributes.get("name", "")
            ).strip().lower()
            if property_name not in {"og:image", "og:image:url", "twitter:image"}:
                continue
            if valid := cls._valid_artist_image_url(
                attributes.get("content"), cover_url=cover_url
            ):
                return valid
        return None

    # Descriptive alias useful to small integrations/tests without exposing a
    # second implementation.
    _extract_artist_image = _artist_image_from_html
    _extract_artist_image_url = _artist_image_from_html
    _extract_artist_image_from_html = _artist_image_from_html

    @classmethod
    def _artist_page_url(
        cls, candidate: Mapping[str, str], region: str
    ) -> str | None:
        artist_id = str(candidate.get("id") or "").strip()
        direct_url = candidate.get("url")
        if direct_url:
            if (
                cls._is_apple_artist_url(direct_url)
                and (urlparse(direct_url).hostname or "").lower().endswith("music.apple.com")
                and cls._artist_url_region(direct_url) in {None, region}
            ):
                return direct_url
        slug = "artist"
        if direct_url:
            try:
                parts = [part for part in urlparse(direct_url).path.split("/") if part]
                if "artist" in parts:
                    index = parts.index("artist")
                    if index + 1 < len(parts):
                        slug = parts[index + 1]
            except (ValueError, IndexError):
                pass
        if not artist_id:
            return direct_url
        if candidate.get("name"):
            slug = quote(candidate["name"], safe="")
        return f"https://music.apple.com/{region}/artist/{slug}/{artist_id}"

    async def _lookup_artist_image(
        self,
        candidate: Mapping[str, str],
        *,
        storefront: str | None,
        cover_url: str | None,
    ) -> str | None:
        if self._artist_rate_limited():
            return None
        regions = self._regions_for_storefront(storefront)
        artist_id = str(candidate.get("id") or "").strip()
        artist_name = str(candidate.get("name") or "").strip()
        if not artist_id and candidate.get("url"):
            artist_id = self._artist_id_from_url(candidate.get("url")) or ""

        if not artist_id and not candidate.get("url"):
            if not artist_name:
                return None
            found: dict[str, str] | None = None
            for search_region in regions:
                search_key = self._artist_search_cache_key(artist_name, search_region)
                cached = self._artist_cache_get(search_key)
                if cached is not None:
                    if cached.get("found"):
                        found = {"name": artist_name}
                        if cached.get("artist_id"):
                            found["id"] = str(cached["artist_id"])
                        if cached.get("artist_url"):
                            found["url"] = str(cached["artist_url"])
                        break
                    continue
                found = await self._search_artist(artist_name, search_region)
                if found:
                    self._artist_cache_put(
                        search_key,
                        found=True,
                        artist_id=found.get("id"),
                        artist_url=found.get("url"),
                    )
                    break
                if getattr(self, "_artist_search_ambiguous", False):
                    self._artist_cache_put(search_key, found=False)
                    return None
                if getattr(self, "_artist_search_incomplete", False):
                    # Only cache a real, well-formed no-match response.  An
                    # outage or changed API response must not suppress retries
                    # for the full negative-cache TTL.
                    return None
                if self._artist_rate_limited():
                    return None
                self._artist_cache_put(search_key, found=False)
            if not found:
                return None
            artist_id = str(found.get("id") or "")
            candidate = {**candidate, **found}

        cache_id = artist_id or "url:" + hashlib.sha1(
            str(candidate.get("url") or artist_name).encode("utf-8")
        ).hexdigest()
        requested_region = regions[0]
        for index, region in enumerate(regions):
            if self._artist_rate_limited():
                return None
            key = self._artist_cache_key(cache_id, region)
            cached = self._artist_cache_get(key)
            if cached is not None:
                if cached.get("found") and cached.get("image_url"):
                    cached_image = self._valid_artist_image_url(
                        cached["image_url"], cover_url=cover_url
                    )
                    if cached_image:
                        return cached_image
                    # The URL may be this song's album artwork (or a stale
                    # placeholder) even though it was valid for an earlier
                    # track.  Do not reuse it as an author avatar.
                else:
                    continue
            page_url = self._artist_page_url(candidate, region)
            if not page_url:
                self._artist_cache_put(key, found=False)
                continue
            status, html = await self._artist_get_text(page_url)
            if status is None:
                # Network/timeout failures are not a confirmed miss and are
                # therefore intentionally not cached for two hours.
                continue
            if status in (403, 429):
                return None
            if status in (408, 425) or status >= 500:
                # A transient server response is not proof the artist page
                # has no portrait.
                continue
            if status >= 400 or not html:
                self._artist_cache_put(key, found=False)
                continue
            image_url = self._artist_image_from_html(
                html,
                artist_id=artist_id or None,
                artist_name=artist_name or None,
                cover_url=cover_url,
            )
            if image_url:
                self._artist_cache_put(
                    key,
                    found=True,
                    image_url=image_url,
                    artist_id=artist_id or None,
                    artist_url=page_url,
                )
                if region != requested_region:
                    self._artist_cache_put(
                        self._artist_cache_key(cache_id, requested_region),
                        found=True,
                        image_url=image_url,
                        artist_id=artist_id or None,
                        artist_url=page_url,
                    )
                return image_url
            self._artist_cache_put(
                key,
                found=False,
                artist_id=artist_id or None,
                artist_url=page_url,
            )
        return None

    async def _get_artist_avatar(
        self,
        *,
        track: Mapping[str, Any],
        album_header: Mapping[str, Any],
        fallback: Mapping[str, Any],
        artist_name: str | None,
        cover_url: str | None,
        storefront: str | None,
    ) -> str | None:
        """Best-effort artist portrait lookup used by the lazy author avatar."""

        try:
            candidates = self._artist_candidates(
                track, album_header, fallback, artist_name
            )
            if not candidates:
                return None
            # Restrict identified candidates to the displayed primary artist.
            # Apple sometimes exposes a name-only subtitle link before a
            # JSON-LD artist URL for that same artist; prefer the URL/ID in
            # that case, since a name search may be ambiguous. This still
            # prevents a later guest artist's ID from winning when the primary
            # artist has only a name.
            primary_name = self._primary_artist_name(artist_name, candidates)
            primary_key = self._normalize_artist_name(primary_name)
            primary_candidates = (
                [
                    item
                    for item in candidates
                    if self._normalize_artist_name(item.get("name")) == primary_key
                ]
                if primary_key
                else []
            )
            candidate_pool = primary_candidates or candidates
            candidate = next(
                (item for item in candidate_pool if item.get("id") or item.get("url")),
                candidate_pool[0],
            )
            if (
                not candidate.get("id")
                and not candidate.get("url")
                and self._normalize_artist_name(candidate.get("name") or artist_name)
                in {"", "未知艺术家", "unknown artist", "unknown"}
            ):
                return None
            # The ordered candidate list comes from artistLinks/subtitleLinks;
            # the first identifiable main artist wins for collaborations.
            return await asyncio.wait_for(
                self._lookup_artist_image(
                    candidate,
                    storefront=storefront,
                    cover_url=cover_url,
                ),
                timeout=self._ARTIST_TOTAL_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ClientError, OSError, ValueError):
            return None
        except Exception:  # noqa: BLE001 - optional page enrichment is fail-open
            # Page shape changes and malformed third-party responses must never
            # prevent the normal song card/audio result from being rendered.
            return None

    @staticmethod
    def _decode_server_data(html: str) -> Mapping[str, Any]:
        match = re.search(
            r"<script[^>]+id=[\"']serialized-server-data[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not match:
            raise ParseException("Apple Music 页面没有返回可识别的数据")
        try:
            payload = json.loads(match.group(1).strip().lstrip("\ufeff"))
        except json.JSONDecodeError as exc:
            raise ParseException("Apple Music 页面数据无法解析") from exc
        if not isinstance(payload, Mapping):
            raise ParseException("Apple Music 页面没有返回可识别的数据")
        return payload

    @staticmethod
    def _page_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = payload.get("data")
        root = (
            data[0]
            if isinstance(data, list) and data
            else data
            if isinstance(data, Mapping)
            else {}
        )
        root = root if isinstance(root, Mapping) else {}
        page = root.get("data")
        return page if isinstance(page, Mapping) else root

    @staticmethod
    def _artwork_url(value: object, *, size: int = 500) -> str | None:
        if not isinstance(value, Mapping):
            return None
        artwork = value.get("dictionary")
        artwork = artwork if isinstance(artwork, Mapping) else value
        return _cover_url(artwork.get("url"), size=size)

    @staticmethod
    def _first_link_title(value: object) -> str | None:
        if not isinstance(value, list):
            return None
        for link in value:
            if isinstance(link, Mapping) and (title := _first_text(link, "title")):
                return title
        return None

    @handle(
        "music.apple.com",
        r"music\.apple\.com/(?:(?P<storefront>[a-z]{2})/)?(?:"
        r"playlist/(?:[^/\s<>?#]+/)?(?P<playlist_id>pl\.[A-Za-z0-9._-]+)"
        r"|song/(?:[^/\s<>?#]+/)?(?P<song_id>\d+)"
        r"|album/(?:[^/\s<>?#]+/)?(?P<album_id>\d+)"
        r"(?=/?\?(?:[^#\s<>]*&)?i=(?P<track_id>\d+)(?:[&#\s<>]|$))"
        r")/?(?=[?#\s<>]|$)(?:\?[^#\s<>]*)?(?:#[^\s<>]*)?",
    )
    async def _handle_music(self, searched):
        # A keyword has only one registered handler; dispatch here so adding
        # single songs cannot override the existing playlist route.
        if searched.group("playlist_id"):
            return await self._handle_playlist(searched)
        return await self._handle_track(searched)

    @classmethod
    def _json_ld_track(cls, html: str, track_id: str) -> Mapping[str, Any]:
        """Read metadata only, and never treat Apple's preview as full audio."""
        for match in re.finditer(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            try:
                nodes = [json.loads(match.group(1).strip())]
            except json.JSONDecodeError:
                continue
            while nodes:
                node = nodes.pop()
                if isinstance(node, list):
                    nodes.extend(reversed(node))
                    continue
                if not isinstance(node, Mapping):
                    continue
                nodes.extend([node.get("@graph"), node.get("audio")])
                if node.get("@type") not in ("MusicComposition", "MusicRecording"):
                    continue
                node_url = _first_text(node, "url") or ""
                try:
                    if urlparse(node_url).hostname != "music.apple.com":
                        continue
                    _, searched = cls.search_url(node_url)
                except (ParseException, ValueError):
                    continue
                if (searched.group("song_id") or searched.group("track_id")) != track_id:
                    continue
                recording = node.get("audio")
                recording = (
                    recording
                    if isinstance(recording, Mapping)
                    and recording.get("@type") == "MusicRecording"
                    else node
                )
                album = recording.get("inAlbum")
                album = album if isinstance(album, Mapping) else {}
                raw_artists = recording.get("byArtist") or node.get("byArtist")
                artist_objects: list[dict[str, str]] = []
                artist_values = (
                    raw_artists
                    if isinstance(raw_artists, list)
                    else [raw_artists]
                    if raw_artists is not None
                    else []
                )
                for artist_value in artist_values:
                    if isinstance(artist_value, Mapping):
                        artist_item: dict[str, str] = {}
                        if artist_title := _first_text(artist_value, "name", "title"):
                            artist_item["name"] = artist_title
                        if artist_url := _first_text(artist_value, "url", "@id"):
                            artist_item["url"] = artist_url
                        if artist_item:
                            artist_objects.append(artist_item)
                    elif isinstance(artist_value, str) and artist_value.strip():
                        artist_objects.append({"name": artist_value.strip()})
                return {
                    "title": _first_text(recording, "name") or _first_text(node, "name"),
                    "artist": " / ".join(
                        _track_artist_names(
                            {"artists": raw_artists}
                        )
                    ),
                    "artists": artist_objects,
                    "album": _first_text(album, "name"),
                    "cover": _cover_url(
                        album.get("image") or recording.get("image") or node.get("image"),
                        size=500,
                    ),
                    "duration": recording.get("duration") or node.get("timeRequired"),
                    "release_date": recording.get("datePublished") or node.get("datePublished"),
                }
        return {}

    @staticmethod
    def _apple_meta_title(html: str, track_id: str) -> str | None:
        """Read Apple's exact song title as a last-resort metadata fallback.

        Apple pages include ``apple:title`` and ``apple:content_id`` even when
        their serialized server data changes shape.  Match the ID when present
        so a page-level title can never be mistaken for a different track.
        """
        metadata: dict[str, str] = {}
        for match in re.finditer(r"<meta\b[^>]*>", html, flags=re.IGNORECASE):
            attributes = {
                key.lower(): unescape(value)
                for key, _quote, value in re.findall(
                    r"([\w:-]+)\s*=\s*([\"'])(.*?)\2",
                    match.group(0),
                    flags=re.DOTALL,
                )
            }
            name = attributes.get("name", "").strip().lower()
            if name in {"apple:title", "apple:content_id"}:
                metadata[name] = attributes.get("content", "").strip()
        content_id = metadata.get("apple:content_id")
        title = metadata.get("apple:title")
        if content_id and content_id != track_id:
            return None
        return title or None

    def _lyrics_enabled(self) -> bool:
        parser_config = self._music_parser_config()
        return bool(getattr(parser_config, "parse_lyrics", False))

    async def _fetch_apple_lyrics(
        self,
        title: str,
        artist_name: str | None = None,
        album_name: str | None = None,
        duration: object = None,
    ) -> str | None:
        """Fetch LRC/plain lyrics matched to Apple Music metadata.

        Apple Music's public song page only exposes whether lyrics exist.  The
        private MusicKit lyrics endpoint requires an Apple-issued token, so a
        best-effort LRCLIB lookup is used when the optional parser setting is
        enabled.  Any lookup failure is intentionally swallowed: lyrics are an
        enrichment and must never make an otherwise valid Apple card fail.
        """

        title = str(title or "").strip()
        if not title:
            return None

        # LRCLIB's ``get`` endpoint is exact for the complete artist string,
        # while Apple sometimes renders collaborations as ``A / B``.  Try the
        # full value first and then a few conservative primary-artist variants.
        artist_candidates: list[str] = []
        if artist_name and str(artist_name).strip():
            raw_artist = str(artist_name).strip()
            artist_candidates.append(raw_artist)
            for part in re.split(
                r"\s+(?:feat\.?|ft\.?|featuring|with)\s+|\s*[/\uFF0F|\uFF5C\u3001]\s*",
                raw_artist,
                flags=re.IGNORECASE,
            ):
                part = part.strip()
                if part and part not in artist_candidates:
                    artist_candidates.append(part)
        else:
            artist_candidates.append("")

        duration_seconds = _duration_seconds(duration)
        for artist in artist_candidates[:3]:
            params: dict[str, str] = {"track_name": title}
            if artist:
                params["artist_name"] = artist
            if album_name and str(album_name).strip():
                params["album_name"] = str(album_name).strip()
            if duration_seconds > 0:
                params["duration"] = str(round(duration_seconds))
            try:
                body = await self._json_request(
                    f"{self._LYRICS_API_URL}?{urlencode(params)}",
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "AstrBot Apple Music lyrics lookup",
                    },
                )
            except (
                ParseException,
                ClientError,
                TimeoutError,
                OSError,
                ValueError,
                UnicodeError,
                RuntimeError,
            ):
                continue
            if not isinstance(body, Mapping):
                continue
            # Prefer time-synchronised lyrics so the existing renderer can
            # preserve timestamps; fall back to plain lyrics for songs without
            # an LRC version.
            for key in ("syncedLyrics", "plainLyrics"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    async def _handle_track(self, searched, *, metadata_only=False):
        track_id = searched.group("song_id") or searched.group("track_id")
        url = "https://" + searched.group(0)
        try:
            async with self.session.get(
                url,
                headers={**self.headers, "Referer": "https://music.apple.com/"},
            ) as response:
                if getattr(response, "status", 200) >= 400:
                    raise ParseException(f"Apple Music 歌曲请求失败: HTTP {response.status}")
                html = await response.text()
        except (ClientError, TimeoutError) as exc:
            raise ParseException(f"Apple Music 歌曲请求失败: {exc}") from exc

        try:
            page = self._page_data(self._decode_server_data(html))
        except ParseException:
            # Public JSON-LD can still provide a card when the page state changes.
            page = {}
        sections = page.get("sections")
        sections = sections if isinstance(sections, list) else []
        album_header: Mapping[str, Any] = {}
        track: Mapping[str, Any] = {}
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            section_id = str(section.get("id") or "")
            items = section.get("items")
            items = items if isinstance(items, list) else []
            if (
                "album-detail-header" in section_id
                and items
                and isinstance(items[0], Mapping)
            ):
                album_header = items[0]
            if not (
                "track-list" in section_id
                or section_id == "songDetail"
                or section.get("itemKind") == "songDetailHeader"
            ):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                descriptor = item.get("contentDescriptor")
                if (
                    not isinstance(descriptor, Mapping)
                    or descriptor.get("kind") != "song"
                ):
                    continue
                identifiers = descriptor.get("identifiers")
                if (
                    isinstance(identifiers, Mapping)
                    and str(identifiers.get("storeAdamID")) == track_id
                ):
                    track = item

        fallback = self._json_ld_track(html, track_id)
        title = (
            _first_text(track, "title", "name")
            or _first_text(fallback, "title")
            or self._apple_meta_title(html, track_id)
        )
        if not title:
            raise ParseException("Apple Music 歌曲不存在或暂时无法访问")
        duration = track.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            # Apple's serialized track-list uses milliseconds, even for short songs.
            duration /= 1000
        artist_name = (
            _first_text(track, "artistName", "artists")
            or self._first_link_title(track.get("subtitleLinks"))
            or self._first_link_title(track.get("artistLinks"))
            or self._first_link_title(album_header.get("subtitleLinks"))
            or _first_text(fallback, "artist")
            or "未知艺术家"
        )
        album_name = (
            _first_text(track, "albumName", "album")
            or self._first_link_title(track.get("tertiaryLinks"))
            or self._first_link_title(track.get("albumLinks"))
            or _first_text(album_header, "title")
            or _first_text(fallback, "album")
        )
        cover_url = (
            self._artwork_url(track.get("artwork"))
            or self._artwork_url(album_header.get("artwork"))
            or _first_text(fallback, "cover")
        )

        # Keep the optional artist lookup lazy.  The normal parser result and
        # audio task are available immediately; the renderer starts this
        # best-effort enrichment only while resolving the author avatar.
        artist_avatar = None
        if not metadata_only:
            artist_avatar = lambda: self._get_artist_avatar(
                track=track,
                album_header=album_header,
                fallback=fallback,
                artist_name=artist_name,
                cover_url=cover_url,
                storefront=searched.group("storefront"),
            )
        result = self._single_track_result(
            title=title,
            artist_name=artist_name,
            album_name=album_name,
            cover=cover_url,
            duration=duration or fallback.get("duration"),
            timestamp=(
                _date_timestamp(track.get("releaseDate"))
                or _date_timestamp(fallback.get("release_date"))
            ),
            url=url,
            identifier=track_id,
            author_avatar=artist_avatar,
            download_cover=not metadata_only,
        )
        if not metadata_only and self._lyrics_enabled():
            lyrics = await self._fetch_apple_lyrics(
                title,
                artist_name,
                album_name,
                duration or fallback.get("duration"),
            )
            if lyrics:
                result.extra["lyrics"] = {
                    "text": lyrics,
                    "title": title,
                    "artists": artist_name,
                    "album": album_name,
                    "duration_seconds": _duration_seconds(
                        duration or fallback.get("duration")
                    ),
                    "release_text": _first_text(track, "releaseDate")
                    or _first_text(fallback, "release_date"),
                    "source_label": "歌词来源：LRCLIB（Apple Music 元数据匹配）",
                }
        backend = getattr(self, "apple_music_backend", None)
        parser_config = self._music_parser_config()
        if (
            backend is not None
            and not metadata_only
            and backend.settings.enabled
            and getattr(parser_config, "return_audio", False)
        ):
            # Keep Apple Music's send choice local to this parser.  The file
            # mode explicitly overrides the global audio_to_file switch, while
            # voice mode asks the sender to build a Record component.
            send_format = getattr(parser_config, "audio_send_format", "file")
            send_as_voice = str(send_format).strip().lower() == "voice"
            result.extra["audio_as_voice"] = send_as_voice
            result.extra["audio_as_file"] = not send_as_voice
            # Both Apple Music delivery modes use OneBot URL segments when
            # available.  This keeps file delivery out of AstrBot's local
            # component serialization path as well as avoiding WAV conversion
            # for voice delivery.
            result.extra["audio_send_via_url"] = True
            result.contents.append(AudioContent(
                backend.download_task(url, _duration_seconds(duration or fallback.get("duration"))),
                duration=_duration_seconds(duration or fallback.get("duration")),
            ))
        return result

    async def _handle_playlist(self, searched):
        source_url = searched.group(0)
        url = source_url
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.headers, "Referer": "https://music.apple.com/"},
        ) as response:
            if getattr(response, "status", 200) >= 400:
                raise ParseException(f"Apple Music 歌单请求失败: HTTP {response.status}")
            html = await response.text()

        page = self._page_data(self._decode_server_data(html))
        sections = page.get("sections")
        sections = sections if isinstance(sections, list) else []

        header: Mapping[str, Any] = {}
        track_items: list[Mapping[str, Any]] = []
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            items = section.get("items")
            items = items if isinstance(items, list) else []
            section_id = str(section.get("id") or "")
            if "playlist-detail-header" in section_id and items:
                candidate = items[0]
                if isinstance(candidate, Mapping):
                    header = candidate
            if "track-list" in section_id:
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    descriptor = item.get("contentDescriptor")
                    if isinstance(descriptor, Mapping) and descriptor.get("kind") == "song":
                        track_items.append(item)

        if not header:
            raise ParseException("Apple Music 歌单不存在或暂时无法访问")
        tracks: list[Mapping[str, Any]] = []
        for item in track_items:
            album = AppleMusicParser._first_link_title(item.get("tertiaryLinks"))
            tracks.append(
                {
                    "title": _first_text(item, "title", "name"),
                    "artist": _first_text(item, "artistName")
                    or AppleMusicParser._first_link_title(item.get("subtitleLinks")),
                    "album": album or _first_text(item, "albumName", "album"),
                    "cover": AppleMusicParser._artwork_url(item.get("artwork"), size=300),
                }
            )

        title = _first_text(header, "title")
        seo_data = page.get("seoData")
        seo_data = seo_data if isinstance(seo_data, Mapping) else {}
        if not title:
            title = _first_text(seo_data, "pageTitle")
        return self._playlist_result(
            title=title or "Apple Music 歌单",
            author_name=(
                AppleMusicParser._first_link_title(header.get("subtitleLinks"))
                or "Apple Music 用户"
            ),
            author_avatar=None,
            description=None,
            cover=AppleMusicParser._artwork_url(header.get("artwork"), size=500),
            track_count=_as_int(header.get("trackCount")) or len(tracks),
            timestamp=None,
            url=url,
            identifier=searched.group("playlist_id"),
            tracks=tracks,
        )


class QQMusicParser(PlaylistParserBase):
    _QQ_PROFILE_URL = "https://y.qq.com/n/ryqq/profile"
    _QQ_COOKIE_CHECK_URL = (
        "https://c6.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg"
    )
    _QQ_VKEY_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    _QQ_BROWSER_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        parser_config = self._music_parser_config()
        self.cookiejar = (
            CookieJar(self.cfg, parser_config, domain="y.qq.com")
            if parser_config is not None
            else None
        )
        self.login = QQMusicLogin(self)

    def _qq_cookie_header(self, url: str) -> str:
        cookiejar = getattr(self, "cookiejar", None)
        getter = getattr(cookiejar, "get_cookie_header_for_url", None)
        if callable(getter):
            return str(getter(url) or "")
        return str(getattr(cookiejar, "cookies_str", "") or "")

    def _qq_cookie_values(self) -> dict[str, str]:
        cookie_header = self._qq_cookie_header(self._QQ_VKEY_URL)
        values: dict[str, str] = {}
        for item in cookie_header.split(";"):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            if name.strip():
                values[name.strip()] = value.strip()
        return values

    def has_cookie(self) -> bool:
        return bool(self._qq_cookie_header(self._QQ_PROFILE_URL))

    def has_auth_credentials(self) -> bool:
        """Whether the Cookie contains QQ Music account identity and auth key."""

        cookies = self._qq_cookie_values()
        identity = cookies.get("uin") or cookies.get("qqmusic_uin")
        auth_key = cookies.get("qqmusic_key") or cookies.get("qm_keyst")
        return bool(identity and auth_key)

    def _return_audio_enabled(self) -> bool:
        return super()._return_audio_enabled()

    def _lyrics_enabled(self) -> bool:
        parser_config = self._music_parser_config()
        return bool(getattr(parser_config, "parse_lyrics", False))

    async def _fetch_qq_lyrics(
        self,
        song_mid: str,
        song_id: int | str | None = None,
    ) -> str | None:
        """Fetch QQ Music LRC lyrics from its song lyric API."""

        cookies = self._qq_cookie_values()
        uin = cookies.get("uin") or cookies.get("ptui_loginuin") or "0"
        payload = {
            "comm": {
                "cv": 0,
                "ct": 24,
                "format": "json",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 1,
                "uin": uin,
            },
            "req_1": {
                "module": "music.musichallSong.PlayLyricInfo",
                "method": "GetPlayLyricInfo",
                "param": {
                    "songMID": song_mid,
                    "songID": _as_int(song_id) or 0,
                    "songType": 0,
                    "crypt": 0,
                    "roma": 0,
                },
            },
        }
        query = urlencode(
            {
                "format": "json",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "data": json.dumps(payload, separators=(",", ":")),
            }
        )
        headers = {"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"}
        if cookie := self._qq_cookie_header(self._QQ_VKEY_URL):
            headers["Cookie"] = cookie
        try:
            body = await self._json_request(f"{self._QQ_VKEY_URL}?{query}", headers=headers)
        except (ParseException, ClientError, TimeoutError):
            return None
        request = body.get("req_1") if isinstance(body, Mapping) else None
        data = request.get("data") if isinstance(request, Mapping) else None
        encoded = _first_text(data, "lyric") if isinstance(data, Mapping) else None
        if not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if decoded.strip() else None

    @staticmethod
    def _track_ids_from_match(searched) -> list[str]:
        raw_url = searched.group(0)
        parsed = urlparse(
            raw_url
            if raw_url.startswith(("http://", "https://"))
            else f"https://{raw_url}"
        )
        query = parse_qs(parsed.query)
        identifiers: list[str] = []
        for key in ("media_mid", "songmid", "song_mid", "mid", "songid"):
            values = query.get(key)
            if values and values[0].strip():
                identifier = values[0].strip().rstrip(
                    ".,!?;:)]}，。！？；："
                )
                if not identifier:
                    continue
                if identifier not in identifiers:
                    identifiers.append(identifier)
        path_parts = [part for part in parsed.path.rstrip("/").split("/") if part]
        if path_parts and path_parts[-2:-1] == ["songDetail"]:
            identifier = path_parts[-1]
            if identifier not in identifiers:
                identifiers.append(identifier)
        return identifiers

    @classmethod
    def _track_mid_from_match(cls, searched) -> str:
        identifiers = cls._track_ids_from_match(searched)
        if not identifiers:
            raise ParseException("QQ Music link does not contain a song ID")
        return identifiers[0]

    @staticmethod
    def _qq_g_tk(value: str) -> int:
        result = 5381
        for char in value:
            result += (result << 5) + ord(char)
        return result & 2_147_483_647

    async def _resolve_qq_audio_source(
        self,
        song_mid: str,
        *,
        media_mid: str | None = None,
        quality_priority: tuple[str, ...] = QQ_VOICE_QUALITY_PRIORITY,
    ) -> tuple[str, str] | None:
        """Resolve the first available URL in an ordered QQ quality chain."""

        cookie_header = self._qq_cookie_header(self._QQ_VKEY_URL)
        cookies = self._qq_cookie_values()
        uin = cookies.get("uin") or cookies.get("ptui_loginuin") or "0"
        filename_mid = media_mid or song_mid
        qualities = tuple(
            quality for quality in quality_priority if quality in QQ_QUALITY_FILENAMES
        ) or QQ_VOICE_QUALITY_PRIORITY
        filenames = [
            f"{QQ_QUALITY_FILENAMES[quality][0]}{filename_mid}"
            f"{QQ_QUALITY_FILENAMES[quality][1]}"
            for quality in qualities
        ]
        params: dict[str, object] = {
            "guid": str(int(time.time() * 1000) % 10_000_000_000),
            "songmid": [song_mid] * len(filenames),
            "songtype": [0] * len(filenames),
            "uin": uin,
            "loginflag": 1,
            "platform": "20",
            "filename": filenames,
        }
        payload = {
            "comm": {
                "cv": 0,
                "ct": 24,
                "format": "json",
                "uin": uin,
                "g_tk": self._qq_g_tk(cookies.get("qqmusic_key", "")),
                "platform": "yqq.json",
                "needNewCode": 1,
            },
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": params,
            },
        }
        headers = dict(self.headers)
        headers["User-Agent"] = self._QQ_BROWSER_USER_AGENT
        headers["Referer"] = "https://y.qq.com/"
        headers["Content-Type"] = "application/json"
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            body = await self._json_request(
                self._QQ_VKEY_URL,
                method="POST",
                headers=headers,
                data=json.dumps(payload, separators=(",", ":")),
            )
        except (ClientError, ParseException, TimeoutError):
            return None

        request = body.get("req_1") if isinstance(body, Mapping) else None
        data = request.get("data") if isinstance(request, Mapping) else None
        if not isinstance(data, Mapping):
            return None
        if _as_int(request.get("code")) not in (None, 0):
            return None
        prefixes = data.get("sip")
        prefixes = prefixes if isinstance(prefixes, list) else []
        rows = data.get("midurlinfo")
        rows = rows if isinstance(rows, list) else []
        rows_by_filename = {
            _first_text(row, "filename"): row
            for row in rows
            if isinstance(row, Mapping) and _first_text(row, "filename")
        }
        for index, (quality, filename) in enumerate(zip(qualities, filenames)):
            row = rows_by_filename.get(filename)
            if row is None and index < len(rows):
                candidate = rows[index]
                # Some QQ responses omit filename on individual rows; in that
                # shape the API preserves the request order.
                if isinstance(candidate, Mapping) and not _first_text(candidate, "filename"):
                    row = candidate
            if not isinstance(row, Mapping):
                continue
            purl = _first_text(row, "purl")
            row_mid = _first_text(row, "songmid")
            if not purl or row_mid not in (None, song_mid):
                continue
            if purl.startswith(("http://", "https://")):
                return purl, quality
            for prefix in prefixes:
                if isinstance(prefix, str) and prefix.strip():
                    return urljoin(prefix, purl), quality
            return purl, quality
        return None

    async def _resolve_qq_audio_url(
        self,
        song_mid: str,
        *,
        media_mid: str | None = None,
    ) -> str | None:
        """Backward-compatible voice resolver using the fixed quality chain."""

        source = await self._resolve_qq_audio_source(
            song_mid,
            media_mid=media_mid,
            quality_priority=QQ_VOICE_QUALITY_PRIORITY,
        )
        return source[0] if source else None

    async def check_cookie(self) -> bool | None:
        """Check QQ account authentication without treating transport errors as expiry.

        ``True`` means QQ's profile API accepted the account key, ``False`` is
        reserved for missing credentials or an explicit authentication-expired
        response, and ``None`` means the check could not reach a reliable result.
        A successful HTTP status by itself is not evidence of a logged-in session.
        """

        cookie_header = self._qq_cookie_header(self._QQ_COOKIE_CHECK_URL)
        cookies = self._qq_cookie_values()
        identity = cookies.get("uin") or cookies.get("qqmusic_uin")
        auth_key = cookies.get("qqmusic_key") or cookies.get("qm_keyst")
        if not cookie_header or not identity or not auth_key:
            return False

        headers = dict(self.headers)
        headers["User-Agent"] = self._QQ_BROWSER_USER_AGENT
        headers["Referer"] = "https://y.qq.com/"
        headers["Cookie"] = cookie_header
        params = {
            "g_tk": str(self._qq_g_tk(auth_key)),
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "notice": "0",
            "cid": "205360838",
            "needNewCode": "0",
            "loginUin": identity,
            "hostUin": "0",
            "userid": identity,
            "reqfrom": "1",
        }
        try:
            async with self.session.get(
                self._QQ_COOKIE_CHECK_URL,
                params=params,
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status >= 500 or response.status in (403, 429):
                    return None
                if response.status == 401:
                    return False
                if not 200 <= response.status < 300:
                    return None
                try:
                    payload = json.loads(await response.text())
                except (json.JSONDecodeError, TypeError, ValueError):
                    return None
        except (ClientError, TimeoutError):
            return None

        if not isinstance(payload, Mapping):
            return None
        try:
            code = int(payload.get("code"))
        except (TypeError, ValueError, OverflowError):
            return None
        if code == 0:
            return True
        if code in {1000, 104400, 104401}:
            return False
        return None

    platform = Platform(name="qqmusic", display_name="QQ音乐")

    async def _fetch_song_details(self, song_ids: list[str]) -> list[Mapping[str, Any]]:
        """补充 QQ 歌单接口只返回 songids 时的歌曲元数据。"""

        async def fetch(song_id: str) -> Mapping[str, Any] | None:
            song_id = str(song_id).strip()
            if not song_id:
                return None
            numeric_id = _as_int(song_id)
            identifier = (
                {"song_id": numeric_id}
                if numeric_id is not None
                else {"song_mid": song_id, "song_type": 0}
            )
            payload = {
                "comm": {"ct": 24, "cv": 0},
                "song": {
                    "method": "get_song_detail_yqq",
                    "module": "music.pf_song_detail_svr",
                    "param": identifier,
                },
            }
            api_url = (
                "https://u.y.qq.com/cgi-bin/musicu.fcg?"
                + urlencode(
                    {
                        "format": "json",
                        "data": json.dumps(payload, separators=(",", ":")),
                    }
                )
            )
            try:
                detail_headers = {"Referer": "https://y.qq.com/"}
                if cookie := self._qq_cookie_header(api_url):
                    detail_headers["Cookie"] = cookie
                body = await self._json_request(
                    api_url,
                    headers=detail_headers,
                )
            except ParseException:
                return None
            song = body.get("song") if isinstance(body, Mapping) else None
            data = song.get("data") if isinstance(song, Mapping) else None
            track = data.get("track_info") if isinstance(data, Mapping) else None
            return track if isinstance(track, Mapping) else None

        results = await asyncio.gather(
            *(fetch(song_id) for song_id in song_ids), return_exceptions=True
        )
        return [item for item in results if isinstance(item, Mapping)]

    @handle("c6.y.qq.com/base/fcgi-bin/u", r"c6\.y\.qq\.com/base/fcgi-bin/u\?[^\s]+")
    async def _handle_short(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return await self._redirect_and_parse(url)

    @handle(
        "i.y.qq.com/v8/playsong",
        r"i\.y\.qq\.com/v8/playsong\.html\?[^\s<>]+",
    )
    @handle(
        "i2.y.qq.com/n3/other/pages/playsong",
        r"i2\.y\.qq\.com/n3/other/pages/playsong/index\.html\?[^\s<>]+",
    )
    @handle(
        "y.qq.com/n/ryqq_v2/songDetail",
        r"y\.qq\.com/n/ryqq_v2/songDetail/[0-9A-Za-z]+(?:\?[^\s<>]*)?",
    )
    @handle(
        "y.qq.com/n/ryqq/songDetail",
        r"y\.qq\.com/n/ryqq/songDetail/[0-9A-Za-z]+(?:\?[^\s<>]*)?",
    )
    async def _handle_track(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        requested_ids = self._track_ids_from_match(searched)
        if not requested_ids:
            raise ParseException("QQ Music link does not contain a song ID")
        requested_id = requested_ids[0]
        track = None
        for candidate_id in requested_ids:
            tracks = await self._fetch_song_details([candidate_id])
            candidate = tracks[0] if tracks else None
            if candidate and (_track_title(candidate) or _first_text(candidate, "mid")):
                requested_id = candidate_id
                track = candidate
                break
        if not track:
            raise ParseException("QQ音乐歌曲不存在或暂时无法访问")

        artist_names = _track_artist_names(track)
        song_mid = _first_text(track, "mid") or requested_id
        audio_url = None
        resolved_quality = None
        if self._return_audio_enabled():
            media_mid = None
            file_info = track.get("file")
            if isinstance(file_info, Mapping):
                media_mid = _first_text(file_info, "media_mid")
            if self._audio_send_format() == "file":
                parser_config = self._music_parser_config()
                quality_priority = normalize_qq_file_quality_priority(
                    getattr(parser_config, "file_quality_priority", None)
                )
            else:
                quality_priority = QQ_VOICE_QUALITY_PRIORITY
            source = await self._resolve_qq_audio_source(
                song_mid,
                media_mid=media_mid,
                quality_priority=quality_priority,
            )
            if source:
                audio_url, resolved_quality = source
            if audio_url is None and isinstance(file_info, Mapping):
                audio_url = _first_text(file_info, "url")
        result = self._single_track_result(
            title=_track_title(track) or "未命名歌曲",
            artist_name=" / ".join(artist_names) or "未知艺术家",
            album_name=_track_album_name(track),
            cover=_track_cover_url(track),
            duration=track.get("interval"),
            url=url,
            identifier=song_mid,
            author_avatar=_qq_artist_avatar_url(track),
            timestamp=_date_timestamp(track.get("time_public")),
            audio_url=audio_url,
        )
        if audio_url:
            self._set_audio_delivery(result, audio_url)
            if resolved_quality:
                result.extra["audio_quality"] = resolved_quality
        if self._lyrics_enabled():
            lyrics = await self._fetch_qq_lyrics(
                song_mid,
                track.get("id") or track.get("songid"),
            )
            if lyrics:
                result.extra["lyrics"] = {
                    "text": lyrics,
                    "title": _track_title(track) or "未知歌曲",
                    "artists": " / ".join(artist_names),
                    "album": _track_album_name(track),
                "duration_seconds": _duration_seconds(track.get("interval")),
                    "release_text": _first_text(track, "time_public"),
                }
        return result

    @handle(
        "y.qq.com/n/ryqq",
        r"y\.qq\.com/n/ryqq(?:_v2)?/playlist/(?P<playlist_id>\d+)",
    )
    @handle(
        "i.y.qq.com/n2/m/share/details/taoge",
        r"i\.y\.qq\.com/n2/m/share/details/taoge\.html\?(?:[^&\s]*&)*id=(?P<playlist_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    @handle(
        "i2.y.qq.com/n3/other/pages/details/playlist",
        r"i2\.y\.qq\.com/n3/other/pages/details/playlist\.html\?(?:[^&\s]*&)*id=(?P<playlist_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    async def _handle_playlist(self, searched):
        playlist_id = searched.group("playlist_id")
        api_url = (
            "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?"
            + urlencode(
                {
                    "disstid": playlist_id,
                    "format": "json",
                    "utf8": "1",
                    "new_format": "1",
                }
            )
        )
        body = await self._json_request(
            api_url,
            headers={"Referer": "https://y.qq.com/"},
        )
        playlists = body.get("cdlist") if isinstance(body, Mapping) else None
        playlist = playlists[0] if isinstance(playlists, list) and playlists else None
        if not isinstance(playlist, Mapping):
            raise ParseException("QQ音乐歌单不存在或暂时无法访问")
        songs = playlist.get("songlist")
        if not isinstance(songs, list):
            songs = []
        if not songs:
            raw_song_ids = playlist.get("songids")
            if isinstance(raw_song_ids, str):
                song_ids = [item for item in raw_song_ids.split(",") if item]
            elif isinstance(raw_song_ids, list):
                song_ids = [str(item) for item in raw_song_ids if item is not None]
            else:
                song_ids = []
            songs = await self._fetch_song_details(song_ids[: self._MAX_PREVIEW_TRACKS])
        return self._playlist_result(
            title=_first_text(playlist, "dissname") or "QQ音乐歌单",
            author_name=_first_text(playlist, "nickname", "nick") or "未知用户",
            author_avatar=_cover_url(playlist.get("headurl"), size=140),
            description=_clean_text(playlist.get("desc")),
            cover=_cover_url(playlist.get("logo")),
            track_count=_as_int(playlist.get("songnum")),
            timestamp=_timestamp(playlist.get("ctime")),
            url=f"https://y.qq.com/n/ryqq_v2/playlist/{playlist_id}",
            identifier=playlist_id,
            tracks=[item for item in songs if isinstance(item, Mapping)],
            stats={
                "visits": playlist.get("visitnum"),
                "comments": playlist.get("cmtnum"),
            },
        )


class NetEaseMusicParser(PlaylistParserBase):
    platform = Platform(name="netease", display_name="网易云音乐")
    _DEFAULT_METING_API = "https://api.qijieya.cn/meting/"

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        parser_config = getattr(getattr(self.cfg, "parser", None), "netease", None)
        self.cookiejar = (
            CookieJar(self.cfg, parser_config, domain="music.163.com")
            if parser_config is not None
            else None
        )

    def _netease_cookie_header(self, url: str) -> str:
        cookiejar = getattr(self, "cookiejar", None)
        if cookiejar is None:
            return ""
        return cookiejar.get_cookie_header_for_url(url)

    def has_cookie(self) -> bool:
        return bool(self._netease_cookie_header("https://music.163.com/"))

    def has_auth_credentials(self) -> bool:
        return self.has_cookie()

    def _netease_api_base(self) -> str:
        parser_config = self._music_parser_config()
        configured = getattr(parser_config, "audio_api_base", None)
        return str(configured or self._DEFAULT_METING_API).strip().rstrip("/")

    def _lyrics_enabled(self) -> bool:
        parser_config = self._music_parser_config()
        return bool(getattr(parser_config, "parse_lyrics", False))

    async def _fetch_netease_artist_avatar(
        self, artist: Mapping[str, Any]
    ) -> str | None:
        """Resolve the first NetEase artist's real portrait lazily.

        ``api/song/detail`` commonly returns ``picUrl``/``img1v1Url`` with a
        shared placeholder and zero image IDs.  The artist endpoint contains
        the current portrait, so use the artist ID when one is available and
        only fall back to the song payload when it already carries a real
        image identity.
        """

        artist_id = _first_text(artist, "id", "artistId")
        if artist_id and _as_int(artist_id) == 0:
            artist_id = None
        fallback = None
        if not artist_id or any(
            _as_int(artist.get(key))
            for key in ("picId", "img1v1Id", "pic_id", "img1v1_id")
        ):
            fallback = _cover_url(
                artist.get("img1v1Url") or artist.get("picUrl"), size=300
            )
        if not artist_id:
            return fallback

        detail_url = f"https://music.163.com/api/artist/{quote(artist_id, safe='')}"
        headers = {"Referer": "https://music.163.com/"}
        if cookie := self._netease_cookie_header(detail_url):
            headers["Cookie"] = cookie
        try:
            body = await self._json_request(detail_url, headers=headers)
        except (ParseException, ClientError, TimeoutError):
            return fallback
        detail = body.get("artist") if isinstance(body, Mapping) else None
        if isinstance(detail, Mapping):
            image = _cover_url(
                detail.get("img1v1Url") or detail.get("picUrl"), size=300
            )
            if image:
                return image
        return fallback

    async def _fetch_netease_lyrics(self, song_id: str) -> str | None:
        lyric_url = "https://music.163.com/api/song/lyric"
        headers = {"Referer": "https://music.163.com/"}
        if cookie := self._netease_cookie_header(lyric_url):
            headers["Cookie"] = cookie
        try:
            body = await self._json_request(
                f"{lyric_url}?{urlencode({'id': song_id, 'lv': 1, 'kv': 1, 'tv': -1})}",
                headers=headers,
            )
        except (ParseException, ClientError, TimeoutError):
            return None
        if not isinstance(body, Mapping):
            return None
        lyric = body.get("lrc")
        text = _first_text(lyric, "lyric") if isinstance(lyric, Mapping) else None
        return text if text and text.strip() else None

    async def _resolve_netease_audio_url(self, song_id: str) -> str | None:
        """按 MomoTune 的 NcmClient 约定请求 Meting 或网易云兼容 API。"""

        base = self._netease_api_base()
        if not base:
            return None
        is_meting = "meting" in base.lower()
        if is_meting:
            request_url = base
            params: dict[str, str] = {
                "server": "netease",
                "type": "url",
                "id": song_id,
                "br": "320",
            }
        else:
            request_url = f"{base}/song/url/v1"
            params = {"id": song_id, "level": "exhigh"}
            cookie = getattr(getattr(self, "cookiejar", None), "cookies_str", "")
            if cookie:
                # 与 MomoTune 一致，仅向用户明确配置的兼容 API 转发 Cookie；
                # 标准 Meting API 不支持该参数，也不接收用户 Cookie。
                params["cookie"] = cookie

        try:
            async with self.session.get(
                request_url,
                params=params,
                headers=self.headers,
                allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    return urljoin(str(response.url), location) if location else None
                if response.status >= 400:
                    return None
                text = (await response.text()).strip()
        except (ClientError, TimeoutError):
            return None

        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError:
            payload = text

        if is_meting:
            if isinstance(payload, str):
                return payload.strip() or None
            if isinstance(payload, Mapping):
                return _first_text(payload, "url")
            return None

        data = payload.get("data") if isinstance(payload, Mapping) else None
        rows = data if isinstance(data, list) else []
        if not rows or not isinstance(rows[0], Mapping):
            return None
        if _as_int(rows[0].get("code")) not in (None, 200):
            return None
        return _first_text(rows[0], "url")

    @handle("163cn.tv", r"163cn\.tv/[A-Za-z0-9_-]+/?")
    async def _handle_short(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return await self._redirect_and_parse(url)

    @handle(
        "y.music.163.com/m/song",
        r"y\.music\.163\.com/m/song\?(?:[^&\s]*&)*id=(?P<song_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    @handle(
        "music.163.com/m/song",
        r"music\.163\.com/m/song\?(?:[^&\s]*&)*id=(?P<song_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    @handle(
        "music.163.com/song",
        r"music\.163\.com/song\?(?:[^&\s]*&)*id=(?P<song_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    @handle(
        "music.163.com/#/song",
        r"music\.163\.com/#/song\?(?:[^&\s]*&)*id=(?P<song_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    async def _handle_track(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        detail_url = (
            "https://music.163.com/api/song/detail?ids="
            + json.dumps([int(searched.group("song_id"))])
        )
        detail_headers = {"Referer": "https://music.163.com/"}
        if cookie := self._netease_cookie_header(detail_url):
            detail_headers["Cookie"] = cookie
        body = await self._json_request(detail_url, headers=detail_headers)
        songs = body.get("songs") if isinstance(body, Mapping) else None
        track = songs[0] if isinstance(songs, list) and songs else None
        if not isinstance(track, Mapping):
            raise ParseException("网易云音乐歌曲不存在或暂时无法访问")

        artists = _track_artist_names(track)
        raw_artists = track.get("artists")
        author_avatar = None
        if isinstance(raw_artists, list) and raw_artists:
            first_artist = raw_artists[0]
            if isinstance(first_artist, Mapping):
                # Resolve only the primary artist.  NetEase's song-detail
                # image fields are often a generic placeholder, so the
                # actual portrait lookup happens when the card needs it.
                author_avatar = lambda artist=first_artist: (
                    self._fetch_netease_artist_avatar(artist)
                )
        song_id = _first_text(track, "id") or searched.group("song_id")
        cover = _track_cover_url(track)
        audio_url = None
        if self._return_audio_enabled():
            audio_url = await self._resolve_netease_audio_url(song_id)
            audio_url = audio_url or _first_text(track, "mp3Url") or (
                f"https://music.163.com/song/media/outer/url?id={song_id}"
            )
        audio_headers = dict(self.headers)
        audio_headers["Referer"] = "https://music.163.com/"
        if audio_url and (cookie := self._netease_cookie_header(audio_url)):
            audio_headers["Cookie"] = cookie
        result = self._single_track_result(
            title=_track_title(track) or "未命名歌曲",
            artist_name=" / ".join(artists) or "未知艺术家",
            album_name=_track_album_name(track),
            cover=cover,
            duration=track.get("duration") or track.get("dt"),
            url=url,
            identifier=song_id,
            author_avatar=author_avatar,
            timestamp=_date_timestamp(track.get("publishTime"), milliseconds=True),
            # 旧详情接口经常不返回 mp3Url。网易云媒体跳转地址会重定向
            # 到歌曲文件，由共用 Downloader 下载并归档到本次解析目录。
            audio_url=audio_url,
            audio_headers=audio_headers,
        )
        if audio_url:
            self._set_audio_delivery(result, audio_url)
        if self._lyrics_enabled():
            lyrics = await self._fetch_netease_lyrics(song_id)
            if lyrics:
                result.extra["lyrics"] = {
                    "text": lyrics,
                    "title": _track_title(track) or "未知歌曲",
                    "artists": " / ".join(artists),
                    "album": _track_album_name(track),
                    "duration_seconds": _duration_seconds(
                        track.get("duration") or track.get("dt")
                    ),
                    "release_text": _first_text(track, "publishTime"),
                }
        return result

    @handle(
        "music.163.com/#/playlist",
        r"music\.163\.com/(?:#/|m/)?playlist\?(?:[^&\s]*&)*id=(?P<playlist_id>\d+)(?=&|\s|$)",
    )
    @handle(
        "music.163.com/playlist",
        r"music\.163\.com/(?:#/|m/)?playlist\?(?:[^&\s]*&)*id=(?P<playlist_id>\d+)(?=&|\s|$)",
    )
    @handle(
        "music.163.com/m/playlist",
        r"music\.163\.com/m/playlist\?(?:[^&\s]*&)*id=(?P<playlist_id>\d+)(?=&|\s|$)",
    )
    @handle(
        "y.music.163.com/m/playlist",
        r"y\.music\.163\.com/m/playlist\?(?:[^&\s]*&)*id=(?P<playlist_id>\d+)(?=&|\s|$)",
    )
    async def _handle_playlist(self, searched):
        playlist_id = searched.group("playlist_id")
        api_url = f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}"
        body = await self._json_request(
            api_url,
            headers={"Referer": "https://music.163.com/"},
        )
        playlist = body.get("playlist") if isinstance(body, Mapping) else None
        if not isinstance(playlist, Mapping):
            raise ParseException("网易云音乐歌单不存在或暂时无法访问")
        creator = playlist.get("creator")
        creator = creator if isinstance(creator, Mapping) else {}
        tracks = playlist.get("tracks")
        if not isinstance(tracks, list):
            tracks = []
        return self._playlist_result(
            title=_first_text(playlist, "name") or "网易云音乐歌单",
            author_name=_first_text(creator, "nickname") or "未知用户",
            author_avatar=_cover_url(creator.get("avatarUrl")),
            description=_clean_text(playlist.get("description")),
            cover=_cover_url(playlist.get("coverImgUrl")),
            track_count=_as_int(playlist.get("trackCount")),
            timestamp=_timestamp(playlist.get("createTime"), milliseconds=True),
            url=f"https://music.163.com/playlist?id={playlist_id}",
            identifier=playlist_id,
            tracks=[item for item in tracks if isinstance(item, Mapping)],
            stats={
                "plays": playlist.get("playCount"),
                "favorites": playlist.get("subscribedCount"),
                "comments": playlist.get("commentCount"),
                "shares": playlist.get("shareCount"),
            },
        )


class KugouMusicParser(PlaylistParserBase):
    platform = Platform(name="kugou", display_name="酷狗音乐")
    _KUGOU_SECRET = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    _MOBILE_REFERER = "https://m.kugou.com/"
    _WEB_PLAY_URL = "https://wwwapi.kugou.com/play/songinfo"
    # This restricted track is used only for a lightweight, read-only startup
    # check of the web-player credentials; media bytes are never downloaded.
    _COOKIE_CHECK_TRACK = {
        "hash": "DEC2BFFC693EFC0895809F0952244048",
        "album_id": "631501",
        "album_audio_id": "h8z8288",
    }

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        parser_config = self._music_parser_config()
        self.cookiejar = (
            CookieJar(self.cfg, parser_config, domain="kugou.com")
            if parser_config is not None
            else None
        )
        self.login = KugouMusicLogin(self)

    def _kugou_cookie_header(self, url: str) -> str:
        cookiejar = getattr(self, "cookiejar", None)
        getter = getattr(cookiejar, "get_cookie_header_for_url", None)
        if callable(getter):
            return str(getter(url) or "")
        return str(getattr(cookiejar, "cookies_str", "") or "")

    @classmethod
    def _kugou_account_credentials(cls, cookie: str) -> tuple[str, str]:
        cookies: dict[str, str] = {}
        for item in cookie.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name:
                cookies.setdefault(name, value)
        account = cls._kugou_nested_cookie_values(cookies.get("KuGoo", ""))
        user_id = account.get("KugooID") or cookies.get("KugooID", "")
        token = account.get("t") or cookies.get("t", "")
        return user_id.strip(), token.strip()

    def has_cookie(self) -> bool:
        """Whether any Kugou Cookie was configured for the web player."""

        return bool(self._kugou_cookie_header(self._WEB_PLAY_URL))

    def has_auth_credentials(self) -> bool:
        """Whether the Cookie contains both account identity and auth token."""

        cookie = self._kugou_cookie_header(self._WEB_PLAY_URL)
        user_id, token = self._kugou_account_credentials(cookie)
        return bool(user_id and token)

    async def check_cookie(self) -> bool | None:
        """Check whether Kugou's signed web-player API accepts the account token.

        ``True`` means the service returned a playable URL for the restricted
        track probe, ``False`` means the request was rejected or no playable
        URL was granted, and ``None`` means the endpoint could not be checked
        (for example, a transient network or response-format error).
        """

        cookie = self._kugou_cookie_header(self._WEB_PLAY_URL)
        params = self._kugou_web_play_params(
            cookie,
            song_hash=self._COOKIE_CHECK_TRACK["hash"],
            album_id=self._COOKIE_CHECK_TRACK["album_id"],
            album_audio_id=self._COOKIE_CHECK_TRACK["album_audio_id"],
        )
        if not params:
            return False

        try:
            payload = await self._json_request(
                self._WEB_PLAY_URL + "?" + urlencode(params),
                headers={
                    "Referer": "https://m.kugou.com/share/",
                    "Cookie": cookie,
                },
            )
        except ParseException:
            return None

        if not isinstance(payload, Mapping):
            return None
        status = str(payload.get("status", ""))
        error_code = str(payload.get("err_code", ""))
        if status == "1" and error_code == "0":
            data = payload.get("data")
            return isinstance(data, Mapping) and bool(
                _first_text(data, "play_url", "url")
            )
        if status in {"0", "-1"} or (error_code and error_code != "0"):
            return False
        return None

    @staticmethod
    def _signature(params: Mapping[str, object], body: str = "") -> str:
        ordered = "".join(f"{key}={params[key]}" for key in sorted(params))
        raw = f"{KugouMusicParser._KUGOU_SECRET}{ordered}{body}{KugouMusicParser._KUGOU_SECRET}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _signed_params(cls, extra: Mapping[str, object] | None = None) -> dict[str, str]:
        now = str(int(time.time() * 1000))
        params: dict[str, str] = {
            "srcappid": "2919",
            "clientver": "20000",
            "clienttime": now,
            "mid": now,
            "uuid": now,
            "dfid": "-",
        }
        if extra:
            params.update({key: str(value) for key, value in extra.items()})
        params["signature"] = cls._signature(params)
        return params

    @staticmethod
    def _decode_mixsong_payload(html: str) -> Mapping[str, Any]:
        marker = re.search(r"dataFromSmarty\s*=\s*", html)
        if not marker:
            raise ParseException("酷狗音乐歌曲页面没有返回可识别的数据")
        try:
            payload, _ = json.JSONDecoder().raw_decode(html[marker.end() :].lstrip())
        except json.JSONDecodeError as exc:
            raise ParseException("酷狗音乐歌曲页面数据格式无法识别") from exc
        if not isinstance(payload, list) or not payload:
            raise ParseException("酷狗音乐歌曲页面没有返回可识别的数据")
        track = payload[0]
        if not isinstance(track, Mapping):
            raise ParseException("酷狗音乐歌曲页面没有返回可识别的数据")
        return track

    @staticmethod
    def _mixsong_album_name(html: str) -> str | None:
        match = re.search(
            r'<p[^>]+class=["\'][^"\']*albumName[^"\']*["\'][^>]*>.*?'
            r'<a[^>]*>(.*?)</a>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return None
        text = re.sub(r"<[^>]+>", "", match.group(1))
        return _clean_text(text)

    async def _fetch_kugou_song_info(
        self,
        song_hash: str,
        *,
        encode_album_audio_id: str | None = None,
        album_id: str | None = None,
        album_audio_id: str | None = None,
        referer: str | None = None,
    ) -> Mapping[str, Any]:
        request_url = (
            "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=" + song_hash
        )
        cookie = self._kugou_cookie_header(request_url)
        web_info = await self._fetch_kugou_web_song_info(
            song_hash,
            cookie=cookie,
            encode_album_audio_id=encode_album_audio_id,
            album_id=album_id,
            album_audio_id=album_audio_id,
            referer=referer,
        )
        if web_info:
            return web_info

        headers = {"Referer": self._MOBILE_REFERER}
        if cookie:
            headers["Cookie"] = cookie
        try:
            payload = await self._json_request(
                request_url,
                headers=headers,
            )
        except ParseException:
            return {}
        return payload if isinstance(payload, Mapping) else {}

    @staticmethod
    def _kugou_nested_cookie_values(value: str) -> dict[str, str]:
        """Read the account fields embedded in Kugou's ``KuGoo`` cookie."""

        nested = value
        values: dict[str, list[str]] = {}
        for _ in range(3):
            nested = re.sub(r"%3[dD]", "=", nested)
            nested = re.sub(r"%26", "&", nested, flags=re.IGNORECASE)
            values = parse_qs(nested, keep_blank_values=True)
            if "KugooID" in values or "t" in values:
                break
            nested = re.sub(r"%25", "%", nested, flags=re.IGNORECASE)
        return {key: items[0] for key, items in values.items() if items}

    @classmethod
    def _kugou_web_play_params(
        cls,
        cookie: str,
        *,
        song_hash: str,
        encode_album_audio_id: str | None = None,
        album_id: str | None = None,
        album_audio_id: str | None = None,
    ) -> dict[str, str] | None:
        """Build the signed web-player request from the browser cookies."""

        cookies: dict[str, str] = {}
        for item in cookie.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name:
                cookies.setdefault(name, value)
        user_id, token = cls._kugou_account_credentials(cookie)
        if not user_id or not token:
            return None

        mid = cookies.get("kg_mid") or str(int(time.time() * 1000))
        identifiers: dict[str, object] = {}
        if encode_album_audio_id:
            # The desktop player prefers this encoded ID over hash/album IDs.
            identifiers["encode_album_audio_id"] = encode_album_audio_id
        else:
            if song_hash:
                identifiers["hash"] = song_hash
            if album_id:
                identifiers["album_id"] = album_id
            if album_audio_id:
                identifiers["album_audio_id"] = album_audio_id

        return cls._signed_params(
            {
                "appid": 1014,
                "dfid": cookies.get("kg_dfid") or "-",
                "mid": mid,
                "uuid": mid,
                "token": token,
                "userid": user_id or "0",
                "platid": 4,
                **identifiers,
            }
        )

    async def _fetch_kugou_web_song_info(
        self,
        song_hash: str,
        *,
        cookie: str,
        encode_album_audio_id: str | None = None,
        album_id: str | None = None,
        album_audio_id: str | None = None,
        referer: str | None = None,
    ) -> Mapping[str, Any]:
        if not cookie:
            return {}
        params = self._kugou_web_play_params(
            cookie,
            song_hash=song_hash,
            encode_album_audio_id=encode_album_audio_id,
            album_id=album_id,
            album_audio_id=album_audio_id,
        )
        if not params:
            return {}

        request_url = "https://wwwapi.kugou.com/play/songinfo?" + urlencode(params)
        try:
            payload = await self._json_request(
                request_url,
                headers={
                    "Referer": referer or "https://www.kugou.com/",
                    "Cookie": cookie,
                },
            )
        except ParseException:
            return {}
        if not isinstance(payload, Mapping):
            return {}
        if str(payload.get("status")) != "1" or str(payload.get("err_code", 0)) != "0":
            return {}

        data = payload.get("data")
        if not isinstance(data, Mapping):
            return {}
        play_url = _first_text(data, "play_url", "url")
        if not play_url:
            return {}
        info = dict(data)
        info.setdefault("url", play_url)
        return info

    @staticmethod
    def _kugou_artist_avatar_url(value: Mapping[str, Any] | None) -> str | None:
        """Return only the first artist portrait exposed by Kugou metadata."""

        if not isinstance(value, Mapping):
            return None
        authors = value.get("authors")
        if isinstance(authors, Mapping):
            authors = [authors]
        if isinstance(authors, list):
            if authors and isinstance(authors[0], Mapping):
                first_artist = authors[0]
                if avatar := _cover_url(
                    first_artist.get("avatar")
                    or first_artist.get("imgUrl")
                    or first_artist.get("imgurl")
                    or first_artist.get("sizable_avatar"),
                    size=165,
                ):
                    return avatar
        return _cover_url(
            value.get("imgUrl") or value.get("singerHead"), size=165
        )

    async def _fetch_kugou_artist_avatar(
        self,
        song_hash: str,
        *,
        fallback: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Fetch Kugou's lightweight song metadata for the primary portrait."""

        request_url = (
            "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash="
            + song_hash
        )
        headers = {"Referer": self._MOBILE_REFERER}
        if cookie := self._kugou_cookie_header(request_url):
            headers["Cookie"] = cookie
        try:
            payload = await self._json_request(request_url, headers=headers)
        except (ParseException, ClientError, TimeoutError):
            payload = None
        if isinstance(payload, Mapping):
            data = payload.get("data")
            data = data if isinstance(data, Mapping) else payload
            if avatar := self._kugou_artist_avatar_url(data):
                return avatar
        return self._kugou_artist_avatar_url(fallback)

    async def _kugou_single_result(
        self,
        *,
        source_url: str,
        song_hash: str,
        fallback: Mapping[str, Any] | None = None,
        album_name: str | None = None,
    ) -> ParseResult:
        fallback = fallback if isinstance(fallback, Mapping) else {}
        return_audio = self._return_audio_enabled()
        info = (
            await self._fetch_kugou_song_info(
                song_hash,
                encode_album_audio_id=_first_text(fallback, "encode_album_audio_id"),
                album_id=_first_text(fallback, "album_id"),
                album_audio_id=_first_text(fallback, "album_audio_id"),
                referer=source_url,
            )
            if return_audio
            else {}
        )
        title = (
            _first_text(info, "songName", "song_name", "fileName", "audio_name")
            or _first_text(fallback, "song_name", "audio_name")
            or "未命名歌曲"
        )
        artist_name = (
            _first_text(info, "author_name", "singerName")
            or _first_text(fallback, "author_name")
            or "未知艺术家"
        )
        cover = _cover_url(
            info.get("album_img")
            or info.get("imgUrl")
            or info.get("img")
            or info.get("sizable_cover")
            or fallback.get("album_img")
        )
        extra = info.get("extra")
        extra = extra if isinstance(extra, Mapping) else {}
        duration = (
            info.get("timeLength")
            or info.get("timelength")
            or extra.get("320timelength")
            or extra.get("128timelength")
            or fallback.get("timelength")
        )
        resolved_audio_url = _first_text(info, "url", "play_url", "play_backup_url")
        audio_url = resolved_audio_url if return_audio else None
        audio_headers = dict(self.headers)
        if audio_url and (cookie := self._kugou_cookie_header(audio_url)):
            audio_headers["Cookie"] = cookie
        author_avatar = (
            self._kugou_artist_avatar_url(info)
            or self._kugou_artist_avatar_url(fallback)
        )
        if not author_avatar:
            author_avatar = lambda: self._fetch_kugou_artist_avatar(
                song_hash, fallback=fallback
            )
        result = self._single_track_result(
            title=title,
            artist_name=artist_name,
            album_name=album_name or _first_text(info, "album_name"),
            cover=cover,
            duration=duration,
            url=source_url,
            identifier=(
                _first_text(info, "encode_album_audio_id", "album_audio_id", "audio_id")
                or _first_text(fallback, "mixsongid", "encode_album_audio_id")
                or song_hash
            ),
            author_avatar=author_avatar,
            audio_url=audio_url,
            audio_headers=audio_headers,
            stats={},
        )
        if audio_url and audio_url.startswith(("http://", "https://")):
            self._set_audio_delivery(result, audio_url)
        return result

    @staticmethod
    def _kugou_activity_params(url: str) -> tuple[str | None, str | None]:
        """Extract the activity page's file and channel identifiers.

        These pages put their parameters after ``#/?`` so a normal HTTP
        request does not see them on the server.  Chat messages still contain
        the fragment, therefore parse both the regular query and fragment.
        """

        parsed = urlparse(
            url if url.startswith(("http://", "https://")) else f"https://{url}"
        )
        values = parse_qs(parsed.query, keep_blank_values=False)
        fragment = parsed.fragment
        if "?" in fragment:
            fragment_values = parse_qs(
                fragment.split("?", 1)[1], keep_blank_values=False
            )
            for key, items in fragment_values.items():
                if key not in values or not values[key]:
                    values[key] = items

        def first(*keys: str) -> str | None:
            for key in keys:
                items = values.get(key)
                if items and str(items[0]).strip():
                    return str(items[0]).strip().rstrip(".,!?;:)]}")
            return None

        return first("file_id", "fileid"), first(
            "channel_id", "global_collection_id", "global_collectionid"
        )

    @handle(
        "activity.kugou.com/vo-activity",
        r"activity\.kugou\.com/vo-activity/[A-Za-z0-9_-]+/detail\.html"
        r"(?:\?[^#\s<>]*)?(?:#[^\s<>]*)?",
    )
    async def _handle_activity_track(self, searched):
        """Parse a Kugou Concept activity detail page.

        The page is only a JavaScript shell.  Its detail API accepts the
        ``file_id`` and ``channel_id`` from the URL and returns regular Kugou
        song metadata, including a hash that can be resolved by the existing
        audio endpoint.
        """

        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        file_id, channel_id = self._kugou_activity_params(url)
        if not file_id or not channel_id:
            raise ParseException("酷狗活动页缺少歌曲标识")

        request_params = self._signed_params(
            {
                "appid": 1058,
                "fileid": file_id,
                "global_collection_id": channel_id,
            }
        )
        api_url = (
            "https://gateway.kugou.com/v1/get_song_detail?"
            + urlencode(request_params)
        )
        body = await self._json_request(
            api_url,
            headers={
                "Referer": "https://activity.kugou.com/",
                "x-router": "youth.kugou.com",
            },
        )
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, Mapping) or _as_int(body.get("status")) != 1:
            raise ParseException("酷狗活动页歌曲不存在或暂时无法访问")

        album_info = data.get("album_info")
        album_info = album_info if isinstance(album_info, Mapping) else {}
        audio_info = data.get("audio_info")
        audio_info = audio_info if isinstance(audio_info, Mapping) else {}
        song_hash = _first_text(data, "hash") or _first_text(audio_info, "hash")
        if not song_hash:
            raise ParseException("酷狗活动页歌曲缺少音频标识")

        # Normalize the activity response to the fallback shape consumed by
        # ``_kugou_single_result``.  That keeps title/cover/avatar handling
        # and the regular Kugou audio resolver in one place.
        fallback = dict(data)
        fallback.update(
            {
                "song_name": _first_text(
                    data, "ori_audio_name", "song_name", "audio_name"
                ),
                "album_name": _first_text(album_info, "album_name", "name"),
                "album_img": _first_text(
                    album_info, "sizable_cover", "cover", "picUrl"
                )
                or _first_text(data, "cover_pic", "pic"),
                "timelength": _first_text(
                    audio_info, "timelength", "timelength_128", "timelength_320"
                ),
                "hash": song_hash,
                "album_id": _first_text(album_info, "album_id", "id"),
                "album_audio_id": _first_text(data, "album_audio_id"),
                "mixsongid": _first_text(data, "mixsongid"),
            }
        )
        return await self._kugou_single_result(
            source_url=url,
            song_hash=song_hash,
            fallback=fallback,
            album_name=_first_text(album_info, "album_name", "name"),
        )

    @handle(
        "m.kugou.com/share",
        r"m\.kugou\.com/share/\?[^\s]+",
    )
    async def _handle_mobile_share(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return await self._redirect_and_parse(url)

    @handle(
        "www.kugou.com/mixsong",
        r"www\.kugou\.com/mixsong/[^\s?]+\.html(?:\?[^\s]+)?",
    )
    async def _handle_mixsong(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.headers, "Referer": "https://www.kugou.com/"},
        ) as response:
            if response.status >= 400:
                raise ParseException(f"酷狗音乐歌曲请求失败：HTTP {response.status}")
            html = await response.text()
        track = self._decode_mixsong_payload(html)
        song_hash = _first_text(track, "hash")
        if not song_hash:
            raise ParseException("酷狗音乐歌曲缺少有效标识")
        return await self._kugou_single_result(
            source_url=url,
            song_hash=song_hash,
            fallback=track,
            album_name=self._mixsong_album_name(html),
        )

    @handle(
        "h5.kugou.com/v2/v-",
        r"h5\.kugou\.com/v2/v-[^/\s]+/index\.html\?[^\s]+",
    )
    async def _handle_h5_share(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        query = parse_qs(urlparse(url).query)
        song_hash = (query.get("hash") or [""])[0]
        if not song_hash:
            raise ParseException("酷狗音乐歌曲缺少有效标识")
        return await self._kugou_single_result(
            source_url=url,
            song_hash=song_hash,
            fallback={
                "album_id": (query.get("album_id") or [None])[0],
                "album_audio_id": (query.get("album_audio_id") or [None])[0],
            },
        )

    @handle("t1.kugou.com", r"t1\.kugou\.com/[A-Za-z0-9_-]+/?")
    async def _handle_short(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return await self._redirect_and_parse(url)

    @handle(
        "activity.kugou.com/share",
        r"activity\.kugou\.com/share/[^\s?]+(?:\?[^\s]+)?",
    )
    async def _handle_share(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        query = parse_qs(urlparse(url).query)
        global_id = (query.get("global_specialid") or [""])[0]
        if not global_id:
            raise ParseException("酷狗分享链接缺少歌单标识")
        return await self._parse_playlist(global_id, url)

    @staticmethod
    def _decode_mobile_songlist(html: str) -> Mapping[str, Any]:
        """Extract the server-rendered ``window.$output`` payload.

        The mobile song-list page embeds the playlist metadata and the first
        page of songs directly in the HTML.  This is more reliable than
        redirecting to the desktop page, which currently presents an
        anti-bot shell to non-browser clients.
        """

        marker = "window.$output"
        marker_pos = html.find(marker)
        if marker_pos < 0:
            raise ParseException("酷狗歌单页面没有返回可识别的数据")
        assignment = html.find("=", marker_pos + len(marker))
        if assignment < 0:
            raise ParseException("酷狗歌单页面没有返回可识别的数据")
        try:
            payload, _ = json.JSONDecoder().raw_decode(html[assignment + 1 :].lstrip())
        except json.JSONDecodeError as exc:
            raise ParseException("酷狗歌单页面数据格式无法识别") from exc
        if not isinstance(payload, Mapping):
            raise ParseException("酷狗歌单页面没有返回可识别的数据")
        return payload

    @handle(
        "m.kugou.com/songlist",
        r"m\.kugou\.com/songlist/gcid_[A-Za-z0-9_-]+(?:/[^\s?]*)?(?:\?[^\s]+)?",
    )
    @handle(
        "www.kugou.com/songlist",
        r"www\.kugou\.com/songlist/gcid_[A-Za-z0-9_-]+(?:/[^\s?]*)?(?:\?[^\s]+)?",
    )
    async def _handle_mobile_songlist(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.android_headers, "Referer": self._MOBILE_REFERER},
        ) as response:
            if response.status >= 400:
                raise ParseException(f"酷狗歌单页面请求失败: HTTP {response.status}")
            html = await response.text()

        payload = self._decode_mobile_songlist(html)
        info = payload.get("info")
        info = info if isinstance(info, Mapping) else {}
        list_info = info.get("listinfo")
        list_info = list_info if isinstance(list_info, Mapping) else {}
        songs = info.get("songs")
        songs = songs if isinstance(songs, list) else []
        if not list_info and not songs:
            raise ParseException("酷狗歌单不存在或暂时无法访问")

        return self._playlist_result(
            title=_first_text(list_info, "name") or "酷狗歌单",
            author_name=_first_text(list_info, "list_create_username") or "未知用户",
            author_avatar=_cover_url(list_info.get("create_user_pic"), size=165),
            description=_clean_text(list_info.get("intro")),
            cover=_cover_url(list_info.get("pic")),
            track_count=_as_int(list_info.get("count")),
            timestamp=_timestamp(list_info.get("create_time")),
            url=url,
            identifier=(
                _first_text(payload, "global_collection_id", "encode_src_gid")
                or url
            ),
            tracks=[item for item in songs if isinstance(item, Mapping)],
            stats={
                "plays": list_info.get("heat"),
                "favorites": list_info.get("collect_count"),
                "comments": list_info.get("comment_count"),
            },
        )

    async def _parse_playlist(self, global_id: str, source_url: str) -> ParseResult:
        list_payload = {"data": [{"specialid": 0, "global_collection_id": global_id}]}
        list_body = json.dumps(list_payload, ensure_ascii=False, separators=(",", ":"))
        list_params = self._signed_params()
        list_params.pop("signature", None)
        list_params["signature"] = self._signature(list_params, list_body)
        list_response = await self._json_request(
            "https://pubsongs.kugou.com/v1/get_list_info?" + urlencode(list_params),
            method="POST",
            headers={
                "Referer": "https://activity.kugou.com/",
                "Content-Type": "application/json;charset=utf-8",
                "KG-TID": "82",
                "x-router": "openapi.kugou.com",
            },
            data=list_body,
        )
        listed = list_response.get("data") if isinstance(list_response, Mapping) else None
        list_info = listed[0] if isinstance(listed, list) and listed else {}
        if not isinstance(list_info, Mapping) or not list_info:
            raise ParseException("酷狗歌单不存在或暂时无法访问")

        file_params = self._signed_params(
            {
                "uid": 0,
                "appid": 1058,
                "token": "",
                "type": 0,
                "module": "playlist",
                "page": 1,
                "pagesize": self._MAX_PREVIEW_TRACKS,
                "global_collection_id": global_id,
            }
        )
        file_body = await self._json_request(
            "https://pubsongscdn.kugou.com/v2/get_other_list_file?"
            + urlencode(file_params),
            headers={"Referer": "https://activity.kugou.com/"},
        )
        file_data = file_body.get("data") if isinstance(file_body, Mapping) else None
        file_data = file_data if isinstance(file_data, Mapping) else {}
        songs = file_data.get("info")
        songs = songs if isinstance(songs, list) else []

        return self._playlist_result(
            title=_first_text(list_info, "name") or "酷狗歌单",
            author_name=_first_text(list_info, "list_create_username") or "未知用户",
            author_avatar=_cover_url(list_info.get("create_user_pic"), size=165),
            description=_clean_text(list_info.get("intro")),
            cover=_cover_url(list_info.get("pic")),
            # The signed list endpoint occasionally reports one fewer item in
            # ``file_data.count`` than the playlist metadata.  Prefer the
            # playlist's declared count for the card and use the returned list
            # only for the small song preview below.
            track_count=_as_int(list_info.get("count") or file_data.get("count")),
            timestamp=_timestamp(list_info.get("create_time")),
            url=source_url,
            identifier=global_id,
            tracks=[item for item in songs if isinstance(item, Mapping)],
            stats={
                "plays": 0,
                "favorites": list_info.get("collect_total"),
            },
        )


class QishuiMusicParser(PlaylistParserBase):
    platform = Platform(name="qishui", display_name="汽水音乐")

    @staticmethod
    def _decode_router_data(html: str) -> Mapping[str, Any]:
        """Extract the server-rendered Qishui router payload from a page."""

        marker = re.search(r"_ROUTER_DATA\s*=\s*", html)
        if marker:
            try:
                payload, _ = json.JSONDecoder().raw_decode(
                    html[marker.end() :].lstrip()
                )
            except json.JSONDecodeError as exc:
                raise ParseException("汽水音乐页面数据无法解析") from exc
            if isinstance(payload, Mapping):
                return payload

        # Keep compatibility with the newer Modern.js page shell, which puts
        # the same JSON payload in a dedicated script element.
        script = re.search(
            r'<script[^>]+id=["\']__MODERN_ROUTER_DATA__["\'][^>]*>(.*?)</script>',
            html,
            flags=re.DOTALL,
        )
        if script:
            try:
                payload = json.loads(script.group(1).strip())
            except json.JSONDecodeError as exc:
                raise ParseException("汽水音乐页面数据无法解析") from exc
            if isinstance(payload, Mapping):
                return payload

        raise ParseException("汽水音乐页面没有返回可识别的数据")

    def _track_result(
        self,
        track: Mapping[str, Any],
        *,
        url: str,
        track_id: str,
    ) -> ParseResult:
        """Build a card and optional audio message for a shared single track."""

        track_info = track.get("trackInfo")
        track_info = track_info if isinstance(track_info, Mapping) else track
        album = track_info.get("album")
        album = album if isinstance(album, Mapping) else {}
        artists = _track_artist_names(track_info)
        artist_name = (
            " / ".join(artists)
            or _first_text(track, "artistName", "artist_name")
            or "未知艺术家"
        )
        title = (
            _first_text(track, "trackName", "name")
            or _first_text(track_info, "name", "title")
            or "未命名歌曲"
        )
        album_name = _first_text(album, "name", "title")
        cover = (
            _cover_url(track.get("coverURL"), size=400)
            or _cover_url(album.get("url_cover"), size=400)
            or _track_cover_url(track_info)
        )

        author_avatar: str | None = None
        raw_artists = track_info.get("artists")
        if isinstance(raw_artists, list) and raw_artists:
            first_artist = raw_artists[0]
            if isinstance(first_artist, Mapping):
                author_avatar = _cover_url(first_artist.get("url_avatar"), size=400)
                user_info = first_artist.get("user_info")
                if not author_avatar and isinstance(user_info, Mapping):
                    author_avatar = _cover_url(
                        user_info.get("medium_avatar_url"), size=720
                    )

        duration = track.get("duration")
        if duration is None:
            duration = track_info.get("duration")
        audio_url = _first_text(track, "url", "audio_url")
        stats = track_info.get("stats")
        stats = stats if isinstance(stats, Mapping) else {}
        return self._single_track_result(
            title=title,
            artist_name=artist_name,
            album_name=album_name,
            cover=cover,
            duration=duration,
            url=url,
            identifier=track_id,
            author_avatar=author_avatar,
            timestamp=_date_timestamp(album.get("release_date")),
            audio_url=audio_url,
            stats=stats,
        )

    @handle("qishui.douyin.com/s", r"qishui\.douyin\.com/s/[A-Za-z0-9_-]+/?")
    async def _handle_short(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        # The short-link edge currently serves its redirect reliably over
        # HTTP; HTTPS can leave an aiohttp connection waiting for the edge
        # timeout in some regions.
        if url.startswith("https://"):
            url = url.replace("https://", "http://", 1)
        try:
            return await self._redirect_and_parse(url)
        except RedirectException:
            # Some deployments serve the short-link redirect over HTTP while
            # keeping the advertised URL on HTTPS.
            if url.startswith("http://"):
                return await self._redirect_and_parse(url.replace("http://", "https://", 1))
            raise

    @handle(
        "music.douyin.com/qishui/share/track",
        r"music\.douyin\.com/qishui/share/track\?(?:[^&\s]*&)*track_id=(?P<track_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    async def _handle_track(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.headers, "Referer": "https://qishui.douyin.com/"},
        ) as response:
            if response.status >= 400:
                raise ParseException(f"汽水音乐页面请求失败：HTTP {response.status}")
            html = await response.text()

        router_data = self._decode_router_data(html)
        loader_data = router_data.get("loaderData")
        loader_data = loader_data if isinstance(loader_data, Mapping) else {}
        page = loader_data.get("track_page")
        page = page if isinstance(page, Mapping) else {}
        track = page.get("audioWithLyricsOption")
        if not isinstance(track, Mapping):
            raise ParseException("汽水音乐页面没有返回可识别的歌曲数据")

        return self._track_result(
            track,
            url=url,
            track_id=str(track.get("track_id") or searched.group("track_id")),
        )

    @handle(
        "music.douyin.com/qishui/share/playlist",
        r"music\.douyin\.com/qishui/share/playlist\?(?:[^&\s]*&)*playlist_id=(?P<playlist_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    async def _handle_playlist(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.session.get(
            url,
            headers={**self.headers, "Referer": "https://qishui.douyin.com/"},
        ) as response:
            if response.status >= 400:
                raise ParseException(f"汽水音乐歌单请求失败: HTTP {response.status}")
            html = await response.text()
        start = html.find("_ROUTER_DATA = ")
        if start < 0:
            raise ParseException("汽水音乐页面没有返回歌单数据")
        start += len("_ROUTER_DATA = ")
        end = html.find("\nfunction runWindowFn", start)
        if end < 0:
            raise ParseException("汽水音乐页面数据格式已变化")
        raw = html[start:end].strip().rstrip(";")
        try:
            router_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseException("汽水音乐歌单数据无法解析") from exc
        page = router_data.get("loaderData", {}).get("playlist_page", {})
        playlist = page.get("playlistInfo")
        if not isinstance(playlist, Mapping):
            raise ParseException("汽水音乐歌单不存在或暂时无法访问")
        owner = playlist.get("owner")
        owner = owner if isinstance(owner, Mapping) else {}
        medias = page.get("medias")
        medias = medias if isinstance(medias, list) else []
        tracks: list[Mapping[str, Any]] = []
        for media in medias:
            if not isinstance(media, Mapping):
                continue
            entity = media.get("entity")
            track = entity.get("track") if isinstance(entity, Mapping) else None
            if isinstance(track, Mapping):
                tracks.append(track)
        stats = playlist.get("stats")
        stats = stats if isinstance(stats, Mapping) else {}
        avatar = owner.get("medium_avatar_url")
        return self._playlist_result(
            title=_first_text(playlist, "title") or "汽水音乐歌单",
            author_name=_first_text(owner, "nickname", "public_name") or "未知用户",
            author_avatar=_cover_url(avatar, size=720),
            description=_clean_text(playlist.get("description")),
            cover=_cover_url(playlist.get("url_cover")),
            track_count=_as_int(playlist.get("count_tracks")),
            timestamp=_timestamp(playlist.get("create_time")),
            url=url,
            identifier=str(playlist.get("id") or searched.group("playlist_id")),
            tracks=tracks,
            stats={
                "favorites": stats.get("count_collected"),
                "shares": stats.get("count_shared"),
                "comments": stats.get("count_commented"),
            },
        )

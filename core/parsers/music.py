"""Music playlist and single-track parsers for supported music platforms.

Playlist links produce a compact summary instead of downloading every track;
single-track links expose the same card fields and attach audio when a playable
media URL can be resolved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import ClientError

from ..data import ImageContent, ParseResult, Platform
from ..exception import DownloadException, ParseException, RedirectException
from .base import BaseParser, handle


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
    display value such as ``02:46``.  Keeping the conversion in one place
    makes the single-track card and any optional audio content use the same
    duration regardless of the source platform.
    """

    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
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

        parser_config = getattr(
            getattr(self.cfg, "parser", None), self.platform.name, None
        )
        value = getattr(parser_config, "show_playlist_cover", None)
        return True if value is None else bool(value)

    def _show_playlist_url(self) -> bool:
        """Return whether the card should expose the source playlist URL."""

        parser_config = getattr(
            getattr(self.cfg, "parser", None), self.platform.name, None
        )
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
        author_avatar: str | None = None,
        timestamp: int | None = None,
        audio_url: str | None = None,
        stats: Mapping[str, object] | None = None,
    ) -> ParseResult:
        """Build the common card/audio representation for a single song."""

        duration_value = _duration_seconds(duration)

        contents = (
            self.create_image_contents([cover], headers=self.headers) if cover else []
        )
        if audio_url and audio_url.startswith(("http://", "https://")):
            contents.append(
                self.create_audio_content(
                    audio_url,
                    duration=duration_value,
                    headers=self.headers,
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

    @staticmethod
    def _decode_server_data(html: str) -> Mapping[str, Any]:
        match = re.search(
            r"<script[^>]+id=[\"']serialized-server-data[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not match:
            raise ParseException("Apple Music 页面没有返回可识别的歌单数据")
        try:
            payload = json.loads(match.group(1).strip().lstrip("\ufeff"))
        except json.JSONDecodeError as exc:
            raise ParseException("Apple Music 歌单数据无法解析") from exc
        if not isinstance(payload, Mapping):
            raise ParseException("Apple Music 页面没有返回可识别的歌单数据")
        return payload

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
        r"music\.apple\.com/(?:(?P<storefront>[a-z]{2})/)?playlist/(?:[^/\s<>]+/)?(?P<playlist_id>pl\.[A-Za-z0-9._-]+)(?:\?[^\s<>]*)?",
    )
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

        payload = self._decode_server_data(html)
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
        page = page if isinstance(page, Mapping) else root
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
    platform = Platform(name="qqmusic", display_name="QQ音乐")

    async def _fetch_song_details(self, song_ids: list[str]) -> list[Mapping[str, Any]]:
        """补充 QQ 歌单接口只返回 songids 时的歌曲元数据。"""

        async def fetch(song_id: str) -> Mapping[str, Any] | None:
            numeric_id = _as_int(song_id)
            if numeric_id is None:
                return None
            payload = {
                "comm": {"ct": 24, "cv": 0},
                "song": {
                    "method": "get_song_detail_yqq",
                    "module": "music.pf_song_detail_svr",
                    "param": {"song_id": numeric_id},
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
                body = await self._json_request(
                    api_url,
                    headers={"Referer": "https://y.qq.com/"},
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
        r"i\.y\.qq\.com/v8/playsong\.html\?(?:[^&\s]*&)*songid=(?P<song_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    @handle(
        "i2.y.qq.com/n3/other/pages/playsong",
        r"i2\.y\.qq\.com/n3/other/pages/playsong/index\.html\?(?:[^&\s]*&)*songid=(?P<song_id>\d+)(?=[&\s]|$)[^\s<>]*",
    )
    @handle(
        "y.qq.com/n/ryqq_v2/songDetail",
        r"y\.qq\.com/n/ryqq_v2/songDetail/(?P<song_id>\d+)(?:\?[^\s<>]*)?",
    )
    async def _handle_track(self, searched):
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        tracks = await self._fetch_song_details([searched.group("song_id")])
        track = tracks[0] if tracks else None
        if not track:
            raise ParseException("QQ音乐歌曲不存在或暂时无法访问")

        artist_names = _track_artist_names(track)
        return self._single_track_result(
            title=_track_title(track) or "未命名歌曲",
            artist_name=" / ".join(artist_names) or "未知艺术家",
            album_name=_track_album_name(track),
            cover=_track_cover_url(track),
            duration=track.get("interval"),
            url=url,
            identifier=_first_text(track, "mid") or searched.group("song_id"),
            timestamp=_date_timestamp(track.get("time_public")),
            audio_url=_first_text(track.get("file"), "url")
            if isinstance(track.get("file"), Mapping)
            else None,
        )

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
        body = await self._json_request(
            "https://music.163.com/api/song/detail?ids="
            + json.dumps([int(searched.group("song_id"))]),
            headers={"Referer": "https://music.163.com/"},
        )
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
                author_avatar = _cover_url(
                    first_artist.get("picUrl") or first_artist.get("img1v1Url")
                )
        song_id = _first_text(track, "id") or searched.group("song_id")
        cover = _track_cover_url(track)
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
            # 旧详情接口经常不返回 mp3Url。网易云的媒体跳转地址会重定向
            # 到歌曲文件，由共用 Downloader 下载并归档到本次解析目录。
            audio_url=_first_text(track, "mp3Url")
            or f"https://music.163.com/song/media/outer/url?id={song_id}",
        )
        # 网易云单曲附加的本地音频应始终作为语音发送，不受全局
        # “音频以文件形式发送”选项影响。
        result.extra["audio_as_voice"] = True
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

    async def _fetch_kugou_song_info(self, song_hash: str) -> Mapping[str, Any]:
        try:
            payload = await self._json_request(
                "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash="
                + song_hash,
                headers={"Referer": self._MOBILE_REFERER},
            )
        except ParseException:
            return {}
        return payload if isinstance(payload, Mapping) else {}

    async def _kugou_single_result(
        self,
        *,
        source_url: str,
        song_hash: str,
        fallback: Mapping[str, Any] | None = None,
        album_name: str | None = None,
    ) -> ParseResult:
        fallback = fallback if isinstance(fallback, Mapping) else {}
        info = await self._fetch_kugou_song_info(song_hash)
        title = (
            _first_text(info, "songName", "fileName")
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
            or fallback.get("album_img")
        )
        extra = info.get("extra")
        extra = extra if isinstance(extra, Mapping) else {}
        duration = (
            info.get("timeLength")
            or extra.get("320timelength")
            or extra.get("128timelength")
            or fallback.get("timelength")
        )
        audio_url = _first_text(info, "url", "play_url")
        return self._single_track_result(
            title=title,
            artist_name=artist_name,
            album_name=album_name or _first_text(info, "album_name"),
            cover=cover,
            duration=duration,
            url=source_url,
            identifier=(
                _first_text(info, "album_audio_id", "audio_id")
                or _first_text(fallback, "mixsongid", "encode_album_audio_id")
                or song_hash
            ),
            author_avatar=_cover_url(info.get("imgUrl"), size=165),
            audio_url=audio_url,
            stats={},
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

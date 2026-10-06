"""Music playlist parsers for QQ Music, NetEase, Kugou and Qishui Music.

The parsers intentionally produce a playlist summary instead of downloading
every track.  That keeps the result useful for the existing information-card
pipeline without turning a shared playlist link into a bulk audio download.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import ClientError

from ..data import ImageContent, ParseResult, Platform
from ..exception import DownloadException, ParseException, RedirectException
from .base import BaseParser, handle


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


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = unescape(str(value)).replace("\r\n", "\n").strip()
    return text or None


def _cover_url(value: object, *, size: int = 400) -> str | None:
    """Normalize the cover formats used by the four APIs."""

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
    url = url.replace("{size}", str(size))
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
        "y.qq.com/n/ryqq",
        r"y\.qq\.com/n/ryqq(?:_v2)?/playlist/(?P<playlist_id>\d+)",
    )
    @handle(
        "i.y.qq.com/n2/m/share/details/taoge",
        r"i\.y\.qq\.com/n2/m/share/details/taoge\.html\?[^\s]*id=(?P<playlist_id>\d+)",
    )
    @handle(
        "i2.y.qq.com/n3/other/pages/details/playlist",
        r"i2\.y\.qq\.com/n3/other/pages/details/playlist\.html\?[^\s]*id=(?P<playlist_id>\d+)",
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
        "music.163.com/#/playlist",
        r"music\.163\.com/(?:#/|m/)?playlist\?id=(?P<playlist_id>\d+)",
    )
    @handle(
        "music.163.com/playlist",
        r"music\.163\.com/(?:#/|m/)?playlist\?id=(?P<playlist_id>\d+)",
    )
    @handle(
        "music.163.com/m/playlist",
        r"music\.163\.com/m/playlist\?id=(?P<playlist_id>\d+)",
    )
    @handle(
        "y.music.163.com/m/playlist",
        r"y\.music\.163\.com/m/playlist\?[^\s]*id=(?P<playlist_id>\d+)",
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
        "music.douyin.com/qishui/share/playlist",
        r"music\.douyin\.com/qishui/share/playlist\?[^\s]*playlist_id=(?P<playlist_id>\d+)[^\s]*",
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

import asyncio
import json
import re
from asyncio import gather, to_thread
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from msgspec import Struct, convert, field

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import ImageContent
from ..download import Downloader
from ..utils import generate_file_name, safe_unlink
from .base import BaseParser, ParseException, Platform, handle


class XHSImage(Struct):
    urlDefault: str | None = None
    url: str | None = None
    urlSizeLarge: str | None = None
    livePhoto: bool = False
    stream: dict[str, list[dict[str, Any]] | None] | None = field(
        default_factory=dict
    )

    @property
    def image_url(self) -> str | None:
        return self.urlDefault or self.urlSizeLarge or self.url

    @property
    def is_live_photo(self) -> bool:
        return self.livePhoto

    @property
    def video_url(self) -> str | None:
        if not self.is_live_photo:
            return None

        groups: list[list[dict[str, Any]]] = []
        streams = self.stream or {}
        for key in ("EF4", "EF5", "EF6", "EF7"):
            if entries := streams.get(key):
                groups.append(entries)
        for key, entries in streams.items():
            if key not in {"EF4", "EF5", "EF6", "EF7"} and entries:
                groups.append(entries)

        for entries in groups:
            for item in entries:
                url = item.get("masterUrl") or item.get("master_url")
                if url:
                    return url
                backups = (
                    item.get("backupUrls")
                    or item.get("backup_urls")
                    or item.get("backupUrl")
                    or item.get("backup_url")
                )
                if backups:
                    if isinstance(backups, str):
                        return backups
                    return backups[0]
        return None


class XHSParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="xhs", display_name="小红书")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.xhs
        self.cookies = self.mycfg.cookies
        self.headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                    "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                )
            }
        )
        self.ios_headers.update(
            {
                "origin": "https://www.xiaohongshu.com",
                "x-requested-with": "XMLHttpRequest",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="xiaohongshu.com")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str
            self.ios_headers["cookie"] = self.cookiejar.cookies_str

    @staticmethod
    def _engagement_payload(value: object) -> dict[str, Any]:
        """兼容小红书不同页面版本的 interactInfo 命名。"""
        if not isinstance(value, dict):
            return {}
        for key in ("interactInfo", "interact_info", "statistics", "stats"):
            candidate = value.get(key)
            if isinstance(candidate, dict) and candidate:
                return candidate
        # 某些 SSR 版本把互动节点再包在 noteData/data 中。
        for child in value.values():
            if isinstance(child, dict):
                found = XHSParser._engagement_payload(child)
                if found:
                    return found
        return {}

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+")
    @handle("xhslink.cn", r"xhslink\.cn/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url, self.ios_headers)

    # https://www.xiaohongshu.com/discovery/item/68e8e3fa00000000030342ec?app_platform=android&ignoreEngage=true&app_version=9.6.0&share_from_user_hidden=true&xsec_source=app_share&type=normal&xsec_token=CBW9rwIV2qhcCD-JsQAOSHd2tTW9jXAtzqlgVXp6c52Sw%3D&author_share=1&xhsshare=QQ&shareRedId=ODs3RUk5ND42NzUyOTgwNjY3OTo8S0tK&apptime=1761372823&share_id=3b61945239ac403db86bea84a4f15124&share_channel=qq
    @handle(
        "xiaohongshu.com",
        r"(explore|discovery/item)/(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)\?[A-Za-z0-9._%&+=/#@-]+)",
    )
    async def _parse_common(self, searched: re.Match[str]):
        xhs_domain = "https://www.xiaohongshu.com"
        query, xhs_id = searched.group("query", "xhs_id")

        try:
            return await self.parse_explore(f"{xhs_domain}/explore/{query}", xhs_id)
        except Exception as e:
            logger.warning(
                f"parse_explore failed, error: {e}, fallback to parse_discovery"
            )
            return await self.parse_discovery(f"{xhs_domain}/discovery/item/{query}")

    async def parse_explore(self, url: str, xhs_id: str):
        async with self.session.get(url, headers=self.headers) as resp:
            html = await resp.text()
            logger.debug(f"url: {resp.url} | status: {resp.status}")

        json_obj = self._extract_initial_state_json(html)

        # ["note"]["noteDetailMap"][xhs_id]["note"]
        note_data = (
            json_obj.get("note", {})
            .get("noteDetailMap", {})
            .get(xhs_id, {})
            .get("note", {})
        )
        if not note_data:
            raise ParseException("can't find note detail in json_obj")

        class User(Struct):
            nickname: str
            avatar: str

        class NoteDetail(Struct):
            type: str
            title: str
            desc: str
            user: User
            imageList: list[XHSImage] = field(default_factory=list)
            video: Video | None = None

            @property
            def nickname(self) -> str:
                return self.user.nickname

            @property
            def avatar_url(self) -> str:
                return self.user.avatar

            @property
            def image_urls(self) -> list[str]:
                return [url for item in self.imageList if (url := item.image_url)]

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        note_detail = convert(note_data, type=NoteDetail)
        engagement = self.engagement_from_mapping(self._engagement_payload(note_data))

        contents = []
        has_motion_photo = False
        # 添加视频内容
        if video_url := note_detail.video_url:
            # 使用第一张图片作为封面
            cover_url = note_detail.image_urls[0] if note_detail.image_urls else None
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        elif note_detail.imageList:
            contents, has_motion_photo = self._create_image_contents(
                note_detail.imageList,
                headers=self.headers,
                referer=url,
            )
        else:
            has_motion_photo = False

        # 构建作者
        author = self.create_author(note_detail.nickname, note_detail.avatar_url)

        return self.result(
            title=note_detail.title,
            text=note_detail.desc,
            author=author,
            contents=contents,
            like_count=engagement.likes,
            comment_count=engagement.comments,
            favorite_count=engagement.favorites,
            share_count=engagement.shares,
            extra={"has_motion_photo": True} if has_motion_photo else {},
        )

    async def parse_discovery(self, url: str):
        async with self.session.get(
            url,
            headers=self.ios_headers,
            allow_redirects=True,
        ) as resp:
            html = await resp.text()

        json_obj = self._extract_initial_state_json(html)
        note_data = json_obj.get("noteData")
        if not note_data:
            raise ParseException("can't find noteData in json_obj")
        preload_data = note_data.get("normalNotePreloadData", {})
        note_data = note_data.get("data", {}).get("noteData", {})
        if not note_data:
            raise ParseException("can't find noteData in noteData.data")
        engagement = self.engagement_from_mapping(self._engagement_payload(note_data))

        class User(Struct):
            nickName: str
            avatar: str

        class NoteData(Struct):
            type: str
            title: str
            desc: str
            user: User
            time: int
            lastUpdateTime: int
            imageList: list[XHSImage] = field(default_factory=list)
            video: Video | None = None

            @property
            def image_urls(self) -> list[str]:
                return [url for item in self.imageList if (url := item.image_url)]

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        class NormalNotePreloadData(Struct):
            title: str
            desc: str
            imagesList: list[XHSImage] = field(default_factory=list)

            @property
            def image_urls(self) -> list[str]:
                return [url for item in self.imagesList if (url := item.image_url)]

        note_data = convert(note_data, type=NoteData)

        contents = []
        has_motion_photo = False
        if video_url := note_data.video_url:
            if preload_data:
                preload_data = convert(preload_data, type=NormalNotePreloadData)
                img_urls = preload_data.image_urls
            else:
                img_urls = note_data.image_urls
            contents.append(self.create_video_content(video_url, img_urls[0]))
        elif note_data.imageList:
            contents, has_motion_photo = self._create_image_contents(
                note_data.imageList,
                headers=self.headers,
                referer=url,
            )

        return self.result(
            title=note_data.title,
            author=self.create_author(note_data.user.nickName, note_data.user.avatar),
            contents=contents,
            text=note_data.desc,
            timestamp=note_data.time // 1000,
            like_count=engagement.likes,
            comment_count=engagement.comments,
            favorite_count=engagement.favorites,
            share_count=engagement.shares,
            extra={"has_motion_photo": True} if has_motion_photo else {},
        )

    def _create_image_contents(
        self,
        images: list[XHSImage],
        *,
        headers: dict[str, str],
        referer: str,
    ) -> tuple[list[ImageContent], bool]:
        """创建小红书普通图片和实况图的媒体内容。"""
        contents: list[ImageContent] = []
        has_motion_photo = False
        for index, image in enumerate(images):
            if image.is_live_photo:
                has_motion_photo = True
            image_url = image.image_url
            if not image_url:
                continue

            video_url = image.video_url
            if image.is_live_photo:
                if video_url:
                    task = asyncio.create_task(
                        self._download_motion_photo(
                            image_url,
                            video_url,
                            headers=headers,
                            referer=referer,
                        ),
                        name=f"xhs_motion_photo_{index}",
                    )
                    contents.append(ImageContent(task, card_error_placeholder=True))
                    continue
                logger.warning(
                    f"[小红书] 实况图缺少视频地址，回退发送静态图: index={index}"
                )

            task = self.downloader.download_img(
                image_url,
                headers=headers,
                proxy=self.proxy,
                worker_proxy_url=self.worker_proxy_url,
            )
            contents.append(
                ImageContent(task, card_error_placeholder=image.is_live_photo)
            )
        return contents, has_motion_photo

    @staticmethod
    def _convert_to_jpeg(source: Path, target: Path) -> Path:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGB").save(target, format="JPEG", quality=95)
        return target

    @staticmethod
    async def _cleanup_motion_photo_files(
        paths: set[Path],
        *,
        reason: str,
    ) -> None:
        if not paths:
            return
        await gather(*(safe_unlink(path) for path in paths))
        remaining = [path.name for path in paths if path.exists()]
        if remaining:
            logger.warning(
                f"[小红书] {reason}，Motion Photo 中间文件未能完全清理: "
                + ", ".join(remaining)
            )

    async def _download_motion_photo(
        self,
        image_url: str,
        video_url: str,
        *,
        headers: dict[str, str],
        referer: str,
    ) -> Path:
        cache_key = f"{image_url}|{video_url}"
        cache_stem = Path(generate_file_name(cache_key)).stem
        output_path = self.cfg.cache_dir / f"xhs_motion_{cache_stem}.jpg"
        if output_path.exists():
            return output_path

        work_id = uuid4().hex
        image_path = self.cfg.cache_dir / (
            f".xhs_motion_{cache_stem}_{work_id}_cover.webp"
        )
        jpeg_path = self.cfg.cache_dir / (
            f".xhs_motion_{cache_stem}_{work_id}_cover.jpg"
        )
        video_path = self.cfg.cache_dir / (
            f".xhs_motion_{cache_stem}_{work_id}_clip.mp4"
        )
        media_headers = headers.copy()
        media_headers.setdefault("Referer", referer)
        image_task = self.downloader.download_img(
            image_url,
            img_name=image_path.name,
            headers=media_headers,
            proxy=self.proxy,
            worker_proxy_url=self.worker_proxy_url,
        )
        video_task = self.downloader.download_video(
            video_url,
            video_name=video_path.name,
            headers=media_headers,
            proxy=self.proxy,
            worker_proxy_url=self.worker_proxy_url,
        )
        image_result, video_result = await gather(
            image_task,
            video_task,
            return_exceptions=True,
        )

        if isinstance(image_result, BaseException):
            paths = {video_result} if isinstance(video_result, Path) else set()
            await self._cleanup_motion_photo_files(paths, reason="静态封面下载失败")
            raise image_result
        if isinstance(video_result, BaseException):
            logger.warning(f"[小红书] 实况片段下载失败，回退发送静态图: {video_result}")
            return image_result

        try:
            await to_thread(self._convert_to_jpeg, image_result, jpeg_path)
            from .douyin.motion_photo import build_motion_photo

            result = await to_thread(
                build_motion_photo,
                jpeg_path,
                video_result,
                output_path,
            )
        except (ImportError, OSError, ValueError) as exc:
            logger.warning(f"[小红书] Motion Photo 封装失败，回退发送静态图: {exc}")
            await self._cleanup_motion_photo_files(
                {video_result, jpeg_path},
                reason="Motion Photo 封装失败",
            )
            return image_result

        await self._cleanup_motion_photo_files(
            {image_result, jpeg_path, video_result},
            reason="Motion Photo 封装成功",
        )
        logger.info(f"[小红书] Motion Photo 封装完成: {result.name}")
        return result

    def _extract_initial_state_json(self, html: str) -> dict[str, Any]:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html)
        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")

        json_str = matched.group(1).replace("undefined", "null")
        return json.loads(json_str)


class Stream(Struct):
    h264: list[dict[str, Any]] | None = None
    h265: list[dict[str, Any]] | None = None
    av1: list[dict[str, Any]] | None = None
    h266: list[dict[str, Any]] | None = None


class Media(Struct):
    stream: Stream


class Video(Struct):
    media: Media

    @property
    def video_url(self) -> str | None:
        stream = self.media.stream

        # h264 有水印，h265 无水印
        if stream.h265:
            return stream.h265[0]["masterUrl"]
        elif stream.h264:
            return stream.h264[0]["masterUrl"]
        elif stream.av1:
            return stream.av1[0]["masterUrl"]
        elif stream.h266:
            return stream.h266[0]["masterUrl"]
        return None

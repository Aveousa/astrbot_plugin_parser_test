import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import AudioContent, ImageContent
from core.exception import DownloadException
from core.parsers.music import AppleMusicParser


class _Response:
    def __init__(self, status: int, body: str, *, delay: float = 0.0):
        self.status = status
        self.body = body
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.body


class _Session:
    closed = False

    def __init__(self, resolver):
        self.resolver = resolver
        self.urls: list[str] = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        response = self.resolver(url)
        if isinstance(response, BaseException):
            raise response
        status, body, *rest = response
        return _Response(status, body, delay=rest[0] if rest else 0.0)


class _Downloader:
    def __init__(self, *, fail=False):
        self.image_urls: list[str] = []
        self.audio_urls: list[str] = []
        self.fail = fail

    def download_img(self, url, **_kwargs):
        self.image_urls.append(url)

        async def complete():
            if self.fail:
                raise DownloadException("image failed")
            return Path("artist.jpg")

        return asyncio.create_task(complete())

    def download_audio(self, url, **_kwargs):
        self.audio_urls.append(url)

        async def complete():
            return Path("song.m4a")

        return asyncio.create_task(complete())


def _parser(tmp_path, resolver, *, fail_download=False):
    parser = AppleMusicParser.__new__(AppleMusicParser)
    parser.cfg = SimpleNamespace(
        cache_dir=tmp_path,
        data_dir=tmp_path / "data",
        card_enabled=True,
        parser=SimpleNamespace(
            applemusic=SimpleNamespace(
                use_proxy=False,
                return_audio=False,
                audio_send_format="file",
            )
        ),
    )
    parser.headers = {"User-Agent": "test"}
    parser.downloader = _Downloader(fail=fail_download)
    parser._session = _Session(resolver)
    return parser


def _artist_html(name="Artist", image="https://cdn.example/artist.jpg", artist_id="42"):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "MusicGroup",
                "name": name,
                "url": f"https://music.apple.com/us/artist/{name}/{artist_id}",
                "image": image,
            }
        )
        + "</script>"
    )


def test_artist_id_page_and_metadata_cache_hit(tmp_path):
    calls = []

    def resolver(url):
        calls.append(url)
        return 200, _artist_html()

    async def run():
        parser = _parser(tmp_path, resolver)
        candidate = {
            "name": "Artist",
            "id": "42",
            "url": "https://music.apple.com/cn/artist/Artist/42",
        }
        first = await parser._lookup_artist_image(
            candidate, storefront="cn", cover_url="https://cdn.example/album.jpg"
        )
        second = await parser._lookup_artist_image(
            candidate, storefront="cn", cover_url="https://cdn.example/album.jpg"
        )
        return first, second, calls

    first, second, calls = asyncio.run(run())
    assert first == second == "https://cdn.example/artist.jpg"
    assert len(calls) == 1
    assert (tmp_path / "data" / "apple_music_artist_meta.json").is_file()


def test_search_skips_wrong_first_result_and_requires_exact_name(tmp_path):
    def resolver(url):
        if "itunes.apple.com/search" in url:
            return 200, json.dumps(
                {
                    "results": [
                        {"artistName": "Other Artist", "artistId": 1},
                        {
                            "artistName": "Target Artist",
                            "artistId": 2,
                            "artistLinkUrl": "https://music.apple.com/us/artist/target/2",
                        },
                    ]
                }
            )
        return 200, _artist_html("Target Artist", artist_id="2")

    async def run():
        parser = _parser(tmp_path, resolver)
        return await parser._lookup_artist_image(
            {"name": "Target Artist"}, storefront="us", cover_url=None
        ), parser._session.urls

    image, urls = asyncio.run(run())
    assert image == "https://cdn.example/artist.jpg"
    assert "artist/target/2" in urls[-1]


def test_ambiguous_same_name_search_is_rejected(tmp_path):
    def resolver(url):
        return 200, json.dumps(
            {
                "results": [
                    {"artistName": "Same", "artistId": 1},
                    {"artistName": "Same", "artistId": 2},
                ]
            }
        )

    async def run():
        parser = _parser(tmp_path, resolver)
        return await parser._lookup_artist_image(
            {"name": "Same"}, storefront="us", cover_url=None
        ), parser._session.urls

    image, urls = asyncio.run(run())
    assert image is None
    assert len(urls) == 1


def test_search_rejects_non_apple_artist_url(tmp_path):
    def resolver(url):
        if "itunes.apple.com/search" in url:
            return 200, json.dumps(
                {
                    "results": [
                        {
                            "artistName": "Target",
                            "artistId": 2,
                            "artistLinkUrl": "https://attacker.example/artist/target/2",
                        }
                    ]
                }
            )
        return 200, _artist_html("Target", artist_id="2")

    async def run():
        parser = _parser(tmp_path, resolver)
        image = await parser._lookup_artist_image(
            {"name": "Target"}, storefront="us", cover_url=None
        )
        return image, parser._session.urls

    image, urls = asyncio.run(run())

    assert image == "https://cdn.example/artist.jpg"
    assert all("attacker.example" not in url for url in urls)


def test_multi_artist_prefers_first_link_with_id():
    candidates = AppleMusicParser._artist_candidates(
        {
            "artistLinks": [
                {"title": "Primary", "url": "https://music.apple.com/us/artist/p/10"},
                {"title": "Guest", "url": "https://music.apple.com/us/artist/g/11"},
            ]
        },
        {},
        {},
        "Primary / Guest",
    )
    assert candidates[0]["id"] == "10"
    assert candidates[0]["name"] == "Primary"


def test_multi_artist_avatar_uses_first_artist_before_later_id(tmp_path):
    """A guest link must not win merely because it has the first usable ID."""

    def resolver(url):
        if "itunes.apple.com/search" in url:
            return 200, json.dumps(
                {
                    "results": [
                        {
                            "artistName": "Primary",
                            "artistId": 10,
                            "artistLinkUrl": "https://music.apple.com/us/artist/primary/10",
                        }
                    ]
                }
            )
        if "/artist/primary/10" in url:
            return 200, _artist_html(
                "Primary",
                image="https://cdn.example/primary.jpg",
                artist_id="10",
            )
        return 200, _artist_html(
            "Guest", image="https://cdn.example/guest.jpg", artist_id="11"
        )

    async def run():
        parser = _parser(tmp_path, resolver)
        image = await parser._get_artist_avatar(
            track={
                "artistLinks": [
                    {"title": "Primary"},
                    {
                        "title": "Guest",
                        "url": "https://music.apple.com/us/artist/guest/11",
                    },
                ]
            },
            album_header={},
            fallback={},
            artist_name="Primary / Guest",
            cover_url=None,
            storefront="us",
        )
        return image, parser._session.urls

    image, urls = asyncio.run(run())

    assert image == "https://cdn.example/primary.jpg"
    assert any("term=Primary" in url for url in urls)
    assert all("/artist/guest/11" not in url for url in urls)


def test_multi_artist_display_name_keeps_commas_in_a_single_artist():
    parts = AppleMusicParser._artist_name_parts("Earth, Wind & Fire")

    assert parts == ["Earth, Wind & Fire"]


def test_artist_candidates_can_take_json_ld_artist_homepage():
    fallback = {
        "artists": [
            {
                "name": "Primary",
                "url": "https://music.apple.com/jp/artist/primary/10",
            },
            {"name": "Guest", "url": "https://music.apple.com/jp/artist/guest/11"},
        ]
    }

    candidates = AppleMusicParser._artist_candidates(
        {}, {}, fallback, "Primary / Guest"
    )

    assert candidates[0] == {
        "name": "Primary",
        "id": "10",
        "url": "https://music.apple.com/jp/artist/primary/10",
    }


def test_track_card_keeps_album_cover_and_resolves_artist_avatar_lazily(tmp_path):
    item = {
        "title": "Song",
        "artistName": "Artist",
        "artwork": {"dictionary": {"url": "https://cdn.example/{w}x{h}.jpg"}},
        "contentDescriptor": {
            "kind": "song",
            "identifiers": {"storeAdamID": "123"},
        },
        "artistLinks": [
            {
                "title": "Artist",
                "url": "https://music.apple.com/cn/artist/artist/42",
            }
        ],
    }
    page = {
        "data": [{"data": {"sections": [{"id": "track-list", "items": [item]}]}}]
    }
    track_html = (
        '<script id="serialized-server-data">'
        + json.dumps(page)
        + "</script>"
    )

    def resolver(url):
        if "/song/123" in url:
            return 200, track_html
        return 200, _artist_html(artist_id="42")

    async def run():
        parser = _parser(tmp_path, resolver)
        _, searched = parser.search_url("https://music.apple.com/cn/song/123")
        result = await parser._handle_track(searched)
        before = list(parser.downloader.image_urls)
        avatar_path = await result.author.get_avatar_path()
        return result, before, avatar_path, parser.downloader.image_urls

    result, before, avatar_path, image_urls = asyncio.run(run())
    assert len(result.contents) == 1
    assert isinstance(result.contents[0], ImageContent)
    assert before == ["https://cdn.example/500x500.jpg"]
    assert avatar_path == Path("artist.jpg")
    assert image_urls == [
        "https://cdn.example/500x500.jpg",
        "https://cdn.example/artist.jpg",
    ]


def test_avatar_failure_does_not_remove_cover_or_audio(tmp_path):
    item = {
        "title": "Song",
        "artistName": "Artist",
        "artwork": {"dictionary": {"url": "https://cdn.example/{w}x{h}.jpg"}},
        "contentDescriptor": {
            "kind": "song",
            "identifiers": {"storeAdamID": "123"},
        },
        "artistLinks": [
            {
                "title": "Artist",
                "url": "https://music.apple.com/us/artist/artist/42",
            }
        ],
    }
    page = {
        "data": [{"data": {"sections": [{"id": "track-list", "items": [item]}]}}]
    }
    track_html = (
        '<script id="serialized-server-data">'
        + json.dumps(page)
        + "</script>"
    )

    def resolver(url):
        if "/song/123" in url:
            return 200, track_html
        # A valid artist page with no usable portrait is a confirmed miss.
        return 200, _artist_html(image="https://cdn.example/placeholder.png")

    async def run():
        parser = _parser(tmp_path, resolver)
        parser.cfg.parser.applemusic.return_audio = True

        async def download_task(_url, _duration):
            return Path("song.m4a")

        parser.apple_music_backend = SimpleNamespace(
            settings=SimpleNamespace(enabled=True),
            download_task=lambda url, duration: asyncio.create_task(
                download_task(url, duration)
            ),
        )
        _, searched = parser.search_url("https://music.apple.com/us/song/123")
        result = await parser._handle_track(searched)
        avatar_path = await result.author.get_avatar_path()
        await asyncio.gather(*(content.get_path() for content in result.contents))
        return result, avatar_path, parser.downloader.image_urls

    result, avatar_path, image_urls = asyncio.run(run())
    assert avatar_path is None
    assert len(result.contents) == 2
    assert isinstance(result.contents[0], ImageContent)
    assert isinstance(result.contents[1], AudioContent)
    assert image_urls == ["https://cdn.example/500x500.jpg"]


def test_lazy_avatar_download_timeout_fails_open():
    class _HangingDownloader:
        def download_img(self, _url, **_kwargs):
            async def hang():
                await asyncio.Event().wait()

            return asyncio.create_task(hang())

    async def run():
        parser = AppleMusicParser.__new__(AppleMusicParser)
        parser.cfg = SimpleNamespace(
            proxy=None,
            parser=SimpleNamespace(applemusic=SimpleNamespace(use_proxy=False)),
        )
        parser.headers = {}
        parser.downloader = _HangingDownloader()
        result = parser._single_track_result(
            title="Song",
            artist_name="Artist",
            album_name=None,
            cover=None,
            duration=120,
            url="https://music.apple.com/us/song/1",
            identifier="1",
            author_avatar=lambda: asyncio.sleep(0, result="https://cdn.example/portrait.jpg"),
        )
        result.author.avatar._TIMEOUT_SECONDS = 0.01
        return await result.author.get_avatar_path(), result

    avatar_path, result = asyncio.run(run())

    assert avatar_path is None
    assert result.extra["card_preview_only"] is False


def test_json_ld_placeholder_and_album_cover_are_filtered():
    assert (
        AppleMusicParser._artist_image_from_html(
            _artist_html(image="https://cdn.example/placeholder.png")
        )
        is None
    )
    assert (
        AppleMusicParser._artist_image_from_html(
            _artist_html(image="https://music.apple.com/assets/meta/apple-music.png")
        )
        is None
    )
    assert (
        AppleMusicParser._artist_image_from_html(
            _artist_html(image="https://cdn.example/album-500x500.jpg"),
            cover_url="https://cdn.example/album-1000x1000.jpg",
        )
        is None
    )
    assert (
        AppleMusicParser._artist_image_from_html(
            '<meta property="og:image" content="https://cdn.example/portrait.jpg">'
        )
        == "https://cdn.example/portrait.jpg"
    )


def test_json_ld_artist_node_requires_identity_when_id_is_known():
    html = (
        '<script type="application/ld+json">'
        + json.dumps(
            [
                {
                    "@type": "MusicGroup",
                    "name": "Unrelated",
                    "image": "https://cdn.example/wrong.jpg",
                },
                {
                    "@type": "MusicGroup",
                    "name": "Artist",
                    "image": "https://cdn.example/right.jpg",
                },
            ]
        )
        + "</script>"
    )

    image = AppleMusicParser._artist_image_from_html(
        html, artist_id="42", artist_name="Artist"
    )

    assert image == "https://cdn.example/right.jpg"


def test_region_fallback_and_no_image_are_bounded(tmp_path):
    def resolver(url):
        if "/cn/" in url:
            return 200, _artist_html(image="https://cdn.example/album-500x500.jpg")
        return 200, _artist_html(image="https://cdn.example/portrait.jpg")

    async def run():
        parser = _parser(tmp_path, resolver)
        return await parser._lookup_artist_image(
            {"id": "42", "name": "Artist"}, storefront="cn", cover_url="https://cdn.example/album-1000x1000.jpg"
        ), parser._session.urls

    image, urls = asyncio.run(run())
    assert image == "https://cdn.example/portrait.jpg"
    assert len(urls) == 2
    assert "/cn/" in urls[0] and "/us/" in urls[1]


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limited_artist_page_short_circuits(tmp_path, status):
    def resolver(_url):
        return status, ""

    async def run():
        parser = _parser(tmp_path, resolver)
        first = await parser._lookup_artist_image(
            {"id": "42", "name": "Artist"}, storefront="us", cover_url=None
        )
        second = await parser._lookup_artist_image(
            {"id": "42", "name": "Artist"}, storefront="us", cover_url=None
        )
        return first, second, parser._session.urls

    first, second, urls = asyncio.run(run())
    assert first is second is None
    assert len(urls) == 1


def test_timeout_network_and_image_download_failures_are_fail_open(tmp_path):
    def resolver(url):
        if "/artist/" in url:
            return (200, "", 0.05)
        raise OSError("offline")

    async def run():
        parser = _parser(tmp_path, resolver, fail_download=True)
        parser._ARTIST_TOTAL_TIMEOUT = 0.01
        image = await parser._lookup_artist_image(
            {"id": "42", "name": "Artist"}, storefront="us", cover_url=None
        )
        result = parser._single_track_result(
            title="Song",
            artist_name="Artist",
            album_name="Album",
            cover=None,
            duration=120,
            url="https://music.apple.com/us/song/1",
            identifier="1",
            author_avatar=lambda: asyncio.sleep(0, result="https://cdn.example/portrait.jpg"),
        )
        avatar_path = await result.author.get_avatar_path()
        return image, avatar_path, result

    image, avatar_path, result = asyncio.run(run())
    assert image is None
    assert avatar_path is None
    assert result.extra["card_preview_only"] is False
    assert result.contents == []

import pytest

from core.parsers.pixiv import PixivParser


@pytest.mark.parametrize(
    ("url", "expected_keyword"),
    [
        ("https://www.pixiv.net/artworks/145313510", "pixiv.net/artworks"),
        ("https://www.pixiv.net/en/artworks/145313510", "pixiv.net/en/artworks"),
    ],
)
def test_pixiv_artwork_routes_support_default_and_english_paths(
    url: str, expected_keyword: str
):
    keyword, searched = PixivParser.search_url(url)

    assert keyword == expected_keyword
    assert searched.group("pid") == "145313510"
    assert PixivParser._handlers[keyword] is PixivParser._handle_artworks

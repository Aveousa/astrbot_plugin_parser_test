import pytest

from core.parsers.pixiv import PixivParser


@pytest.mark.parametrize(
    ("page_count", "illust_type", "enabled", "expected"),
    [
        (2, 0, True, True),
        (2, 0, False, False),
        (1, 0, True, False),
        (2, 1, True, False),
        (2, 2, True, False),
    ],
    ids=[
        "ordinary-multiple-enabled",
        "ordinary-multiple-disabled",
        "ordinary-single",
        "manga",
        "ugoira",
    ],
)
def test_multi_image_forward_only_applies_to_ordinary_multi_image_illusts(
    page_count: int,
    illust_type: int,
    enabled: bool,
    expected: bool,
):
    assert (
        PixivParser._should_forward_multi_image(page_count, illust_type, enabled)
        is expected
    )

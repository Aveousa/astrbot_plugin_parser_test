import pytest

from core.music_quality import (
    QQ_FILE_QUALITY_PRIORITY,
    QQ_QUALITY_FILENAMES,
    QQ_VOICE_QUALITY_PRIORITY,
    is_valid_qq_file_quality_priority,
    normalize_qq_file_quality_priority,
)


def test_qq_quality_tiers_map_to_supported_vkey_file_names():
    assert QQ_QUALITY_FILENAMES == {
        "standard": ("M500", ".mp3"),
        "high": ("M800", ".mp3"),
        "lossless": ("F000", ".flac"),
    }


def test_qq_file_quality_defaults_to_lossless_then_fallbacks():
    assert QQ_FILE_QUALITY_PRIORITY == ("lossless", "high", "standard")
    assert QQ_VOICE_QUALITY_PRIORITY == ("high", "standard")


@pytest.mark.parametrize("value,expected", [
    (["high", "standard"], ("high", "standard")),
    ("standard,lossless", ("standard", "lossless")),
    (["HIGH", "high", "invalid", "standard"], ("high", "standard")),
    ([], QQ_FILE_QUALITY_PRIORITY),
    (None, QQ_FILE_QUALITY_PRIORITY),
])
def test_normalize_qq_file_quality_priority(value, expected):
    assert normalize_qq_file_quality_priority(value) == expected


@pytest.mark.parametrize("value", [
    ["lossless", "high", "standard"],
    ["standard"],
    ["high", "lossless"],
])
def test_valid_qq_file_quality_priority(value):
    assert is_valid_qq_file_quality_priority(value)


@pytest.mark.parametrize("value", [
    [],
    ["standard", "standard"],
    ["hires"],
    ["lossless", "high", "standard", "standard"],
    "lossless,high",
])
def test_invalid_qq_file_quality_priority(value):
    assert not is_valid_qq_file_quality_priority(value)

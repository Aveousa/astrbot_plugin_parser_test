"""Shared quality policy for music-provider file playback."""

from __future__ import annotations

QQ_FILE_QUALITY_PRIORITY = ("lossless", "high", "standard")
# Voice delivery keeps the existing compact, fixed preference chain. It is not
# affected by the configurable file-quality order.
QQ_VOICE_QUALITY_PRIORITY = ("high", "standard")
QQ_SUPPORTED_QUALITIES = ("standard", "high", "lossless")
QQ_QUALITY_FILENAMES = {
    "standard": ("M500", ".mp3"),
    "high": ("M800", ".mp3"),
    "lossless": ("F000", ".flac"),
}


def normalize_qq_file_quality_priority(value: object) -> tuple[str, ...]:
    """Return a safe, ordered QQ file-quality preference with fallbacks."""

    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return QQ_FILE_QUALITY_PRIORITY
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        quality = item.strip().lower()
        if quality in QQ_QUALITY_FILENAMES and quality not in result:
            result.append(quality)
    return tuple(result) or QQ_FILE_QUALITY_PRIORITY


def is_valid_qq_file_quality_priority(value: object) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= len(QQ_SUPPORTED_QUALITIES)
        and all(isinstance(item, str) and item in QQ_SUPPORTED_QUALITIES for item in value)
        and len(value) == len(set(value))
    )

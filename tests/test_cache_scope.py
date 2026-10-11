from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.cache import (
    cache_dir_scope,
    clear_cache_dir,
    create_parse_cache_dir,
    get_active_cache_dir,
    get_cache_size,
)


def test_parse_cache_directories_are_isolated_and_sanitized(tmp_path: Path):
    first = create_parse_cache_dir(tmp_path, "bilibili.com/video")
    second = create_parse_cache_dir(tmp_path, "小红书/图文")

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first != second
    assert first.is_dir() and second.is_dir()
    assert "/" not in first.name and "\\" not in first.name
    assert "/" not in second.name and "\\" not in second.name


def test_cache_scope_is_inherited_by_download_tasks(tmp_path: Path):
    parse_dir = create_parse_cache_dir(tmp_path, "parse")
    default = tmp_path / "cache"

    async def read_in_task():
        return get_active_cache_dir(default)

    async def run():
        assert get_active_cache_dir(default) == default
        with cache_dir_scope(parse_dir):
            task = asyncio.create_task(read_in_task())
            assert get_active_cache_dir(default) == parse_dir
            assert await task == parse_dir
        assert get_active_cache_dir(default) == default

    asyncio.run(run())


def test_cache_size_and_clear_do_not_follow_symlinks(tmp_path: Path):
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    (cache / "media.bin").write_bytes(b"12345")
    nested = cache / "parse"
    nested.mkdir()
    (nested / "card.png").write_bytes(b"12")
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = cache / "outside-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        link = None

    assert get_cache_size(cache) == (7, 2)
    assert clear_cache_dir(cache) == (2, 7)
    assert cache.is_dir() and list(cache.iterdir()) == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    if link is not None:
        assert not link.exists()


def test_cache_clear_reports_locked_entries(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    nested = cache / "parse"
    nested.mkdir(parents=True)
    (nested / "locked.bin").write_bytes(b"still in use")

    def fail_remove(_path):
        raise PermissionError("locked")

    monkeypatch.setattr("core.cache.shutil.rmtree", fail_remove)
    with pytest.raises(OSError, match="could not remove"):
        clear_cache_dir(cache)

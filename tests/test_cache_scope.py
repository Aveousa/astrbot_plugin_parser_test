from __future__ import annotations

import asyncio
from pathlib import Path

from core.cache import (
    cache_dir_scope,
    create_parse_cache_dir,
    get_active_cache_dir,
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


from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import core.clean as clean_module
from core.clean import CacheCleaner


def test_schedule_hot_update_reuses_job_and_recovers_missing_job(tmp_path):
    async def run():
        cfg = SimpleNamespace(timezone="Asia/Shanghai", clean_cron="30 2 * * *", cache_dir=tmp_path)
        cleaner = CacheCleaner(cfg)
        original = cleaner.scheduler.get_job(cleaner.JOBNAME)
        try:
            cfg.clean_cron = "0 3 * * *"
            cleaner.update_schedule()
            assert cleaner.scheduler.get_job(cleaner.JOBNAME) is original
            assert len(cleaner.scheduler.get_jobs()) == 1
            assert "hour='3'" in str(cleaner.trigger)
            cleaner.scheduler.remove_job(cleaner.JOBNAME)
            cleaner.update_schedule()
            assert len(cleaner.scheduler.get_jobs()) == 1
        finally:
            await cleaner.stop()
    asyncio.run(run())


def test_cache_cleaner_keeps_the_existing_whole_cache_cleanup_policy(tmp_path):
    """Cards, downloaded media, emoji assets and temporary HTML share one cache.

    The renderer deliberately writes its transient HTML next to the card PNG;
    scheduled cleanup must continue to clear every inactive generated file
    while preserving the cache root itself.
    """
    cache_dir = tmp_path / "cache"
    emoji_dir = cache_dir / "emojis"
    emoji_dir.mkdir(parents=True)
    (cache_dir / "card_example.png").write_bytes(b"card")
    (cache_dir / "card_example.html").write_text("temporary", encoding="utf-8")
    (cache_dir / "video_example.mp4").write_bytes(b"video")
    (emoji_dir / "emoji.png").write_bytes(b"emoji")

    cleaner = object.__new__(CacheCleaner)
    cleaner.cfg = SimpleNamespace(cache_dir=cache_dir)
    asyncio.run(cleaner._clean_plugin_cache())

    assert cache_dir.is_dir()
    assert list(cache_dir.iterdir()) == []


def test_startup_cleanup_removes_only_stale_unused_playwright_directories(
    tmp_path, monkeypatch
):
    old_profile = tmp_path / "playwright_chromiumdev_profile-old"
    old_profile.mkdir()
    (old_profile / "state").write_text("stale", encoding="utf-8")
    old_artifacts = tmp_path / "playwright-artifacts-old"
    old_artifacts.mkdir()
    recent_profile = tmp_path / "playwright_chromiumdev_profile-recent"
    recent_profile.mkdir()
    unrelated = tmp_path / "chromium-profile-unrelated"
    unrelated.mkdir()

    stale_time = time.time() - CacheCleaner.PLAYWRIGHT_PROFILE_MAX_AGE_SECONDS - 1
    os.utime(old_profile, (stale_time, stale_time))
    os.utime(old_artifacts, (stale_time, stale_time))

    monkeypatch.setattr(clean_module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        CacheCleaner,
        "_get_running_process_command_lines",
        staticmethod(list),
    )

    removed = CacheCleaner._clean_stale_playwright_profiles()

    assert removed == 2
    assert not old_profile.exists()
    assert not old_artifacts.exists()
    assert recent_profile.is_dir()
    assert unrelated.is_dir()


def test_startup_cleanup_preserves_profile_referenced_by_running_browser(
    tmp_path, monkeypatch
):
    active_profile = tmp_path / "playwright_chromiumdev_profile-active"
    active_profile.mkdir()
    stale_time = time.time() - CacheCleaner.PLAYWRIGHT_PROFILE_MAX_AGE_SECONDS - 1
    os.utime(active_profile, (stale_time, stale_time))

    monkeypatch.setattr(clean_module.tempfile, "gettempdir", lambda: str(tmp_path))
    command_line = f'chromium --user-data-dir="{active_profile}" --headless'
    monkeypatch.setattr(
        CacheCleaner,
        "_get_running_process_command_lines",
        staticmethod(lambda: [command_line]),
    )

    assert CacheCleaner._clean_stale_playwright_profiles() == 0
    assert active_profile.is_dir()


def test_startup_cleanup_skips_all_profiles_when_process_inspection_fails(
    tmp_path, monkeypatch
):
    stale_profile = tmp_path / "playwright_chromiumdev_profile-unknown"
    stale_profile.mkdir()
    stale_time = time.time() - CacheCleaner.PLAYWRIGHT_PROFILE_MAX_AGE_SECONDS - 1
    os.utime(stale_profile, (stale_time, stale_time))

    monkeypatch.setattr(clean_module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        CacheCleaner,
        "_get_running_process_command_lines",
        staticmethod(lambda: None),
    )

    assert CacheCleaner._clean_stale_playwright_profiles() == 0
    assert stale_profile.is_dir()

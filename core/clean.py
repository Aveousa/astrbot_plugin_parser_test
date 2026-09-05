import asyncio
import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger

from .config import PluginConfig


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。
    """

    JOBNAME = "CacheCleaner"
    # Keep this allow-list narrow so startup cleanup cannot touch unrelated
    # application data in the system temporary directory.
    PLAYWRIGHT_TEMP_PREFIXES = (
        "playwright_chromiumdev_profile-",
        "playwright-artifacts-",
    )
    PLAYWRIGHT_PROFILE_MAX_AGE_SECONDS = 24 * 60 * 60
    _PLAYWRIGHT_DIR_ARG_RE = re.compile(
        r"(?:^|\s)--(?:user-data-dir|artifacts-dir)(?:=|\s+)"
        r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)",
        flags=re.IGNORECASE,
    )

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.scheduler = AsyncIOScheduler(timezone=self.cfg.timezone)
        self.scheduler.start()

        self.register_task()

        logger.info(f"{self.JOBNAME} 已启动，任务周期：{self.cfg.clean_cron}")

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cfg.clean_cron)
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    async def _clean_plugin_cache(self) -> None:
        """删除并重建缓存目录"""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, shutil.rmtree, self.cfg.cache_dir)
            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Cache directory cleaned and recreated.")
        except Exception:
            logger.exception("Error while cleaning cache directory.")

    async def clean_stale_playwright_profiles(self) -> None:
        """启动时回收陈旧的 Playwright 临时目录。

        目录清理在工作线程中执行，避免进程枚举和 ``rmtree`` 阻塞插件的
        asyncio 事件循环。任何无法确认进程状态或删除失败的情况都会被
        安全地跳过，不影响插件启动。
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._clean_stale_playwright_profiles)
        except Exception:
            logger.exception("Error while cleaning stale Playwright directories.")

    @classmethod
    def _clean_stale_playwright_profiles(cls) -> int:
        """同步清理陈旧 Playwright 临时目录，返回成功删除的数量。"""
        temp_root = cls._temporary_directory_root()
        if temp_root is None:
            return 0

        now = time.time()
        candidates: list[Path] = []
        try:
            entries = tuple(temp_root.iterdir())
        except OSError as exc:
            logger.warning(f"[Playwright cleanup] 无法扫描临时目录 {temp_root}: {exc}")
            return 0

        for entry in entries:
            if not cls._is_playwright_temp_directory(entry, temp_root):
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError as exc:
                logger.debug(f"[Playwright cleanup] 无法读取目录时间 {entry}: {exc}")
                continue
            if age > cls.PLAYWRIGHT_PROFILE_MAX_AGE_SECONDS:
                candidates.append(entry)

        if not candidates:
            return 0

        # If process inspection is unavailable, preserving every candidate is
        # safer than guessing that it is unused.
        process_command_lines = cls._get_running_process_command_lines()
        if process_command_lines is None:
            logger.warning(
                "[Playwright cleanup] 无法确认浏览器进程状态，跳过陈旧目录清理。"
            )
            return 0

        removed = 0
        for directory in candidates:
            if cls._is_directory_referenced_by_browser(
                directory, process_command_lines
            ):
                logger.debug(
                    f"[Playwright cleanup] 目录仍被浏览器使用，跳过: {directory}"
                )
                continue
            try:
                # Re-check the target immediately before deletion. This also
                # protects against a path being replaced by a symlink after
                # the initial scan.
                if not cls._is_playwright_temp_directory(directory, temp_root):
                    continue
                shutil.rmtree(directory)
                removed += 1
                logger.info(f"[Playwright cleanup] 已删除陈旧目录: {directory}")
            except OSError as exc:
                logger.warning(f"[Playwright cleanup] 删除目录失败 {directory}: {exc}")

        return removed

    @staticmethod
    def _temporary_directory_root() -> Path | None:
        try:
            root = Path(tempfile.gettempdir()).resolve()
            return root if root.is_dir() else None
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(f"[Playwright cleanup] 无法确定系统临时目录: {exc}")
            return None

    @classmethod
    def _is_playwright_temp_directory(cls, entry: Path, temp_root: Path) -> bool:
        name = entry.name.casefold()
        if not any(
            name.startswith(prefix.casefold())
            for prefix in cls.PLAYWRIGHT_TEMP_PREFIXES
        ):
            return False
        try:
            # Do not follow symlinks/junctions, and only allow direct children
            # of the system temporary directory as deletion targets.
            if entry.is_symlink() or not entry.is_dir():
                return False
            resolved = entry.resolve()
            return resolved.parent == temp_root
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _get_running_process_command_lines() -> list[str] | None:
        """返回运行中进程命令行；无法可靠获取时返回 ``None``。"""
        if os.name == "nt":
            # WMIC is absent on recent Windows installations. CIM via
            # PowerShell returns the complete command line needed to inspect
            # Chromium's profile/artifact directory arguments.
            # Encode each value as UTF-8/base64 inside PowerShell.  This avoids
            # losing non-ASCII user/profile path characters to the Windows
            # console code page while the output is captured by Python.
            command = (
                "Get-CimInstance Win32_Process "
                "| ForEach-Object { if ($_.CommandLine) { "
                "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
                "[string]$_.CommandLine)) } }"
            )
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        command,
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode != 0:
                return None
            lines: list[str] = []
            for encoded_line in result.stdout.splitlines():
                if not encoded_line.strip():
                    continue
                try:
                    lines.append(
                        base64.b64decode(encoded_line, validate=True).decode("utf-8")
                    )
                except (ValueError, UnicodeDecodeError):
                    return None
            return lines or None

        proc_root = Path("/proc")
        if proc_root.is_dir():
            try:
                entries = tuple(proc_root.iterdir())
            except OSError:
                return None
            command_lines: list[str] = []
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "cmdline").read_bytes()
                except FileNotFoundError:
                    # The process exited while the directory was being read.
                    continue
                except OSError:
                    return None
                if raw:
                    command_lines.append(
                        raw.replace(b"\x00", b" ").decode(errors="replace")
                    )
            return command_lines or None

        # Fallback for Unix-like systems without /proc.
        try:
            result = subprocess.run(
                ["ps", "-eo", "args="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return lines or None

    @classmethod
    def _is_directory_referenced_by_browser(
        cls, directory: Path, process_command_lines: list[str]
    ) -> bool:
        target = str(directory).replace("\\", "/").rstrip("/").casefold()
        for command_line in process_command_lines:
            for match in cls._PLAYWRIGHT_DIR_ARG_RE.finditer(command_line):
                value = match.group("value").strip("\"'")
                normalized = value.replace("\\", "/").rstrip("/").casefold()
                if normalized == target:
                    return True
        return False

    async def stop(self):
        self.scheduler.remove_all_jobs()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info(f"[{self.JOBNAME}] 已停止")

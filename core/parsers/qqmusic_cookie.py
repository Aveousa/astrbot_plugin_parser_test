from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import copy
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import logger

from ..cookie import CookieJar

# QQ-only import tracking prevents an unchanged configuration from reverting a renewed key.


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class QQMusicCookieJar(CookieJar):
    """Import changed configuration, otherwise retain the last successfully saved cookies."""

    def __init__(self, config, parser_cfg, domain: str = "y.qq.com") -> None:
        self._configured_digest = _digest(
            self.clean_cookies_str(parser_cfg.cookies or "")
        )
        cookie_file = Path(config.cookie_dir) / f"{parser_cfg.name}_cookies.txt"
        self._import_record = cookie_file.with_suffix(".import.json")
        preserve_saved = False
        try:
            if (
                not cookie_file.is_symlink()
                and not self._import_record.is_symlink()
                and cookie_file.is_file()
                and self._import_record.stat().st_size <= 1024
            ):
                record = json.loads(self._import_record.read_text(encoding="utf-8"))
                preserve_saved = (
                    record.get("version") == 1
                    and record.get("configured") == self._configured_digest
                    and cookie_file.stat().st_size <= 262144
                    and hashlib.sha256(cookie_file.read_bytes()).hexdigest()
                    in (record.get("saved"), record.get("pending"))
                )
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        source = (
            SimpleNamespace(name=parser_cfg.name, cookies=None)
            if preserve_saved
            else parser_cfg
        )
        super().__init__(config, source, domain)
        try:
            self._saved_fingerprint = self._file_fingerprint()
        except (OSError, ValueError):
            self._saved_fingerprint = "unavailable"
        # Empty configurations load a saved login without calling save_to_file.
        if self.cookie_file.is_file() and not self.cookie_file.is_symlink():
            self._save_import_record()

    def file_unchanged(self) -> bool:
        """Do not overwrite credentials edited outside this live jar."""
        try:
            return self._file_fingerprint() == self._saved_fingerprint
        except (OSError, ValueError):
            return False

    def _file_fingerprint(self) -> str | None:
        if self.cookie_file.is_symlink():
            raise ValueError("symlink")
        if not self.cookie_file.exists():
            return None
        if self.cookie_file.stat().st_size > 262144:
            raise ValueError("size")
        return hashlib.sha256(self.cookie_file.read_bytes()).hexdigest()

    def save_to_file(self) -> None:
        """Commit a complete cookie file, leaving the previous file intact on failure."""
        if not getattr(self, "persistence_enabled", True):
            return
        if self.cookie_file.is_symlink():
            raise ValueError("symlink")
        fd, temporary = tempfile.mkstemp(
            prefix=".qq-cookie-save-", dir=self.cookie_file.parent
        )
        os.close(fd)
        try:
            candidate = copy(self)
            candidate.cookie_file = Path(temporary)
            CookieJar.save_to_file(candidate)
            with open(temporary, "rb+") as stream:
                candidate_bytes = stream.read(262145)
                if len(candidate_bytes) > 262144:
                    raise ValueError("size")
                fingerprint = hashlib.sha256(candidate_bytes).hexdigest()
                os.fsync(stream.fileno())
            previous = self._file_fingerprint()
            # Prepare both possible outcomes before the cookie commit point. A crash
            # or failed replace leaves either hash recoverable under unchanged config.
            self._save_import_record(saved=previous, pending=fingerprint, strict=True)
            if self.cookie_file.is_symlink():
                raise ValueError("symlink")
            os.replace(temporary, self.cookie_file)
            self._saved_fingerprint = fingerprint
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                logger.warning("[QQ 音乐] Cookie 临时文件清理失败")

    def _save_import_record(self, *, saved=None, pending=None, strict=False) -> None:
        temporary = None
        try:
            if self.cookie_file.is_symlink() or self._import_record.is_symlink():
                raise ValueError("symlink")
            record = {
                "version": 1,
                "configured": self._configured_digest,
                "saved": saved if strict else self._saved_fingerprint,
            }
            if pending is not None:
                record["pending"] = pending
            fd, temporary = tempfile.mkstemp(
                prefix=".qq-cookie-import-", dir=self.cookie_file.parent
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._import_record)
            temporary = None
        except (OSError, ValueError):
            if strict:
                raise
            logger.warning("[QQ 音乐] Cookie 导入记录保存失败；请检查数据目录写入权限")
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

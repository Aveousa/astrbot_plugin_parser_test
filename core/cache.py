"""Per-parse cache directory helpers.

The plugin shares one :class:`Downloader` and one renderer between all
platform parsers.  A context variable lets work scheduled during one parse
inherit that parse's directory without mutating the global ``PluginConfig``
or introducing races between concurrent messages.
"""

from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Iterator


_ACTIVE_CACHE_DIR: ContextVar[Path | None] = ContextVar(
    "parser_active_cache_dir", default=None
)
_CACHE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def get_active_cache_dir(default: Path | str) -> Path:
    """Return the current parse directory, or ``default`` outside a parse."""

    active = _ACTIVE_CACHE_DIR.get()
    return active if active is not None else Path(default)


def create_parse_cache_dir(cache_root: Path | str, label: str = "parse") -> Path:
    """Create and return an isolated directory for one parse operation.

    The timestamp and short random suffix make directories easy to inspect
    while remaining collision-safe when several messages arrive together.
    ``label`` is informational only and is strictly sanitized before it is
    used as a path component.
    """

    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    safe_label = _CACHE_LABEL_RE.sub("_", str(label or "parse")).strip("._-")
    safe_label = (safe_label or "parse")[:48]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = root / f"parse_{timestamp}_{safe_label}_{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=False, exist_ok=False)
    return directory


@contextmanager
def cache_dir_scope(directory: Path | str | None) -> Iterator[Path | None]:
    """Make ``directory`` available to parser/download tasks in this scope."""

    if directory is None:
        yield None
        return

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    token = _ACTIVE_CACHE_DIR.set(path)
    try:
        yield path
    finally:
        _ACTIVE_CACHE_DIR.reset(token)


def get_cache_size(cache_root: Path | str) -> tuple[int, int]:
    """Return ``(bytes, files)`` for the plugin cache without following links."""

    root = Path(cache_root)
    if not root.exists() or not root.is_dir() or root.is_symlink():
        return 0, 0
    total = 0
    files = 0
    try:
        for entry in root.rglob("*"):
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                total += entry.stat().st_size
                files += 1
            except OSError:
                continue
    except OSError:
        return total, files
    return total, files


def clear_cache_dir(cache_root: Path | str) -> tuple[int, int]:
    """Remove cache contents and recreate the configured cache directory.

    The root itself is preserved and symlinked children are unlinked rather
    than followed. Returns the number of removed files and bytes.
    """

    root = Path(cache_root)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("cache root is not a directory")
    root.mkdir(parents=True, exist_ok=True)
    removed_files = 0
    removed_bytes = 0
    for entry in tuple(root.iterdir()):
        if entry.is_symlink() or entry.is_file():
            try:
                if entry.is_file() and not entry.is_symlink():
                    removed_bytes += entry.stat().st_size
                entry.unlink()
                removed_files += 1
            except OSError:
                continue
        elif entry.is_dir():
            size, count = get_cache_size(entry)
            try:
                shutil.rmtree(entry)
                removed_files += count
                removed_bytes += size
            except OSError:
                continue
    return removed_files, removed_bytes

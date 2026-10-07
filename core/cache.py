"""Per-parse cache directory helpers.

The plugin shares one :class:`Downloader` and one renderer between all
platform parsers.  A context variable lets work scheduled during one parse
inherit that parse's directory without mutating the global ``PluginConfig``
or introducing races between concurrent messages.
"""

from __future__ import annotations

import re
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


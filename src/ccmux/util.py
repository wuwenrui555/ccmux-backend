"""Shared utility helpers.

Kept deliberately small: path resolution for the ccmux config dir,
crash-safe JSON writes, and a `window_bindings.json` lookup helper.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CCMUX_DIR_ENV = "CCMUX_DIR"


def ccmux_dir() -> Path:
    """Resolve the ccmux config directory.

    Returns Path from `CCMUX_DIR` env var, or `~/.ccmux` if not set.
    """
    raw = os.environ.get(CCMUX_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".ccmux"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temp file in the same directory, then renames it to the
    target path. `os.replace` is atomic on Linux, so the file is never
    in a half-written state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def has_session_map_entry(session_name: str) -> bool:
    """Check if `window_bindings.json` has a populated entry for this session."""
    from .config import config

    if not config.bindings_file.exists():
        return False
    try:
        session_map = json.loads(config.bindings_file.read_text())
        info = session_map.get(session_name, {})
        return bool(info.get("session_id"))
    except (json.JSONDecodeError, OSError):
        return False

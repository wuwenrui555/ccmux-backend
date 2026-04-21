"""Session map — persistent window_id <-> Claude session mapping.

Owns `window_bindings.json` (written by the `ccmux hook` CLI).
Provides lookup by window_id, session_name, or claude_session_id.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowBinding:
    """Claude-side view of a bound tmux window."""

    window_id: str
    session_name: str
    claude_session_id: str
    cwd: str


@dataclass
class ClaudeSession:
    """Summary of a Claude Code JSONL session file."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


class WindowBindings:
    """window_id <-> (session_id, cwd, session_name) persistent map.

    Backed by `window_bindings.json`. Read-only from ccmux's perspective
    (the hook CLI writes it). Reloaded each fast-loop tick via `load()`.
    """

    def __init__(self, map_file: Path | None = None) -> None:
        self._map_file = map_file if map_file is not None else config.instances_file
        self._data: dict[str, dict[str, str]] = {}
        self._read()

    def _read(self) -> None:
        self._data = {}
        if not self._map_file.exists():
            logger.info("window_bindings.json not found")
            return
        try:
            raw = json.loads(self._map_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load window_bindings.json: %s", e)
            return
        if isinstance(raw, dict):
            for sn, entry in raw.items():
                if isinstance(entry, dict):
                    self._data[sn] = entry

    async def load(self) -> None:
        """Reload from disk."""
        self._read()

    # -- lookups --------------------------------------------------------

    def get(self, window_id: str) -> WindowBinding | None:
        if not window_id:
            return None
        for name, entry in self._data.items():
            if entry.get("window_id") == window_id:
                return self._to_binding(name, entry)
        return None

    def get_by_session_name(self, session_name: str) -> WindowBinding | None:
        entry = self._data.get(session_name)
        if not entry:
            return None
        return self._to_binding(session_name, entry)

    def all(self) -> Iterator[WindowBinding]:
        for name, entry in list(self._data.items()):
            wid = entry.get("window_id", "")
            if wid:
                yield self._to_binding(name, entry)

    def is_session_in_map(self, session_name: str) -> bool:
        entry = self._data.get(session_name)
        return bool(entry and entry.get("window_id") and entry.get("session_id"))

    def find_by_claude_session_id(self, claude_session_id: str) -> WindowBinding | None:
        if not claude_session_id:
            return None
        for name, entry in self._data.items():
            if entry.get("session_id") == claude_session_id:
                return self._to_binding(name, entry)
        return None

    # -- raw access (for internal consumers) ----------------------------

    @property
    def raw(self) -> Mapping[str, dict[str, str]]:
        """Read-only view of session_name -> entry dict.

        Typed as Mapping (not dict) to prevent callers from mutating the
        internal state. Entries themselves are still mutable dicts but
        no consumer currently writes through them.
        """
        return self._data

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _to_binding(name: str, entry: dict[str, str]) -> WindowBinding:
        return WindowBinding(
            window_id=entry.get("window_id", ""),
            session_name=name,
            claude_session_id=entry.get("session_id", ""),
            cwd=entry.get("cwd", ""),
        )

    @staticmethod
    def encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming."""
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

"""Claude instance registry -- persistent ``instance_id -> window/session``
map backed by ``$CCMUX_DIR/claude_instances.json``.

A ``ClaudeInstance`` is one running Claude Code process in a tmux
window. The registry is the persisted record of every known instance;
it is written by the ``ccmux hook`` CLI on SessionStart and read by the
backend's poll loops.

Instance identity:

- ``instance_id`` -- stable key (the tmux session name chosen at bind
  time). Survives Claude resume, ``/clear``, and re-attach.
- ``window_id`` -- current tmux window id; changes when the backend
  auto-resumes a dead Claude session.
- ``session_id`` -- Claude's JSONL session UUID; changes on ``/clear``.
- ``cwd`` -- the launch directory; stable.
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
class ClaudeInstance:
    """Backend view of one running Claude Code process."""

    instance_id: str
    window_id: str
    session_id: str
    cwd: str


@dataclass
class ClaudeSession:
    """Summary of a Claude Code JSONL session file (unchanged from v1.x)."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


class ClaudeInstanceRegistry:
    """``instance_id -> ClaudeInstance`` persistent map.

    Backed by ``claude_instances.json``. Read-only from the backend's
    perspective (the hook CLI writes it). Reloaded each fast-loop tick
    via ``load()``.
    """

    def __init__(self, map_file: Path | None = None) -> None:
        self._map_file = map_file if map_file is not None else config.instances_file
        self._data: dict[str, dict[str, str]] = {}
        self._read()

    def _read(self) -> None:
        self._data = {}
        if not self._map_file.exists():
            logger.info("claude_instances.json not found")
            return
        try:
            raw = json.loads(self._map_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load claude_instances.json: %s", e)
            return
        if isinstance(raw, dict):
            for instance_id, entry in raw.items():
                if isinstance(entry, dict):
                    self._data[instance_id] = entry

    async def load(self) -> None:
        """Reload from disk."""
        self._read()

    # -- lookups --------------------------------------------------------

    def get(self, instance_id: str) -> ClaudeInstance | None:
        """Primary lookup by stable id."""
        entry = self._data.get(instance_id)
        if not entry:
            return None
        return self._to_instance(instance_id, entry)

    def get_by_window_id(self, window_id: str) -> ClaudeInstance | None:
        if not window_id:
            return None
        for instance_id, entry in self._data.items():
            if entry.get("window_id") == window_id:
                return self._to_instance(instance_id, entry)
        return None

    def find_by_session_id(self, session_id: str) -> ClaudeInstance | None:
        if not session_id:
            return None
        for instance_id, entry in self._data.items():
            if entry.get("session_id") == session_id:
                return self._to_instance(instance_id, entry)
        return None

    def contains(self, instance_id: str) -> bool:
        """True iff ``instance_id`` has both a window_id and a session_id."""
        entry = self._data.get(instance_id)
        return bool(entry and entry.get("window_id") and entry.get("session_id"))

    def all(self) -> Iterator[ClaudeInstance]:
        """Iterate only instances with a non-empty window_id."""
        for instance_id, entry in list(self._data.items()):
            wid = entry.get("window_id", "")
            if wid:
                yield self._to_instance(instance_id, entry)

    # -- raw access (for internal consumers) ----------------------------

    @property
    def raw(self) -> Mapping[str, dict[str, str]]:
        return self._data

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _to_instance(instance_id: str, entry: dict[str, str]) -> ClaudeInstance:
        return ClaudeInstance(
            instance_id=instance_id,
            window_id=entry.get("window_id", ""),
            session_id=entry.get("session_id", ""),
            cwd=entry.get("cwd", ""),
        )

    @staticmethod
    def encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming."""
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

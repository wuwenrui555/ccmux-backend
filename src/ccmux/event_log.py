"""Append-only event log: schema, writer, reader, compaction.

The hook writes one JSONL line per Claude Code lifecycle event
(SessionStart, UserPromptSubmit). Backend projects the log into
an in-memory ``dict[tmux_session_name, CurrentClaudeBinding]``.

Each line is one self-contained JSON object terminated by ``\\n`` and
kept under 4 KB so POSIX ``O_APPEND`` single-write atomicity holds
across concurrent hooks without explicit locking.

See ``docs/superpowers/specs/2026-04-28-event-log-self-heal-design.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TmuxInfo:
    session_id: str
    session_name: str
    window_id: str
    window_index: str
    window_name: str
    pane_id: str
    pane_index: str

    @classmethod
    def empty(cls) -> "TmuxInfo":
        return cls("", "", "", "", "", "", "")

    @classmethod
    def from_dict(cls, d: dict) -> "TmuxInfo":
        return cls(
            session_id=d.get("session_id", ""),
            session_name=d.get("session_name", ""),
            window_id=d.get("window_id", ""),
            window_index=d.get("window_index", ""),
            window_name=d.get("window_name", ""),
            pane_id=d.get("pane_id", ""),
            pane_index=d.get("pane_index", ""),
        )


@dataclass(frozen=True)
class ClaudeInfo:
    session_id: str
    transcript_path: str
    cwd: str
    permission_mode: str

    @classmethod
    def from_dict(cls, d: dict) -> "ClaudeInfo":
        return cls(
            session_id=d.get("session_id", ""),
            transcript_path=d.get("transcript_path", ""),
            cwd=d.get("cwd", ""),
            permission_mode=d.get("permission_mode", ""),
        )


@dataclass(frozen=True)
class HookEvent:
    timestamp: datetime
    hook_event: str
    tmux: TmuxInfo
    claude: ClaudeInfo

    def to_jsonl(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "hook_event": self.hook_event,
            "tmux": asdict(self.tmux),
            "claude": asdict(self.claude),
        }
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @classmethod
    def from_jsonl(cls, line: str) -> "HookEvent":
        d = json.loads(line)
        return cls(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            hook_event=d["hook_event"],
            tmux=TmuxInfo.from_dict(d.get("tmux", {})),
            claude=ClaudeInfo.from_dict(d.get("claude", {})),
        )


@dataclass(frozen=True)
class CurrentClaudeBinding:
    """Reader's projection: the most recent event for a tmux session."""

    tmux_session_name: str
    window_id: str
    claude_session_id: str
    cwd: str
    transcript_path: str
    last_seen: datetime


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


# PIPE_BUF on Linux/macOS is at least 4096 bytes. Single write() of <= PIPE_BUF
# is guaranteed atomic by POSIX, even with O_APPEND from concurrent processes.
_PIPE_BUF_SAFE_LIMIT = 4096


class EventLogWriter:
    """Atomic single-line appender.

    Writes each ``HookEvent`` as one ``O_APPEND`` ``write()`` syscall on a
    regular file. Lines under PIPE_BUF (~4 KB) interleave atomically across
    concurrent hooks without explicit locking.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: HookEvent) -> None:
        line = event.to_jsonl()
        encoded = line.encode("utf-8")
        if len(encoded) >= _PIPE_BUF_SAFE_LIMIT:
            raise ValueError(
                f"event line exceeds PIPE_BUF ({len(encoded)} >= "
                f"{_PIPE_BUF_SAFE_LIMIT}); concurrent appends would tear"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND lets the kernel ensure each write goes to EOF; a single
        # write() of the full line is the atomic unit.
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class EventLogReader:
    """Tail the event log and project to ``dict[tmux_session_name, binding]``.

    Last-event-wins per tmux_session_name. Out-of-tmux events
    (empty ``tmux.session_name``) are skipped. Malformed lines are
    logged and skipped without raising.
    """

    def __init__(self, path: Path, poll_interval: float = 0.5) -> None:
        self._path = path
        self._offset = 0
        self._current: dict[str, CurrentClaudeBinding] = {}
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stopping = False

    # -- async lifecycle ----------------------------------------------------

    async def start(self) -> None:
        """Initial full read + spawn the poll task."""
        self.refresh()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                self.refresh()
            except Exception:
                logger.exception("event_log: poll iteration failed")
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    # -- sync read + project ------------------------------------------------

    def refresh(self) -> None:
        """Read any new bytes since last refresh and update the projection."""
        if not self._path.exists():
            return
        size = self._path.stat().st_size
        if size <= self._offset:
            # Truncation or no growth. We deliberately do NOT re-read on
            # truncate (compaction rewrites the file via rename, which the
            # OS treats as a new inode; readers tied to the old fd would
            # miss it anyway, and tests cover the rename-compact path).
            return
        with self._path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        # Only consume up through the last newline; partial trailing line
        # waits for the next refresh.
        last_nl = text.rfind("\n")
        if last_nl < 0:
            return
        consumable = text[: last_nl + 1]
        self._offset += len(consumable.encode("utf-8"))
        for line in consumable.splitlines():
            self._project_line(line)

    def get(self, tmux_session_name: str) -> CurrentClaudeBinding | None:
        return self._current.get(tmux_session_name)

    def all_alive(self) -> list[CurrentClaudeBinding]:
        return list(self._current.values())

    def _project_line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            ev = HookEvent.from_jsonl(line + "\n")
        except (ValueError, KeyError) as exc:
            logger.warning("event_log: skipping malformed line: %s", exc)
            return
        if not ev.tmux.session_name:
            return  # out-of-tmux event; not routable
        self._current[ev.tmux.session_name] = CurrentClaudeBinding(
            tmux_session_name=ev.tmux.session_name,
            window_id=ev.tmux.window_id,
            claude_session_id=ev.claude.session_id,
            cwd=ev.claude.cwd,
            transcript_path=ev.claude.transcript_path,
            last_seen=ev.timestamp,
        )


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def compact(path: Path) -> tuple[int, int]:
    """Rewrite the log keeping only the latest event per ``tmux_session_name``.

    Returns ``(lines_before, lines_after)``. Writes a temp file and renames
    into place atomically. Missing or empty source files are no-ops returning
    ``(0, 0)``.
    """
    if not path.exists():
        return (0, 0)
    by_name: dict[str, HookEvent] = {}
    n_before = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            n_before += 1
            try:
                ev = HookEvent.from_jsonl(line)
            except (ValueError, KeyError):
                continue
            if not ev.tmux.session_name:
                continue
            existing = by_name.get(ev.tmux.session_name)
            if existing is None or ev.timestamp >= existing.timestamp:
                by_name[ev.tmux.session_name] = ev
    tmp = path.with_suffix(path.suffix + ".compact.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for ev in sorted(by_name.values(), key=lambda e: e.timestamp):
            f.write(ev.to_jsonl())
    tmp.replace(path)
    return (n_before, len(by_name))

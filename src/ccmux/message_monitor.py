"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Reads active Claude session IDs from the WindowBindings.
  2. Reads new JSONL lines from each session file using byte-offset tracking.
  3. Parses entries via TranscriptParser and emits ClaudeMessage objects to a callback.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: MessageMonitor, TrackedClaudeSession, MonitorState.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles

from .config import config
from .claude_transcript_parser import ClaudeMessage, TranscriptParser
from .util import atomic_write_json

if TYPE_CHECKING:
    from .window_bindings import WindowBindings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


@dataclass
class TrackedClaudeSession:
    """State for a tracked Claude Code session.

    Attributes
    ----------
    session_id : str
        The Claude Code session UUID.
    file_path : str
        Path to the session's JSONL transcript file.
    last_byte_offset : int
        Byte position up to which the file has been read.
        Used for incremental reading to avoid re-processing.
    """

    session_id: str
    file_path: Path
    last_byte_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization.

        Returns
        -------
        dict[str, Any]
            Dictionary with keys `session_id`, `file_path`,
            `last_byte_offset`.
        """
        d = asdict(self)
        d["file_path"] = str(d["file_path"])
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackedClaudeSession":
        """Create a TrackedClaudeSession from a dictionary.

        Parameters
        ----------
        data : dict[str, Any]
            Dictionary with keys `session_id`, `file_path`, and
            optionally `last_byte_offset` (defaults to 0).

        Returns
        -------
        TrackedClaudeSession
            Reconstructed instance.
        """
        return cls(
            session_id=data.get("session_id", ""),
            file_path=Path(data.get("file_path", "")),
            last_byte_offset=data.get("last_byte_offset", 0),
        )


@dataclass
class MonitorState:
    """Persistent state for the session monitor.

    Stores tracking information for all monitored sessions
    to prevent duplicate notifications after restarts.

    Attributes
    ----------
    state_file : Path
        Path to the JSON file used for persistence
        (typically `~/.ccmux/claude_monitor.json`).
    tracked_sessions : dict[str, TrackedClaudeSession]
        Mapping of session_id to its tracked state.
    _dirty : bool
        Whether in-memory state has diverged from the persisted file.
        Set to `True` on any mutation, reset on `save`.
    """

    state_file: Path
    tracked_sessions: dict[str, TrackedClaudeSession] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)

    def load(self) -> None:
        """Load tracked sessions from the state file.

        Reads `state_file` and populates `tracked_sessions`.
        If the file is missing, corrupt, or unreadable, the session
        dict is left empty and a warning is logged.
        """
        if not self.state_file.exists():
            logger.debug("State file does not exist: %s", self.state_file)
            return

        try:
            data = json.loads(self.state_file.read_text())
            sessions = data.get("tracked_sessions", {})
            self.tracked_sessions = {
                k: TrackedClaudeSession.from_dict(v) for k, v in sessions.items()
            }
            logger.info(
                f"Loaded {len(self.tracked_sessions)} tracked sessions from state"
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to load state file: %s", e)
            self.tracked_sessions = {}

    def save(self) -> None:
        """Persist tracked sessions to the state file atomically.

        Uses `atomic_write_json` so a crash mid-write
        won't corrupt existing state. Resets the dirty flag on success.
        """
        data = {
            "tracked_sessions": {
                k: v.to_dict() for k, v in self.tracked_sessions.items()
            }
        }

        try:
            atomic_write_json(self.state_file, data)
            self._dirty = False
            logger.debug(
                "Saved %d tracked sessions to state", len(self.tracked_sessions)
            )
        except OSError as e:
            logger.error("Failed to save state file: %s", e)

    def get_session(self, session_id: str) -> TrackedClaudeSession | None:
        """Look up a tracked session by its ID.

        Parameters
        ----------
        session_id : str
            The Claude Code session UUID.

        Returns
        -------
        TrackedClaudeSession or None
            The tracked session, or `None` if not found.
        """
        return self.tracked_sessions.get(session_id)

    def update_session(self, session: TrackedClaudeSession) -> None:
        """Add or update a tracked session and mark state as dirty.

        Parameters
        ----------
        session : TrackedClaudeSession
            The session to upsert, keyed by `session.session_id`.
        """
        self.tracked_sessions[session.session_id] = session
        self._dirty = True

    def remove_session(self, session_id: str) -> None:
        """Remove a tracked session by ID.

        No-op if the session is not currently tracked.

        Parameters
        ----------
        session_id : str
            The Claude Code session UUID to remove.
        """
        if session_id in self.tracked_sessions:
            del self.tracked_sessions[session_id]
            self._dirty = True

    def save_if_dirty(self) -> None:
        """Save state only if it has been modified since the last save."""
        if self._dirty:
            self.save()


# ---------------------------------------------------------------------------
# Session monitor
# ---------------------------------------------------------------------------


class MessageMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Provides a `poll` method that performs a single scan cycle: detects
    window_bindings changes, reads new JSONL content, and returns ClaudeMessage
    objects. `DefaultBackend` drives the polling loop; standalone callers
    may also invoke `poll` directly.

    Parameters
    ----------
    projects_path : Path or None
        Root directory containing Claude Code project folders.
        Defaults to `config.claude_projects_path`.
    state_file : Path or None
        Path to the monitor state JSON file. Defaults to
        `config.monitor_state_file`.

    Attributes
    ----------
    projects_path : Path
        Root directory containing Claude Code project folders.
    state : MonitorState
        Persistent byte-offset state for all tracked sessions.
    _pending_tools : dict[str, dict[str, Any]]
        Per-session pending tool_use state carried across poll cycles.
    _last_cmd_names : dict[str, str | None]
        Per-session trailing slash-command name carried across poll
        cycles, so a `local_command_invoke` in one poll can pair with
        its `<local-command-stdout>` in the next.
    _file_mtimes : dict[str, float]
        In-memory mtime cache per session_id; skips reads for
        unchanged files.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        state_file: Path | None = None,
        window_bindings: "WindowBindings | None" = None,
        show_user_messages: bool | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        # Injected window registry — the authoritative source of active
        # Claude sessions. Left optional for test convenience; callers in
        # production (DefaultBackend) always pass one.
        self._window_bindings = window_bindings

        # Controls whether user-typed messages are emitted. Default falls
        # back to the env-driven `config.show_user_messages` so existing
        # deployments keep working; pass explicitly to override.
        self._show_user_messages = (
            config.show_user_messages
            if show_user_messages is None
            else show_user_messages
        )

        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._last_cmd_names: dict[str, str | None] = {}
        self._file_mtimes: dict[str, float] = {}

    async def scan_claude_projects(self) -> list[TrackedClaudeSession]:
        """Scan all JSONL session files across project directories.

        Returns all discoverable sessions; filtering by active session IDs
        is done in `check_for_updates`. This avoids cwd-matching issues
        where pane cwd diverges from the original Claude Code project path.

        Returns
        -------
        list[TrackedClaudeSession]
            All discoverable sessions under `projects_path`.
        """
        sessions = []

        if not self.projects_path.exists():
            return sessions

        for project_dir in self.projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    sessions.append(
                        TrackedClaudeSession(
                            session_id=jsonl_file.stem,
                            file_path=jsonl_file,
                        )
                    )
            except OSError as e:
                logger.debug("Error scanning jsonl files in %s: %s", project_dir, e)

        return sessions

    async def _read_new_lines(self, session: TrackedClaudeSession) -> list[dict]:
        """Read new JSONL lines from a session file since last byte offset.

        Detects file truncation (e.g. after /clear) and resets offset.
        Recovers from corrupted offsets (mid-line) by scanning to next line.
        Partial writes (incomplete JSON) stop reading; retry next cycle.

        Parameters
        ----------
        session : TrackedClaudeSession
            Tracked state with the current byte offset (mutated in place).

        Returns
        -------
        list[dict]
            Parsed JSONL entries since the last offset.
        """
        new_entries = []
        try:
            async with aiofiles.open(session.file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, reset
                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s "
                        "(offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), "
                            "scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        session.last_byte_offset = await f.tell()
                        return []
                    await f.seek(session.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: only advance past lines that parsed
                # successfully. A non-empty line that fails JSON parsing is
                # likely a partial write; stop and retry next cycle.
                safe_offset = session.last_byte_offset
                async for line in f:
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = await f.tell()
                    elif line.strip():
                        # Partial JSONL line — don't advance offset past it
                        logger.warning(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = await f.tell()

                session.last_byte_offset = safe_offset

        except OSError as e:
            logger.error("Error reading session file %s: %s", session.file_path, e)
        return new_entries

    async def check_for_updates(
        self, active_session_ids: set[str]
    ) -> list[ClaudeMessage]:
        """Check all active sessions for new messages.

        Scans project directories, filters to active sessions, reads new
        JSONL entries from the byte offset, parses them, and returns
        `ClaudeMessage` objects. Saves state after processing.

        Parameters
        ----------
        active_session_ids : set[str]
            Session IDs currently present in window_bindings.

        Returns
        -------
        list[ClaudeMessage]
            Newly detected messages across all active sessions.
        """
        new_messages: list[ClaudeMessage] = []

        # Scan projects to get available session files
        sessions = await self.scan_claude_projects()

        # Only process sessions that are in window_bindings
        for session_info in sessions:
            if session_info.session_id not in active_session_ids:
                continue
            try:
                tracked = self.state.get_session(session_info.session_id)

                if tracked is None:
                    # For new sessions, initialize offset to end of file
                    # to avoid re-processing old messages
                    try:
                        file_size = session_info.file_path.stat().st_size
                        current_mtime = session_info.file_path.stat().st_mtime
                    except OSError:
                        file_size = 0
                        current_mtime = 0.0
                    tracked = TrackedClaudeSession(
                        session_id=session_info.session_id,
                        file_path=session_info.file_path,
                        last_byte_offset=file_size,
                    )
                    self.state.update_session(tracked)
                    self._file_mtimes[session_info.session_id] = current_mtime
                    logger.info("Started tracking session: %s", session_info.session_id)
                    continue

                # Check mtime + file size to see if file has changed
                try:
                    st = session_info.file_path.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                last_mtime = self._file_mtimes.get(session_info.session_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    # File hasn't changed, skip reading
                    continue

                # File changed, read new content from last offset
                new_entries = await self._read_new_lines(tracked)
                self._file_mtimes[session_info.session_id] = current_mtime

                if new_entries:
                    logger.debug(
                        f"Read {len(new_entries)} new entries for "
                        f"session {session_info.session_id}"
                    )

                # Parse new entries using the shared logic, carrying over pending tools
                # and the trailing slash-command name across poll cycles.
                sid = session_info.session_id
                carry = self._pending_tools.get(sid, {})
                carry_cmd = self._last_cmd_names.get(sid)
                parsed_entries, remaining, trailing_cmd = (
                    TranscriptParser.parse_entries(
                        new_entries,
                        session_id=sid,
                        pending_tools=carry,
                        last_cmd_name=carry_cmd,
                    )
                )
                if remaining:
                    self._pending_tools[sid] = remaining
                else:
                    self._pending_tools.pop(sid, None)
                if trailing_cmd is not None:
                    self._last_cmd_names[sid] = trailing_cmd
                else:
                    self._last_cmd_names.pop(sid, None)

                for entry in parsed_entries:
                    if not entry.text and not entry.image_data:
                        continue
                    # Skip user messages unless show_user_messages is enabled
                    if entry.role == "user" and not self._show_user_messages:
                        continue
                    entry.is_complete = True
                    new_messages.append(entry)

                self.state.update_session(tracked)

            except OSError as e:
                logger.debug(
                    "Error processing session %s: %s", session_info.session_id, e
                )

        self.state.save_if_dirty()
        return new_messages

    def startup_cleanup(self) -> None:
        """One-time cleanup on bot startup.

        Removes tracked sessions not present in the current
        `window_bindings.json` (cleans up leftover state from sessions
        that no longer exist). No-op when no WindowBindings was injected
        (test fixtures).
        """
        if self._window_bindings is None:
            return

        active_session_ids = {
            entry["session_id"]
            for entry in self._window_bindings.raw.values()
            if entry.get("session_id")
        }
        stale = [
            sid
            for sid in self.state.tracked_sessions.keys()
            if sid not in active_session_ids
        ]
        if stale:
            logger.info("[Startup] Removing %d stale tracked sessions", len(stale))
            for sid in stale:
                self.state.remove_session(sid)
                self._file_mtimes.pop(sid, None)
            self.state.save_if_dirty()

    async def poll(self) -> list[ClaudeMessage]:
        """Perform a single scan cycle.

        Reads active Claude session IDs from the WindowBindings (all bindings,
        not just alive — monitor is resilient to temporarily dead bindings).
        Returns ClaudeMessage objects for any new JSONL content.

        Returns
        -------
        list[ClaudeMessage]
            Newly detected messages across all active sessions.
        """
        # Poll every Claude session known to the registry. Filtering by
        # topic-level liveness is a frontend concern; the monitor just
        # reads JSONL, which is cheap for dead sessions (mtime cache skip).
        if self._window_bindings is None:
            return []
        active_session_ids = {
            window.claude_session_id
            for window in self._window_bindings.all()
            if window.claude_session_id
        }

        new_messages = await self.check_for_updates(active_session_ids)

        for msg in new_messages:
            status = "complete" if msg.is_complete else "streaming"
            preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
            logger.info("[%s] session=%s: %s", status, msg.session_id, preview)

        return new_messages

    def shutdown(self) -> None:
        """Persist state on shutdown."""
        self.state.save()
        logger.info("Message monitor state saved")

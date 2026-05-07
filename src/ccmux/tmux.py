"""Tmux session/window management via libtmux.

Provides TmuxSession (per-session operations) and TmuxSessionRegistry
(multi-session routing). The global `registry` instance replaces the
former `tmux_manager` singleton.

Key classes:
  - TmuxSession: async wrappers for libtmux operations on a single session.
  - TmuxSessionRegistry: manages multiple TmuxManagers, routes by window_id.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import libtmux
import libtmux.exc
from libtmux._internal.query_list import ObjectDoesNotExist as _ObjectDoesNotExist

from .config import SENSITIVE_ENV_VARS, config

logger = logging.getLogger(__name__)

# Tuple used by libtmux call sites: libtmux's own errors, ObjectDoesNotExist
# (raised by `.sessions.get(...)` on misses and NOT a LibTmuxException
# subclass), and OSError for cases where libtmux shells out to the tmux
# binary (subprocess/IPC failures).
_TMUX_ERRORS: tuple[type[BaseException], ...] = (
    libtmux.exc.LibTmuxException,
    _ObjectDoesNotExist,
    OSError,
)


@dataclass
class TmuxWindow:
    """Information about a tmux window.

    Identity is `window_id` (e.g. `@5`); the tmux-level window name is
    not tracked — one Claude session per tmux window is the convention,
    so the name carries no routing information.
    """

    window_id: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane


def _exit_pane_mode_if_active(pane: libtmux.Pane) -> None:
    """Exit any active tmux pane mode so subsequent send-keys reach the shell.

    When a pane is in copy-mode, view-mode, choose-mode (tree / buffer /
    client / window), or clock-mode, keystrokes are interpreted as mode
    commands rather than input. Sending `hello` while the pane is in copy
    mode triggers `h` (cursor left), `e`, `l`, `l`, `o` against the copy
    buffer instead of typing into Claude Code's input box.

    `pane_in_mode` reports 1 for any of those modes; `send-keys -X cancel`
    is tmux's universal mode-exit action and a no-op outside of any mode.
    Best effort — failures are logged at debug and the caller still
    proceeds with its send.
    """
    try:
        result = pane.cmd("display-message", "-p", "#{pane_in_mode}")
        in_mode = bool(result.stdout) and result.stdout[0].strip() == "1"
        if in_mode:
            pane.cmd("send-keys", "-X", "cancel")
    except _TMUX_ERRORS as e:
        logger.debug("pane_in_mode probe failed for %s: %s", pane, e)


def sanitize_session_name(name: str, existing_names: set[str]) -> str:
    """Sanitize a string for use as a tmux session name.

    tmux forbids: dot (.), colon (:), newline, null byte.
    Replaces illegal chars with '-', truncates to 50 chars,
    appends numeric suffix if name collides with existing sessions.
    """
    sanitized = ""
    for ch in name:
        if ch in ".:\n\0":
            sanitized += "-"
        else:
            sanitized += ch
    sanitized = sanitized.strip("-")[:50]

    if not sanitized:
        sanitized = "session"

    if sanitized not in existing_names:
        return sanitized
    counter = 2
    while f"{sanitized}-{counter}" in existing_names:
        counter += 1
    return f"{sanitized}-{counter}"


class TmuxSession:
    """Manages tmux windows for Claude Code sessions."""

    def __init__(
        self,
        session_name: str | None = None,
        *,
        server: libtmux.Server | None = None,
    ):
        """Initialize tmux manager.

        Parameters
        ----------
        session_name : str or None
            Name of the tmux session to use (default from config).
        server : libtmux.Server or None
            Shared libtmux.Server instance. Pass one from TmuxSessionRegistry
            to reuse a single connection across all sessions; leave None for
            a standalone instance that lazily builds its own.
        """
        # Explicit None check so an empty string from a confused caller is
        # preserved (and then rejected by tmux with BadSessionName) rather
        # than silently promoted to the configured default, which would
        # write windows into the wrong session.
        self.session_name = (
            session_name if session_name is not None else config.tmux_session_name
        )
        self._server: libtmux.Server | None = server

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except _TMUX_ERRORS:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        # Create new session. Callers that want a specific initial window name
        # should use `create_session` directly, which passes window_name through
        # to `new_session` so no placeholder is ever created.
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets the frontend has registered in
        `SENSITIVE_ENV_VARS` (e.g. bot tokens, API keys). The backend
        itself registers none; frontends append as needed.
        """
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except _TMUX_ERRORS:
                pass  # var not set in session env — nothing to remove

    async def list_windows(self) -> list[TmuxWindow]:
        """List bindable windows in the session.

        Returns an empty list for the reserved bot session
        (`config.tmux_session_name`) so it can never be picked as a
        Claude-binding target.
        """

        def _sync_list_windows() -> list[TmuxWindow]:
            windows: list[TmuxWindow] = []
            if self.session_name == config.tmux_session_name:
                return windows

            session = self.get_session()
            if not session:
                return windows

            for window in session.windows:
                try:
                    pane = window.active_pane
                    if pane:
                        cwd = pane.pane_current_path or ""
                        pane_cmd = pane.pane_current_command or ""
                    else:
                        cwd = ""
                        pane_cmd = ""

                    windows.append(
                        TmuxWindow(
                            window_id=window.window_id or "",
                            cwd=cwd,
                            pane_current_command=pane_cmd,
                        )
                    )
                except _TMUX_ERRORS as e:
                    logger.debug("Error getting window info: %s", e)

            return windows

        return await asyncio.to_thread(_sync_list_windows)

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Parameters
        ----------
        window_id : str
            The tmux window ID to match.

        Returns
        -------
        TmuxWindow or None
            Matching window, or None if not found.
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def active_pane_id(self, window_id: str) -> str:
        """Return the `%N` pane id of the active pane in `window_id`.

        Empty string if the session or window doesn't exist.
        """

        def _sync() -> str:
            session = self.get_session()
            if not session:
                return ""
            for window in session.windows:
                if (window.window_id or "") == window_id:
                    pane = window.active_pane
                    return (pane.pane_id or "") if pane else ""
            return ""

        return await asyncio.to_thread(_sync)

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a window's active pane.

        Parameters
        ----------
        window_id : str
            The window ID to capture.
        with_ansi : bool
            If True, capture with ANSI color codes.

        Returns
        -------
        str or None
            The captured text, or None on failure.

        Notes
        -----
        Default ``tmux capture-pane`` returns the live tail of the pane
        buffer, not the user's scrolled-to position in copy mode
        (verified on tmux 3.5).
        """
        if with_ansi:
            # Use async subprocess to call tmux capture-pane -e for ANSI colors
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    "capture-pane",
                    "-e",
                    "-p",
                    "-t",
                    window_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout.decode("utf-8")
                logger.error(
                    "Failed to capture pane %s: %s", window_id, stderr.decode("utf-8")
                )
                return None
            except _TMUX_ERRORS as e:
                logger.error("Unexpected error capturing pane %s: %s", window_id, e)
                return None

        # Original implementation for plain text - wrap in thread
        def _sync_capture() -> str | None:
            session = self.get_session()
            if not session:
                return None
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return None
                pane = window.active_pane
                if not pane:
                    return None
                lines = pane.capture_pane()
                return "\n".join(lines) if isinstance(lines, list) else str(lines)
            except _TMUX_ERRORS as e:
                logger.error("Failed to capture pane %s: %s", window_id, e)
                return None

        return await asyncio.to_thread(_sync_capture)

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Parameters
        ----------
        window_id : str
            The window ID to send to.
        text : str
            Text to send.
        enter : bool
            Whether to press enter after the text.
        literal : bool
            If True, send text literally. If False, interpret special keys
            like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Claude Code's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  A 500ms gap lets the TUI process the
            # text before receiving Enter.
            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error("Window %s not found", window_id)
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error("No active pane in window %s", window_id)
                        return False
                    # Exit copy/view/choose/clock mode so the keystrokes
                    # below land in the shell input rather than being
                    # interpreted as vim-style mode commands.
                    _exit_pane_mode_if_active(pane)
                    # Bypass libtmux's send_keys wrapper: it invokes
                    # `tmux send-keys -l <text>` without a `--` separator,
                    # so a leading "-" in `chars` (e.g. a markdown bullet
                    # "- foo") is consumed by tmux's argument parser as a
                    # flag and the whole command errors out.
                    pane.cmd("send-keys", "-l", "--", chars)
                    return True
                except _TMUX_ERRORS as e:
                    logger.error("Failed to send keys to window %s: %s", window_id, e)
                    return False

            def _send_enter() -> bool:
                session = self.get_session()
                if not session:
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        return False
                    pane = window.active_pane
                    if not pane:
                        return False
                    pane.send_keys("", enter=True, literal=False)
                    return True
                except _TMUX_ERRORS as e:
                    logger.error("Failed to send Enter to window %s: %s", window_id, e)
                    return False

            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                if not await asyncio.to_thread(_send_literal, text):
                    return False
            await asyncio.sleep(0.5)
            return await asyncio.to_thread(_send_enter)

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error("Window %s not found", window_id)
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error("No active pane in window %s", window_id)
                    return False

                # Exit any active pane mode (copy / view / choose / clock)
                # so the keystrokes below reach the shell input rather
                # than being interpreted as mode commands.
                _exit_pane_mode_if_active(pane)

                if literal:
                    # See note at the literal+enter call site: libtmux's
                    # send_keys omits "--", so leading "-" in user text
                    # is eaten as a flag. Bypass it. The outer
                    # `if literal and enter:` branch handles enter=True;
                    # this branch is reached only with enter=False.
                    pane.cmd("send-keys", "-l", "--", text)
                else:
                    pane.send_keys(text, enter=enter, literal=literal)
                return True

            except _TMUX_ERRORS as e:
                logger.error("Failed to send keys to window %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except _TMUX_ERRORS as e:
                logger.error("Failed to rename window %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_sync_rename)

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID."""

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except _TMUX_ERRORS as e:
                logger.error("Failed to kill window %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_sync_kill)

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start Claude Code.

        Parameters
        ----------
        work_dir : str
            Working directory for the new window.
        window_name : str or None
            Optional window name (defaults to directory name).
        start_claude : bool
            Whether to start the claude command.
        resume_session_id : str or None
            If set, append ``--resume <id>`` to the claude command.

        Returns
        -------
        tuple[bool, str, str, str]
            (success, message, window_name, window_id).
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Create window name, adding suffix if name already exists
        final_window_name = window_name if window_name else path.name

        # Uniqueness check against tmux directly (window_name isn't part
        # of our data model; we read libtmux on demand).
        def _name_taken(name: str) -> bool:
            session = self.get_session()
            if not session:
                return False
            return any(w.window_name == name for w in session.windows)

        base_name = final_window_name
        counter = 2
        while await asyncio.to_thread(_name_taken, final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""

                # Prevent Claude Code from overriding window name
                window.set_option("allow-rename", "off")

                # Start Claude Code if requested
                if start_claude:
                    pane = window.active_pane
                    if pane:
                        cmd = config.claude_command
                        if resume_session_id:
                            cmd = f"{cmd} --resume {resume_session_id}"
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except _TMUX_ERRORS as e:
                logger.error("Failed to create window: %s", e)
                return False, f"Failed to create window: {e}", "", ""

        return await asyncio.to_thread(_create_and_start)

    async def create_session(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        skip_permissions: bool = False,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux session with a single Claude Code window.

        The session's first (and only) window is created directly with the
        requested name — no placeholder window is ever created.

        Returns
        -------
        tuple[bool, str, str, str]
            (success, message, window_name, window_id).
        """
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        final_window_name = window_name if window_name else path.name

        def _create() -> tuple[bool, str, str, str]:
            try:
                session = self.server.new_session(
                    session_name=self.session_name,
                    window_name=final_window_name,
                    start_directory=str(path),
                )
                self._scrub_session_env(session)

                window = session.windows[0]
                wid = window.window_id or ""
                window.set_option("allow-rename", "off")

                if start_claude:
                    pane = window.active_pane
                    if pane:
                        cmd = config.claude_command
                        if resume_session_id:
                            cmd = f"{cmd} --resume {resume_session_id}"
                        if skip_permissions:
                            cmd = f"{cmd} --dangerously-skip-permissions"
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created session '%s' with window '%s' (id=%s) at %s",
                    self.session_name,
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created session '{self.session_name}'",
                    final_window_name,
                    wid,
                )
            except _TMUX_ERRORS as e:
                logger.error("Failed to create session '%s': %s", self.session_name, e)
                return False, f"Failed to create session: {e}", "", ""

        return await asyncio.to_thread(_create)


class TmuxSessionRegistry:
    """Registry of TmuxSession instances, one per tmux session.

    Maintains a shared libtmux.Server and a reverse mapping from
    window_id to session_name for fast lookup.
    """

    def __init__(self) -> None:
        self._managers: dict[str, TmuxSession] = {}
        self._server: libtmux.Server | None = None
        self._window_to_session: dict[str, str] = {}

    @property
    def server(self) -> libtmux.Server:
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_or_create(self, session_name: str) -> TmuxSession:
        """Get or create a TmuxSession for the given session name."""
        if session_name not in self._managers:
            tm = TmuxSession(session_name=session_name, server=self.server)
            self._managers[session_name] = tm
            logger.info("Registered TmuxSession for session '%s'", session_name)
        return self._managers[session_name]

    def remove(self, session_name: str) -> None:
        """Remove a TmuxSession from the registry (does NOT kill the tmux session)."""
        if session_name in self._managers:
            self._window_to_session = {
                wid: sn
                for wid, sn in self._window_to_session.items()
                if sn != session_name
            }
            del self._managers[session_name]
            logger.info("Removed TmuxSession for session '%s'", session_name)

    def registered_session_names(self) -> set[str]:
        """Return all registered session names."""
        return set(self._managers.keys())

    def get_by_window_id(self, window_id: str) -> TmuxSession | None:
        """Look up the TmuxSession that owns a window ID.

        Uses a cached reverse map. On cache miss, rebuilds by querying
        all registered TmuxManagers synchronously.
        """
        session_name = self._window_to_session.get(window_id)
        if session_name and session_name in self._managers:
            return self._managers[session_name]

        self._rebuild_window_map()
        session_name = self._window_to_session.get(window_id)
        if session_name and session_name in self._managers:
            return self._managers[session_name]

        return None

    def _rebuild_window_map(self) -> None:
        """Rebuild the window_id -> session_name reverse map."""
        new_map: dict[str, str] = {}
        for session_name, tm in self._managers.items():
            session = tm.get_session()
            if not session:
                continue
            for window in session.windows:
                wid = window.window_id
                if wid:
                    new_map[wid] = session_name
        self._window_to_session = new_map
        logger.debug(
            "Rebuilt window map: %d windows across %d sessions",
            len(new_map),
            len(self._managers),
        )

    def update_window_map(self, window_id: str, session_name: str) -> None:
        """Register a window_id -> session_name mapping."""
        self._window_to_session[window_id] = session_name

    async def list_all_windows(self) -> list[TmuxWindow]:
        """Aggregate list_windows() from all registered TmuxManagers."""
        all_windows: list[TmuxWindow] = []
        for session_name, tm in list(self._managers.items()):
            try:
                windows = await tm.list_windows()
                all_windows.extend(windows)
            except _TMUX_ERRORS as e:
                logger.debug(
                    "list_windows failed for session '%s': %s", session_name, e
                )
        return all_windows

    def all_server_session_names(self) -> set[str]:
        """Return names of ALL tmux sessions on the server."""
        try:
            return {s.session_name for s in self.server.sessions if s.session_name}
        except _TMUX_ERRORS:
            return set()

    def list_unbound_sessions(self, bound_session_names: set[str]) -> list[str]:
        """List tmux sessions on the server not bound to any topic."""
        try:
            all_sessions = self.server.sessions
        except _TMUX_ERRORS:
            return []
        return sorted(
            s.session_name
            for s in all_sessions
            if s.session_name and s.session_name not in bound_session_names
        )


# Global registry instance (replaces old `tmux_manager` singleton)
tmux_registry = TmuxSessionRegistry()

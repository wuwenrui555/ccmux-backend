"""Backend — the single Protocol any frontend drives.

The contract is split into two sub-Protocols by domain:

- `TmuxOps` — everything that touches tmux (send_text, send_keys,
  capture_pane, create_window, list_windows).
- `ClaudeOps` — everything that touches Claude Code's JSONL side
  (list_sessions, get_history).

Cross-domain queries (`is_alive`, `get_window_binding`) and the lifecycle
methods (`start`, `stop`) live on the top-level `Backend`.

The smart-backend semantics: `tmux.send_text` is idempotent wrt Claude
liveness (auto-resumes dead Claude before sending on the slow loop
cycle); `start()` spawns the fast/slow poll loops internally and pushes
every event to `on_message` / `on_status`.

Module-level default singleton (`get_default_backend` /
`set_default_backend`) lets callers that cannot easily thread a backend
handle through (e.g. Telegram handlers that receive only `Update` +
`Context`) reach the backend without bot_data dict lookups. Tests can
swap the singleton by calling `set_default_backend` with a fake.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .config import config
from .message_monitor import MessageMonitor
from .status_monitor import StatusMonitor, WindowStatus
from .claude_files import ClaudeFileResolver
from .liveness import LivenessChecker
from .window_bindings import ClaudeSession, WindowBindings, WindowBinding
from .tmux import TmuxSessionRegistry, TmuxWindow
from .claude_transcript_parser import ClaudeMessage

logger = logging.getLogger(__name__)


class TmuxOps(Protocol):
    """Tmux-side of the Backend contract.

    All methods are idempotent and internally handle tmux liveness.
    """

    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to the window's pane. Returns (success, message)."""
        ...

    async def send_keys(self, window_id: str, keys: list[str]) -> None:
        """Send special keys (e.g. Up, Enter, Escape) in order to a window."""
        ...

    async def capture_pane(self, window_id: str) -> str:
        """Capture the visible text of the window's active pane (empty on error)."""
        ...

    async def create_window(self, cwd: str, session_name: str | None = None) -> str:
        """Create a new tmux window running `claude` in cwd. Returns window_id."""
        ...

    async def list_windows(self) -> list[TmuxWindow]:
        """Return TmuxWindow entries across every registered tmux session."""
        ...


class ClaudeOps(Protocol):
    """Claude-side of the Backend contract (JSONL transcripts)."""

    async def list_sessions(self, cwd: str) -> list[ClaudeSession]:
        """Return Claude JSONL session summaries for a cwd, newest first."""
        ...

    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]:
        """Read parsed JSONL messages for a Claude session, with byte slicing."""
        ...


class Backend(Protocol):
    """Single Protocol the frontend drives.

    Tmux operations live on `backend.tmux`, Claude-JSONL operations on
    `backend.claude`. Cross-domain queries stay at the top level because
    they naturally span both sides.
    """

    tmux: TmuxOps
    claude: ClaudeOps

    # --- Queries (cross-domain) ---
    def is_alive(self, window_id: str) -> bool:
        """Return the last cached liveness verdict (tmux + Claude) for a window."""
        ...

    def get_window_binding(self, window_id: str) -> WindowBinding | None:
        """Joint view: window_id → tmux session + Claude session_id + cwd."""
        ...

    # --- Lifecycle ---
    async def start(
        self,
        on_message: Callable[[ClaudeMessage], Awaitable[None]],
        on_status: Callable[[WindowStatus], Awaitable[None]],
    ) -> None:
        """Spawn the fast (message + status) and slow (verify) poll loops."""
        ...

    async def stop(self) -> None:
        """Cancel internal tasks and persist monitor state."""
        ...


class _TmuxOpsImpl:
    """Concrete tmux-ops bundle used by DefaultBackend."""

    def __init__(self, tmux_registry: TmuxSessionRegistry) -> None:
        self._tmux_registry = tmux_registry

    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]:
        tm = self._tmux_registry.get_by_window_id(window_id)
        if not tm:
            return False, "Window no longer exists"
        window = await tm.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        success = await tm.send_keys(window.window_id, text)
        return (True, "Sent") if success else (False, "Failed to send keys")

    async def send_keys(self, window_id: str, keys: list[str]) -> None:
        tm = self._tmux_registry.get_by_window_id(window_id)
        if tm is None:
            return
        for key in keys:
            await tm.send_keys(window_id, key, enter=False, literal=False)

    async def capture_pane(self, window_id: str) -> str:
        tm = self._tmux_registry.get_by_window_id(window_id)
        if tm is None:
            return ""
        text = await tm.capture_pane(window_id)
        return text or ""

    async def create_window(self, cwd: str, session_name: str | None = None) -> str:
        sn = session_name or config.tmux_session_name
        tm = self._tmux_registry.get_or_create(sn)
        success, message, _, wid = await tm.create_window(work_dir=cwd)
        if not success:
            raise RuntimeError(f"create_window failed: {message}")
        return wid

    async def list_windows(self) -> list[TmuxWindow]:
        return await self._tmux_registry.list_all_windows()


class _ClaudeOpsImpl:
    """Concrete claude-ops bundle used by DefaultBackend."""

    def __init__(self, files: ClaudeFileResolver) -> None:
        self._files = files

    async def list_sessions(self, cwd: str) -> list[ClaudeSession]:
        """Scan `<projects>/<encoded-cwd>/*.jsonl`, newest first."""
        encoded = WindowBindings.encode_cwd(cwd)
        project_dir = config.claude_projects_path / encoded
        if not project_dir.exists():
            return []

        paths: list[Path] = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        sessions: list[ClaudeSession] = []
        for path in paths:
            cs = await self._files.get_session_summary(path.stem, cwd)
            if cs is not None:
                sessions.append(cs)
        return sessions

    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]:
        file_path = await self._files.find_file(session_id)
        if file_path is None:
            return []
        return await self._files.read_messages(
            file_path, session_id, start_byte=start_byte, end_byte=end_byte
        )


class DefaultBackend:
    """Default tmux-backed Backend composing the existing collaborators.

    Internal fast loop (`config.monitor_poll_interval`): reload session map,
    poll MessageMonitor + StatusMonitor in parallel, fan out events through
    `on_message` and `on_status`.

    Internal slow loop (60 s): run `LivenessChecker.verify_all()` to reconcile
    tmux/Claude liveness and auto-resume dead Claude sessions.
    """

    def __init__(
        self,
        tmux_registry: TmuxSessionRegistry,
        window_bindings: WindowBindings,
        message_monitor: MessageMonitor | None = None,
        status_monitor: StatusMonitor | None = None,
        slow_interval: float = 60.0,
        show_user_messages: bool | None = None,
    ) -> None:
        self._tmux_registry = tmux_registry
        self._window_bindings = window_bindings
        self._liveness = LivenessChecker(window_bindings, tmux_registry)
        self._files = ClaudeFileResolver(window_bindings)
        self._message_monitor = message_monitor or MessageMonitor(
            window_bindings=window_bindings,
            show_user_messages=show_user_messages,
        )
        self._status_monitor = status_monitor or StatusMonitor(
            window_bindings=window_bindings,
            tmux_registry=tmux_registry,
        )
        self._slow_interval = slow_interval
        self._fast_task: asyncio.Task[None] | None = None
        self._slow_task: asyncio.Task[None] | None = None

        self.tmux: TmuxOps = _TmuxOpsImpl(tmux_registry)
        self.claude: ClaudeOps = _ClaudeOpsImpl(self._files)

    # ------------------------------------------------------------------
    # Queries (cross-domain)
    # ------------------------------------------------------------------

    def is_alive(self, window_id: str) -> bool:
        return self._liveness.is_alive(window_id)

    def get_window_binding(self, window_id: str) -> WindowBinding | None:
        return self._window_bindings.get(window_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        on_message: Callable[[ClaudeMessage], Awaitable[None]],
        on_status: Callable[[WindowStatus], Awaitable[None]],
    ) -> None:
        self._message_monitor.startup_cleanup()
        # Populate liveness cache eagerly so is_alive() returns real
        # verdicts from the very first fast-loop tick (not optimistic True).
        await self._liveness.verify_all()

        async def fast_loop() -> None:
            logger.info(
                "Fast poll loop started (interval: %ss)",
                config.monitor_poll_interval,
            )

            while True:
                try:
                    await self._window_bindings.load()
                    new_messages, statuses = await asyncio.gather(
                        self._message_monitor.poll(),
                        self._status_monitor.poll(),
                    )
                    for msg in new_messages:
                        try:
                            await on_message(msg)
                        except Exception as e:
                            logger.debug("on_message consumer error: %s", e)
                    for s in statuses:
                        try:
                            await on_status(s)
                        except Exception as e:
                            logger.debug("on_status consumer error: %s", e)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Fast poll loop error: %s", e)
                await asyncio.sleep(config.monitor_poll_interval)

        async def slow_loop() -> None:
            logger.info("Slow poll loop started (interval: %ss)", self._slow_interval)
            while True:
                try:
                    await self._liveness.verify_all()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Slow poll loop error: %s", e)
                await asyncio.sleep(self._slow_interval)

        self._fast_task = asyncio.create_task(fast_loop())
        self._slow_task = asyncio.create_task(slow_loop())
        logger.info("Backend poll loops started")

    async def stop(self) -> None:
        for name, task in (("fast", self._fast_task), ("slow", self._slow_task)):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("%s poll loop raised during stop: %s", name, e)
            logger.info("%s poll loop stopped", name)
        self._fast_task = None
        self._slow_task = None

        try:
            self._message_monitor.shutdown()
        except Exception as e:
            logger.debug("message monitor shutdown error: %s", e)


# ---------------------------------------------------------------------------
# Module-level default singleton
# ---------------------------------------------------------------------------

_default_backend: Backend | None = None


def set_default_backend(backend: Backend | None) -> None:
    """Install the process-wide default Backend. Pass None to clear."""
    global _default_backend
    _default_backend = backend


def get_default_backend() -> Backend:
    """Return the installed default backend, or raise if unset."""
    if _default_backend is None:
        raise RuntimeError(
            "No default Backend installed. Call set_default_backend first."
        )
    return _default_backend

"""Backend — the single Protocol any frontend drives.

Split into two sub-Protocols by domain (TmuxOps, ClaudeOps) and a
top-level Backend that orchestrates the poll loops.

Backend emits two kinds of observation via two callbacks:

- on_state(instance_id, ClaudeState) — per fast tick per known instance
  (or on slow-tick Dead detection)
- on_message(instance_id, ClaudeMessage) — per new JSONL line

The DefaultBackend owns the fast/slow tasks, injects state_monitor /
message_monitor with an internal fan-in, and handles auto-resume
when state_monitor reports Dead.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .claude_files import ClaudeFileResolver, ClaudeSession, _encode_cwd
from claude_code_state import ClaudeState, Dead
from .claude_transcript_parser import ClaudeMessage
from .config import config
from .event_log import CurrentClaudeBinding, EventLogReader
from .message_monitor import MessageMonitor
from .state_log import StateLog
from .state_monitor import StateMonitor, _claude_proc_names
from .tmux import TmuxSessionRegistry, TmuxWindow

logger = logging.getLogger(__name__)

# Auto-resume verification + circuit breaker.
#
# ``_try_resume`` is fire-and-forget by construction: tmux returns success as
# soon as ``claude --resume <id>`` has been typed into the new window's pane,
# not when claude has actually started. If the resumed claude exits
# immediately (bad session id, expired auth, missing cwd, network blip), the
# new window quickly drops back to a shell prompt. The next slow tick then
# observes Dead again and creates yet another window — a runaway loop, one
# new window per ``slow_interval``.
#
# We close the loop in two steps:
#   1. After ``create_window``, poll the new window's foreground command
#      until either claude/node appears (success) or ``RESUME_VERIFY_TIMEOUT``
#      elapses (failure).
#   2. Track consecutive verification failures per instance. After
#      ``MAX_RESUME_FAILURES`` in a row, stop auto-resuming that instance.
#      The Dead state stays visible to the frontend so the user can recover
#      manually; a successful resume resets the counter.
RESUME_VERIFY_TIMEOUT: float = 10.0
RESUME_VERIFY_POLL: float = 1.0
MAX_RESUME_FAILURES: int = 3


class TmuxOps(Protocol):
    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]: ...
    async def send_keys(self, window_id: str, keys: list[str]) -> None: ...
    async def capture_pane(self, window_id: str) -> str: ...
    async def create_window(self, cwd: str, session_name: str | None = None) -> str: ...
    async def list_windows(self) -> list[TmuxWindow]: ...


class ClaudeOps(Protocol):
    async def list_sessions(self, cwd: str) -> list[ClaudeSession]: ...
    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]: ...


class Backend(Protocol):
    tmux: TmuxOps
    claude: ClaudeOps
    event_reader: EventLogReader

    def get_instance(self, instance_id: str) -> CurrentClaudeBinding | None:
        """Return the current binding for the given tmux session name.

        Derived from the event log; no fallback. ``None`` when no Claude
        has ever been observed in that tmux session (or the tmux session
        is gone).
        """
        ...

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None: ...

    async def stop(self) -> None: ...


class _TmuxOpsImpl:
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
    def __init__(self, files: ClaudeFileResolver) -> None:
        self._files = files

    async def list_sessions(self, cwd: str) -> list[ClaudeSession]:
        encoded = _encode_cwd(cwd)
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


def _build_state_log() -> StateLog | None:
    """Return a StateLog if CCMUX_STATE_LOG_PATH is set and non-empty, else None."""
    path = os.getenv("CCMUX_STATE_LOG_PATH", "").strip()
    if not path:
        return None
    return StateLog(path)


class DefaultBackend:
    """Default tmux-backed Backend.

    Owns the fast and slow poll tasks and the auto-resume coordinator.
    StateMonitor (fast tick: pane classification; slow tick: process
    probe) reports every ClaudeState observation to
    ``on_state_with_resume``, which fans out to the caller's ``on_state``
    first (so the UI reflects Dead before any recovery runs) and then
    attempts ``claude --resume`` in the same tmux session when the
    observed state is Dead. Re-entry during an in-flight resume is
    suppressed by ``self._resuming``.

    MessageMonitor runs on the fast tick alongside StateMonitor; each
    new JSONL line is paired with its ``instance_id`` and handed to the
    caller's ``on_message``.
    """

    def __init__(
        self,
        tmux_registry: TmuxSessionRegistry,
        message_monitor: MessageMonitor | None = None,
        slow_interval: float = 60.0,
        *,
        event_reader: EventLogReader | None = None,
    ) -> None:
        self._tmux_registry = tmux_registry

        if event_reader is None:
            from .util import ccmux_dir

            event_reader = EventLogReader(ccmux_dir() / "claude_events.jsonl")
        self.event_reader: EventLogReader = event_reader

        self._files = ClaudeFileResolver()
        self._message_monitor = message_monitor or MessageMonitor(
            event_reader=self.event_reader,
        )
        self._slow_interval = slow_interval
        self._fast_task: asyncio.Task[None] | None = None
        self._slow_task: asyncio.Task[None] | None = None
        self._resuming: set[str] = set()
        self._resume_failures: dict[str, int] = {}
        self._state_log: StateLog | None = None

        self.tmux: TmuxOps = _TmuxOpsImpl(tmux_registry)
        self.claude: ClaudeOps = _ClaudeOpsImpl(self._files)

    # --- Queries -----------------------------------------------------

    def get_instance(self, instance_id: str) -> CurrentClaudeBinding | None:
        return self.event_reader.get(instance_id)

    # --- Lifecycle ---------------------------------------------------

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None:
        # Reader's initial refresh runs synchronously here; the poll task
        # spawned by start() then tails new appends.
        await self.event_reader.start()
        self._message_monitor.startup_cleanup()

        async def on_state_with_resume(instance_id: str, state: ClaudeState) -> None:
            try:
                await on_state(instance_id, state)
            except Exception as e:
                logger.debug("on_state consumer error: %s", e)
            if isinstance(state, Dead):
                try:
                    await self._try_resume(instance_id)
                except Exception as e:
                    logger.warning("auto-resume failed for %s: %s", instance_id, e)

        self._state_log = _build_state_log()
        state_monitor = StateMonitor(
            event_reader=self.event_reader,
            tmux_registry=self._tmux_registry,
            on_state=on_state_with_resume,
            state_log=self._state_log,
        )

        async def fast_loop() -> None:
            logger.info(
                "Fast poll loop started (interval: %ss)",
                config.monitor_poll_interval,
            )
            while True:
                try:
                    new_pairs, _ = await asyncio.gather(
                        self._message_monitor.poll(),
                        state_monitor.fast_tick(),
                    )
                    for instance_id, msg in new_pairs:
                        try:
                            await on_message(instance_id, msg)
                        except Exception as e:
                            logger.debug("on_message consumer error: %s", e)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Fast poll loop error: %s", e)
                await asyncio.sleep(config.monitor_poll_interval)

        async def slow_loop() -> None:
            logger.info("Slow poll loop started (interval: %ss)", self._slow_interval)
            while True:
                try:
                    await state_monitor.slow_tick()
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
        await self.event_reader.stop()

        try:
            self._message_monitor.shutdown()
        except Exception as e:
            logger.debug("message monitor shutdown error: %s", e)

        if self._state_log is not None:
            try:
                await self._state_log.close()
            except Exception as e:
                logger.debug("state_log close error: %s", e)
            self._state_log = None

    # --- Auto-resume -------------------------------------------------

    async def _try_resume(self, instance_id: str) -> None:
        if instance_id in self._resuming:
            logger.debug("resume already in flight for %s; skipping", instance_id)
            return
        if self._resume_failures.get(instance_id, 0) >= MAX_RESUME_FAILURES:
            logger.debug(
                "auto-resume circuit breaker tripped for %s; skipping",
                instance_id,
            )
            return
        self._resuming.add(instance_id)
        try:
            binding = self.event_reader.get(instance_id)
            if binding is None:
                return
            cwd = binding.cwd or str(Path.home())
            logger.info(
                "Attempting to resume Claude session %s in instance %s (cwd=%s)",
                binding.claude_session_id,
                instance_id,
                cwd,
            )
            tm = self._tmux_registry.get_or_create(instance_id)
            ok, msg, _, new_wid = await tm.create_window(
                work_dir=cwd, resume_session_id=binding.claude_session_id
            )
            if not ok:
                failures = self._bump_resume_failure(instance_id)
                logger.warning(
                    "Failed to resume %s: %s (failures=%d)",
                    binding.claude_session_id,
                    msg,
                    failures,
                )
                if failures >= MAX_RESUME_FAILURES:
                    logger.error(
                        "Auto-resume disabled for %s after %d consecutive "
                        "failures; Dead state will remain visible until you "
                        "intervene manually",
                        instance_id,
                        failures,
                    )
                return

            if await self._verify_resume(tm, new_wid):
                self._resume_failures.pop(instance_id, None)
                logger.info(
                    "Resumed %s in window %s", binding.claude_session_id, new_wid
                )
            else:
                failures = self._bump_resume_failure(instance_id)
                logger.warning(
                    "Resume verification failed for %s in window %s; claude "
                    "did not appear within %.1fs (failures=%d)",
                    instance_id,
                    new_wid,
                    RESUME_VERIFY_TIMEOUT,
                    failures,
                )
                if failures >= MAX_RESUME_FAILURES:
                    logger.error(
                        "Auto-resume disabled for %s after %d consecutive "
                        "failures; Dead state will remain visible until you "
                        "intervene manually",
                        instance_id,
                        failures,
                    )
        finally:
            self._resuming.discard(instance_id)

    async def _verify_resume(
        self,
        tm,
        window_id: str,
        *,
        timeout: float = RESUME_VERIFY_TIMEOUT,
        poll: float = RESUME_VERIFY_POLL,
    ) -> bool:
        """Poll the new window until claude/node appears or timeout elapses."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        proc_names = _claude_proc_names()
        while loop.time() < deadline:
            await asyncio.sleep(poll)
            w = await tm.find_window_by_id(window_id)
            if w is None:
                return False
            if w.pane_current_command in proc_names:
                return True
        return False

    def _bump_resume_failure(self, instance_id: str) -> int:
        n = self._resume_failures.get(instance_id, 0) + 1
        self._resume_failures[instance_id] = n
        return n


# --- Module-level default singleton --------------------------------------

_default_backend: Backend | None = None


def get_default_backend() -> Backend:
    if _default_backend is None:
        raise RuntimeError(
            "Default backend not set; call set_default_backend() before accessing."
        )
    return _default_backend


def set_default_backend(backend: Backend | None) -> None:
    global _default_backend
    _default_backend = backend

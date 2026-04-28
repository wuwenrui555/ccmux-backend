"""State monitor — classifies every known ClaudeInstance into a
ClaudeState and emits observations via a callback.

Two ticks:

- ``fast_tick()`` — called at ``config.monitor_poll_interval``. For each
  instance, captures its pane, classifies into
  ``Working / Idle / Blocked``, emits via ``on_state``. Silent skip
  when the window is gone or capture returns empty.
- ``slow_tick()`` — called at ``slow_interval`` (default 60s). For each
  instance, probes ``pane_current_command``; emits ``Dead()`` when
  tmux is alive but the foreground process is no longer ``claude`` /
  ``node``. Auto-resume is the backend's responsibility — this module
  only reports.

The monitor keeps no state between ticks. Each emission is a fresh
observation.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, TYPE_CHECKING

from .claude_state import Blocked, ClaudeState, Dead, Idle, Working
from .tmux_pane_parser import (
    extract_interactive_content,
    has_input_chrome,
    parse_status_line,
)

if TYPE_CHECKING:
    from .event_log import CurrentClaudeBinding, EventLogReader
    from .tmux import TmuxSessionRegistry

logger = logging.getLogger(__name__)


_DEFAULT_CLAUDE_PROC_NAMES: frozenset[str] = frozenset({"claude", "node"})


def _claude_proc_names() -> frozenset[str]:
    """Resolve the set of process names counted as 'Claude is alive'."""
    raw = os.getenv("CCMUX_CLAUDE_PROC_NAMES", "")
    names = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(names) if names else _DEFAULT_CLAUDE_PROC_NAMES


OnStateCallback = Callable[[str, ClaudeState], Awaitable[None]]


class StateMonitor:
    """Produces ``(instance_id, ClaudeState)`` observations."""

    def __init__(
        self,
        *,
        event_reader: "EventLogReader",
        tmux_registry: "TmuxSessionRegistry",
        on_state: OnStateCallback,
    ) -> None:
        self._event_reader = event_reader
        self._tmux_registry = tmux_registry
        self._on_state = on_state

    async def fast_tick(self) -> None:
        """Classify each live binding from its pane text; emit or skip."""
        for b in list(self._event_reader.all_alive()):
            try:
                state = await self._classify_from_pane(b)
            except Exception as e:
                logger.debug(
                    "fast_tick classify error for %s: %s", b.tmux_session_name, e
                )
                continue
            if state is not None:
                await self._on_state(b.tmux_session_name, state)

    async def slow_tick(self) -> None:
        """Probe each binding's foreground process; emit Dead when needed."""
        for b in list(self._event_reader.all_alive()):
            try:
                dead = await self._probe_dead(b)
            except Exception as e:
                logger.debug("slow_tick probe error for %s: %s", b.tmux_session_name, e)
                continue
            if dead:
                await self._on_state(b.tmux_session_name, Dead())

    # ------------------------------------------------------------------

    async def _classify_from_pane(
        self, b: "CurrentClaudeBinding"
    ) -> ClaudeState | None:
        """Return a ClaudeState from pane text, or None to skip."""
        if not b.window_id:
            return None
        tm = self._tmux_registry.get_by_window_id(b.window_id)
        if tm is None:
            return None
        w = await tm.find_window_by_id(b.window_id)
        if w is None:
            return None
        pane_text = await tm.capture_pane(b.window_id)
        if not pane_text:
            return None

        lines = pane_text.strip().split("\n")
        if not has_input_chrome(lines):
            ui = extract_interactive_content(pane_text)
            if ui is None:
                return None
            return Blocked(ui=ui.ui, content=ui.content)

        status_text = parse_status_line(pane_text)
        if status_text:
            return Working(status_text=status_text)
        return Idle()

    async def _probe_dead(self, b: "CurrentClaudeBinding") -> bool:
        """True when the tmux window exists but the pane foreground is not claude."""
        if not b.window_id:
            return False
        tm = self._tmux_registry.get_by_window_id(b.window_id)
        if tm is None:
            # Cache miss (restart, session rename): fall back to the stable
            # tmux_session_name. Without this fallback, a binding whose
            # window_id is not yet in the tmux cache is never observed as
            # Dead even when its process exits.
            tm = self._tmux_registry.get_or_create(b.tmux_session_name)
        w = await tm.find_window_by_id(b.window_id)
        if w is None:
            return False
        return w.pane_current_command not in _claude_proc_names()

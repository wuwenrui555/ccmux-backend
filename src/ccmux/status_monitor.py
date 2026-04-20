"""Terminal status monitoring — pure producer of WindowStatus events.

Iterates every Claude window known to the injected `WindowBindings`,
captures each pane, and returns raw observations (status line,
interactive UI detection, liveness).

Zero Telegram knowledge: emitted `WindowStatus` contains only backend
identifiers. Consumers that need routing (e.g. the Telegram status_line
module) resolve `window_id → topic binding` on their side.

Key classes: StatusMonitor, WindowStatus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .tmux_pane_parser import (
    InteractiveUIContent,
    _has_input_chrome,
    extract_interactive_content,
    parse_status_line,
)

if TYPE_CHECKING:
    from .tmux import TmuxSessionRegistry
    from .window_bindings import WindowBinding, WindowBindings

logger = logging.getLogger(__name__)


class PaneState(StrEnum):
    """High-level classification of a captured Claude pane.

    The three productive states map directly onto user-visible behaviour:

    - ``WORKING``: a spinner is above Claude's input chrome — Claude is
      generating a response, the user should not be bothered.
    - ``IDLE``: input chrome present but no spinner — Claude is waiting
      for the user's next message.
    - ``BLOCKED``: the input chrome is absent because some UI (permission
      prompt, AskUserQuestion, ExitPlanMode, Settings panel) replaced
      it; the user must choose an option before Claude can continue.

    ``UNKNOWN`` is the fallback when the pane could not be captured or
    the window is gone.
    """

    UNKNOWN = "unknown"
    WORKING = "working"
    IDLE = "idle"
    BLOCKED = "blocked"


def derive_pane_state(pane_text: str, status_text: str | None) -> PaneState:
    """Classify a captured pane using the input-chrome presence test.

    Live blocking UIs always replace the `────\\n❯\\n────\\nstatusbar`
    sandwich that marks Claude's input region. When that sandwich is
    present we're in WORKING or IDLE; when it's gone we're in BLOCKED.
    """
    if not pane_text:
        return PaneState.UNKNOWN
    lines = pane_text.strip().split("\n")
    if not _has_input_chrome(lines):
        return PaneState.BLOCKED
    return PaneState.WORKING if status_text else PaneState.IDLE


@dataclass
class WindowStatus:
    """Raw terminal observations for one Claude-bound window.

    Emitted by StatusMonitor.poll(); consumed by frontend status handlers.
    Contains only backend-native fields — frontend attaches its own
    routing (user_id, thread_id, chat_id) by looking up `window_id`
    against its own topic-binding map.
    """

    window_id: str
    window_exists: bool  # False → window gone from tmux (consumer should clear)
    pane_captured: (
        bool  # False → transient capture failure (consumer should keep existing)
    )
    status_text: str | None  # parse_status_line result
    interactive_ui: InteractiveUIContent | None  # extract_interactive_content result
    pane_state: PaneState = field(default=PaneState.UNKNOWN)


class StatusMonitor:
    """Scans every Claude-bound window, produces WindowStatus observations.

    Stateless producer. The caller owns the polling loop and passes the
    result list to consumers for presentation.
    """

    def __init__(
        self,
        window_bindings: "WindowBindings | None" = None,
        tmux_registry: "TmuxSessionRegistry | None" = None,
    ) -> None:
        self._window_bindings = window_bindings
        self._tmux_registry = tmux_registry

    async def poll(self) -> list[WindowStatus]:
        """Observe every known Claude window's terminal state.

        Returns one entry per window_id present in the injected
        `WindowBindings`. Returns empty list if no bindings or tmux
        registry were injected.
        """
        results: list[WindowStatus] = []
        if self._window_bindings is None or self._tmux_registry is None:
            return results

        for info in list(self._window_bindings.all()):
            if not info.window_id:
                continue
            try:
                results.append(await self._observe(info))
            except Exception as e:
                logger.debug(
                    "Status observe error for window %s: %s",
                    info.window_id,
                    e,
                )
        return results

    async def _observe(self, info: "WindowBinding") -> WindowStatus:
        assert self._tmux_registry is not None
        wid = info.window_id

        def make(
            *,
            window_exists: bool,
            pane_captured: bool,
            status_text: str | None = None,
            interactive_ui: InteractiveUIContent | None = None,
            pane_state: PaneState = PaneState.UNKNOWN,
        ) -> WindowStatus:
            return WindowStatus(
                window_id=wid,
                window_exists=window_exists,
                pane_captured=pane_captured,
                status_text=status_text,
                interactive_ui=interactive_ui,
                pane_state=pane_state,
            )

        tm = self._tmux_registry.get_by_window_id(wid)
        if not tm:
            return make(window_exists=False, pane_captured=False)
        w = await tm.find_window_by_id(wid)
        if not w:
            return make(window_exists=False, pane_captured=False)
        pane_text = await tm.capture_pane(w.window_id)
        if not pane_text:
            return make(window_exists=True, pane_captured=False)
        status_text = parse_status_line(pane_text)
        return make(
            window_exists=True,
            pane_captured=True,
            status_text=status_text,
            interactive_ui=extract_interactive_content(pane_text),
            pane_state=derive_pane_state(pane_text, status_text),
        )

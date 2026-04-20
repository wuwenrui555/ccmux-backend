"""Liveness verification — tmux + Claude health checks and auto-resume.

Runs on the slow loop (60s). For each entry in the session map, probes
whether the tmux window still exists and whether Claude Code is still
the foreground process in the window's pane. Dead Claude sessions are
auto-resumed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tmux import TmuxSessionRegistry
    from .window_bindings import WindowBindings

logger = logging.getLogger(__name__)

# Default foreground process names that indicate Claude Code is running
# in the pane. `claude` is the CLI wrapper; `node` is the runtime the CLI
# spawns (visible in `pane_current_command` on most setups). Override
# with `CCMUX_CLAUDE_PROC_NAMES=claude,node,bun` if a Claude Code release
# switches runtimes.
_DEFAULT_CLAUDE_PROC_NAMES: frozenset[str] = frozenset({"claude", "node"})


def _claude_proc_names() -> frozenset[str]:
    """Resolve the set of process names counted as "Claude is alive".

    Read fresh each call so `CCMUX_CLAUDE_PROC_NAMES` can be flipped
    without restarting the backend. Empty / whitespace env var falls
    back to the default set.
    """
    raw = os.getenv("CCMUX_CLAUDE_PROC_NAMES", "")
    names = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(names) if names else _DEFAULT_CLAUDE_PROC_NAMES


class LivenessChecker:
    """Window-keyed liveness: verify_all() probes, is_alive() reads cache."""

    def __init__(
        self,
        window_bindings: WindowBindings,
        tmux_registry: TmuxSessionRegistry,
    ) -> None:
        self._window_bindings = window_bindings
        self._tmux_registry = tmux_registry
        self._window_alive: dict[str, bool] = {}

    def is_alive(self, window_id: str) -> bool:
        """Cached verdict. Unknown windows default to True (optimistic)."""
        if not window_id:
            return False
        return self._window_alive.get(window_id, True)

    async def verify_all(self) -> None:
        """Probe every entry in the session map, update _window_alive."""
        await self._window_bindings.load()
        seen_window_ids: set[str] = set()
        for session_name, entry in list(self._window_bindings.raw.items()):
            window_id = entry.get("window_id", "")
            claude_session_id = entry.get("session_id", "")

            tmux_alive, pane_cmd = await self._probe_pane(window_id, session_name)
            claude_alive = tmux_alive and self._pane_runs_claude(pane_cmd)

            if tmux_alive and not claude_alive and claude_session_id:
                await self._try_resume(session_name, claude_session_id)
                # After resume, the SessionStart hook asynchronously updates
                # window_bindings with the new window_id. Skip updating
                # _window_alive for the old id; next verify_all picks up the
                # new entry.
                continue

            alive = tmux_alive and claude_alive
            if window_id:
                self._window_alive[window_id] = alive
                seen_window_ids.add(window_id)

            if not alive:
                logger.debug(
                    "Window dead (window_id=%s session=%s): tmux=%s claude=%s "
                    "(pane_cmd=%s)",
                    window_id,
                    session_name,
                    tmux_alive,
                    claude_alive,
                    pane_cmd,
                )

        # Drop cached entries for window_ids no longer present in bindings.
        # Prevents the cache from growing unbounded as windows come and go.
        stale = set(self._window_alive) - seen_window_ids
        for wid in stale:
            self._window_alive.pop(wid, None)

    # -- internal probes ------------------------------------------------

    async def _probe_pane(self, window_id: str, session_name: str) -> tuple[bool, str]:
        """Return (tmux_window_exists, pane_current_command)."""
        if not window_id:
            return False, ""
        tm = self._tmux_registry.get_by_window_id(window_id)
        if tm is None:
            tm = self._tmux_registry.get_or_create(session_name)
        try:
            w = await tm.find_window_by_id(window_id)
        except Exception:
            return False, ""
        if w is None:
            return False, ""
        return True, w.pane_current_command

    @staticmethod
    def _pane_runs_claude(pane_cmd: str) -> bool:
        """True when the pane's foreground process looks like Claude Code."""
        return pane_cmd in _claude_proc_names()

    async def _try_resume(self, session_name: str, claude_session_id: str) -> None:
        tm = self._tmux_registry.get_or_create(session_name)
        cwd = self._window_bindings.raw.get(session_name, {}).get("cwd", "")
        if not cwd:
            cwd = str(Path.home())
        logger.info(
            "Attempting to resume Claude session %s in tmux session %s (cwd=%s)",
            claude_session_id,
            session_name,
            cwd,
        )
        ok, msg, _, new_wid = await tm.create_window(
            work_dir=cwd, resume_session_id=claude_session_id
        )
        if ok:
            logger.info("Resumed %s in window %s", claude_session_id, new_wid)
        else:
            logger.warning("Failed to resume %s: %s", claude_session_id, msg)

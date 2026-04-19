"""Liveness verification — tmux + Claude health checks and auto-resume.

Runs on the slow loop (60s). For each entry in the session map, probes
whether the tmux window still exists and whether Claude Code's session_id
matches what we expect. Dead Claude sessions are auto-resumed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .tmux import tmux_registry

if TYPE_CHECKING:
    from .window_bindings import WindowBindings

logger = logging.getLogger(__name__)


class LivenessChecker:
    """Window-keyed liveness: verify_all() probes, is_alive() reads cache."""

    def __init__(self, window_bindings: WindowBindings) -> None:
        self._window_bindings = window_bindings
        self._window_alive: dict[str, bool] = {}

    def is_alive(self, window_id: str) -> bool:
        """Cached verdict. Unknown windows default to True (optimistic)."""
        if not window_id:
            return False
        return self._window_alive.get(window_id, True)

    async def verify_all(self) -> None:
        """Probe every entry in the session map, update _window_alive."""
        await self._window_bindings.load()
        for session_name, entry in list(self._window_bindings.raw.items()):
            window_id = entry.get("window_id", "")
            claude_session_id = entry.get("session_id", "")

            tmux_alive = await self._check_tmux(window_id, session_name)
            claude_alive = self._check_claude(session_name, claude_session_id)

            if tmux_alive and not claude_alive and claude_session_id:
                await self._try_resume(session_name, claude_session_id)
                await self._window_bindings.load()
                entry2 = self._window_bindings.raw.get(session_name, {})
                claude_session_id = entry2.get("session_id", "")
                claude_alive = self._check_claude(session_name, claude_session_id)

            alive = tmux_alive and claude_alive
            if window_id:
                self._window_alive[window_id] = alive

            if not alive:
                logger.debug(
                    "Window dead (window_id=%s session=%s): tmux=%s claude=%s",
                    window_id,
                    session_name,
                    tmux_alive,
                    claude_alive,
                )

    # -- internal probes ------------------------------------------------

    async def _check_tmux(self, window_id: str, session_name: str) -> bool:
        if not window_id:
            return False
        tm = tmux_registry.get_by_window_id(window_id)
        if tm is None:
            tm = tmux_registry.get_or_create(session_name)
        try:
            return (await tm.find_window_by_id(window_id)) is not None
        except Exception:
            return False

    def _check_claude(self, session_name: str, claude_session_id: str) -> bool:
        if not claude_session_id:
            return False
        entry = self._window_bindings.raw.get(session_name)
        if not entry:
            return False
        return entry.get("session_id") == claude_session_id

    async def _try_resume(self, session_name: str, claude_session_id: str) -> None:
        tm = tmux_registry.get_or_create(session_name)
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

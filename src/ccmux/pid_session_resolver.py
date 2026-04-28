"""Map a tmux pane to its (Claude session_id, launch_cwd).

Lifted from ``hook.py`` so the same chain can be used by reconcile
logic in the backend's runtime path. Behavior unchanged from the
existing private ``_resolve_session_via_pid``; the only public symbol
is ``resolve_for_pane``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SESSION_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
)
_PANE_RE = re.compile(r"^%\d+$")


def _encode_project_dir(cwd: str) -> str:
    """Return the `~/.claude/projects/<encoded>` basename for a launch cwd."""
    return re.sub(r"[/_.]", "-", cwd)


def _find_claude_pid(shell_pid: int) -> int | None:
    """Return the direct `claude` child PID of `shell_pid`, or None.

    Strategy: enumerate direct children via ``pgrep -P``. For each
    candidate, prefer the Linux signal ``/proc/<pid>/cmdline`` matching
    ``claude`` (cheap, exact). When ``/proc`` isn't readable -- macOS
    or restricted Linux -- fall back to a portable signal: a Claude
    Code instance writes ``~/.claude/sessions/<pid>.json`` on startup,
    so the presence of that file uniquely identifies it.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(shell_pid)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    sessions_dir = Path.home() / ".claude" / "sessions"
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue

        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            raw = None

        if raw is not None:
            argv0 = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
            if Path(argv0).name == "claude":
                return pid
            # On Linux, if cmdline read succeeded but didn't match,
            # this child is definitely not Claude. Skip the fallback
            # for this pid.
            continue

        # Fallback (macOS, or Linux with restricted /proc): does this
        # child own a Claude sessions file?
        if (sessions_dir / f"{pid}.json").exists():
            return pid
    return None


def resolve_for_pane(pane_id: str) -> tuple[str, str] | None:
    """Recover ``(session_id, launch_cwd)`` for the Claude in ``pane_id``.

    Walks: tmux pane -> shell pid -> claude pid -> launch cwd (stable
    across /clear) -> newest transcript jsonl in the project dir.

    Returns ``None`` if any step fails.
    """
    if not _PANE_RE.match(pane_id):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    try:
        shell_pid = int(result.stdout.strip())
    except ValueError:
        return None

    claude_pid = _find_claude_pid(shell_pid)
    if claude_pid is None:
        return None

    sessions_file = Path.home() / ".claude" / "sessions" / f"{claude_pid}.json"
    try:
        launch_info = json.loads(sessions_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    launch_cwd = launch_info.get("cwd", "")
    if not launch_cwd or not os.path.isabs(launch_cwd):
        return None

    project_dir = Path.home() / ".claude" / "projects" / _encode_project_dir(launch_cwd)
    try:
        candidates = [
            p
            for p in project_dir.iterdir()
            if p.is_file() and _SESSION_FILE_RE.match(p.name)
        ]
    except OSError:
        return None
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.stem, launch_cwd

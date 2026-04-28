"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart and UserPromptSubmit hooks to
append one event per fire to ``<CCMUX_DIR>/claude_events.jsonl``.
Backend's EventLogReader projects this log to the active
``(tmux_session_name → CurrentClaudeBinding)`` map.

This module deliberately avoids importing `config.py` so the hook stays
cheap to start from inside a tmux pane (no dotenv load, no env parsing).
Config directory resolution uses `util.ccmux_dir()` instead, which is
shared with `config.py`.

Logging is teed to both stderr (captured by Claude Code) and
<CCMUX_DIR>/hook.log so past invocations can be diagnosed after Claude
Code's error banner scrolls away. Unhandled exceptions are logged via
`logger.exception` before the process exits 1.

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regexes shared with the empty-stdin PID-fallback chain.
# ---------------------------------------------------------------------------

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


def _session_id_by_mtime(pid: int, launch_cwd: str) -> str | None:
    """Pick the JSONL this pid is actively writing to via mtime correlation.

    ``~/.claude/sessions/<pid>.json`` carries an ``updatedAt`` field that
    Claude bumps on activity. The active JSONL in
    ``~/.claude/projects/<encoded-cwd>/`` has an mtime that tracks the
    same timestamp, so the correct session_id for this pid is the JSONL
    whose mtime is closest to ``updatedAt``. Works on Linux and macOS
    alike.
    """
    sessions_file = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        info = json.loads(sessions_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    updated_at_ms = info.get("updatedAt")
    if not isinstance(updated_at_ms, (int, float)):
        return None
    target_mtime = updated_at_ms / 1000.0

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

    best = min(candidates, key=lambda p: abs(p.stat().st_mtime - target_mtime))
    return best.stem


def _resolve_session_via_pid(pane_id: str) -> tuple[str, str] | None:
    """Recover ``(session_id, launch_cwd)`` for the Claude in ``pane_id``.

    Walks: tmux pane -> shell pid -> claude pid -> launch cwd (stable
    across /clear) -> session_id via JSONL mtime correlation.

    Used as a fallback when the hook's stdin payload arrives empty or
    invalid (rare; root cause unidentified).
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

    sid = _session_id_by_mtime(claude_pid, launch_cwd)
    if sid is not None:
        return sid, launch_cwd

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


# Claude Code's settings file, where hooks are configured
_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "ccmux hook"


def _find_ccmux_path() -> str:
    """Find the full path to the ccmux executable.

    Returns
    -------
    str
        Absolute path to ccmux, resolved in order:
        1. `shutil.which("ccmux")` — if ccmux is in PATH
        2. Same directory as `sys.executable` — for venv installs
        3. `"ccmux"` — fallback, assumes it will be in PATH at runtime
    """
    # Try PATH first
    ccmux_path = shutil.which("ccmux")
    if ccmux_path:
        return ccmux_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccmux is installed in a venv
    python_dir = Path(sys.executable).parent
    ccmux_in_venv = python_dir / "ccmux"
    if ccmux_in_venv.exists():
        return str(ccmux_in_venv)

    # Last resort: assume it will be in PATH
    return "ccmux"


# Events the hook listens for. SessionStart catches new Claudes
# and continuum-style respawns; UserPromptSubmit refreshes the
# (tmux session, window, claude session, cwd, transcript) row on
# every user message, so stale values self-heal between turns.
_EVENTS_TO_REGISTER = ("SessionStart", "UserPromptSubmit")


def _is_hook_installed(settings: dict, event: str = "SessionStart") -> bool:
    """Check if ccmux hook is already installed for the given event.

    Parameters
    ----------
    settings : dict
        Parsed contents of `~/.claude/settings.json`.
    event : str
        Hook event key under `settings["hooks"]` to inspect.

    Returns
    -------
    bool
        True if a hook command matching `ccmux hook` (or a full path
        ending with it) is found under that event.
    """
    hooks = settings.get("hooks", {})
    entries = hooks.get(event, [])

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            # Match 'ccmux hook' or paths ending with 'ccmux hook'
            if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX):
                return True
    return False


def _install_hook() -> int:
    """Install the ccmux hook into Claude Code's settings.json.

    Registers `ccmux hook` under every event in `_EVENTS_TO_REGISTER`,
    idempotently per event. Existing entries are preserved.

    Returns
    -------
    int
        0 on success, 1 on error.
    """
    settings_file = _CLAUDE_SETTINGS_FILE
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    # Find the full path to ccmux
    ccmux_path = _find_ccmux_path()
    hook_command = f"{ccmux_path} hook"
    hook_config = {"type": "command", "command": hook_command, "timeout": 5}

    if "hooks" not in settings:
        settings["hooks"] = {}

    added_any = False
    for event in _EVENTS_TO_REGISTER:
        if _is_hook_installed(settings, event):
            logger.info("Hook already installed for %s in %s", event, settings_file)
            continue
        if event not in settings["hooks"]:
            settings["hooks"][event] = []
        settings["hooks"][event].append({"hooks": [hook_config]})
        added_any = True
        logger.info("Installing hook command for %s: %s", event, hook_command)

    if not added_any:
        print(f"Hook already installed in {settings_file}")
        return 0

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", settings_file)
    print(f"Hook installed successfully in {settings_file}")
    return 0


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _configure_hook_logging() -> None:
    """Tee hook logs to stderr (for Claude Code) and <CCMUX_DIR>/hook.log.

    The file handler lets us recover tracebacks after Claude Code's error
    banner scrolls off-screen; stderr stays so Claude Code still surfaces
    failures inline. Safe to call multiple times (guarded by handler
    presence check).
    """
    from .util import ccmux_dir

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if not any(
        isinstance(h, logging.StreamHandler)
        and getattr(h, "stream", None) is sys.stderr
        for h in root.handlers
    ):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.DEBUG)
        stderr_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(stderr_handler)

    log_path = ccmux_dir() / "hook.log"
    already_has_file = any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", None) == str(log_path)
        for h in root.handlers
    )
    if already_has_file:
        return

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(file_handler)
    except OSError as e:
        # File logging is best-effort; don't let a read-only FS block the hook.
        logger.warning("Could not open hook.log: %s", e)


def hook_main() -> None:
    """CLI entry point for the hook subcommand.

    Two modes:
    - `ccmux hook --install`: install the hook into `~/.claude/settings.json`
    - `ccmux hook`: read a SessionStart event from stdin and update
      `claude_instances.json` with the tmux session → window_id + session_id mapping
    """
    _configure_hook_logging()
    try:
        _hook_main_impl()
    except SystemExit:
        raise
    except Exception:
        logger.exception("Hook crashed with unhandled exception")
        sys.exit(1)


def _hook_main_impl() -> None:
    parser = argparse.ArgumentParser(
        prog="ccmux hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into ~/.claude/settings.json",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    # --install mode: write hook config and exit (don't process stdin)
    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())

    # Check tmux environment first — not in tmux means nothing to do.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.debug("TMUX_PANE not set, not running in tmux — skipping")
        return
    if not _PANE_RE.match(pane_id):
        logger.warning("Invalid TMUX_PANE format: %r — skipping", pane_id)
        return

    # Read hook payload from stdin. In rare cases stdin arrives empty or
    # invalid (root cause unidentified; empirically NOT caused by /clear —
    # startup/resume/clear all deliver full JSON), so we treat stdin as
    # best-effort and fall back to PID-based resolution below.
    logger.debug("Processing hook event from stdin")
    session_id = ""
    cwd = ""
    event = "SessionStart"
    transcript_path = ""
    permission_mode = ""
    try:
        payload = json.load(sys.stdin)
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        # `or` handles the case where the key is present but the value is "".
        event = payload.get("hook_event_name", "SessionStart") or "SessionStart"
        transcript_path = payload.get("transcript_path", "") or ""
        permission_mode = payload.get("permission_mode", "") or ""
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug("stdin not usable JSON (%s); will try PID fallback", e)

    _ACCEPTED_EVENTS = {"SessionStart", "UserPromptSubmit"}
    if event not in _ACCEPTED_EVENTS:
        logger.debug("Ignoring event: %s", event)
        return

    # cwd must be absolute to be trustworthy.
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        cwd = ""

    # Fall back to PID-based resolution when stdin didn't deliver both a
    # valid session_id and cwd. Covers the rare empty-stdin edge case
    # above; specific trigger remains unknown.
    if not session_id or not _UUID_RE.match(session_id) or not cwd:
        resolved = _resolve_session_via_pid(pane_id)
        if resolved is None:
            logger.debug("Could not resolve session via PID fallback; skipping")
            return
        session_id, cwd = resolved
        logger.info(
            "Resolved session via PID fallback: session_id=%s cwd=%s",
            session_id,
            cwd,
        )

    # Get tmux session name and window ID for the pane running this hook
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    parts = raw_output.split(":", 1)
    if len(parts) < 2:
        logger.warning(
            "Failed to parse session_name:window_id from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return
    tmux_session_name, window_id = parts

    logger.debug(
        "tmux session=%s, window_id=%s, session_id=%s, cwd=%s",
        tmux_session_name,
        window_id,
        session_id,
        cwd,
    )

    from .util import ccmux_dir

    # v4.0.0 event log: append one line for every accepted event. No locking
    # — single-write O_APPEND is atomic for lines under PIPE_BUF (4 KB).
    from datetime import datetime, timezone

    from .event_log import ClaudeInfo, EventLogWriter, HookEvent, TmuxInfo

    try:
        writer = EventLogWriter(ccmux_dir() / "claude_events.jsonl")
        writer.append(
            HookEvent(
                timestamp=datetime.now(timezone.utc),
                hook_event=event,
                tmux=TmuxInfo(
                    session_id="",  # tmux's $-id not yet captured (not consumed)
                    session_name=tmux_session_name,
                    window_id=window_id,
                    window_index="",
                    window_name="",
                    pane_id=pane_id,
                    pane_index="",
                ),
                claude=ClaudeInfo(
                    session_id=session_id,
                    transcript_path=transcript_path,
                    cwd=cwd,
                    permission_mode=permission_mode,
                ),
            )
        )
        logger.debug(
            "Appended event-log entry: event=%s session=%s",
            event,
            session_id,
        )
    except Exception:
        logger.exception("Failed to append to event log")

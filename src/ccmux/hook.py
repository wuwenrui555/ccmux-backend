"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window ↔ session
mapping in <CCMUX_DIR>/window_bindings.json. Also provides `--install` to
auto-configure the hook in ~/.claude/settings.json.

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
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Validate TMUX_PANE format (e.g. %12) so a malformed env var produces a
# clear log line instead of a cryptic tmux error.
_PANE_RE = re.compile(r"^%\d+$")

# Claude Code derives its per-project transcript directory from the launch
# cwd by replacing every `/`, `_`, and `.` with `-`. Verified against the
# full listing of `~/.claude/projects/` on a live system.
def _encode_project_dir(cwd: str) -> str:
    """Return the `~/.claude/projects/<encoded>` basename for a launch cwd."""
    return re.sub(r"[/_.]", "-", cwd)


def _find_claude_pid(shell_pid: int) -> int | None:
    """Return the direct `claude` child PID of `shell_pid`, or None.

    Uses `pgrep -P` to enumerate direct children, then matches argv0 of
    `/proc/<pid>/cmdline`. We match by basename so either a bare `claude`
    on $PATH or an absolute path like `/usr/local/bin/claude` is accepted.
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
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            continue
        argv0 = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        if Path(argv0).name == "claude":
            return pid
    return None


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


def _is_hook_installed(settings: dict) -> bool:
    """Check if ccmux hook is already installed in Claude Code settings.

    Parameters
    ----------
    settings : dict
        Parsed contents of `~/.claude/settings.json`.

    Returns
    -------
    bool
        True if a SessionStart hook command matching `ccmux hook`
        (or a full path ending with it) is found.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
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

    Reads `~/.claude/settings.json`, appends a SessionStart hook entry
    pointing to the ccmux executable, and writes back.

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

    # Check if already installed
    if _is_hook_installed(settings):
        logger.info("Hook already installed in %s", settings_file)
        print(f"Hook already installed in {settings_file}")
        return 0

    # Find the full path to ccmux
    ccmux_path = _find_ccmux_path()
    hook_command = f"{ccmux_path} hook"
    hook_config = {"type": "command", "command": hook_command, "timeout": 5}
    logger.info("Installing hook command: %s", hook_command)

    # Install the hook
    if "hooks" not in settings:
        settings["hooks"] = {}
    if "SessionStart" not in settings["hooks"]:
        settings["hooks"]["SessionStart"] = []

    settings["hooks"]["SessionStart"].append({"hooks": [hook_config]})

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
      `window_bindings.json` with the tmux session → window_id + session_id mapping
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

    # Check tmux environment first — not in tmux means nothing to do
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.debug("TMUX_PANE not set, not running in tmux — skipping")
        return
    if not _PANE_RE.match(pane_id):
        logger.warning("Invalid TMUX_PANE format: %r — skipping", pane_id)
        return

    # Read hook payload from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # cwd is persisted so the auto-resume path in LivenessChecker._try_resume
    # knows where to start Claude Code when a session needs to be revived.
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        cwd = ""

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

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

    # Read-modify-write with file locking to prevent concurrent hook races
    from .util import ccmux_dir

    map_file = ccmux_dir() / "window_bindings.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                # Guard: if this session already has a different window,
                # a second Claude was opened in the same tmux session.
                # Skip to avoid overwriting the existing entry.
                existing = session_map.get(tmux_session_name)
                if existing and existing.get("window_id") != window_id:
                    logger.warning(
                        "Session '%s' already has window %s, refusing to "
                        "overwrite with %s. Only one Claude per tmux session.",
                        tmux_session_name,
                        existing.get("window_id"),
                        window_id,
                    )
                    return

                session_map[tmux_session_name] = {
                    "window_id": window_id,
                    "session_id": session_id,
                    "cwd": cwd,
                }

                from .util import atomic_write_json

                # Write to temp file then rename — prevents corrupt JSON
                # if the process is interrupted mid-write
                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> window_id=%s, session_id=%s, cwd=%s",
                    tmux_session_name,
                    window_id,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)

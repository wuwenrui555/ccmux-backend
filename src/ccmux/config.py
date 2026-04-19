"""Backend configuration — reads env vars and exposes a `config` singleton.

Only the Claude-tmux backend's own settings live here. Frontend packages
ship their own `config.py` for bot tokens, allow-lists, etc.

.env loading priority: local `.env` (cwd) > `$CCMUX_DIR/.env` (default
`~/.ccmux/.env`). Reads:

- `TMUX_SESSION_NAME` (default `__ccmux__`) — reserved session that
  holds the bot process itself; never listed as a binding target.
- `CLAUDE_COMMAND` (default `claude`) — command to launch Claude Code.
- `CCMUX_CLAUDE_PROJECTS_PATH` / `CLAUDE_CONFIG_DIR` — where Claude
  Code writes its JSONL transcripts.
- `MONITOR_POLL_INTERVAL` (default `0.5` seconds) — fast-loop tick.
- `CCMUX_DIR` (default `~/.ccmux`) — state-file root.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .util import ccmux_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux).
# Kept minimal in the backend; frontends append their own (bot tokens, API
# keys) to their local copy.
SENSITIVE_ENV_VARS: set[str] = set()


class Config:
    """Backend configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccmux_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        # Reserved tmux session name — holds the bot process itself.
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "__ccmux__")

        # Claude command to run in new windows
        self.claude_command = os.getenv("CLAUDE_COMMAND", "claude")

        self.bindings_file = self.config_dir / "window_bindings.json"
        self.monitor_state_file = self.config_dir / "claude_monitor.json"

        custom_projects_path = os.getenv("CCMUX_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "0.5"))

        # Emit user-typed messages as ClaudeMessage events. Frontends
        # often prefer to drop these (they echoed them already).
        self.show_user_messages = (
            os.getenv("CCMUX_SHOW_USER_MESSAGES", "true").lower() != "false"
        )

        logger.debug(
            "Config initialized: dir=%s, tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.tmux_session_name,
            self.claude_projects_path,
        )


config = Config()

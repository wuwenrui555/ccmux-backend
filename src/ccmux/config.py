"""Backend configuration — reads env vars and exposes a `config` singleton.

Only the Claude-tmux backend's own settings live here. Frontend packages
ship their own `config.py` for bot tokens, allow-lists, etc.

Loads `settings.env` only — backend has no secrets, so it never reads
`.env` (which is reserved for sensitive values consumed by frontends).
Loading priority: cwd `settings.env` > `$CCMUX_DIR/settings.env`
(default `~/.ccmux/settings.env`). Reads:

- `CCMUX_TMUX_SESSION_NAME` (default `__ccmux__`) — reserved session
  that holds the bot process itself; never listed as a binding target.
- `CCMUX_CLAUDE_COMMAND` (default `claude`) — command to launch Claude Code.
- `CCMUX_CLAUDE_PROJECTS_PATH` / `CLAUDE_CONFIG_DIR` — where Claude
  Code writes its JSONL transcripts.
- `CCMUX_MONITOR_POLL_INTERVAL` (default `0.5` seconds) — fast-loop tick.
- `CCMUX_DIR` (default `~/.ccmux`) — state-file root. Read from the
  process environment; not loaded from settings.env (chicken-and-egg).
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .util import ccmux_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux).
# Backend has none; frontends maintain their own list.
SENSITIVE_ENV_VARS: set[str] = set()


class Config:
    """Backend configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccmux_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        local_settings = Path("settings.env")
        global_settings = self.config_dir / "settings.env"
        if local_settings.is_file():
            load_dotenv(local_settings)
            logger.debug("Loaded settings from %s", local_settings.resolve())
        if global_settings.is_file():
            load_dotenv(global_settings)
            logger.debug("Loaded settings from %s", global_settings)

        # Reserved tmux session name — holds the bot process itself.
        self.tmux_session_name = os.getenv("CCMUX_TMUX_SESSION_NAME", "__ccmux__")

        # Claude command to run in new windows
        self.claude_command = os.getenv("CCMUX_CLAUDE_COMMAND", "claude")

        self.instances_file = self.config_dir / "claude_instances.json"
        self.monitor_state_file = self.config_dir / "claude_monitor.json"

        custom_projects_path = os.getenv("CCMUX_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(
            os.getenv("CCMUX_MONITOR_POLL_INTERVAL", "0.5")
        )

        logger.debug(
            "Config initialized: dir=%s, tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.tmux_session_name,
            self.claude_projects_path,
        )


config = Config()

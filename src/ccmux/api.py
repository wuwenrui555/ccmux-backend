"""Public API of the ccmux backend.

**Frontends must import from `ccmux.api` only.** Everything reachable
via `from ccmux.<submodule>` is internal and may change without notice.

Four groups:

1. Protocol + lifecycle — the abstract contract and its default implementation.
2. Data types — event payloads, query returns, composition inputs.
3. Parsers — pane text and JSONL parsing functions/classes.
4. Composition helpers — singleton, naming utility.
"""

from __future__ import annotations

# --- 1. Protocol + lifecycle ----------------------------------------------

from .backend import (
    Backend,
    TmuxOps,
    ClaudeOps,
    DefaultBackend,
    get_default_backend,
    set_default_backend,
)

# --- 2. Data types --------------------------------------------------------

# State family + parser primitives — from external package
from claude_code_state import (
    Blocked,
    BlockedUI,
    ClaudeState,
    Dead,
    Idle,
    InteractiveUIContent,
    Working,
    extract_interactive_content,
    parse_status_line,
)

# Message family (emitted via on_message)
from .claude_transcript_parser import ClaudeMessage, TranscriptParser

# Claude session summary (frontend `/list` and similar commands)
from .claude_files import ClaudeSession

# Event log: hook-written append-only log + reader projection.
# Source of truth for "which Claude is in tmux session X" since v4.0.0.
from .event_log import (
    ClaudeInfo,
    CurrentClaudeBinding,
    EventLogReader,
    EventLogWriter,
    HookEvent,
    TmuxInfo,
)

# Bash / usage scrapers — backend-local
from .pane_extras import UsageInfo, extract_bash_output, parse_usage_output

# Query returns
from .tmux import TmuxWindow

# Composition inputs
from .tmux import TmuxSessionRegistry

# --- 4. Composition helpers -----------------------------------------------

from .tmux import tmux_registry, sanitize_session_name


__all__ = [
    # Protocol + lifecycle
    "Backend",
    "TmuxOps",
    "ClaudeOps",
    "DefaultBackend",
    "get_default_backend",
    "set_default_backend",
    # State family
    "ClaudeState",
    "Working",
    "Idle",
    "Blocked",
    "Dead",
    "BlockedUI",
    # Message / transcript
    "ClaudeMessage",
    "TranscriptParser",
    # Session summary
    "ClaudeSession",
    # Event log
    "CurrentClaudeBinding",
    "EventLogReader",
    "EventLogWriter",
    "HookEvent",
    "TmuxInfo",
    "ClaudeInfo",
    # Composition inputs
    "TmuxSessionRegistry",
    # Parser surfaces
    "InteractiveUIContent",
    "UsageInfo",
    "extract_bash_output",
    "extract_interactive_content",
    "parse_status_line",
    "parse_usage_output",
    # Query types
    "TmuxWindow",
    # Composition helpers
    "tmux_registry",
    "sanitize_session_name",
]

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

# Event payloads (pushed to on_message / on_status callbacks)
from .claude_transcript_parser import ClaudeMessage
from .status_monitor import WindowStatus
from .tmux_pane_parser import InteractiveUIContent, UsageInfo

# Query returns (from Protocol method calls)
from .window_bindings import WindowBinding, ClaudeSession
from .tmux import TmuxWindow

# Composition inputs (constructed by frontend, passed to DefaultBackend)
from .window_bindings import WindowBindings
from .tmux import TmuxSessionRegistry

# --- 3. Parsers -----------------------------------------------------------

from .tmux_pane_parser import (
    extract_bash_output,
    extract_interactive_content,
    parse_status_line,
    parse_usage_output,
)
from .claude_transcript_parser import TranscriptParser

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
    # Event payloads
    "ClaudeMessage",
    "WindowStatus",
    "InteractiveUIContent",
    "UsageInfo",
    # Query returns
    "WindowBinding",
    "ClaudeSession",
    "TmuxWindow",
    # Composition inputs
    "WindowBindings",
    "TmuxSessionRegistry",
    # Parsers
    "extract_bash_output",
    "extract_interactive_content",
    "parse_status_line",
    "parse_usage_output",
    "TranscriptParser",
    # Composition helpers
    "tmux_registry",
    "sanitize_session_name",
]

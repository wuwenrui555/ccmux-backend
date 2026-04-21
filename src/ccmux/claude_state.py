"""ClaudeState sealed union — the four-case classification of a running
Claude Code instance.

A running Claude Code process is always in exactly one of:

- ``Working`` — the input chrome is rendered and a spinner with ``…``
  is running above it. Carries the status text (e.g. ``Thinking… (3s)``).
- ``Idle`` — the input chrome is rendered, no spinner. Claude is waiting
  for the user's next message. Carries no payload.
- ``Blocked`` — the input chrome has been replaced by a blocking UI
  (permission prompt, AskUserQuestion, ExitPlanMode, Settings, etc.).
  Carries the matched ``BlockedUI`` variant and the extracted content.
- ``Dead`` — the tmux window is alive but the ``claude`` process is no
  longer foreground in its pane. Triggers auto-resume. Carries no payload.

Union is sealed: adding a fifth case requires editing every match site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BlockedUI(StrEnum):
    """Which blocking UI is currently covering the input chrome.

    Mirrors the names in ``parser_config.UI_PATTERNS`` so parser
    classification and state classification share vocabulary.
    """

    PERMISSION_PROMPT = "permission_prompt"
    ASK_USER_QUESTION = "ask_user_question"
    EXIT_PLAN_MODE = "exit_plan_mode"
    BASH_APPROVAL = "bash_approval"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    SETTINGS = "settings"


@dataclass(frozen=True)
class Working:
    """Spinner running above the input chrome."""

    status_text: str

    def __post_init__(self) -> None:
        if not self.status_text:
            raise ValueError("Working.status_text must be non-empty")
        if "…" not in self.status_text:
            raise ValueError(
                "Working.status_text must contain an ellipsis '…' (U+2026); "
                "completion summaries like 'Worked for 56s' are not running states"
            )


@dataclass(frozen=True)
class Idle:
    """Input chrome present, no spinner."""


@dataclass(frozen=True)
class Blocked:
    """Input chrome replaced by a blocking UI."""

    ui: BlockedUI
    content: str


@dataclass(frozen=True)
class Dead:
    """tmux window alive, ``claude`` process not foreground."""


ClaudeState = Working | Idle | Blocked | Dead

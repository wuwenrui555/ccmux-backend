"""Optional user overrides for Claude-Code-coupled parser constants.

Loaded once at module import from `$CCMUX_DIR/parser_config.json`. When
the file is absent, `OVERRIDES` is empty and every consuming parser
sees its built-in constants unchanged. See
`docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Moved here from `tmux_pane_parser` so `parser_overrides` can
    construct instances from JSON without a circular import.
    Extraction scans patterns top-down; the first matching top anchor
    starts a region that closes at the first matching bottom anchor.
    """

    name: str
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2


@dataclass(frozen=True)
class ParserOverrides:
    """User-supplied overrides for the five Claude-Code-coupled constants."""

    ui_patterns: list[UIPattern] = field(default_factory=list)
    skippable_overlays: tuple[re.Pattern[str], ...] = ()
    status_spinners: frozenset[str] = frozenset()
    simple_summary_fields: dict[str, str] = field(default_factory=dict)
    bare_summary_tools: frozenset[str] = frozenset()


def load() -> ParserOverrides:
    """Load overrides from `$CCMUX_DIR/parser_config.json`. Placeholder."""
    return ParserOverrides()


OVERRIDES: ParserOverrides = load()

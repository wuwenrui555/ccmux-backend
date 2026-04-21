"""Future single source of truth for Claude-Code-coupled parser constants.

Promotes the five built-in datasets from tmux_pane_parser and
claude_transcript_parser up to a single module, merges them with
optional user overrides from ``$CCMUX_DIR/parser_config.json``, and
exposes the composed public constants so parser modules can eventually
import them directly rather than re-deriving the composition locally.

Public constants (post-merge):
  - UI_PATTERNS
  - STATUS_SPINNERS
  - SKIPPABLE_OVERLAY_PATTERNS
  - SIMPLE_SUMMARY_FIELDS
  - BARE_SUMMARY_TOOLS
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .util import ccmux_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

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

    ui_patterns: tuple[UIPattern, ...] = ()
    skippable_overlays: tuple[re.Pattern[str], ...] = ()
    status_spinners: frozenset[str] = frozenset()
    simple_summary_fields: dict[str, str] = field(default_factory=dict)
    bare_summary_tools: frozenset[str] = frozenset()
    status_skip_glyphs: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Built-in datasets (copied verbatim from the parser modules)
# ---------------------------------------------------------------------------

_BUILTIN_UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            # CC 2.1.x+ /config UI: tab bar replaces the old "Settings:" header.
            # Active tab highlighting is invisible in plain pane capture — we
            # just anchor on the fixed word order.
            re.compile(r"^\s*Status\s+Config\s+Usage\s+Stats\s*$"),
            # /model picker (both pre- and post-2.1)
            re.compile(r"^\s*Select model"),
            # Legacy (pre-2.1.x) — kept for older CC installs
            re.compile(r"^\s*Settings:.*tab to cycle"),
        ),
        bottom=(
            # cancel/exit/clear/close span tab variants across CC versions
            re.compile(r"Esc to (cancel|exit|clear|close)"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]

# Spinner characters Claude Code uses in its status line
_BUILTIN_STATUS_SPINNERS: frozenset[str] = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])

# Overlay lines that may sit between the real spinner and the chrome.
_BUILTIN_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Session-rating modal (CC 2.1.x+).
    re.compile(r"^\s*●\s*How is Claude doing this session\?"),
    re.compile(r"^\s*1:\s*Bad\b"),
)

# Glyphs that identify TodoWrite / task-checklist lines. When `parse_status_line`
# scans upward from the chrome separator, lines whose first non-space character
# is in this set are treated the same as blanks and overlays: skipped without
# bailing and without consuming the bail-budget. This lets the spinner be found
# above arbitrarily long task lists (subagent runs, multi-step plans).
_BUILTIN_STATUS_SKIP_GLYPHS: frozenset[str] = frozenset(
    ["◼", "◻", "☐", "☒", "✔", "✓"]
)

# One-field tools: tool name -> input dict key to surface as summary.
_BUILTIN_SIMPLE_SUMMARY_FIELDS: dict[str, str] = {
    "Read": "file_path",
    "Write": "file_path",
    "Bash": "command",
    "Grep": "pattern",
    "Task": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "Skill": "skill",
}

# Tools that intentionally render as bare "**Name**" with no argument.
_BUILTIN_BARE_SUMMARY_TOOLS: frozenset[str] = frozenset({"TodoRead", "ExitPlanMode"})


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CONFIG_FILENAME = "parser_config.json"
_SUPPORTED_SCHEMA_VERSION = 1


def _config_path() -> Path:
    return ccmux_dir() / _CONFIG_FILENAME


def _parse_ui_patterns(raw: object) -> tuple[UIPattern, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[UIPattern] = []
    for index, entry in enumerate(raw):
        try:
            if not isinstance(entry, dict):
                raise TypeError("entry is not a JSON object")
            name = entry.get("name")
            top_src = entry.get("top")
            bottom_src = entry.get("bottom")
            if not isinstance(name, str):
                raise KeyError("name")
            if not isinstance(top_src, list):
                raise KeyError("top")
            if not isinstance(bottom_src, list):
                raise KeyError("bottom")
            top = tuple(re.compile(p) for p in top_src if isinstance(p, str))
            bottom = tuple(re.compile(p) for p in bottom_src if isinstance(p, str))
            min_gap_raw = entry.get("min_gap", 2)
            min_gap = min_gap_raw if isinstance(min_gap_raw, int) else 2
            out.append(UIPattern(name=name, top=top, bottom=bottom, min_gap=min_gap))
        except (KeyError, TypeError, re.error) as e:
            logger.warning("ui_patterns[%d] skipped: %s", index, e)
    return tuple(out)


def _parse_regex_list(raw: object) -> tuple[re.Pattern[str], ...]:
    if not isinstance(raw, list):
        return ()
    compiled: list[re.Pattern[str]] = []
    for src in raw:
        if isinstance(src, str):
            compiled.append(re.compile(src))
    return tuple(compiled)


def _parse_chars(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(s for s in raw if isinstance(s, str) and len(s) == 1)


def _parse_str_dict(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _parse_str_set(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(s for s in raw if isinstance(s, str))


def load() -> ParserOverrides:
    """Load overrides from ``$CCMUX_DIR/parser_config.json``.

    Returns ``ParserOverrides()`` (empty) on any top-level failure:
    missing file, unreadable file, invalid JSON, unknown schema
    version. Per-section failures are handled inside the ``_parse_*``
    helpers so one bad section never poisons the others.
    """
    path = _config_path()
    if not path.exists():
        return ParserOverrides()
    try:
        text = path.read_text()
    except OSError as e:
        logger.warning("could not read parser_config.json: %s", e)
        return ParserOverrides()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("invalid JSON in parser_config.json: %s", e)
        return ParserOverrides()
    if not isinstance(raw, dict):
        logger.warning("parser_config.json top-level must be an object")
        return ParserOverrides()
    version = raw.get("$schema_version")
    if version != _SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "parser_config.json $schema_version=%r unsupported "
            "(expected %d); ignoring file",
            version,
            _SUPPORTED_SCHEMA_VERSION,
        )
        return ParserOverrides()
    overrides = ParserOverrides(
        ui_patterns=_parse_ui_patterns(raw.get("ui_patterns")),
        skippable_overlays=_parse_regex_list(raw.get("skippable_overlays")),
        status_spinners=_parse_chars(raw.get("status_spinners")),
        simple_summary_fields=_parse_str_dict(raw.get("simple_summary_fields")),
        bare_summary_tools=_parse_str_set(raw.get("bare_summary_tools")),
        status_skip_glyphs=_parse_chars(raw.get("status_skip_glyphs")),
    )
    logger.info(
        "loaded parser_config.json: "
        "ui_patterns=%d, skippable_overlays=%d, status_spinners=%d, "
        "simple_summary_fields=%d, bare_summary_tools=%d, status_skip_glyphs=%d",
        len(overrides.ui_patterns),
        len(overrides.skippable_overlays),
        len(overrides.status_spinners),
        len(overrides.simple_summary_fields),
        len(overrides.bare_summary_tools),
        len(overrides.status_skip_glyphs),
    )
    return overrides


# ---------------------------------------------------------------------------
# Shadow helpers (unit-testable, called at module bottom)
# ---------------------------------------------------------------------------


def _log_ui_pattern_shadows(
    user: Iterable[UIPattern],
    builtin: Iterable[UIPattern],
) -> None:
    """Emit INFO for each user UIPattern whose name matches a built-in entry."""
    builtin_names = {p.name for p in builtin}
    for p in user:
        if p.name in builtin_names:
            logger.info("shadowing built-in ui_pattern '%s'", p.name)


def _log_summary_field_shadows(
    user: Mapping[str, str],
    builtin: Mapping[str, str],
) -> None:
    """Emit INFO for each user simple_summary_fields key that shadows a built-in."""
    for key, value in user.items():
        if key in builtin:
            logger.info(
                "shadowing built-in simple_summary_field '%s' (%s -> %s)",
                key,
                builtin[key],
                value,
            )


# ---------------------------------------------------------------------------
# Module-level composition
# ---------------------------------------------------------------------------

_OVERRIDES: ParserOverrides = load()

# User ui_patterns prepend so they match first; built-ins are fallback.
UI_PATTERNS: list[UIPattern] = list(_OVERRIDES.ui_patterns) + _BUILTIN_UI_PATTERNS

# Sets take the union.
STATUS_SPINNERS: frozenset[str] = _BUILTIN_STATUS_SPINNERS | _OVERRIDES.status_spinners

# User skippable_overlays prepend (same reasoning as ui_patterns).
SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    _OVERRIDES.skippable_overlays + _BUILTIN_SKIPPABLE_OVERLAY_PATTERNS
)

# Dict merge: user wins per key.
SIMPLE_SUMMARY_FIELDS: dict[str, str] = {
    **_BUILTIN_SIMPLE_SUMMARY_FIELDS,
    **_OVERRIDES.simple_summary_fields,
}

# Set union.
BARE_SUMMARY_TOOLS: frozenset[str] = (
    _BUILTIN_BARE_SUMMARY_TOOLS | _OVERRIDES.bare_summary_tools
)

# Set union — glyphs to skip (free, no bail) between spinner and chrome.
STATUS_SKIP_GLYPHS: frozenset[str] = (
    _BUILTIN_STATUS_SKIP_GLYPHS | _OVERRIDES.status_skip_glyphs
)

# ---------------------------------------------------------------------------
# Shadow detection
# ---------------------------------------------------------------------------

_log_ui_pattern_shadows(_OVERRIDES.ui_patterns, _BUILTIN_UI_PATTERNS)
_log_summary_field_shadows(
    _OVERRIDES.simple_summary_fields, _BUILTIN_SIMPLE_SUMMARY_FIELDS
)

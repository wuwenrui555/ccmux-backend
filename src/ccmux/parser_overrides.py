"""Optional user overrides for Claude-Code-coupled parser constants.

Loaded once at module import from `$CCMUX_DIR/parser_config.json`. When
the file is absent, `OVERRIDES` is empty and every consuming parser
sees its built-in constants unchanged. See
`docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import ccmux_dir

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

    ui_patterns: tuple[UIPattern, ...] = ()
    skippable_overlays: tuple[re.Pattern[str], ...] = ()
    status_spinners: frozenset[str] = frozenset()
    simple_summary_fields: dict[str, str] = field(default_factory=dict)
    bare_summary_tools: frozenset[str] = frozenset()


_CONFIG_FILENAME = "parser_config.json"
_SUPPORTED_SCHEMA_VERSION = 1

# Names / keys present in the built-in parser constants. Kept in sync
# with tmux_pane_parser.UI_PATTERNS and
# claude_transcript_parser.TranscriptParser._BUILTIN_SIMPLE_SUMMARY_FIELDS.
# Used only for INFO-level shadow detection; merge semantics live in
# the consuming modules.
_BUILTIN_UI_PATTERN_NAMES: frozenset[str] = frozenset(
    {
        "ExitPlanMode",
        "AskUserQuestion",
        "PermissionPrompt",
        "BashApproval",
        "RestoreCheckpoint",
        "Settings",
    }
)
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
            top = tuple(
                re.compile(p) for p in top_src if isinstance(p, str)
            )
            bottom = tuple(
                re.compile(p) for p in bottom_src if isinstance(p, str)
            )
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


def _log_shadows(overrides: ParserOverrides) -> None:
    for pattern in overrides.ui_patterns:
        if pattern.name in _BUILTIN_UI_PATTERN_NAMES:
            logger.info(
                "shadowing built-in ui_pattern '%s'", pattern.name
            )
    for key, value in overrides.simple_summary_fields.items():
        if key in _BUILTIN_SIMPLE_SUMMARY_FIELDS:
            logger.info(
                "shadowing built-in simple_summary_field '%s' (%s -> %s)",
                key,
                _BUILTIN_SIMPLE_SUMMARY_FIELDS[key],
                value,
            )


def load() -> ParserOverrides:
    """Load overrides from `$CCMUX_DIR/parser_config.json`.

    Returns `ParserOverrides()` (empty) on any top-level failure:
    missing file, unreadable file, invalid JSON, unknown schema
    version. Per-section failures are handled inside the `_parse_*`
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
    )
    logger.info(
        "loaded parser_config.json: "
        "ui_patterns=%d, skippable_overlays=%d, status_spinners=%d, "
        "simple_summary_fields=%d, bare_summary_tools=%d",
        len(overrides.ui_patterns),
        len(overrides.skippable_overlays),
        len(overrides.status_spinners),
        len(overrides.simple_summary_fields),
        len(overrides.bare_summary_tools),
    )
    _log_shadows(overrides)
    return overrides


OVERRIDES: ParserOverrides = load()

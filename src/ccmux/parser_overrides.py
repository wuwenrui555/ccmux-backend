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


def _config_path() -> Path:
    return ccmux_dir() / _CONFIG_FILENAME


def _parse_ui_patterns(raw: object) -> tuple[UIPattern, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[UIPattern] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        top_src = entry.get("top")
        bottom_src = entry.get("bottom")
        if (
            not isinstance(name, str)
            or not isinstance(top_src, list)
            or not isinstance(bottom_src, list)
        ):
            continue
        top = tuple(re.compile(p) for p in top_src if isinstance(p, str))
        bottom = tuple(re.compile(p) for p in bottom_src if isinstance(p, str))
        min_gap = entry.get("min_gap", 2)
        if not isinstance(min_gap, int):
            min_gap = 2
        out.append(UIPattern(name=name, top=top, bottom=bottom, min_gap=min_gap))
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
    """Load overrides from `$CCMUX_DIR/parser_config.json`."""
    path = _config_path()
    if not path.exists():
        return ParserOverrides()
    raw = json.loads(path.read_text())
    if raw.get("$schema_version") != _SUPPORTED_SCHEMA_VERSION:
        return ParserOverrides()
    return ParserOverrides(
        ui_patterns=_parse_ui_patterns(raw.get("ui_patterns")),
        skippable_overlays=_parse_regex_list(raw.get("skippable_overlays")),
        status_spinners=_parse_chars(raw.get("status_spinners")),
        simple_summary_fields=_parse_str_dict(raw.get("simple_summary_fields")),
        bare_summary_tools=_parse_str_set(raw.get("bare_summary_tools")),
    )


OVERRIDES: ParserOverrides = load()

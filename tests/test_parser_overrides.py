"""Tests for ccmux.parser_overrides."""

import json
import logging
from pathlib import Path

import pytest

from ccmux import parser_overrides as po
from ccmux.parser_overrides import OVERRIDES, ParserOverrides, UIPattern


def test_overrides_singleton_is_parser_overrides_instance() -> None:
    assert isinstance(OVERRIDES, ParserOverrides)


def test_ui_pattern_is_defined_in_parser_overrides() -> None:
    # UIPattern lives here to break the tmux_pane_parser circular import.
    assert UIPattern.__module__ == "ccmux.parser_overrides"


@pytest.fixture
def isolated_ccmux_dir(monkeypatch, tmp_path):
    """Point CCMUX_DIR at a tmp dir and return the resulting Path."""
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    return tmp_path


def test_load_returns_empty_when_file_missing(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="ccmux.parser_overrides")
    result = po.load()
    assert result.ui_patterns == ()
    assert result.skippable_overlays == ()
    assert result.status_spinners == frozenset()
    assert result.simple_summary_fields == {}
    assert result.bare_summary_tools == frozenset()
    assert caplog.records == []  # no warning for absent file


def _write_config(dir_: Path, data: dict) -> Path:
    path = dir_ / "parser_config.json"
    path.write_text(json.dumps(data))
    return path


def test_load_parses_all_sections(isolated_ccmux_dir: Path) -> None:
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {
                    "name": "Custom",
                    "top": ["^Custom top"],
                    "bottom": ["^Custom bottom"],
                    "min_gap": 3,
                }
            ],
            "skippable_overlays": ["^\\s*skipme"],
            "status_spinners": ["★"],
            "simple_summary_fields": {"NewTool": "arg"},
            "bare_summary_tools": ["AnotherTool"],
        },
    )

    result = po.load()

    # ui_patterns (tuple per Task 1 fix)
    assert len(result.ui_patterns) == 1
    p = result.ui_patterns[0]
    assert p.name == "Custom"
    assert p.top[0].pattern == "^Custom top"
    assert p.bottom[0].pattern == "^Custom bottom"
    assert p.min_gap == 3

    # skippable_overlays
    assert len(result.skippable_overlays) == 1
    assert result.skippable_overlays[0].pattern == "^\\s*skipme"

    # status_spinners
    assert result.status_spinners == frozenset({"★"})

    # simple_summary_fields
    assert result.simple_summary_fields == {"NewTool": "arg"}

    # bare_summary_tools
    assert result.bare_summary_tools == frozenset({"AnotherTool"})

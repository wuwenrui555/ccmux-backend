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

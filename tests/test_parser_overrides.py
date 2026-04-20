"""Tests for ccmux.parser_overrides."""

from ccmux.parser_overrides import OVERRIDES, ParserOverrides, UIPattern


def test_overrides_singleton_is_parser_overrides_instance() -> None:
    assert isinstance(OVERRIDES, ParserOverrides)


def test_ui_pattern_is_defined_in_parser_overrides() -> None:
    # UIPattern lives here to break the tmux_pane_parser circular import.
    assert UIPattern.__module__ == "ccmux.parser_overrides"

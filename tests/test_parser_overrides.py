"""Tests for ccmux.parser_overrides."""

from ccmux.parser_overrides import OVERRIDES, ParserOverrides, UIPattern


def test_overrides_singleton_is_parser_overrides_instance() -> None:
    assert isinstance(OVERRIDES, ParserOverrides)


def test_ui_pattern_is_exported() -> None:
    # Public re-export surface: parser modules import UIPattern from here.
    assert UIPattern is not None

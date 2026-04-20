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


def test_invalid_regex_in_ui_pattern_skips_entry(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="ccmux.parser_overrides")
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "Bad", "top": ["("], "bottom": ["ok"]},
                {"name": "Good", "top": ["^ok$"], "bottom": ["^ok$"]},
            ],
        },
    )
    result = po.load()
    names = [p.name for p in result.ui_patterns]
    assert names == ["Good"]
    assert any("ui_patterns[0]" in r.message for r in caplog.records)


def test_missing_required_field_skipped(isolated_ccmux_dir: Path) -> None:
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "OnlyName"},  # missing top/bottom
                {"name": "Good", "top": ["^x$"], "bottom": ["^y$"]},
            ],
        },
    )
    result = po.load()
    assert [p.name for p in result.ui_patterns] == ["Good"]


def test_non_single_char_spinner_rejected(isolated_ccmux_dir: Path) -> None:
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "status_spinners": ["✻", "abc", "", "✽"],
        },
    )
    result = po.load()
    assert result.status_spinners == frozenset({"✻", "✽"})


def test_wrong_section_type_scoped_to_that_section(
    isolated_ccmux_dir: Path,
) -> None:
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": {"not": "a list"},
            "bare_summary_tools": ["StillHere"],
        },
    )
    result = po.load()
    assert result.ui_patterns == ()
    assert result.bare_summary_tools == frozenset({"StillHere"})


def test_malformed_json_falls_back_with_warning(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="ccmux.parser_overrides")
    (isolated_ccmux_dir / "parser_config.json").write_text("{not-json")
    result = po.load()
    assert result == po.ParserOverrides()
    assert any("invalid JSON" in r.message for r in caplog.records)


def test_unknown_schema_version_falls_back_with_warning(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="ccmux.parser_overrides")
    _write_config(isolated_ccmux_dir, {"$schema_version": 99})
    result = po.load()
    assert result == po.ParserOverrides()
    assert any("schema_version" in r.message and "99" in r.message for r in caplog.records)


def test_permission_error_falls_back_with_warning(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="ccmux.parser_overrides")
    path = _write_config(isolated_ccmux_dir, {"$schema_version": 1})
    path.chmod(0o000)
    try:
        result = po.load()
    finally:
        path.chmod(0o600)  # restore so teardown can clean up
    assert result == po.ParserOverrides()
    assert any("parser_config" in r.message for r in caplog.records)


# Names / keys known to exist in the built-in parser constants.
# Kept here so the shadow tests don't depend on importing the parser
# modules (which we haven't refactored yet).
_BUILTIN_UI_PATTERN_NAMES = {
    "ExitPlanMode",
    "AskUserQuestion",
    "PermissionPrompt",
    "BashApproval",
    "RestoreCheckpoint",
    "Settings",
}
_BUILTIN_SIMPLE_SUMMARY_KEYS = {
    "Read",
    "Write",
    "Bash",
    "Grep",
    "Task",
    "WebFetch",
    "WebSearch",
    "Skill",
}


def test_shadow_ui_pattern_logs_info(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_overrides")
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "ExitPlanMode", "top": ["^x$"], "bottom": ["^y$"]},
            ],
        },
    )
    po.load()
    assert any(
        "shadow" in r.message.lower() and "ExitPlanMode" in r.message
        for r in caplog.records
    )


def test_shadow_simple_summary_field_logs_info_with_values(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_overrides")
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "simple_summary_fields": {"Read": "new_field"},
        },
    )
    po.load()
    assert any(
        "Read" in r.message
        and "file_path" in r.message
        and "new_field" in r.message
        for r in caplog.records
    )


def test_no_shadow_no_info_log(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_overrides")
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "BrandNewUI", "top": ["^x$"], "bottom": ["^y$"]},
            ],
            "simple_summary_fields": {"BrandNewTool": "arg"},
        },
    )
    po.load()
    shadow_records = [r for r in caplog.records if "shadow" in r.message.lower()]
    assert shadow_records == []


def test_successful_load_emits_summary_info(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_overrides")
    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "BrandNewUI", "top": ["^x$"], "bottom": ["^y$"]},
            ],
            "skippable_overlays": ["^overlay"],
            "status_spinners": [],
            "simple_summary_fields": {"BrandNewTool": "arg"},
            "bare_summary_tools": [],
        },
    )
    po.load()
    summaries = [r for r in caplog.records if "loaded parser_config" in r.message]
    assert len(summaries) == 1
    msg = summaries[0].message
    assert "ui_patterns=1" in msg
    assert "skippable_overlays=1" in msg
    assert "status_spinners=0" in msg
    assert "simple_summary_fields=1" in msg
    assert "bare_summary_tools=0" in msg


def test_missing_file_emits_no_summary(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_overrides")
    po.load()
    assert not any("loaded parser_config" in r.message for r in caplog.records)

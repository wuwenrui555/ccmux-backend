"""Tests for pane_extras — bash-output and /usage modal scrapers."""

from ccmux.pane_extras import (
    extract_bash_output,
    parse_usage_output,
    _strip_pane_chrome,
)


# ── _strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert _strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert _strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert _strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert _strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")


# ── parse_usage_output ───────────────────────────────────────────────────


class TestParseUsageOutput:
    def test_new_usage_tab_ui(self):
        """Regression: /usage content is now under the new /config tab bar,
        no longer wrapped in a `Settings: ... Usage` header."""
        pane = (
            "──────\n"
            "   Status   Config   Usage   Stats\n"
            "\n"
            "  Current session\n"
            "  ██                                    4% used\n"
            "  Resets 1am (America/New_York)\n"
            "\n"
            "  Current week (all models)\n"
            "  ████                                  8% used\n"
            "  Resets Apr 23, 3pm (America/New_York)\n"
            "\n"
            "  Esc to cancel\n"
        )
        result = parse_usage_output(pane)
        assert result is not None
        joined = "\n".join(result.parsed_lines)
        assert "Current session" in joined
        assert "4% used" in joined
        assert "Resets" in joined
        assert "██" not in joined
        assert "████" not in joined
        assert "Esc to cancel" not in joined
        assert "Status   Config" not in joined
        # Section breaks from the pane must survive as blank lines
        assert "" in result.parsed_lines
        # No leading/trailing blanks, no back-to-back blanks
        assert result.parsed_lines[0] != ""
        assert result.parsed_lines[-1] != ""
        for i in range(len(result.parsed_lines) - 1):
            assert not (
                result.parsed_lines[i] == "" and result.parsed_lines[i + 1] == ""
            )

    def test_returns_none_when_not_usage_modal(self):
        pane = "some conversation text\n\nmore text\n"
        assert parse_usage_output(pane) is None

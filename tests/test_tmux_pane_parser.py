"""Tests for tmux_pane_parser — regex-based detection of Claude Code UI elements."""

import pytest

from ccmux.tmux_pane_parser import (
    extract_bash_output,
    extract_interactive_content,
    parse_status_line,
    parse_usage_output,
    _has_input_chrome,
    _strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task…", "Working on task…"),
            ("✻", "  Reading file…  ", "Reading file…"),
            ("✽", "Thinking deeply…", "Thinking deeply…"),
            ("✶", "Analyzing code…", "Analyzing code…"),
            ("✳", "Processing input…", "Processing input…"),
            ("✢", "Building project…", "Building project…"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        """Running status: spinner char + text with `…` ellipsis.

        Only running statuses should be surfaced; completion summaries
        (`Worked for 56s`) still use a spinner char but lack the
        ellipsis and are handled by the next parametrize block.
        """
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        ("spinner", "rest"),
        [
            ("✻", "Worked for 56s"),
            ("✻", "Cogitated for 1m 25s"),
            ("·", "Brewed for 2m 16s"),
            ("✻", "Churned for 50s"),
            ("·", "Sautéed for 45s"),
        ],
    )
    def test_completion_lines_return_none(self, spinner: str, rest: str, chrome: str):
        """Completion lines (spinner + `Verbed for X` without ellipsis)
        must NOT be treated as running status. Otherwise the Telegram
        frontend creates a throwaway `Worked for 56s` bubble that then
        gets eaten by the user's next message via
        `_convert_status_to_content`, losing the timeline marker.

        Required-`…` rule is the cheap, stable discriminator: every
        observed running status contains U+2026, every completion
        summary uses the `for <duration>` past-tense form without it.
        """
        pane = f"some output\n{spinner} {rest}\n{chrome}"
        assert parse_status_line(pane) is None

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work…\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work…\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work…"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py…"

    def test_rating_modal_does_not_hide_spinner(self, chrome: str):
        """When CC's "How is Claude doing this session?" modal appears
        between the real spinner and chrome, the spinner must still be
        detected — skipping the modal lines, not bailing on them."""
        pane = (
            "some output\n"
            "✽ Osmosing… (33s · ↑ 188 tokens)\n"
            "\n"
            "● How is Claude doing this session? (optional)\n"
            "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
            f"{chrome}"
        )
        assert parse_status_line(pane) == "Osmosing… (33s · ↑ 188 tokens)"

    def test_unknown_overlay_still_bails(self, chrome: str):
        """A non-spinner, non-overlay line between spinner and chrome
        still short-circuits — we don't want to silently swallow future
        UI additions and return stale spinners."""
        pane = f"✽ Old spinner\nSome unknown modal line\n{chrome}"
        assert parse_status_line(pane) is None

    def test_skips_through_task_checklist(self, chrome: str):
        """TodoWrite checklist between spinner and chrome must not bail."""
        pane = (
            "some output\n"
            "✶ Exploring project context… (2m · ↑ 1.3k tokens)\n"
            "  ◼ Explore ccmux-telegram project context\n"
            "  ◻ Ask clarifying questions on both UX issues\n"
            "  ◻ Propose approaches\n"
            "\n"
            f"{chrome}"
        )
        assert (
            parse_status_line(pane) == "Exploring project context… (2m · ↑ 1.3k tokens)"
        )

    def test_skips_through_long_task_checklist(self, chrome: str):
        """A checklist larger than the legacy 10-line scan window still
        finds the spinner — checklist lines are free-skip, not counted."""
        tasks = "\n".join(f"  ◻ Task {i}" for i in range(20))
        pane = f"output\n✽ Running…\n{tasks}\n\n{chrome}"
        assert parse_status_line(pane) == "Running…"

    def test_all_checklist_glyphs_are_skippable(self, chrome: str):
        """Each glyph in STATUS_SKIP_GLYPHS must be free-skip."""
        from ccmux.parser_config import STATUS_SKIP_GLYPHS

        for glyph in STATUS_SKIP_GLYPHS:
            pane = f"✽ Running…\n  {glyph} Some task\n{chrome}"
            assert parse_status_line(pane) == "Running…", f"failed for glyph {glyph!r}"

    def test_checklist_only_no_spinner_returns_none(self, chrome: str):
        """Pane with task list but no spinner must not false-positive."""
        pane = "output\n  ◼ Task 1\n  ◻ Task 2\n" + chrome
        assert parse_status_line(pane) is None

    def test_unknown_text_after_checklist_still_bails(self, chrome: str):
        """Unknown text above checklist still short-circuits scan."""
        pane = (
            "✽ Very old spinner that should NOT be returned\n"
            "some rogue line that is not known chrome\n"
            "  ◼ Task 1\n"
            "  ◻ Task 2\n"
            f"{chrome}"
        )
        assert parse_status_line(pane) is None

    def test_checklist_plus_rating_modal(self, chrome: str):
        """Checklist and overlay modal can co-exist between spinner and chrome."""
        pane = (
            "✶ Working on stuff…\n"
            "  ◼ Task 1\n"
            "  ◻ Task 2\n"
            "\n"
            "● How is Claude doing this session? (optional)\n"
            "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
            f"{chrome}"
        )
        assert parse_status_line(pane) == "Working on stuff…"


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    def test_settings_new_config_ui(self):
        """Regression: `/config` was redesigned in CC 2025.x — tab bar
        `Status Config Usage Stats` replaces the old `Settings: press tab
        to cycle` header, and footer uses `Esc to clear`."""
        pane = (
            "──────\n"
            "   Status   Config   Usage   Stats\n"
            "\n"
            "  Auto-compact                               true\n"
            "  Show tips                                  true\n"
            "  Verbose output                             false\n"
            "\n"
            "  Type to filter · Enter/↓ to select · ↑ to tabs · Esc to clear\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Auto-compact" in result.content
        assert "Config" in result.content

    def test_settings_new_usage_ui(self):
        """Regression: `/usage` is now a Usage tab inside the new /config UI."""
        pane = (
            "──────\n"
            "   Status   Config   Usage   Stats\n"
            "\n"
            "  Current session\n"
            "  ██                                    4% used\n"
            "  Resets 1am (America/New_York)\n"
            "\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Current session" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None

    def test_input_chrome_present_suppresses_ui_detection(self):
        """Regression: if Claude's input chrome (`────\n❯\n────\nstatusbar`)
        is still at the pane bottom, Claude is working or idle, not blocked
        on a UI. Any UI-looking text above is scrollback (e.g., a user
        paste of an earlier prompt) and must NOT match. Real UIs always
        replace the input chrome, verified empirically on
        PermissionPrompt/AskUserQuestion/ExitPlanMode panes.
        """
        chrome = "─" * 60
        pane = (
            # scrollback with UI-looking text
            "  Do you want to proceed?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "  Esc to cancel\n"
            "\n"
            "● Some later assistant output\n"
            "\n"
            # live input chrome below
            + chrome
            + "\n"
            + "❯ \n"
            + chrome
            + "\n"
            + "  [Opus 4.7] 43% | projects\n"
            + "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        assert extract_interactive_content(pane) is None

    def test_scrollback_exit_plan_text_with_chrome_returns_none(self):
        """Specific regression for the ExitPlanMode pattern being matched
        from a user's pasted transcript when the live pane is idle."""
        chrome = "─" * 60
        pane = (
            "  Would you like to proceed?\n"
            "  ─────\n"
            "  Yes     No\n"
            "  ─────\n"
            "  ctrl-g to edit in vim\n"
            + chrome
            + "\n"
            + "❯ \n"
            + chrome
            + "\n"
            + "  [Opus 4.7] 10% | projects\n"
        )
        assert extract_interactive_content(pane) is None

    def test_ask_user_multi_tab_realistic_pane_matches(self):
        """Real AskUserQuestion pane has NO input chrome — the UI replaces
        it entirely. This test uses the realistic pane shape confirmed
        empirically with a live trigger."""
        pane = (
            "─" * 60 + "\n"
            "←  ☐ 最爱色  ☐ 界面配色  ☐ 避开色  ✔ Submit  →\n"
            "\n"
            "你最喜欢哪种颜色？\n"
            "\n"
            "❯ 1. 蓝色\n"
            "     冷静、沉稳\n"
            "  2. 红色\n"
            "     热情、醒目\n"
            "─" * 60 + "\n"
            "  3. Chat about this\n"
            "\n"
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "蓝色" in result.content

    def test_permission_prompt_realistic_pane_matches(self):
        """Real PermissionPrompt pane also has NO input chrome."""
        pane = (
            "  Reading 1 file… (ctrl+o to expand)\n"
            "  ⎿  /etc/hosts\n"
            "\n"
            "─" * 60 + "\n"
            " Read file\n"
            "\n"
            "  Read(/etc/hosts)\n"
            "\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. Yes, allow reading from etc/ during this session\n"
            "   3. No\n"
            "\n"
            " Esc to cancel · Tab to amend\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content


class TestHasInputChrome:
    """The `────\n❯\n────\nstatusbar` sandwich at the pane bottom marks
    Claude as "working or idle" — it is absent when a blocking UI has
    taken over the input region (permission prompt, AskUserQuestion,
    ExitPlanMode, Settings)."""

    def test_normal_working_pane_has_chrome(self):
        chrome = "─" * 60
        lines = (
            "some output\n"
            "✶ Sublimating… (32s · ↓ 224 tokens)\n"
            + chrome
            + "\n"
            + "❯ \n"
            + chrome
            + "\n"
            + "  [Opus 4.7] 33% | wenruiwu\n"
        ).split("\n")
        assert _has_input_chrome(lines) is True

    def test_idle_pane_has_chrome(self):
        chrome = "─" * 60
        lines = (
            "● Worked for 1m 52s\n"
            + chrome
            + "\n"
            + "❯ \n"
            + chrome
            + "\n"
            + "  [Opus 4.7] 10% | wenruiwu\n"
        ).split("\n")
        assert _has_input_chrome(lines) is True

    def test_input_chrome_with_user_typed_text(self):
        """User can be typing into the ❯ prompt — still chrome."""
        chrome = "─" * 60
        lines = (
            "● Some output\n"
            + chrome
            + "\n"
            + "❯ half-typed message\n"
            + chrome
            + "\n"
            + "  [Opus 4.7] ...\n"
        ).split("\n")
        assert _has_input_chrome(lines) is True

    def test_permission_prompt_pane_has_no_chrome(self):
        lines = (
            "  Reading 1 file…\n"
            "─" * 60 + "\n"
            " Read file\n"
            "\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
            " Esc to cancel\n"
        ).split("\n")
        assert _has_input_chrome(lines) is False

    def test_ask_user_pane_has_no_chrome(self):
        lines = (
            "─" * 60 + "\n"
            "←  ☐ Option A  ☐ Option B  ✔ Submit  →\n"
            "\n"
            "Pick one\n"
            "\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "─" * 60 + "\n"
            "Enter to select · Esc to cancel\n"
        ).split("\n")
        assert _has_input_chrome(lines) is False

    def test_exit_plan_pane_has_no_chrome(self):
        lines = (
            "─" * 60 + "\n"
            " Ready to code?\n"
            " Here is Claude's plan:\n"
            " ...plan body...\n"
            " Would you like to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
            " ctrl-g to edit in VS Code\n"
        ).split("\n")
        assert _has_input_chrome(lines) is False

    def test_empty_pane_has_no_chrome(self):
        assert _has_input_chrome([]) is False

    def test_short_dash_line_not_counted_as_chrome(self):
        """Internal UI dividers (< _CHROME_MIN_LEN) must not satisfy the
        chrome predicate."""
        lines = (
            "● Some output\n"
            + "─" * 8
            + "\n"
            + "❯ 1. option\n"
            + "─" * 8
            + "\n"
            + "Esc to cancel\n"
        ).split("\n")
        assert _has_input_chrome(lines) is False


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


# ── Pattern-drift warning ──────────────────────────────────────────────────


class TestPatternDriftWarning:
    """`extract_interactive_content` warns once when prompt-like content is
    present but no UI_PATTERNS matched — the canary for Claude Code UI
    upgrades that need a regex update."""

    def setup_method(self):
        from ccmux import tmux_pane_parser as T

        T._unmatched_prompt_fingerprints.clear()

    def test_matched_pane_does_not_warn(self, caplog, sample_pane_exit_plan):
        import logging

        with caplog.at_level(logging.WARNING, logger="ccmux.drift"):
            extract_interactive_content(sample_pane_exit_plan)
        assert not any("no UI_PATTERNS matched" in r.message for r in caplog.records)

    def test_unmatched_but_prompt_like_warns(self, caplog):
        import logging

        # Reworded confirmation line — no current pattern catches it, but
        # the "Esc to cancel" signal makes it clearly a prompt.
        pane = (
            "body text\n"
            "Ready to proceed with this action?\n"  # hypothetical reworded top
            "\n"
            "  1. Yes\n"
            "  2. No\n"
            "Esc to cancel\n"
        )
        with caplog.at_level(logging.WARNING, logger="ccmux.drift"):
            result = extract_interactive_content(pane)
        assert result is None
        assert any("no UI_PATTERNS matched" in r.message for r in caplog.records)

    def test_unmatched_prompt_dedups_by_fingerprint(self, caplog):
        import logging

        pane = "noise\n\nReady to proceed?\nEsc to cancel\n"
        with caplog.at_level(logging.WARNING, logger="ccmux.drift"):
            extract_interactive_content(pane)
            extract_interactive_content(pane)
            extract_interactive_content(pane)
        warnings = [r for r in caplog.records if "no UI_PATTERNS matched" in r.message]
        assert len(warnings) == 1

    def test_no_prompt_signals_no_warning(self, caplog):
        import logging

        pane = "just output lines\nnothing prompt-like here\n"
        with caplog.at_level(logging.WARNING, logger="ccmux.drift"):
            extract_interactive_content(pane)
        assert not any("no UI_PATTERNS matched" in r.message for r in caplog.records)

    def test_bypass_permissions_status_line_does_not_warn(self, caplog):
        """Regression: "(shift+tab to cycle)" in the bypass-permissions hint
        is present on every pane and must not trigger drift warnings."""
        import logging

        pane = (
            "some regular claude output\n"
            "more output\n"
            "─" * 30 + "\n"
            "❯\n"
            "─" * 30 + "\n"
            "  [Opus 4.7] █░░░░░░░░░ 13% | projects\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        with caplog.at_level(logging.WARNING, logger="ccmux.drift"):
            extract_interactive_content(pane)
        assert not any("no UI_PATTERNS matched" in r.message for r in caplog.records)


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


def test_user_ui_pattern_is_prepended_and_matches_first(monkeypatch, tmp_path) -> None:
    import importlib
    import json

    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    (tmp_path / "parser_config.json").write_text(
        json.dumps(
            {
                "$schema_version": 1,
                "ui_patterns": [
                    {
                        "name": "ExitPlanMode",
                        "top": ["^CUSTOM TOP$"],
                        "bottom": ["^CUSTOM BOTTOM$"],
                    }
                ],
            }
        )
    )

    from ccmux import parser_config

    importlib.reload(parser_config)

    names = [p.name for p in parser_config.UI_PATTERNS]
    assert names[0] == "ExitPlanMode"
    # 2 ExitPlanMode (user-prepended) + however many built-in share that name.
    # At least 2; exact count depends on whether built-in has variants.
    assert names.count("ExitPlanMode") >= 2
    assert parser_config.UI_PATTERNS[0].top[0].pattern == "^CUSTOM TOP$"

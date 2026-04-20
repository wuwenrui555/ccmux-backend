"""Tests for PaneState / derive_pane_state.

The pane's state is derived from two pieces of pane text evidence:

1. Is Claude's input chrome (`────\\n❯\\n────\\nstatusbar` sandwich)
   still at the pane bottom?  Absence implies a blocking UI has taken
   over the input region.
2. Is there a spinner line above that chrome?  Presence implies Claude
   is actively generating output.

Together they produce a four-state classification used by the frontend
to decide between sending status updates, edits, or UI prompts.
"""

from __future__ import annotations

import pytest

from ccmux.status_monitor import PaneState, WindowStatus, derive_pane_state


class TestPaneStateEnum:
    def test_has_four_values(self) -> None:
        assert {PaneState.UNKNOWN, PaneState.WORKING, PaneState.IDLE, PaneState.BLOCKED}

    def test_str_values(self) -> None:
        assert str(PaneState.WORKING) == "working"
        assert str(PaneState.IDLE) == "idle"
        assert str(PaneState.BLOCKED) == "blocked"
        assert str(PaneState.UNKNOWN) == "unknown"


class TestDerivePaneState:
    def _chrome(self) -> str:
        return "─" * 60

    @pytest.fixture
    def working_pane(self) -> str:
        return (
            "some output\n"
            "✶ Sublimating… (32s · ↓ 224 tokens · thought for 19s)\n"
            + self._chrome()
            + "\n"
            + "❯ \n"
            + self._chrome()
            + "\n"
            + "  [Opus 4.7] 33% | wenruiwu\n"
        )

    @pytest.fixture
    def idle_pane(self) -> str:
        return (
            "● Worked for 1m 52s\n"
            + self._chrome()
            + "\n"
            + "❯ \n"
            + self._chrome()
            + "\n"
            + "  [Opus 4.7] 10% | wenruiwu\n"
        )

    @pytest.fixture
    def permission_pane(self) -> str:
        return (
            "─" * 60 + "\n"
            " Read file\n"
            "\n"
            "  Read(/etc/hosts)\n"
            "\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
            " Esc to cancel · Tab to amend\n"
        )

    @pytest.fixture
    def ask_user_pane(self) -> str:
        return (
            "─" * 60 + "\n"
            "←  ☐ 最爱色  ☐ 界面配色  ☐ 避开色  ✔ Submit  →\n"
            "\n"
            "你最喜欢哪种颜色？\n"
            "\n"
            "❯ 1. 蓝色\n"
            "  2. 红色\n"
            "─" * 60 + "\n"
            "Enter to select · Esc to cancel\n"
        )

    def test_working_pane_is_working(self, working_pane):
        assert derive_pane_state(working_pane, "Sublimating… (32s · …)") == PaneState.WORKING

    def test_idle_pane_is_idle(self, idle_pane):
        assert derive_pane_state(idle_pane, None) == PaneState.IDLE

    def test_permission_prompt_is_blocked(self, permission_pane):
        assert derive_pane_state(permission_pane, None) == PaneState.BLOCKED

    def test_ask_user_pane_is_blocked(self, ask_user_pane):
        assert derive_pane_state(ask_user_pane, None) == PaneState.BLOCKED

    def test_empty_pane_is_unknown(self):
        assert derive_pane_state("", None) == PaneState.UNKNOWN

    def test_status_text_without_chrome_stays_blocked(self, permission_pane):
        """Even if the status parser somehow reports a spinner, absence of
        input chrome still wins — the UI is what the user is looking at."""
        assert derive_pane_state(permission_pane, "ghost spinner") == PaneState.BLOCKED


class TestWindowStatusDefault:
    def test_pane_state_defaults_to_unknown(self) -> None:
        """Callers that omit pane_state (pre-existing fixtures, etc.) keep
        working — UNKNOWN is the neutral fallback."""
        s = WindowStatus(
            window_id="@1",
            window_exists=True,
            pane_captured=False,
            status_text=None,
            interactive_ui=None,
        )
        assert s.pane_state == PaneState.UNKNOWN

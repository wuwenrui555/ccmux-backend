"""Tests for the ClaudeState sealed union + BlockedUI enum."""

import pytest

from ccmux.claude_state import (
    BlockedUI,
    Blocked,
    ClaudeState,
    Dead,
    Idle,
    Working,
)


class TestBlockedUI:
    def test_has_six_members(self) -> None:
        assert {m.value for m in BlockedUI} == {
            "permission_prompt",
            "ask_user_question",
            "exit_plan_mode",
            "bash_approval",
            "restore_checkpoint",
            "settings",
        }

    def test_is_strenum(self) -> None:
        assert BlockedUI.PERMISSION_PROMPT == "permission_prompt"


class TestWorking:
    def test_accepts_valid_status_text(self) -> None:
        w = Working(status_text="Thinking… (3s)")
        assert w.status_text == "Thinking… (3s)"

    def test_rejects_empty_status_text(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Working(status_text="")

    def test_rejects_missing_ellipsis(self) -> None:
        with pytest.raises(ValueError, match="ellipsis"):
            Working(status_text="Thinking for 3s")

    def test_is_frozen(self) -> None:
        w = Working(status_text="Reading…")
        with pytest.raises(Exception):
            w.status_text = "Writing…"  # type: ignore[misc]


class TestIdle:
    def test_has_no_payload(self) -> None:
        i = Idle()
        assert i == Idle()

    def test_is_frozen(self) -> None:
        i = Idle()
        with pytest.raises(Exception):
            i.foo = 1  # type: ignore[attr-defined]


class TestBlocked:
    def test_carries_ui_and_content(self) -> None:
        b = Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="Do you want to proceed?")
        assert b.ui is BlockedUI.PERMISSION_PROMPT
        assert b.content == "Do you want to proceed?"


class TestDead:
    def test_has_no_payload(self) -> None:
        assert Dead() == Dead()


class TestExhaustiveMatch:
    def test_match_covers_every_variant(self) -> None:
        """All four variants must be reachable via structural pattern match."""
        seen: set[str] = set()
        states: list[ClaudeState] = [
            Working(status_text="Thinking…"),
            Idle(),
            Blocked(ui=BlockedUI.SETTINGS, content="Status | Config | Usage"),
            Dead(),
        ]
        for s in states:
            match s:
                case Working(text):
                    seen.add(f"working:{text}")
                case Idle():
                    seen.add("idle")
                case Blocked(ui, content):
                    seen.add(f"blocked:{ui}:{content}")
                case Dead():
                    seen.add("dead")
        assert len(seen) == 4

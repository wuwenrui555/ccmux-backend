"""Tests for ccmux.state_log — JSONL state-log writer."""

from __future__ import annotations

import json

import pytest

from claude_code_state import Blocked, BlockedUI, Dead, Idle, Working

from ccmux.state_log import _serialize_state


class TestSerializeState:
    def test_working(self) -> None:
        s = Working(status_text="Thinking… (3s)")
        assert _serialize_state(s) == {
            "type": "Working",
            "status_text": "Thinking… (3s)",
        }

    def test_idle(self) -> None:
        s = Idle()
        assert _serialize_state(s) == {"type": "Idle"}

    def test_blocked(self) -> None:
        s = Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="Allow Bash...")
        assert _serialize_state(s) == {
            "type": "Blocked",
            "ui": "permission_prompt",
            "content": "Allow Bash...",
        }

    def test_dead(self) -> None:
        s = Dead()
        assert _serialize_state(s) == {"type": "Dead"}

    def test_serialized_is_json_safe(self) -> None:
        for s in (
            Working(status_text="Thinking… (3s)"),
            Idle(),
            Blocked(ui=BlockedUI.ASK_USER_QUESTION, content="Pick one"),
            Dead(),
        ):
            d = _serialize_state(s)
            json.dumps(d)

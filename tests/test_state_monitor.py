"""Tests for StateMonitor — classifies a ClaudeInstance into ClaudeState."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ccmux.claude_instance import ClaudeInstance
from ccmux.claude_state import (
    Blocked,
    BlockedUI,
    ClaudeState,
    Dead,
    Idle,
    Working,
)
from ccmux.state_monitor import StateMonitor


# ---- Fakes ----------------------------------------------------------------


@dataclass
class _FakeTmux:
    """Stub tmux session registry: only what StateMonitor reads."""

    panes: dict[str, str]
    window_ids_present: set[str]
    pane_commands: dict[str, str]  # window_id -> current foreground command

    def get_by_window_id(self, wid: str):
        if wid not in self.window_ids_present:
            return None
        return self

    async def find_window_by_id(self, wid: str):
        if wid not in self.window_ids_present:
            return None
        return _FakeWindow(
            window_id=wid, pane_current_command=self.pane_commands.get(wid, "claude")
        )

    async def capture_pane(self, wid: str) -> str:
        return self.panes.get(wid, "")

    def get_or_create(self, session_name: str):
        return self


@dataclass
class _FakeWindow:
    window_id: str
    pane_current_command: str


@dataclass
class _FakeRegistry:
    instances: list[ClaudeInstance]
    raw: dict[str, Any]

    def all(self):
        return iter(self.instances)

    async def load(self) -> None:
        pass


@pytest.fixture
def chrome() -> str:
    return "─────────────────────────────\n❯\n─────\nstatusbar"


# ---- Tests ----------------------------------------------------------------


class TestClassification:
    @pytest.mark.asyncio
    async def test_working_from_spinner(self, chrome: str) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        pane = f"some output\n✽ Thinking… (3s)\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={"a": {"window_id": "@1"}})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert seen[0][0] == "a"
        assert isinstance(seen[0][1], Working)
        assert seen[0][1].status_text == "Thinking… (3s)"

    @pytest.mark.asyncio
    async def test_idle_from_chrome_no_spinner(self, chrome: str) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        pane = f"just some scrollback\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert isinstance(seen[0][1], Idle)

    @pytest.mark.asyncio
    async def test_blocked_from_missing_chrome(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        pane = (
            "Edit /tmp/foo\n"
            "Do you want to proceed?\n"
            "1. Yes\n"
            "2. No\n"
            "Esc to cancel\n"
        )
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert isinstance(seen[0][1], Blocked)
        assert seen[0][1].ui is BlockedUI.PERMISSION_PROMPT


class TestSkipRules:
    @pytest.mark.asyncio
    async def test_skip_when_window_missing(self) -> None:
        inst = ClaudeInstance(
            instance_id="a", window_id="@gone", session_id="s", cwd="/"
        )
        tmux = _FakeTmux(panes={}, window_ids_present=set(), pane_commands={})
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []

    @pytest.mark.asyncio
    async def test_skip_when_pane_capture_empty(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        tmux = _FakeTmux(
            panes={"@1": ""},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []


class TestSlowTickDead:
    @pytest.mark.asyncio
    async def test_dead_when_claude_not_foreground(self) -> None:
        inst = ClaudeInstance(
            instance_id="a", window_id="@1", session_id="s", cwd="/home/u"
        )
        tmux = _FakeTmux(
            panes={"@1": "irrelevant"},
            window_ids_present={"@1"},
            pane_commands={"@1": "zsh"},
        )
        reg = _FakeRegistry(
            instances=[inst],
            raw={"a": {"window_id": "@1", "session_id": "s", "cwd": "/home/u"}},
        )
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        deads = [(iid, s) for iid, s in seen if isinstance(s, Dead)]
        assert len(deads) == 1
        assert deads[0][0] == "a"

    @pytest.mark.asyncio
    async def test_slow_tick_silent_when_claude_alive(self) -> None:
        inst = ClaudeInstance(
            instance_id="a", window_id="@1", session_id="s", cwd="/home/u"
        )
        tmux = _FakeTmux(
            panes={"@1": "irrelevant"},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(
            instances=[inst],
            raw={"a": {"window_id": "@1", "session_id": "s", "cwd": "/home/u"}},
        )
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        assert seen == []

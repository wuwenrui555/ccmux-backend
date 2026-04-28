"""Tests for StateMonitor — classifies a CurrentClaudeBinding into ClaudeState."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any  # used in _FakeTmuxWithFallback below

import pytest

from ccmux.claude_state import (
    Blocked,
    BlockedUI,
    ClaudeState,
    Dead,
    Idle,
    Working,
)
from ccmux.event_log import CurrentClaudeBinding
from ccmux.state_monitor import StateMonitor


def _binding(
    name: str = "a",
    window_id: str = "@1",
    *,
    cwd: str = "/",
    claude_session_id: str = "s",
) -> CurrentClaudeBinding:
    return CurrentClaudeBinding(
        tmux_session_name=name,
        window_id=window_id,
        claude_session_id=claude_session_id,
        cwd=cwd,
        transcript_path="",
        last_seen=datetime(2026, 4, 28, tzinfo=timezone.utc),
    )


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
class _FakeReader:
    bindings: list[CurrentClaudeBinding]

    def all_alive(self) -> list[CurrentClaudeBinding]:
        return list(self.bindings)


@pytest.fixture
def chrome() -> str:
    return "─────────────────────────────\n❯\n─────\nstatusbar"


# ---- Tests ----------------------------------------------------------------


class TestClassification:
    @pytest.mark.asyncio
    async def test_working_from_spinner(self, chrome: str) -> None:
        b = _binding()
        pane = f"some output\n✽ Thinking… (3s)\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert seen[0][0] == "a"
        assert isinstance(seen[0][1], Working)
        assert seen[0][1].status_text == "Thinking… (3s)"

    @pytest.mark.asyncio
    async def test_idle_from_chrome_no_spinner(self, chrome: str) -> None:
        b = _binding()
        pane = f"just some scrollback\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert isinstance(seen[0][1], Idle)

    @pytest.mark.asyncio
    async def test_blocked_from_missing_chrome(self) -> None:
        b = _binding()
        pane = "Edit /tmp/foo\nDo you want to proceed?\n1. Yes\n2. No\nEsc to cancel\n"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert isinstance(seen[0][1], Blocked)
        assert seen[0][1].ui is BlockedUI.PERMISSION_PROMPT
        assert seen[0][1].content  # non-empty; exact text depends on parser

    @pytest.mark.asyncio
    async def test_skips_when_chrome_absent_and_no_ui_pattern_matches(self) -> None:
        """Chrome is gone but no known UIPattern matches the pane text.
        This is the 'drift' case — state_monitor must skip (no callback)
        rather than emit a guess."""
        b = _binding()
        pane = "Just some garbled text that matches no UI pattern\nLine two\n"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []


class TestSkipRules:
    @pytest.mark.asyncio
    async def test_skip_when_window_missing(self) -> None:
        b = _binding(window_id="@gone")
        tmux = _FakeTmux(panes={}, window_ids_present=set(), pane_commands={})
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []

    @pytest.mark.asyncio
    async def test_skip_when_pane_capture_empty(self) -> None:
        b = _binding()
        tmux = _FakeTmux(
            panes={"@1": ""},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []


class TestSlowTickDead:
    @pytest.mark.asyncio
    async def test_dead_when_claude_not_foreground(self) -> None:
        b = _binding(cwd="/home/u")
        tmux = _FakeTmux(
            panes={"@1": "irrelevant"},
            window_ids_present={"@1"},
            pane_commands={"@1": "zsh"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        deads = [(iid, s) for iid, s in seen if isinstance(s, Dead)]
        assert len(deads) == 1
        assert deads[0][0] == "a"

    @pytest.mark.asyncio
    async def test_slow_tick_silent_when_claude_alive(self) -> None:
        b = _binding(cwd="/home/u")
        tmux = _FakeTmux(
            panes={"@1": "irrelevant"},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        assert seen == []

    @pytest.mark.asyncio
    async def test_dead_via_get_or_create_fallback(self) -> None:
        """When tmux_registry.get_by_window_id returns None (cache miss),
        the probe falls back to get_or_create(tmux_session_name) and still
        emits Dead when the pane's foreground process is not claude."""
        b = _binding(name="__ccmux__", cwd="/home/u")

        @dataclass
        class _FakeTmuxWithFallback:
            """get_by_window_id returns None; get_or_create returns a working
            session whose find_window_by_id reports pane_current_command='zsh'."""

            session: Any

            def get_by_window_id(self, wid: str):
                return None  # simulate cache miss

            def get_or_create(self, session_name: str):
                return self.session

        @dataclass
        class _FakeSession:
            async def find_window_by_id(self, wid: str):
                return _FakeWindow(window_id=wid, pane_current_command="zsh")

            async def capture_pane(self, wid: str) -> str:
                return ""

        tmux = _FakeTmuxWithFallback(session=_FakeSession())
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        deads = [(iid, s) for iid, s in seen if isinstance(s, Dead)]
        assert len(deads) == 1
        assert deads[0][0] == "__ccmux__"

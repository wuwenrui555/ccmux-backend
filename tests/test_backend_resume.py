"""Auto-resume verification + circuit breaker in DefaultBackend.

These tests cover ``_verify_resume`` (does the resumed window actually
contain claude?) and ``_try_resume``'s failure-counting / circuit-breaker
behaviour. The bug they pin down: a runaway loop that creates a fresh
tmux window every slow tick when ``claude --resume`` repeatedly fails to
keep claude alive.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux.backend import MAX_RESUME_FAILURES, DefaultBackend
from ccmux.event_log import CurrentClaudeBinding, EventLogReader


def _binding(name: str = "proj", window_id: str = "@5") -> CurrentClaudeBinding:
    return CurrentClaudeBinding(
        tmux_session_name=name,
        window_id=window_id,
        claude_session_id="uuid",
        cwd="/tmp",
        transcript_path="",
        last_seen=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )


def _make_backend(tmp_path) -> DefaultBackend:
    tmux_registry = MagicMock()
    reader = EventLogReader(tmp_path / "claude_events.jsonl")
    return DefaultBackend(
        tmux_registry=tmux_registry,
        message_monitor=MagicMock(),
        event_reader=reader,
    )


def _window(cmd: str) -> MagicMock:
    w = MagicMock()
    w.pane_current_command = cmd
    return w


# ---------------------------------------------------------------------------
# _verify_resume
# ---------------------------------------------------------------------------


class TestVerifyResume:
    @pytest.mark.asyncio
    async def test_true_when_window_becomes_claude(self, tmp_path):
        backend = _make_backend(tmp_path)
        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(
            side_effect=[_window("zsh"), _window("claude")]
        )
        assert await backend._verify_resume(tm, "@9", timeout=0.5, poll=0.01) is True

    @pytest.mark.asyncio
    async def test_true_when_window_becomes_node(self, tmp_path):
        # Claude Code is a node CLI; pane_current_command is "node" in
        # practice, not "claude". Accept either.
        backend = _make_backend(tmp_path)
        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(return_value=_window("node"))
        assert await backend._verify_resume(tm, "@9", timeout=0.5, poll=0.01) is True

    @pytest.mark.asyncio
    async def test_false_when_window_stays_shell(self, tmp_path):
        backend = _make_backend(tmp_path)
        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(return_value=_window("zsh"))
        assert await backend._verify_resume(tm, "@9", timeout=0.05, poll=0.01) is False

    @pytest.mark.asyncio
    async def test_false_when_window_disappears(self, tmp_path):
        backend = _make_backend(tmp_path)
        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(return_value=None)
        assert await backend._verify_resume(tm, "@9", timeout=0.05, poll=0.01) is False


# ---------------------------------------------------------------------------
# _try_resume + circuit breaker
# ---------------------------------------------------------------------------


class TestTryResumeCircuitBreaker:
    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, tmp_path, monkeypatch):
        backend = _make_backend(tmp_path)
        backend._resume_failures["proj"] = 2  # pretend we've failed twice

        tm = MagicMock()
        tm.create_window = AsyncMock(return_value=(True, "ok", "name", "@9"))
        backend._tmux_registry.get_or_create = MagicMock(return_value=tm)
        backend.event_reader._current["proj"] = _binding("proj")
        monkeypatch.setattr(backend, "_verify_resume", AsyncMock(return_value=True))

        await backend._try_resume("proj")
        assert "proj" not in backend._resume_failures

    @pytest.mark.asyncio
    async def test_verify_failure_increments_count(self, tmp_path, monkeypatch):
        backend = _make_backend(tmp_path)
        tm = MagicMock()
        tm.create_window = AsyncMock(return_value=(True, "ok", "name", "@9"))
        backend._tmux_registry.get_or_create = MagicMock(return_value=tm)
        backend.event_reader._current["proj"] = _binding("proj")
        monkeypatch.setattr(backend, "_verify_resume", AsyncMock(return_value=False))

        await backend._try_resume("proj")
        assert backend._resume_failures["proj"] == 1

        await backend._try_resume("proj")
        assert backend._resume_failures["proj"] == 2

    @pytest.mark.asyncio
    async def test_create_window_failure_increments_count(self, tmp_path, monkeypatch):
        backend = _make_backend(tmp_path)
        tm = MagicMock()
        tm.create_window = AsyncMock(return_value=(False, "boom", "", ""))
        backend._tmux_registry.get_or_create = MagicMock(return_value=tm)
        backend.event_reader._current["proj"] = _binding("proj")
        # _verify_resume should NOT be reached when create_window fails;
        # fail loudly if it is.
        monkeypatch.setattr(
            backend,
            "_verify_resume",
            AsyncMock(side_effect=AssertionError("should not be called")),
        )

        await backend._try_resume("proj")
        assert backend._resume_failures["proj"] == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_after_max_failures(
        self, tmp_path, monkeypatch
    ):
        backend = _make_backend(tmp_path)
        backend._resume_failures["proj"] = MAX_RESUME_FAILURES

        tm = MagicMock()
        tm.create_window = AsyncMock(return_value=(True, "ok", "name", "@9"))
        backend._tmux_registry.get_or_create = MagicMock(return_value=tm)
        backend.event_reader._current["proj"] = _binding("proj")

        await backend._try_resume("proj")
        tm.create_window.assert_not_awaited()
        # counter not bumped further while breaker is tripped
        assert backend._resume_failures["proj"] == MAX_RESUME_FAILURES

    @pytest.mark.asyncio
    async def test_concurrent_call_skipped(self, tmp_path, monkeypatch):
        # Existing _resuming guard behaviour stays intact.
        backend = _make_backend(tmp_path)
        backend._resuming.add("proj")

        tm = MagicMock()
        tm.create_window = AsyncMock(return_value=(True, "ok", "name", "@9"))
        backend._tmux_registry.get_or_create = MagicMock(return_value=tm)
        backend.event_reader._current["proj"] = _binding("proj")

        await backend._try_resume("proj")
        tm.create_window.assert_not_awaited()
        # Counter unchanged: a re-entrant skip is not a failure.
        assert "proj" not in backend._resume_failures

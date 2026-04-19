"""Tests for `WindowRegistry.verify_all` — backend-only liveness pass.

Covers the slow-loop contract: for every entry in `window_bindings.json`,
probe tmux liveness and Claude-session freshness, auto-resume mismatched
Claude sessions, and write the verdict into `_window_alive` (readable
via `is_window_alive(window_id)`).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.liveness import LivenessChecker
from ccmux.window_bindings import WindowBindings


def _make_entry(window_id: str, session_id: str, cwd: str = "/tmp") -> dict:
    return {"window_id": window_id, "session_id": session_id, "cwd": cwd}


def _write_map(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def session_map(tmp_path):
    map_file = tmp_path / "window_bindings.json"
    _write_map(map_file, {})
    return WindowBindings(map_file=map_file)


@pytest.fixture
def checker(session_map):
    return LivenessChecker(session_map)


@pytest.mark.asyncio
async def test_verify_all_marks_live_window_alive(checker, session_map, tmp_path):
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=MagicMock())
    with patch("ccmux.liveness.tmux_registry.get_by_window_id", return_value=mock_tm):
        await checker.verify_all()

    assert checker.is_alive("@1") is True


@pytest.mark.asyncio
async def test_verify_all_marks_missing_tmux_window_dead(
    checker, session_map, tmp_path
):
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=None)
    with patch("ccmux.liveness.tmux_registry.get_by_window_id", return_value=mock_tm):
        await checker.verify_all()

    assert checker.is_alive("@1") is False


@pytest.mark.asyncio
async def test_verify_all_triggers_resume_on_claude_session_change(
    checker, session_map, tmp_path
):
    """If the tmux window is alive but the recorded session_id differs from
    the one tracked after a presumed /clear, auto-resume is invoked."""
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-original", "/tmp")},
    )

    # Prime `_check_claude_alive` to report False so resume runs. We simulate
    # that by starting with a session_map whose session_id differs from what
    # `_check_claude_alive` expects; easier path: patch the private helper.
    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=MagicMock())
    mock_tm.create_window = AsyncMock(return_value=(True, "ok", "aclf", "@2"))

    calls = []

    async def fake_resume(session_name, claude_session_id):
        calls.append((session_name, claude_session_id))

    with (
        patch("ccmux.liveness.tmux_registry.get_by_window_id", return_value=mock_tm),
        patch.object(checker, "_check_claude", return_value=False),
        patch.object(checker, "_try_resume", side_effect=fake_resume),
    ):
        await checker.verify_all()

    assert calls == [("aclf", "sid-original")]


@pytest.mark.asyncio
async def test_is_window_alive_optimistic_before_first_verify(checker):
    """Unknown windows default to True so the UI doesn't flash dead at startup."""
    assert checker.is_alive("@42") is True


@pytest.mark.asyncio
async def test_is_window_alive_false_for_empty_id(checker):
    assert checker.is_alive("") is False

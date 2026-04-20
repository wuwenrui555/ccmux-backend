"""Tests for `LivenessChecker.verify_all` — backend-only liveness pass.

Covers the slow-loop contract: for every entry in `window_bindings.json`,
probe tmux liveness and Claude-session freshness (via the pane's
foreground process), auto-resume dead Claude sessions, and write the
verdict into `_window_alive` (readable via `is_alive(window_id)`).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux.liveness import LivenessChecker
from ccmux.window_bindings import WindowBindings


def _make_entry(window_id: str, session_id: str, cwd: str = "/tmp") -> dict:
    return {"window_id": window_id, "session_id": session_id, "cwd": cwd}


def _write_map(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _window(cmd: str = "claude") -> MagicMock:
    """Mock TmuxWindow with a given pane_current_command."""
    w = MagicMock()
    w.pane_current_command = cmd
    return w


@pytest.fixture
def session_map(tmp_path):
    map_file = tmp_path / "window_bindings.json"
    _write_map(map_file, {})
    return WindowBindings(map_file=map_file)


@pytest.fixture
def fake_registry():
    reg = MagicMock()
    reg.get_by_window_id = MagicMock()
    reg.get_or_create = MagicMock()
    return reg


@pytest.fixture
def checker(session_map, fake_registry):
    return LivenessChecker(session_map, fake_registry)


@pytest.mark.asyncio
async def test_verify_all_marks_live_window_alive(
    checker, session_map, fake_registry, tmp_path
):
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=_window("claude"))
    fake_registry.get_by_window_id.return_value = mock_tm

    await checker.verify_all()

    assert checker.is_alive("@1") is True


@pytest.mark.asyncio
async def test_verify_all_marks_missing_tmux_window_dead(
    checker, session_map, fake_registry, tmp_path
):
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=None)
    fake_registry.get_by_window_id.return_value = mock_tm

    await checker.verify_all()

    assert checker.is_alive("@1") is False


@pytest.mark.asyncio
async def test_verify_all_marks_shell_pane_as_dead_claude(
    checker, session_map, fake_registry, tmp_path, monkeypatch
):
    """Pane exists but foreground process is a shell → Claude is gone."""
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=_window("zsh"))
    fake_registry.get_by_window_id.return_value = mock_tm
    fake_registry.get_or_create.return_value = mock_tm

    # Stub _try_resume so the slow-loop action is observable without
    # actually spawning a window.
    calls: list[tuple[str, str]] = []

    async def fake_resume(session_name: str, claude_session_id: str) -> None:
        calls.append((session_name, claude_session_id))

    monkeypatch.setattr(checker, "_try_resume", fake_resume)

    await checker.verify_all()

    assert calls == [("aclf", "sid-live")]


@pytest.mark.asyncio
async def test_verify_all_triggers_resume_on_dead_claude(
    checker, session_map, fake_registry, tmp_path, monkeypatch
):
    """Regression: when pane is alive but runs shell (Claude exited),
    auto-resume is invoked with the recorded session_id."""
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-original", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=_window("bash"))
    mock_tm.create_window = AsyncMock(return_value=(True, "ok", "aclf", "@2"))
    fake_registry.get_by_window_id.return_value = mock_tm
    fake_registry.get_or_create.return_value = mock_tm

    calls: list[tuple[str, str]] = []

    async def fake_resume(session_name: str, claude_session_id: str) -> None:
        calls.append((session_name, claude_session_id))

    monkeypatch.setattr(checker, "_try_resume", fake_resume)

    await checker.verify_all()

    assert calls == [("aclf", "sid-original")]


@pytest.mark.asyncio
async def test_is_window_alive_optimistic_before_first_verify(checker):
    """Unknown windows default to True so the UI doesn't flash dead at startup."""
    assert checker.is_alive("@42") is True


@pytest.mark.asyncio
async def test_is_window_alive_false_for_empty_id(checker):
    assert checker.is_alive("") is False


@pytest.mark.asyncio
async def test_env_override_accepts_custom_runtime_name(
    checker, session_map, fake_registry, tmp_path, monkeypatch
):
    """CCMUX_CLAUDE_PROC_NAMES lets ops recover if Claude Code switches
    runtimes (e.g. node → bun) without shipping a new backend."""
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    # Default set rejects "bun" → pane would be flagged as dead.
    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=_window("bun"))
    fake_registry.get_by_window_id.return_value = mock_tm
    fake_registry.get_or_create.return_value = mock_tm

    async def noop_resume(*args, **kwargs):
        pass

    monkeypatch.setattr(checker, "_try_resume", noop_resume)
    monkeypatch.setenv("CCMUX_CLAUDE_PROC_NAMES", "claude,node,bun")

    await checker.verify_all()

    assert checker.is_alive("@1") is True


@pytest.mark.asyncio
async def test_env_override_empty_falls_back_to_default(
    checker, session_map, fake_registry, tmp_path, monkeypatch
):
    """Empty / whitespace env var must not collapse the proc-name set
    to empty (which would mark every pane dead)."""
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )

    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=_window("claude"))
    fake_registry.get_by_window_id.return_value = mock_tm

    monkeypatch.setenv("CCMUX_CLAUDE_PROC_NAMES", "   ,  , ")
    await checker.verify_all()

    assert checker.is_alive("@1") is True


@pytest.mark.asyncio
async def test_verify_all_drops_stale_cache_entries(
    checker, session_map, fake_registry, tmp_path
):
    """Entries for window_ids no longer in bindings are pruned."""
    # First round: bindings contain @1 (alive).
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@1", "sid-live", "/tmp")},
    )
    mock_tm = MagicMock()
    mock_tm.find_window_by_id = AsyncMock(return_value=_window("claude"))
    fake_registry.get_by_window_id.return_value = mock_tm
    await checker.verify_all()
    assert checker.is_alive("@1") is True

    # Second round: bindings now map to @2; @1 should be evicted.
    _write_map(
        tmp_path / "window_bindings.json",
        {"aclf": _make_entry("@2", "sid-live", "/tmp")},
    )
    await checker.verify_all()
    assert checker.is_alive("@2") is True
    # @1 no longer cached → falls back to optimistic default (True).
    # What we really check is that the cache dict itself doesn't grow.
    assert "@1" not in checker._window_alive

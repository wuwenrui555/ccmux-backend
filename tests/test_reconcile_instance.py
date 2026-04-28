"""reconcile_instance algorithm coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ccmux.api import (
    ClaudeInstanceRegistry,
    DefaultBackend,
    TmuxWindow,
    tmux_registry,
)


def _window(wid: str, cmd: str = "claude") -> TmuxWindow:
    return TmuxWindow(window_id=wid, cwd="/Users/wenruiwu", pane_current_command=cmd)


@pytest.fixture
def registry(tmp_path: Path) -> ClaudeInstanceRegistry:
    map_file = tmp_path / "claude_instances.json"
    map_file.write_text("{}")
    return ClaudeInstanceRegistry(map_file=map_file)


@pytest.fixture
def backend(registry: ClaudeInstanceRegistry) -> DefaultBackend:
    return DefaultBackend(tmux_registry=tmux_registry, registry=registry)


@pytest.mark.asyncio
async def test_reconcile_no_claude_windows(backend: DefaultBackend) -> None:
    fake_session = AsyncMock()
    fake_session.list_windows.return_value = [
        _window("@10", cmd="zsh"),  # not Claude
    ]
    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        result = await backend.reconcile_instance("outlook")
    assert result is None


@pytest.mark.asyncio
async def test_reconcile_single_claude_window(
    backend: DefaultBackend,
) -> None:
    fake_session = AsyncMock()
    fake_session.list_windows.return_value = [_window("@22")]
    fake_session.active_pane_id = AsyncMock(return_value="%22")
    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        with patch.object(tmux_registry, "get_by_window_id", return_value=fake_session):
            with patch(
                "ccmux.backend._resolve_via_pane",
                return_value=("aaaa-bbbb", "/Users/wenruiwu"),
                create=True,
            ):
                result = await backend.reconcile_instance("outlook")
    assert result is not None
    assert result.window_id == "@22"
    assert result.session_id == "aaaa-bbbb"


@pytest.mark.asyncio
async def test_reconcile_multiple_session_id_match(
    backend: DefaultBackend, registry: ClaudeInstanceRegistry
) -> None:
    sid_x = "11111111-1111-1111-1111-111111111111"
    sid_y = "22222222-2222-2222-2222-222222222222"
    registry._data["outlook"] = {
        "window_id": "@35",
        "session_id": sid_x,
        "cwd": "/Users/wenruiwu",
    }

    fake_session = AsyncMock()
    fake_session.list_windows.return_value = [_window("@22"), _window("@34")]
    fake_session.active_pane_id = AsyncMock(side_effect=lambda wid: f"%{wid[1:]}")

    def fake_resolve(pane_id: str):
        return {
            "%22": (sid_y, "/Users/wenruiwu"),
            "%34": (sid_x, "/Users/wenruiwu"),
        }[pane_id]

    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        with patch.object(tmux_registry, "get_by_window_id", return_value=fake_session):
            with patch(
                "ccmux.backend._resolve_via_pane",
                side_effect=fake_resolve,
                create=True,
            ):
                result = await backend.reconcile_instance("outlook")

    assert result is not None
    assert result.window_id == "@34"
    assert result.session_id == sid_x


@pytest.mark.asyncio
async def test_reconcile_multiple_falls_back_to_lowest_window_index(
    backend: DefaultBackend,
) -> None:
    fake_session = AsyncMock()
    # list_windows returns in tmux's index order; lowest window_index first.
    fake_session.list_windows.return_value = [_window("@22"), _window("@34")]
    fake_session.active_pane_id = AsyncMock(side_effect=lambda wid: f"%{wid[1:]}")

    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        with patch.object(tmux_registry, "get_by_window_id", return_value=fake_session):
            with patch(
                "ccmux.backend._resolve_via_pane",
                return_value=None,
                create=True,
            ):
                result = await backend.reconcile_instance("outlook")

    assert result is not None
    assert result.window_id == "@22"  # first in list_windows order

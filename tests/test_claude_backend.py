"""Smoke test: backend.start dispatches monitor events to the on_message callback.

Stubs MessageMonitor + StatusMonitor so one poll cycle deterministically
yields one ClaudeMessage and no WindowStatus, and verifies the callbacks
are invoked.  Also confirms backend.stop cancels the internal tasks
cleanly with no leaked pending tasks.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux.backend import DefaultBackend
from ccmux.claude_transcript_parser import ClaudeMessage


@pytest.mark.asyncio
async def test_backend_start_dispatches_on_message():
    stub_message = ClaudeMessage(
        session_id="test-session",
        role="assistant",
        content_type="text",
        text="hello",
        timestamp="2026-04-14T00:00:00Z",
    )

    message_monitor = MagicMock()
    message_monitor.startup_cleanup = MagicMock()
    message_monitor.poll = AsyncMock(side_effect=[[stub_message], []])
    message_monitor.shutdown = MagicMock()

    status_monitor = MagicMock()
    status_monitor.poll = AsyncMock(return_value=[])

    registry = MagicMock()
    windows = MagicMock()
    windows.load = AsyncMock()
    windows.verify_all = AsyncMock()

    backend = DefaultBackend(
        tmux_registry=registry,
        window_bindings=windows,
        message_monitor=message_monitor,
        status_monitor=status_monitor,
        slow_interval=3600.0,
    )

    on_message = AsyncMock()
    on_status = AsyncMock()

    await backend.start(on_message=on_message, on_status=on_status)

    # Let one fast-poll cycle run.
    await asyncio.sleep(0.6)

    await backend.stop()

    on_message.assert_any_await(stub_message)
    message_monitor.startup_cleanup.assert_called_once()
    message_monitor.shutdown.assert_called_once()
    assert backend._fast_task is None
    assert backend._slow_task is None


@pytest.mark.asyncio
async def test_backend_get_window_binding_delegates_to_registry():
    """get_window_binding just passes through to WindowBindings.get."""
    from ccmux.window_bindings import WindowBinding

    registry = MagicMock()
    windows = MagicMock()
    windows.load = AsyncMock()
    windows.verify_all = AsyncMock()
    expected = WindowBinding(
        window_id="@5", session_name="proj", claude_session_id="uuid", cwd="/tmp"
    )
    windows.get = MagicMock(return_value=expected)

    backend = DefaultBackend(tmux_registry=registry, window_bindings=windows)

    result = backend.get_window_binding("@5")

    assert result is expected
    windows.get.assert_called_once_with("@5")


@pytest.mark.asyncio
async def test_backend_is_alive_delegates_to_liveness():
    """is_alive proxies to LivenessChecker.is_alive (pure backend query)."""
    session_map = MagicMock()
    backend = DefaultBackend(tmux_registry=MagicMock(), window_bindings=session_map)
    backend._liveness = MagicMock()
    backend._liveness.is_alive = MagicMock(return_value=True)

    assert backend.is_alive("@9") is True
    backend._liveness.is_alive.assert_called_once_with("@9")


@pytest.mark.asyncio
async def test_backend_send_text_delegates_to_registry():
    """send_text finds the TmuxSession via registry and sends keys."""
    mock_tm = MagicMock()
    mock_window = MagicMock()
    mock_window.window_id = "@3"
    mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
    mock_tm.send_keys = AsyncMock(return_value=True)

    mock_registry = MagicMock()
    mock_registry.get_by_window_id.return_value = mock_tm

    windows = MagicMock()
    windows.load = AsyncMock()
    windows.verify_all = AsyncMock()
    windows.files = MagicMock()

    backend = DefaultBackend(tmux_registry=mock_registry, window_bindings=windows)

    ok, msg = await backend.tmux.send_text("@3", "hello")

    assert ok is True and msg == "Sent"
    mock_tm.send_keys.assert_awaited_once_with("@3", "hello")


@pytest.mark.asyncio
async def test_fake_backend_satisfies_protocol():
    """FakeBackend conforms to the Backend structural Protocol."""
    from ccmux.backend import Backend

    from tests.fake_backend import FakeBackend

    fake = FakeBackend()
    # Static Protocol check: assign to the Protocol type.
    _: Backend = fake  # noqa: F841

    on_msg = AsyncMock()
    on_status = AsyncMock()
    await fake.start(on_msg, on_status)
    assert fake.started is True

    stub = ClaudeMessage(
        session_id="x", role="assistant", content_type="text", text="hi"
    )
    await fake.emit_message(stub)
    on_msg.assert_awaited_once_with(stub)

    await fake.stop()
    assert fake.stopped is True
    # Calls recorded
    call_names = [c[0] for c in fake.calls]
    assert "start" in call_names and "stop" in call_names

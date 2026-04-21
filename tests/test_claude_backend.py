"""Smoke test: backend.start dispatches monitor events to the on_message callback.

Stubs MessageMonitor + StateMonitor so one poll cycle deterministically
yields one (instance_id, ClaudeMessage) pair and no state observations,
and verifies the callbacks are invoked.  Also confirms backend.stop
cancels the internal tasks cleanly with no leaked pending tasks.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux.backend import DefaultBackend
from ccmux.claude_instance import ClaudeInstance
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
    message_monitor.poll = AsyncMock(side_effect=[[("alpha", stub_message)], []])
    message_monitor.shutdown = MagicMock()

    tmux_registry = MagicMock()
    registry = MagicMock()
    registry.load = AsyncMock()
    # StateMonitor iterates registry.all(); we don't want any state emissions
    # in this smoke test.
    registry.all = MagicMock(return_value=iter([]))

    backend = DefaultBackend(
        tmux_registry=tmux_registry,
        registry=registry,
        message_monitor=message_monitor,
        slow_interval=3600.0,
    )

    on_state = AsyncMock()
    on_message = AsyncMock()

    await backend.start(on_state=on_state, on_message=on_message)

    # Let one fast-poll cycle run.
    await asyncio.sleep(0.6)

    await backend.stop()

    on_message.assert_any_await("alpha", stub_message)
    message_monitor.startup_cleanup.assert_called_once()
    message_monitor.shutdown.assert_called_once()
    assert backend._fast_task is None
    assert backend._slow_task is None


@pytest.mark.asyncio
async def test_backend_get_instance_delegates_to_registry():
    """get_instance just passes through to ClaudeInstanceRegistry.get."""
    tmux_registry = MagicMock()
    registry = MagicMock()
    registry.load = AsyncMock()
    expected = ClaudeInstance(
        instance_id="proj", window_id="@5", session_id="uuid", cwd="/tmp"
    )
    registry.get = MagicMock(return_value=expected)

    backend = DefaultBackend(tmux_registry=tmux_registry, registry=registry)

    result = backend.get_instance("proj")

    assert result is expected
    registry.get.assert_called_once_with("proj")


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

    instance_registry = MagicMock()
    instance_registry.load = AsyncMock()

    backend = DefaultBackend(tmux_registry=mock_registry, registry=instance_registry)

    ok, msg = await backend.tmux.send_text("@3", "hello")

    assert ok is True and msg == "Sent"
    mock_tm.send_keys.assert_awaited_once_with("@3", "hello")


@pytest.mark.asyncio
async def test_fake_backend_satisfies_protocol():
    """FakeBackend conforms to the Backend structural Protocol."""
    from ccmux.backend import Backend
    from ccmux.claude_state import Idle

    from tests.fake_backend import FakeBackend

    fake = FakeBackend()
    # Static Protocol check: assign to the Protocol type.
    _: Backend = fake  # noqa: F841

    on_state = AsyncMock()
    on_message = AsyncMock()
    await fake.start(on_state, on_message)
    assert fake.started is True

    stub = ClaudeMessage(
        session_id="x", role="assistant", content_type="text", text="hi"
    )
    await fake.emit_message("alpha", stub)
    on_message.assert_awaited_once_with("alpha", stub)

    await fake.emit_state("alpha", Idle())
    on_state.assert_awaited_once_with("alpha", Idle())

    await fake.stop()
    assert fake.stopped is True
    # Calls recorded
    call_names = [c[0] for c in fake.calls]
    assert "start" in call_names and "stop" in call_names

"""Backend.get_instance delegates to EventLogReader.

v4.0.0: get_instance returns a CurrentClaudeBinding (or None) sourced
exclusively from the event-log projection. There is no legacy fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccmux.event_log import (
    ClaudeInfo,
    EventLogReader,
    EventLogWriter,
    HookEvent,
    TmuxInfo,
)


@pytest.mark.asyncio
async def test_get_instance_returns_reader_row(tmp_path: Path) -> None:
    """A pre-existing event-log line for tmux session 'ccmux' should be
    visible via Backend.get_instance immediately after start().
    """
    log = tmp_path / "claude_events.jsonl"
    EventLogWriter(log).append(
        HookEvent(
            timestamp=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
            hook_event="UserPromptSubmit",
            tmux=TmuxInfo("$1", "ccmux", "@5", "1", "n", "%1", "1"),
            claude=ClaudeInfo("uA", "/p", "/c", "default"),
        )
    )

    from ccmux.api import DefaultBackend, tmux_registry

    reader = EventLogReader(log, poll_interval=0.05)
    backend = DefaultBackend(tmux_registry=tmux_registry, event_reader=reader)

    await reader.start()
    try:
        binding = backend.get_instance("ccmux")
        assert binding is not None
        assert binding.window_id == "@5"
        assert binding.claude_session_id == "uA"
        assert binding.cwd == "/c"
    finally:
        await reader.stop()


@pytest.mark.asyncio
async def test_get_instance_returns_none_when_no_row(tmp_path: Path) -> None:
    """No event-log row for the queried tmux session → None. No fallback."""
    log = tmp_path / "claude_events.jsonl"
    log.touch()

    from ccmux.api import DefaultBackend, tmux_registry

    reader = EventLogReader(log, poll_interval=0.05)
    backend = DefaultBackend(tmux_registry=tmux_registry, event_reader=reader)

    await reader.start()
    try:
        assert backend.get_instance("does-not-exist") is None
    finally:
        await reader.stop()

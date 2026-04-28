"""Smoke tests for the public API surface in `ccmux.api`.

Verifies every symbol in `__all__` is importable and callable; also
covers the Protocol contract via FakeBackend and DefaultBackend's
lifecycle. Guards against accidental API breakage at v2.0.0+.
"""

from __future__ import annotations

import pytest

import ccmux.api as api
from ccmux.api import (
    Backend,
    ClaudeMessage,
    ClaudeSession,
    ClaudeState,
    CurrentClaudeBinding,
    DefaultBackend,
    EventLogReader,
    InteractiveUIContent,
    TmuxSessionRegistry,
    TmuxWindow,
    TranscriptParser,
    UsageInfo,
    extract_bash_output,
    extract_interactive_content,
    get_default_backend,
    parse_status_line,
    parse_usage_output,
    sanitize_session_name,
    set_default_backend,
    tmux_registry,
)

from tests.fake_backend import FakeBackend


EXPECTED_EXPORTS = {
    # Protocol + lifecycle
    "Backend",
    "TmuxOps",
    "ClaudeOps",
    "DefaultBackend",
    "get_default_backend",
    "set_default_backend",
    # State family
    "ClaudeState",
    "Working",
    "Idle",
    "Blocked",
    "Dead",
    "BlockedUI",
    # Message / transcript
    "ClaudeMessage",
    "TranscriptParser",
    # Session summary
    "ClaudeSession",
    # Event log
    "CurrentClaudeBinding",
    "EventLogReader",
    "EventLogWriter",
    "HookEvent",
    "TmuxInfo",
    "ClaudeInfo",
    # Composition inputs
    "TmuxSessionRegistry",
    # Parser surfaces
    "InteractiveUIContent",
    "UsageInfo",
    "extract_bash_output",
    "extract_interactive_content",
    "parse_status_line",
    "parse_usage_output",
    # Query types
    "TmuxWindow",
    # Composition helpers
    "tmux_registry",
    "sanitize_session_name",
}


class TestApiSurface:
    """Every name in __all__ must exist on the module and round-trip import."""

    def test_api_exports_match_expected_set(self) -> None:
        assert set(api.__all__) == EXPECTED_EXPORTS
        for name in EXPECTED_EXPORTS:
            assert hasattr(api, name), f"api missing {name!r}"

    def test_all_attribute_populated(self) -> None:
        assert len(api.__all__) >= 18

    def test_every_export_is_resolvable(self) -> None:
        missing = [name for name in api.__all__ if not hasattr(api, name)]
        assert missing == [], f"Missing __all__ exports: {missing}"

    def test_tmux_registry_singleton_is_TmuxSessionRegistry(self) -> None:
        assert isinstance(tmux_registry, TmuxSessionRegistry)


class TestFakeBackendSatisfiesProtocol:
    """FakeBackend must be structurally compatible with the Backend Protocol."""

    def test_fake_has_all_protocol_attrs(self) -> None:
        fake = FakeBackend()
        # get_instance replaces old is_alive / get_window_binding
        assert callable(fake.get_instance)
        # tmux sub-protocol: 5 ops
        assert callable(fake.tmux.send_text)
        assert callable(fake.tmux.send_keys)
        assert callable(fake.tmux.capture_pane)
        assert callable(fake.tmux.create_window)
        assert callable(fake.tmux.list_windows)
        # claude sub-protocol: 2 ops
        assert callable(fake.claude.list_sessions)
        assert callable(fake.claude.get_history)
        # lifecycle
        assert callable(fake.start)
        assert callable(fake.stop)

    def test_fake_assignable_to_Backend_annotation(self) -> None:
        # Protocol checks are structural; this asserts Backend is a proper
        # Protocol (not a concrete class) by confirming FakeBackend
        # without inheriting it still satisfies it.
        fake: Backend = FakeBackend()
        assert fake is not None


class TestDefaultBackendConstruction:
    def test_construct_minimal(self, tmp_path) -> None:
        reg = TmuxSessionRegistry()
        backend = DefaultBackend(
            tmux_registry=reg,
            event_reader=EventLogReader(tmp_path / "claude_events.jsonl"),
        )
        assert backend.tmux is not None
        assert backend.claude is not None

    @pytest.mark.asyncio
    async def test_start_stop_idempotent_on_stop(self, tmp_path) -> None:
        reg = TmuxSessionRegistry()
        backend = DefaultBackend(
            tmux_registry=reg,
            event_reader=EventLogReader(tmp_path / "claude_events.jsonl"),
        )

        async def noop_state(instance_id: str, state: ClaudeState) -> None:
            pass

        async def noop_message(instance_id: str, msg: ClaudeMessage) -> None:
            pass

        await backend.start(on_state=noop_state, on_message=noop_message)
        await backend.stop()
        # Second stop must not raise
        await backend.stop()


class TestDefaultBackendSingleton:
    def test_get_raises_when_unset(self) -> None:
        set_default_backend(None)
        with pytest.raises(RuntimeError):
            get_default_backend()

    def test_get_returns_installed(self) -> None:
        fake = FakeBackend()
        set_default_backend(fake)
        try:
            assert get_default_backend() is fake
        finally:
            set_default_backend(None)


class TestParsersImportable:
    """The parser helpers are documented entry points; this just proves
    they're callable, not that they handle every input."""

    def test_parse_status_line_on_empty(self) -> None:
        assert parse_status_line("") is None

    def test_parse_usage_output_on_empty(self) -> None:
        assert parse_usage_output("") is None

    def test_extract_interactive_content_on_empty(self) -> None:
        assert extract_interactive_content("") is None

    def test_extract_bash_output_on_empty(self) -> None:
        assert extract_bash_output("", "ls") is None

    def test_transcript_parser_parse_line(self) -> None:
        assert TranscriptParser.parse_line("") is None

    def test_tmux_session_empty_name_preserved(self) -> None:
        """TmuxSession(session_name='') must preserve the empty string.

        Regression: the old `self.session_name = session_name or default`
        idiom silently replaced empty strings with the configured default,
        which masked bugs where a caller passed "" by accident and the
        session then resolved to __ccmux__, writing a window into the
        wrong session.
        """
        from ccmux.tmux import TmuxSession

        tm = TmuxSession(session_name="")
        assert tm.session_name == ""

    def test_tmux_session_none_falls_back_to_default(self) -> None:
        """TmuxSession(session_name=None) still uses config.tmux_session_name.

        Preserves the documented backward-compatible default path for
        callers that explicitly pass None.
        """
        from ccmux.tmux import TmuxSession
        from ccmux.config import config as ccmux_config

        tm = TmuxSession(session_name=None)
        assert tm.session_name == ccmux_config.tmux_session_name

    def test_sanitize_session_name(self) -> None:
        # Just verify the helper executes and returns a string; behavior
        # is covered in tmux-specific tests.
        assert isinstance(sanitize_session_name("name.with.dots", set()), str)


class TestDataclassShapes:
    """Lock the event-payload fields at the Protocol surface."""

    def test_claude_message_fields(self) -> None:
        msg = ClaudeMessage(
            session_id="s1", role="assistant", content_type="text", text="hi"
        )
        assert msg.tool_name is None and msg.is_complete is False

    def test_claude_session_fields(self) -> None:
        cs = ClaudeSession(
            session_id="sid", summary="x", message_count=3, file_path="/p"
        )
        assert cs.message_count == 3

    def test_tmux_window_fields(self) -> None:
        w = TmuxWindow(window_id="@1", cwd="/tmp")
        assert w.pane_current_command == ""

    def test_interactive_ui_content_fields(self) -> None:
        from ccmux.claude_state import BlockedUI

        ui = InteractiveUIContent(content="…", ui=BlockedUI.ASK_USER_QUESTION)
        assert ui.ui is BlockedUI.ASK_USER_QUESTION

    def test_usage_info_fields(self) -> None:
        u = UsageInfo(parsed_lines=["a", "b"])
        assert u.parsed_lines == ["a", "b"]

    def test_current_claude_binding_fields(self) -> None:
        from datetime import datetime, timezone

        b = CurrentClaudeBinding(
            tmux_session_name="ccmux",
            window_id="@1",
            claude_session_id="sid",
            cwd="/tmp",
            transcript_path="/p/sid.jsonl",
            last_seen=datetime(2026, 4, 28, tzinfo=timezone.utc),
        )
        assert b.cwd == "/tmp"
        assert b.tmux_session_name == "ccmux"

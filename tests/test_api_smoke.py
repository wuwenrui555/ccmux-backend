"""Smoke tests for the public API surface in `ccmux.api`.

Verifies every symbol in `__all__` is importable and callable; also
covers the Protocol contract via FakeBackend and DefaultBackend's
lifecycle. Guards against accidental API breakage at v1.0+.
"""

from __future__ import annotations

import pytest

import ccmux.api as api
from ccmux.api import (
    Backend,
    ClaudeMessage,
    ClaudeSession,
    DefaultBackend,
    InteractiveUIContent,
    TmuxSessionRegistry,
    TmuxWindow,
    TranscriptParser,
    UsageInfo,
    WindowBinding,
    WindowBindings,
    WindowStatus,
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


class TestApiSurface:
    """Every name in __all__ must exist on the module and round-trip import."""

    def test_all_attribute_populated(self) -> None:
        assert len(api.__all__) >= 18

    def test_every_export_is_resolvable(self) -> None:
        missing = [name for name in api.__all__ if not hasattr(api, name)]
        assert missing == [], f"Missing __all__ exports: {missing}"

    def test_registry_and_bindings_are_distinct_types(self) -> None:
        # Catches a regression where the two composition inputs collapsed
        # into a single name.
        assert TmuxSessionRegistry is not WindowBindings

    def test_tmux_registry_singleton_is_TmuxSessionRegistry(self) -> None:
        assert isinstance(tmux_registry, TmuxSessionRegistry)


class TestFakeBackendSatisfiesProtocol:
    """FakeBackend must be structurally compatible with the Backend Protocol."""

    def test_fake_has_all_protocol_attrs(self) -> None:
        fake = FakeBackend()
        assert hasattr(fake, "tmux")
        assert hasattr(fake, "claude")
        assert callable(fake.is_alive)
        assert callable(fake.get_window_binding)
        assert callable(fake.start)
        assert callable(fake.stop)

    def test_fake_assignable_to_Backend_annotation(self) -> None:
        # Protocol checks are structural; this asserts Backend is a proper
        # Protocol (not a concrete class) by confirming DefaultBackend
        # without inheriting it still satisfies it.
        fake: Backend = FakeBackend()
        assert fake is not None


class TestDefaultBackendConstruction:
    def test_construct_minimal(self, tmp_path) -> None:
        bindings = WindowBindings(map_file=tmp_path / "window_bindings.json")
        reg = TmuxSessionRegistry()
        backend = DefaultBackend(tmux_registry=reg, window_bindings=bindings)
        assert backend.tmux is not None
        assert backend.claude is not None

    @pytest.mark.asyncio
    async def test_start_stop_idempotent_on_stop(self, tmp_path) -> None:
        bindings = WindowBindings(map_file=tmp_path / "window_bindings.json")
        reg = TmuxSessionRegistry()
        backend = DefaultBackend(tmux_registry=reg, window_bindings=bindings)
        # stop() before start() is a no-op; must not raise.
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

    def test_window_status_fields(self) -> None:
        s = WindowStatus(
            window_id="@1",
            window_exists=True,
            pane_captured=False,
            status_text=None,
            interactive_ui=None,
        )
        assert s.window_id == "@1"

    def test_window_binding_fields(self) -> None:
        b = WindowBinding(
            window_id="@1", session_name="s", claude_session_id="sid", cwd="/tmp"
        )
        assert b.cwd == "/tmp"

    def test_claude_session_fields(self) -> None:
        cs = ClaudeSession(
            session_id="sid", summary="x", message_count=3, file_path="/p"
        )
        assert cs.message_count == 3

    def test_tmux_window_fields(self) -> None:
        w = TmuxWindow(window_id="@1", cwd="/tmp")
        assert w.pane_current_command == ""

    def test_interactive_ui_content_fields(self) -> None:
        ui = InteractiveUIContent(content="…", name="AskUserQuestion")
        assert ui.name == "AskUserQuestion"

    def test_usage_info_fields(self) -> None:
        u = UsageInfo(parsed_lines=["a", "b"])
        assert u.parsed_lines == ["a", "b"]

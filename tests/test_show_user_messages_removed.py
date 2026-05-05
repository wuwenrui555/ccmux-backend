"""Contract tests for the v5.0.0 removal of show_user_messages.

These tests document the post-removal contract:
- MessageMonitor.__init__ no longer accepts show_user_messages.
- DefaultBackend.__init__ no longer accepts show_user_messages.
- ccmux.config.config has no show_user_messages attribute.
- User-role JSONL entries are always emitted; the filter is gone.

Pre-implementation these tests FAIL (kwarg accepted, attr present,
filter still drops user messages). Post-implementation they PASS.
"""

import json

import pytest

from ccmux.backend import DefaultBackend
from ccmux.config import config as backend_config
from ccmux.message_monitor import MessageMonitor, TrackedClaudeSession


class TestKwargRemoved:
    def test_message_monitor_rejects_show_user_messages_kwarg(self, tmp_path):
        with pytest.raises(TypeError):
            MessageMonitor(  # type: ignore[call-arg]
                projects_path=tmp_path / "projects",
                state_file=tmp_path / "claude_monitor.json",
                show_user_messages=True,
            )

    def test_default_backend_rejects_show_user_messages_kwarg(self):
        from ccmux.api import tmux_registry

        with pytest.raises(TypeError):
            DefaultBackend(  # type: ignore[call-arg]
                tmux_registry=tmux_registry,
                show_user_messages=True,
            )


class TestConfigFieldRemoved:
    def test_config_has_no_show_user_messages_attribute(self):
        assert not hasattr(backend_config, "show_user_messages")


class TestUserMessagesAlwaysEmitted:
    """User-role entries flow through MessageMonitor without any filter."""

    @pytest.mark.asyncio
    async def test_user_role_entry_emitted(self, tmp_path, make_jsonl_entry):
        # Realistic project layout: <projects>/<project>/<session_id>.jsonl
        projects_path = tmp_path / "projects"
        project_dir = projects_path / "myproject"
        project_dir.mkdir(parents=True)
        session_id = "test-session-id"
        jsonl_file = project_dir / f"{session_id}.jsonl"

        user_entry = make_jsonl_entry(
            msg_type="user",
            content=[{"type": "text", "text": "hello from user"}],
            session_id=session_id,
        )
        jsonl_file.write_text(json.dumps(user_entry) + "\n", encoding="utf-8")

        monitor = MessageMonitor(
            projects_path=projects_path,
            state_file=tmp_path / "claude_monitor.json",
        )
        # Seed tracking with offset=0 so check_for_updates reads from start.
        monitor.state.update_session(
            TrackedClaudeSession(
                session_id=session_id,
                file_path=jsonl_file,
                last_byte_offset=0,
            )
        )

        msgs = await monitor.check_for_updates({session_id})

        user_msgs = [m for m in msgs if m.role == "user"]
        assert len(user_msgs) == 1
        assert "hello from user" in user_msgs[0].text


class TestConfigLoadsSettingsEnv:
    """Config loads ~/.ccmux/settings.env, not ~/.ccmux/.env."""

    def test_settings_env_provides_ccmux_var(self, tmp_path, monkeypatch):
        # Point CCMUX_DIR at a temp dir, write a settings.env with a value
        # that diverges from the default, instantiate Config, observe.
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.delenv("CCMUX_TMUX_SESSION_NAME", raising=False)
        (tmp_path / "settings.env").write_text(
            "CCMUX_TMUX_SESSION_NAME=test_session_name_from_settings_env\n",
            encoding="utf-8",
        )

        from ccmux.config import Config

        cfg = Config()
        assert cfg.tmux_session_name == "test_session_name_from_settings_env"

    def test_dot_env_is_not_read_by_backend(self, tmp_path, monkeypatch):
        # Place a value only in .env. Backend must NOT read it.
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.delenv("CCMUX_TMUX_SESSION_NAME", raising=False)
        (tmp_path / ".env").write_text(
            "CCMUX_TMUX_SESSION_NAME=should_not_be_loaded_by_backend\n",
            encoding="utf-8",
        )

        from ccmux.config import Config

        cfg = Config()
        # Default applies because backend never reads .env.
        assert cfg.tmux_session_name == "__ccmux__"

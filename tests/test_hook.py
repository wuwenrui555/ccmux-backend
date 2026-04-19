"""Tests for Claude Code session tracking hook."""

import io
import json
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from ccmux.hook import _UUID_RE, _is_hook_installed, hook_main


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "ccmux hook", "timeout": 5}
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/ccmux hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "window_bindings.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "window_bindings.json").exists()

    def test_no_tmux_pane_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "window_bindings.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "window_bindings.json").exists()


class TestHookSessionMapWrite:
    """Tests that verify actual window_bindings.json writing behavior.

    These mock subprocess.run (tmux display-message) and TMUX_PANE to
    simulate running inside a tmux pane.
    """

    def _mock_tmux(self, monkeypatch: pytest.MonkeyPatch, output: str) -> None:
        """Mock subprocess.run to return a fake tmux display-message output."""
        result = MagicMock()
        result.stdout = output
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

    def _run_hook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict,
        tmux_output: str,
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setenv("TMUX_PANE", "%0")
        self._mock_tmux(monkeypatch, tmux_output)
        hook_main()

    def test_writes_session_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))

        self._run_hook(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/home/user/project",
                "hook_event_name": "SessionStart",
            },
            "aclf:@4\n",
        )

        data = json.loads((tmp_path / "window_bindings.json").read_text())
        assert data["aclf"]["window_id"] == "@4"
        assert data["aclf"]["session_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert data["aclf"]["cwd"] == "/home/user/project"

    def test_same_window_updates_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Same window_id (e.g. after /clear) should update session_id."""
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))

        self._run_hook(
            monkeypatch,
            {
                "session_id": "aaaa0000-0000-0000-0000-000000000000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
            "daily:@3\n",
        )

        self._run_hook(
            monkeypatch,
            {
                "session_id": "bbbb0000-0000-0000-0000-000000000000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
            "daily:@3\n",
        )

        data = json.loads((tmp_path / "window_bindings.json").read_text())
        assert data["daily"]["session_id"] == "bbbb0000-0000-0000-0000-000000000000"

    def test_different_window_refuses_overwrite(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Second Claude in the same session (different window_id) should NOT overwrite."""
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))

        self._run_hook(
            monkeypatch,
            {
                "session_id": "aaaa0000-0000-0000-0000-000000000000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
            "aclf:@4\n",
        )

        self._run_hook(
            monkeypatch,
            {
                "session_id": "bbbb0000-0000-0000-0000-000000000000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
            "aclf:@9\n",
        )

        data = json.loads((tmp_path / "window_bindings.json").read_text())
        assert data["aclf"]["window_id"] == "@4"
        assert data["aclf"]["session_id"] == "aaaa0000-0000-0000-0000-000000000000"

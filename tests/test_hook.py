"""Tests for Claude Code session tracking hook."""

import io
import json
import logging
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from ccmux.hook import _UUID_RE, _encode_project_dir, _is_hook_installed, hook_main


@pytest.fixture(autouse=True)
def _reset_root_logger_handlers():
    """Prevent handlers installed by hook_main from leaking across tests."""
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        yield
    finally:
        for h in list(root.handlers):
            if h not in original:
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass


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


class TestHookFileLogging:
    """hook.log in CCMUX_DIR captures invocations + unhandled exceptions."""

    def test_hook_log_created_with_invocation_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "session_id": "550e8400-e29b-41d4-a716-446655440000",
                        "cwd": "/home/user/project",
                        "hook_event_name": "SessionStart",
                    }
                )
            ),
        )
        monkeypatch.setenv("TMUX_PANE", "%0")
        result = MagicMock()
        result.stdout = "aclf:@4\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

        hook_main()

        log_path = tmp_path / "hook.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "550e8400" in content
        assert "aclf" in content

    def test_unhandled_exception_logged_with_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A crash inside the hook body is written to hook.log before exit 1."""
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))

        from ccmux import hook as hook_mod

        def _boom() -> None:
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(hook_mod, "_hook_main_impl", _boom)

        with pytest.raises(SystemExit) as exc:
            hook_main()
        assert exc.value.code == 1

        log_path = tmp_path / "hook.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "synthetic failure" in content
        assert "Traceback" in content


class TestEncodeProjectDir:
    @pytest.mark.parametrize(
        "cwd, expected",
        [
            ("/mnt/md0/home/wenruiwu", "-mnt-md0-home-wenruiwu"),
            ("/mnt/md0/home/wenruiwu/.claude", "-mnt-md0-home-wenruiwu--claude"),
            (
                "/mnt/md0/home/wenruiwu/obsidian_notes",
                "-mnt-md0-home-wenruiwu-obsidian-notes",
            ),
            (
                "/mnt/md0/home/wenruiwu/projects/aclf_review",
                "-mnt-md0-home-wenruiwu-projects-aclf-review",
            ),
            ("/tmp", "-tmp"),
        ],
        ids=["plain", "dotfile", "underscore", "nested-underscore", "short"],
    )
    def test_matches_claude_project_dir_naming(
        self, cwd: str, expected: str
    ) -> None:
        assert _encode_project_dir(cwd) == expected

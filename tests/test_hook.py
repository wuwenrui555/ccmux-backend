"""Tests for Claude Code session tracking hook."""

import io
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ccmux.hook import (
    _UUID_RE,
    _encode_project_dir,
    _find_claude_pid,
    _install_hook,
    _is_hook_installed,
    _resolve_session_via_pid,
    hook_main,
)


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
        assert not (tmp_path / "claude_events.jsonl").exists()

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
        assert not (tmp_path / "claude_events.jsonl").exists()

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
        assert not (tmp_path / "claude_events.jsonl").exists()

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
        assert not (tmp_path / "claude_events.jsonl").exists()


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
    def test_matches_claude_project_dir_naming(self, cwd: str, expected: str) -> None:
        assert _encode_project_dir(cwd) == expected


class TestFindClaudePid:
    def _mock_pgrep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        children: list[int],
        returncode: int = 0,
    ) -> None:
        """Make subprocess.run return the given children for any pgrep call."""
        result = MagicMock()
        result.stdout = "\n".join(str(c) for c in children) + ("\n" if children else "")
        result.returncode = returncode
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

    def _mock_cmdline(
        self, monkeypatch: pytest.MonkeyPatch, mapping: dict[int, str]
    ) -> None:
        """Make `/proc/<pid>/cmdline` reads return mapping[pid] as argv0."""
        from ccmux import hook as hook_mod

        def fake_read_bytes(self: "Path") -> bytes:  # type: ignore[name-defined]
            parts = self.parts
            assert parts[1] == "proc" and parts[-1] == "cmdline", self
            pid = int(parts[2])
            if pid not in mapping:
                raise FileNotFoundError(str(self))
            return mapping[pid].encode() + b"\0--dangerously-skip-permissions\0"

        monkeypatch.setattr(hook_mod.Path, "read_bytes", fake_read_bytes)

    def test_returns_claude_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_pgrep(monkeypatch, [12345])
        self._mock_cmdline(monkeypatch, {12345: "claude"})
        assert _find_claude_pid(shell_pid=999) == 12345

    def test_returns_claude_when_argv0_is_absolute_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [12345])
        self._mock_cmdline(monkeypatch, {12345: "/usr/local/bin/claude"})
        assert _find_claude_pid(shell_pid=999) == 12345

    def test_skips_non_claude_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_pgrep(monkeypatch, [100, 200, 300])
        self._mock_cmdline(
            monkeypatch,
            {100: "vim", 200: "node", 300: "claude"},
        )
        assert _find_claude_pid(shell_pid=999) == 300

    def test_returns_none_when_pgrep_has_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [], returncode=1)
        assert _find_claude_pid(shell_pid=999) is None

    def test_returns_none_when_no_child_is_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [100, 200])
        self._mock_cmdline(monkeypatch, {100: "vim", 200: "node"})
        assert _find_claude_pid(shell_pid=999) is None


class TestResolveSessionViaPid:
    """Reconstruct (session_id, cwd) from tmux pane -> claude PID -> filesystem."""

    def _setup_claude_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        *,
        claude_pid: int,
        launch_cwd: str,
        session_jsonls: list[str],
        newest: str | None,
    ) -> None:
        """Lay out a fake `~/.claude/` tree and route Path.home() to it."""
        claude_dir = tmp_path / ".claude"
        (claude_dir / "sessions").mkdir(parents=True)
        (claude_dir / "sessions" / f"{claude_pid}.json").write_text(
            json.dumps(
                {
                    "pid": claude_pid,
                    "sessionId": "stale-00000000-0000-0000-0000-000000000000",
                    "cwd": launch_cwd,
                }
            )
        )
        project_dir = claude_dir / "projects" / _encode_project_dir(launch_cwd)
        project_dir.mkdir(parents=True)
        for name in session_jsonls:
            (project_dir / name).write_text("{}\n")
            if name == newest:
                time.sleep(0.01)
                (project_dir / name).write_text('{"touched": true}\n')
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _mock_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shell_pid: int,
        children: list[int],
    ) -> None:
        """Dispatch subprocess.run based on argv[0] to handle tmux + pgrep."""

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "tmux":
                result.stdout = f"{shell_pid}\n"
            elif cmd[0] == "pgrep":
                result.stdout = (
                    "\n".join(str(c) for c in children) + "\n" if children else ""
                )
                result.returncode = 0 if children else 1
            else:
                raise AssertionError(f"unexpected subprocess: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)

    def _mock_cmdline(
        self, monkeypatch: pytest.MonkeyPatch, mapping: dict[int, str]
    ) -> None:
        from ccmux import hook as hook_mod

        original_read_bytes = hook_mod.Path.read_bytes

        def fake_read_bytes(self: "Path") -> bytes:  # type: ignore[name-defined]
            parts = self.parts
            if len(parts) >= 3 and parts[1] == "proc" and parts[-1] == "cmdline":
                pid = int(parts[2])
                if pid not in mapping:
                    raise FileNotFoundError(str(self))
                return mapping[pid].encode() + b"\0"
            return original_read_bytes(self)

        monkeypatch.setattr(hook_mod.Path, "read_bytes", fake_read_bytes)

    def test_returns_newest_session_id_and_launch_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        launch_cwd = "/mnt/data/project"
        self._setup_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd=launch_cwd,
            session_jsonls=[
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl",
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl",
            ],
            newest="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl",
        )
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        result = _resolve_session_via_pid("%17")

        assert result == (
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            launch_cwd,
        )

    def test_returns_none_when_no_claude_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        assert _resolve_session_via_pid("%17") is None

    def test_returns_none_when_session_file_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        assert _resolve_session_via_pid("%17") is None

    def test_returns_none_when_project_dir_has_no_jsonl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._setup_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd="/mnt/data/project",
            session_jsonls=[],
            newest=None,
        )
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        assert _resolve_session_via_pid("%17") is None

    def test_ignores_non_uuid_jsonl_filenames(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._setup_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd="/mnt/data/project",
            session_jsonls=["scratchpad.jsonl"],
            newest="scratchpad.jsonl",
        )
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        assert _resolve_session_via_pid("%17") is None

    def test_rejects_invalid_pane_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Note: no subprocess mock — the function should short-circuit before it.
        assert _resolve_session_via_pid("not-a-pane") is None


class TestHookMainEmptyStdinFallback:
    """When Claude Code sends empty stdin, hook_main must reconstruct
    session_id + cwd via the PID fallback and still append an event-log
    line.
    """

    def _lay_out_claude_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        *,
        claude_pid: int,
        launch_cwd: str,
        new_session_id: str,
    ) -> None:
        claude_dir = tmp_path / ".claude"
        (claude_dir / "sessions").mkdir(parents=True)
        (claude_dir / "sessions" / f"{claude_pid}.json").write_text(
            json.dumps({"pid": claude_pid, "cwd": launch_cwd})
        )
        project_dir = claude_dir / "projects" / _encode_project_dir(launch_cwd)
        project_dir.mkdir(parents=True)
        (project_dir / f"{new_session_id}.jsonl").write_text("{}\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _dispatch_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shell_pid: int,
        claude_pid: int,
        tmux_window: str,
    ) -> None:
        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "tmux" and cmd[-1] == "#{pane_pid}":
                result.stdout = f"{shell_pid}\n"
            elif cmd[0] == "tmux":
                result.stdout = tmux_window
            elif cmd[0] == "pgrep":
                result.stdout = f"{claude_pid}\n"
            else:
                raise AssertionError(f"unexpected: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)

    def _stub_cmdline(self, monkeypatch: pytest.MonkeyPatch, claude_pid: int) -> None:
        from ccmux import hook as hook_mod

        original_read_bytes = hook_mod.Path.read_bytes

        def fake_read_bytes(self: "Path") -> bytes:  # type: ignore[name-defined]
            parts = self.parts
            if len(parts) >= 3 and parts[1] == "proc" and parts[-1] == "cmdline":
                if int(parts[2]) == claude_pid:
                    return b"claude\0"
                raise FileNotFoundError(str(self))
            return original_read_bytes(self)

        monkeypatch.setattr(hook_mod.Path, "read_bytes", fake_read_bytes)

    def test_empty_stdin_triggers_fallback_and_writes_bindings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Isolate both CCMUX_DIR and ~/.claude from the real host.
        ccmux_dir = tmp_path / "ccmux"
        ccmux_dir.mkdir()
        monkeypatch.setenv("CCMUX_DIR", str(ccmux_dir))

        new_session_id = "99999999-9999-9999-9999-999999999999"
        self._lay_out_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd="/mnt/data/project",
            new_session_id=new_session_id,
        )
        self._dispatch_subprocess(
            monkeypatch,
            shell_pid=9999,
            claude_pid=4242,
            tmux_window="ccmux:@16\n",
        )
        self._stub_cmdline(monkeypatch, claude_pid=4242)

        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # empty stdin
        monkeypatch.setenv("TMUX_PANE", "%17")

        hook_main()

        log = ccmux_dir / "claude_events.jsonl"
        assert log.exists()
        from ccmux.event_log import HookEvent

        line = log.read_text().splitlines()[0]
        ev = HookEvent.from_jsonl(line + "\n")
        assert ev.tmux.session_name == "ccmux"
        assert ev.tmux.window_id == "@16"
        assert ev.claude.session_id == new_session_id
        assert ev.claude.cwd == "/mnt/data/project"

    def test_empty_stdin_with_failed_fallback_skips_silently(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """No claude child -> no write, but also no exception."""
        ccmux_dir = tmp_path / "ccmux"
        ccmux_dir.mkdir()
        monkeypatch.setenv("CCMUX_DIR", str(ccmux_dir))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        # No _stub_cmdline needed: pgrep failure short-circuits before /proc reads.
        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            if cmd[0] == "tmux":
                result.stdout = "1234\n"
                result.returncode = 0
            elif cmd[0] == "pgrep":
                result.stdout = ""
                result.returncode = 1
            else:
                raise AssertionError(f"unexpected: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setenv("TMUX_PANE", "%17")

        hook_main()  # must not raise

        assert not (ccmux_dir / "claude_instances.json").exists()

    def test_invalid_uuid_in_stdin_triggers_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Non-empty stdin with a bad session_id must still trigger the PID
        fallback (not just empty stdin). When the fallback fails, no write."""
        ccmux_dir = tmp_path / "ccmux"
        ccmux_dir.mkdir()
        monkeypatch.setenv("CCMUX_DIR", str(ccmux_dir))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            if cmd[0] == "tmux":
                result.stdout = "1234\n"
                result.returncode = 0
            elif cmd[0] == "pgrep":
                result.stdout = ""
                result.returncode = 1
            else:
                raise AssertionError(f"unexpected: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "session_id": "not-a-uuid",
                        "cwd": "/tmp",
                        "hook_event_name": "SessionStart",
                    }
                )
            ),
        )
        monkeypatch.setenv("TMUX_PANE", "%17")

        hook_main()

        assert not (ccmux_dir / "claude_instances.json").exists()


class TestHookWritesEventLog:
    """v4.0.0: hook accepts SessionStart + UserPromptSubmit and appends to
    ~/.ccmux/claude_events.jsonl in addition to whatever legacy paths it runs.
    """

    def _mock_tmux(self, monkeypatch: pytest.MonkeyPatch, output: str) -> None:
        result = MagicMock()
        result.stdout = output
        result.returncode = 0
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

    def test_user_prompt_submit_appends_to_event_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        self._run_hook(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/home/u",
                "transcript_path": "/home/u/.claude/projects/p/sess.jsonl",
                "permission_mode": "default",
                "hook_event_name": "UserPromptSubmit",
            },
            "ccmux:@5\n",
        )

        log = tmp_path / "claude_events.jsonl"
        assert log.exists()
        lines = log.read_text().splitlines()
        assert len(lines) == 1

        from ccmux.event_log import HookEvent

        event = HookEvent.from_jsonl(lines[0] + "\n")
        assert event.hook_event == "UserPromptSubmit"
        assert event.tmux.session_name == "ccmux"
        assert event.tmux.window_id == "@5"
        assert event.claude.session_id == "550e8400-e29b-41d4-a716-446655440000"
        assert event.claude.transcript_path == "/home/u/.claude/projects/p/sess.jsonl"
        assert event.claude.cwd == "/home/u"

    def test_session_start_appends_to_event_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """SessionStart appends one line to claude_events.jsonl. v4.0.0
        does not maintain the legacy claude_instances.json file.
        """
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        self._run_hook(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/home/u",
                "transcript_path": "/home/u/.claude/projects/p/sess.jsonl",
                "permission_mode": "default",
                "hook_event_name": "SessionStart",
            },
            "ccmux:@5\n",
        )

        # Legacy file is gone in v4.0.0.
        assert not (tmp_path / "claude_instances.json").exists()

        events_log = tmp_path / "claude_events.jsonl"
        assert events_log.exists()
        lines = events_log.read_text().splitlines()
        assert len(lines) == 1

        from ccmux.event_log import HookEvent

        event = HookEvent.from_jsonl(lines[0] + "\n")
        assert event.hook_event == "SessionStart"
        assert event.tmux.session_name == "ccmux"
        assert event.tmux.window_id == "@5"

    def test_install_registers_session_start_and_user_prompt_submit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        settings_file = tmp_path / "settings.json"
        from ccmux import hook as hook_mod

        monkeypatch.setattr(hook_mod, "_CLAUDE_SETTINGS_FILE", settings_file)

        rc = _install_hook()
        assert rc == 0

        settings = json.loads(settings_file.read_text())
        assert "hooks" in settings
        assert "SessionStart" in settings["hooks"]
        assert "UserPromptSubmit" in settings["hooks"]

        def _has_ccmux(entries: list) -> bool:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "") if isinstance(h, dict) else ""
                    if cmd.endswith("ccmux hook") or cmd == "ccmux hook":
                        return True
            return False

        assert _has_ccmux(settings["hooks"]["SessionStart"])
        assert _has_ccmux(settings["hooks"]["UserPromptSubmit"])

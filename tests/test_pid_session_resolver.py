"""Unit tests for ccmux.pid_session_resolver."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to a tmp dir so we can stage ~/.claude/."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _stage_claude_session(
    home: Path, claude_pid: int, cwd: str, session_id: str
) -> None:
    """Create the files resolve_for_pane reads."""
    sessions_dir = home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{claude_pid}.json").write_text(json.dumps({"cwd": cwd}))
    encoded = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / encoded
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text("{}\n")


def test_resolve_for_pane_happy_path(fake_home: Path) -> None:
    from ccmux.pid_session_resolver import resolve_for_pane

    pane = "%17"
    shell_pid = 4321
    claude_pid = 4322
    cwd = "/Users/wenruiwu"
    sid = "11111111-2222-3333-4444-555555555555"
    _stage_claude_session(fake_home, claude_pid, cwd, sid)

    def fake_run(args, **kwargs):
        class _R:
            returncode = 0
            stdout = ""

        if args[0] == "tmux":
            _R.stdout = f"{shell_pid}\n"
        elif args[0] == "pgrep":
            _R.stdout = f"{claude_pid}\n"
        else:
            raise AssertionError(f"unexpected: {args}")
        return _R()

    with patch("ccmux.pid_session_resolver.subprocess.run", side_effect=fake_run):
        result = resolve_for_pane(pane)

    assert result == (sid, cwd)


def test_resolve_for_pane_invalid_pane_format(fake_home: Path) -> None:
    from ccmux.pid_session_resolver import resolve_for_pane

    # Pane id must look like %N
    assert resolve_for_pane("not-a-pane") is None
    assert resolve_for_pane("@22") is None
    assert resolve_for_pane("") is None


def test_find_claude_pid_picks_child_with_sessions_file(
    fake_home: Path,
) -> None:
    """Without /proc, the resolver must still pick the Claude child."""
    from ccmux.pid_session_resolver import _find_claude_pid

    shell_pid = 1000
    sibling_pid = 1001  # not Claude
    claude_pid = 1002

    sessions_dir = fake_home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{claude_pid}.json").write_text("{}")

    def fake_run(args, **kwargs):
        class _R:
            returncode = 0
            stdout = f"{sibling_pid}\n{claude_pid}\n"

        return _R()

    with patch("ccmux.pid_session_resolver.subprocess.run", side_effect=fake_run):
        # Force /proc lookup to fail so we exercise the portable path.
        with patch.object(Path, "read_bytes", side_effect=OSError("no /proc")):
            result = _find_claude_pid(shell_pid)

    assert result == claude_pid


def test_find_claude_pid_returns_none_when_no_child_has_sessions_file(
    fake_home: Path,
) -> None:
    from ccmux.pid_session_resolver import _find_claude_pid

    def fake_run(args, **kwargs):
        class _R:
            returncode = 0
            stdout = "1001\n1002\n"

        return _R()

    with patch("ccmux.pid_session_resolver.subprocess.run", side_effect=fake_run):
        with patch.object(Path, "read_bytes", side_effect=OSError("no /proc")):
            result = _find_claude_pid(1000)

    assert result is None

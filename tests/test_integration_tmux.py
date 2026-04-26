"""End-to-end tests against a real tmux server.

Skipped automatically when the `tmux` binary is not on PATH. Marked
`integration` so default pytest runs (which exclude that marker via
`-m "not integration"`) skip them too — opt in with
``uv run pytest -m integration`` or run by file path.

Each test creates a uniquely-named throwaway session and tears it down
in a finalizer; failures should not leave server state behind.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

import pytest

from ccmux.tmux import TmuxSessionRegistry

pytestmark = pytest.mark.integration


def _have_tmux() -> bool:
    return shutil.which("tmux") is not None


pytest.importorskip("libtmux")
if not _have_tmux():
    pytest.skip("tmux binary not on PATH", allow_module_level=True)


@pytest.fixture
def session_name() -> str:
    """Generate a unique, throwaway tmux session name for the test."""
    return f"_ccmux_it_{int(time.time() * 1000)}"


@pytest.fixture
def registry(session_name: str):
    """Yield a fresh registry; ensure the test session is killed afterwards.

    Teardown shells out to ``tmux kill-session`` directly rather than going
    through libtmux — the libtmux ``sessions`` collection caches and a
    cache-miss after create_session() was leaking sessions in CI. tmux
    exits non-zero when the session is already gone; that's fine.
    """
    reg = TmuxSessionRegistry()
    yield reg
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
    )


def test_create_session_and_list_windows(registry, session_name, tmp_path):
    """create_session() then list_windows() returns the new window."""
    tm = registry.get_or_create(session_name)

    ok, msg, win_name, wid = asyncio.run(
        tm.create_session(
            work_dir=str(tmp_path),
            window_name="test-window",
            start_claude=False,
        )
    )

    assert ok, msg
    assert wid.startswith("@")
    assert win_name == "test-window"

    windows = asyncio.run(tm.list_windows())
    assert any(w.window_id == wid for w in windows)


def test_registered_session_names_after_create(registry, session_name, tmp_path):
    """registered_session_names() reflects sessions added through the registry."""
    tm = registry.get_or_create(session_name)
    asyncio.run(tm.create_session(work_dir=str(tmp_path), start_claude=False))

    assert session_name in registry.registered_session_names()
    assert session_name in registry.all_server_session_names()


def test_get_by_window_id_routes_to_owning_session(registry, session_name, tmp_path):
    """get_by_window_id() returns the TmuxSession that owns the window."""
    tm = registry.get_or_create(session_name)
    _, _, _, wid = asyncio.run(
        tm.create_session(work_dir=str(tmp_path), start_claude=False)
    )

    routed = registry.get_by_window_id(wid)
    assert routed is tm


def test_get_session_returns_none_for_missing_session(registry, session_name):
    """get_session() must return None when the tmux session does not exist.

    Regression: libtmux raises ObjectDoesNotExist (not LibTmuxException) when
    no session matches. Previously _TMUX_ERRORS did not catch it, so any
    caller that expected the documented "return None" contract crashed on
    the very common new-session case.
    """
    tm = registry.get_or_create(session_name)
    assert tm.get_session() is None


def test_create_session_on_registry_entry_with_no_prior_tmux_session(
    registry, session_name, tmp_path
):
    """create_session() succeeds for a freshly registered TmuxSession.

    Regression for the `_create_session_and_bind` flow: the caller registers
    a TmuxSession, probes existence via get_session(), then calls
    create_session() on the None branch. The probe must not raise.
    """
    tm = registry.get_or_create(session_name)

    assert tm.get_session() is None

    ok, msg, _, wid = asyncio.run(
        tm.create_session(work_dir=str(tmp_path), start_claude=False)
    )
    assert ok, msg
    assert wid.startswith("@")


def test_send_keys_literal_text_with_leading_dash(registry, session_name, tmp_path):
    """Regression: a string starting with '-' must reach the pane literally.

    libtmux 0.55's Pane.send_keys invokes `tmux send-keys -l <text>` without
    a `--` separator, so when <text> begins with '-' tmux's argument parser
    consumes it as a flag and the command errors out — nothing reaches the
    pane. Backend bypasses libtmux's wrapper and emits `--` itself; this
    test fails on the unfixed code path because the literal text never
    appears in the captured pane.
    """
    tm = registry.get_or_create(session_name)
    _, _, _, wid = asyncio.run(
        tm.create_session(work_dir=str(tmp_path), start_claude=False)
    )

    sent = asyncio.run(tm.send_keys(wid, "- hello", enter=False, literal=True))
    assert sent

    # tmux needs a tick to render the buffer before capture-pane sees it.
    time.sleep(0.2)
    captured = asyncio.run(tm.capture_pane(wid))
    assert captured is not None
    assert "- hello" in captured


def test_send_keys_exits_copy_mode_before_sending(registry, session_name, tmp_path):
    """Regression: send_keys must exit any active pane mode first.

    When the user scrolls up in tmux, the pane enters copy-mode and
    keystrokes get interpreted as vim-style navigation (`h`/`j`/`k`/`l`,
    search via `/`, jump via `g`, …) instead of being typed into the
    running shell. Bot-relayed text used to be silently swallowed by
    that mode. send_keys now probes `#{pane_in_mode}` and emits
    `send-keys -X cancel` before its real keystrokes.
    """
    tm = registry.get_or_create(session_name)
    _, _, _, wid = asyncio.run(
        tm.create_session(work_dir=str(tmp_path), start_claude=False)
    )

    session = tm.get_session()
    assert session is not None
    window = session.windows.get(window_id=wid)
    pane = window.active_pane
    assert pane is not None

    # Force the pane into copy-mode and confirm.
    pane.cmd("copy-mode")
    in_mode_before = pane.cmd("display-message", "-p", "#{pane_in_mode}")
    assert in_mode_before.stdout and in_mode_before.stdout[0].strip() == "1"

    # Send a message — the fix should cancel copy-mode first, then type.
    sent = asyncio.run(tm.send_keys(wid, "hello", enter=False, literal=True))
    assert sent

    # Pane should be back in normal mode after send_keys.
    in_mode_after = pane.cmd("display-message", "-p", "#{pane_in_mode}")
    assert in_mode_after.stdout and in_mode_after.stdout[0].strip() == "0"

    # And "hello" must appear literally in the pane (not interpreted as
    # the copy-mode navigation sequence h-e-l-l-o).
    time.sleep(0.2)
    captured = asyncio.run(tm.capture_pane(wid))
    assert captured is not None
    assert "hello" in captured

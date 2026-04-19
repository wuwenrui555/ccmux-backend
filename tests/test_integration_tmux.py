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
    asyncio.run(
        tm.create_session(work_dir=str(tmp_path), start_claude=False)
    )

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

"""Tests for ccmux.state_log.StateSnapshot — atomic-rewrite snapshot file."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from claude_code_state import Blocked, BlockedUI, Idle, Working

from ccmux.state_log import StateSnapshot


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


class TestStateSnapshot:
    @pytest.fixture
    def snap_path(self, tmp_path: Path) -> Path:
        return tmp_path / "state_current.json"

    @pytest.mark.asyncio
    async def test_first_record_creates_file_with_one_entry(
        self, snap_path: Path
    ) -> None:
        snap = StateSnapshot(snap_path)
        try:
            await snap.record(
                instance_id="a",
                window_id="@1",
                pane_text="screen",
                state=Idle(),
            )
        finally:
            await snap.close()
        data = _read_json(snap_path)
        assert set(data.keys()) == {"a"}
        entry = data["a"]
        assert entry["state"] == {"type": "Idle"}
        assert entry["window_id"] == "@1"
        assert "last_seen" in entry
        # pane_text MUST NOT be in the snapshot
        assert "pane_text" not in entry

    @pytest.mark.asyncio
    async def test_repeated_record_updates_last_seen(self, snap_path: Path) -> None:
        snap = StateSnapshot(snap_path)
        try:
            await snap.record(
                instance_id="a", window_id="@1", pane_text="x", state=Idle()
            )
            data1 = _read_json(snap_path)
            ls1 = data1["a"]["last_seen"]
            # Sleep just enough to advance the clock past the timestamp
            # resolution. Microseconds are present in datetime.now(timezone.utc).
            await asyncio.sleep(0.001)
            await snap.record(
                instance_id="a", window_id="@1", pane_text="x", state=Idle()
            )
        finally:
            await snap.close()
        data2 = _read_json(snap_path)
        assert set(data2.keys()) == {"a"}
        assert data2["a"]["last_seen"] >= ls1

    @pytest.mark.asyncio
    async def test_two_instances_both_appear(self, snap_path: Path) -> None:
        snap = StateSnapshot(snap_path)
        try:
            await snap.record(
                instance_id="a", window_id="@1", pane_text="x", state=Idle()
            )
            await snap.record(
                instance_id="b",
                window_id="@2",
                pane_text="y",
                state=Working(status_text="Thinking… (1s)"),
            )
        finally:
            await snap.close()
        data = _read_json(snap_path)
        assert set(data.keys()) == {"a", "b"}
        assert data["a"]["state"]["type"] == "Idle"
        assert data["b"]["state"]["type"] == "Working"
        assert data["b"]["state"]["status_text"] == "Thinking… (1s)"

    @pytest.mark.asyncio
    async def test_state_change_overwrites_previous_entry(
        self, snap_path: Path
    ) -> None:
        snap = StateSnapshot(snap_path)
        try:
            await snap.record(
                instance_id="a", window_id="@1", pane_text="x", state=Idle()
            )
            await snap.record(
                instance_id="a",
                window_id="@1",
                pane_text="y",
                state=Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="?"),
            )
        finally:
            await snap.close()
        data = _read_json(snap_path)
        assert data["a"]["state"]["type"] == "Blocked"
        assert data["a"]["state"]["ui"] == "permission_prompt"

    @pytest.mark.asyncio
    async def test_pane_text_not_persisted(self, snap_path: Path) -> None:
        snap = StateSnapshot(snap_path)
        secret = "super-secret-pane-content-do-not-leak"
        try:
            await snap.record(
                instance_id="a", window_id="@1", pane_text=secret, state=Idle()
            )
        finally:
            await snap.close()
        assert secret not in snap_path.read_text()

    @pytest.mark.asyncio
    async def test_concurrent_records_produce_valid_json(self, snap_path: Path) -> None:
        snap = StateSnapshot(snap_path)
        try:

            async def hammer(i: int) -> None:
                for _ in range(4):
                    await snap.record(
                        instance_id=f"i{i}",
                        window_id=f"@{i}",
                        pane_text="x",
                        state=Idle(),
                    )

            await asyncio.gather(*(hammer(i) for i in range(20)))
        finally:
            await snap.close()
        data = _read_json(snap_path)
        # Every instance present
        assert set(data.keys()) == {f"i{i}" for i in range(20)}

    @pytest.mark.asyncio
    async def test_close_is_noop_no_change_to_file(self, snap_path: Path) -> None:
        snap = StateSnapshot(snap_path)
        await snap.record(instance_id="a", window_id="@1", pane_text="x", state=Idle())
        before = snap_path.read_text()
        await snap.close()
        after = snap_path.read_text()
        assert before == after

    def test_init_creates_parent_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "missing" / "state_current.json"
        # Should NOT raise even though the parent doesn't exist yet.
        StateSnapshot(nested)
        # The directory now exists (file may or may not — only created on
        # first record()).
        assert nested.parent.is_dir()

    @pytest.mark.asyncio
    async def test_init_does_not_clobber_existing_file_until_first_record(
        self, snap_path: Path
    ) -> None:
        snap_path.write_text(
            '{"old": {"state": {"type": "Idle"}, "window_id": "@x", "last_seen": "2020-01-01T00:00:00+00:00"}}'
        )
        snap = StateSnapshot(snap_path)
        # Construction alone should not have rewritten the file.
        assert "old" in snap_path.read_text()
        # First record() rewrites with fresh content (does NOT preserve 'old',
        # since we don't load-from-disk on init — see spec).
        try:
            await snap.record(
                instance_id="a", window_id="@1", pane_text="x", state=Idle()
            )
        finally:
            await snap.close()
        data = _read_json(snap_path)
        assert "old" not in data
        assert "a" in data

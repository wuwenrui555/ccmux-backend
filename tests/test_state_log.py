"""Tests for ccmux.state_log — JSONL state-log writer."""

from __future__ import annotations

import json

import pytest

from claude_code_state import Blocked, BlockedUI, Dead, Idle, Working

from ccmux.state_log import _serialize_state


class TestSerializeState:
    def test_working(self) -> None:
        s = Working(status_text="Thinking… (3s)")
        assert _serialize_state(s) == {
            "type": "Working",
            "status_text": "Thinking… (3s)",
        }

    def test_idle(self) -> None:
        s = Idle()
        assert _serialize_state(s) == {"type": "Idle"}

    def test_blocked(self) -> None:
        s = Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="Allow Bash...")
        assert _serialize_state(s) == {
            "type": "Blocked",
            "ui": "permission_prompt",
            "content": "Allow Bash...",
        }

    def test_dead(self) -> None:
        s = Dead()
        assert _serialize_state(s) == {"type": "Dead"}

    def test_serialized_is_json_safe(self) -> None:
        for s in (
            Working(status_text="Thinking… (3s)"),
            Idle(),
            Blocked(ui=BlockedUI.ASK_USER_QUESTION, content="Pick one"),
            Dead(),
        ):
            d = _serialize_state(s)
            json.dumps(d)


import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from ccmux.state_log import StateLog


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestStateLogSingleInstance:
    @pytest.fixture
    def log_path(self, tmp_path: Path) -> Path:
        return tmp_path / "state.jsonl"

    @pytest.mark.asyncio
    async def test_init_opens_file_and_first_record_stages_only(
        self, log_path: Path
    ) -> None:
        log = StateLog(log_path)
        try:
            await log.record(
                instance_id="a",
                window_id="@1",
                pane_text="screen v1",
                state=Idle(),
            )
            assert _read_jsonl(log_path) == []
        finally:
            await log.close()

    @pytest.mark.asyncio
    async def test_identical_pane_text_bumps_tick_count_no_write(
        self, log_path: Path
    ) -> None:
        log = StateLog(log_path)
        try:
            for _ in range(3):
                await log.record(
                    instance_id="a",
                    window_id="@1",
                    pane_text="screen v1",
                    state=Idle(),
                )
            assert _read_jsonl(log_path) == []
        finally:
            await log.close()
        # close() flushes; the staged record now has tick_count == 3.
        records = _read_jsonl(log_path)
        assert len(records) == 1
        assert records[0]["tick_count"] == 3
        assert records[0]["pane_text"] == "screen v1"
        assert records[0]["state"] == {"type": "Idle"}
        assert records[0]["instance_id"] == "a"
        assert records[0]["window_id"] == "@1"

    @pytest.mark.asyncio
    async def test_pane_text_change_flushes_previous(self, log_path: Path) -> None:
        log = StateLog(log_path)
        try:
            await log.record(
                instance_id="a",
                window_id="@1",
                pane_text="screen v1",
                state=Idle(),
            )
            await log.record(
                instance_id="a",
                window_id="@1",
                pane_text="screen v1",
                state=Idle(),
            )
            # File still empty: prev is staged.
            assert _read_jsonl(log_path) == []

            await log.record(
                instance_id="a",
                window_id="@1",
                pane_text="screen v2",
                state=Working(status_text="Thinking… (1s)"),
            )
            # Old record flushed; new record staged.
            records = _read_jsonl(log_path)
            assert len(records) == 1
            assert records[0]["pane_text"] == "screen v1"
            assert records[0]["tick_count"] == 2
            assert records[0]["state"] == {"type": "Idle"}
        finally:
            await log.close()
        # After close, both flushed.
        records = _read_jsonl(log_path)
        assert len(records) == 2
        assert records[1]["pane_text"] == "screen v2"
        assert records[1]["tick_count"] == 1
        assert records[1]["state"] == {
            "type": "Working",
            "status_text": "Thinking… (1s)",
        }

    @pytest.mark.asyncio
    async def test_first_seen_and_last_seen_iso8601_utc(self, log_path: Path) -> None:
        log = StateLog(log_path)
        try:
            for _ in range(2):
                await log.record(
                    instance_id="a",
                    window_id="@1",
                    pane_text="screen",
                    state=Idle(),
                )
        finally:
            await log.close()
        rec = _read_jsonl(log_path)[0]
        first = datetime.fromisoformat(rec["first_seen"])
        last = datetime.fromisoformat(rec["last_seen"])
        assert first.tzinfo is not None
        assert last.tzinfo is not None
        assert last >= first

    @pytest.mark.asyncio
    async def test_close_flushes_staged_record(self, log_path: Path) -> None:
        log = StateLog(log_path)
        await log.record(
            instance_id="a",
            window_id="@1",
            pane_text="only",
            state=Idle(),
        )
        assert _read_jsonl(log_path) == []
        await log.close()
        records = _read_jsonl(log_path)
        assert len(records) == 1
        assert records[0]["pane_text"] == "only"
        assert records[0]["tick_count"] == 1

    @pytest.mark.asyncio
    async def test_reopen_appends_does_not_truncate(self, log_path: Path) -> None:
        log1 = StateLog(log_path)
        await log1.record(
            instance_id="a", window_id="@1", pane_text="first", state=Idle()
        )
        await log1.close()

        log2 = StateLog(log_path)
        await log2.record(
            instance_id="a", window_id="@1", pane_text="second", state=Idle()
        )
        await log2.close()

        records = _read_jsonl(log_path)
        assert [r["pane_text"] for r in records] == ["first", "second"]

    def test_init_raises_when_parent_dir_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            StateLog(tmp_path / "missing-dir" / "state.jsonl")

    @pytest.mark.asyncio
    async def test_emitted_lines_are_valid_json(self, log_path: Path) -> None:
        log = StateLog(log_path)
        try:
            await log.record(
                instance_id="a", window_id="@1", pane_text="x", state=Idle()
            )
            await log.record(
                instance_id="a",
                window_id="@1",
                pane_text="y",
                state=Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="?"),
            )
        finally:
            await log.close()
        for line in log_path.read_text().splitlines():
            assert line.strip()
            json.loads(line)


class TestStateLogMultiInstance:
    @pytest.fixture
    def log_path(self, tmp_path: Path) -> Path:
        return tmp_path / "state.jsonl"

    @pytest.mark.asyncio
    async def test_two_instances_independently_staged(self, log_path: Path) -> None:
        log = StateLog(log_path)
        try:
            await log.record(
                instance_id="a", window_id="@1", pane_text="A1", state=Idle()
            )
            await log.record(
                instance_id="b", window_id="@2", pane_text="B1", state=Idle()
            )
            await log.record(
                instance_id="a", window_id="@1", pane_text="A1", state=Idle()
            )
            # Both have only one staged record; nothing written yet.
            assert _read_jsonl(log_path) == []

            # 'a' changes pane: only 'a' flushes, 'b' stays staged.
            await log.record(
                instance_id="a", window_id="@1", pane_text="A2", state=Idle()
            )
            records = _read_jsonl(log_path)
            assert len(records) == 1
            assert records[0]["instance_id"] == "a"
            assert records[0]["pane_text"] == "A1"
            assert records[0]["tick_count"] == 2
        finally:
            await log.close()

        # close() flushed remaining staged records ('b' B1, 'a' A2).
        records = _read_jsonl(log_path)
        assert len(records) == 3
        # Order in file is: A1 (flushed early), then close-time flush of dict.
        # The remaining two are 'a' A2 and 'b' B1; their order depends on
        # dict iteration. Assert by membership rather than order.
        remaining = sorted(
            (r["instance_id"], r["pane_text"]) for r in records[1:]
        )
        assert remaining == [("a", "A2"), ("b", "B1")]


class TestStateLogConcurrency:
    @pytest.fixture
    def log_path(self, tmp_path: Path) -> Path:
        return tmp_path / "state.jsonl"

    @pytest.mark.asyncio
    async def test_concurrent_records_no_torn_lines(self, log_path: Path) -> None:
        log = StateLog(log_path)
        try:
            # Each instance toggles between two pane texts so every other
            # call flushes a record. Run 50 instances * 4 calls = 200
            # record() invocations concurrently.
            async def hammer(i: int) -> None:
                for j in range(4):
                    await log.record(
                        instance_id=f"i{i}",
                        window_id=f"@{i}",
                        pane_text=f"v{j % 2}",
                        state=Idle(),
                    )

            await asyncio.gather(*(hammer(i) for i in range(50)))
        finally:
            await log.close()

        # Every line must be valid JSON (no torn writes).
        lines = log_path.read_text().splitlines()
        for line in lines:
            assert line.strip()
            json.loads(line)
        # Every instance contributes at least one record.
        instance_ids = {json.loads(line)["instance_id"] for line in lines}
        assert instance_ids == {f"i{i}" for i in range(50)}

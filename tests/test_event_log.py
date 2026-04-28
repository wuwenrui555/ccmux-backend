"""Tests for the v4.0.0 append-only event log: schema, writer, reader, compact.

Covers:
- HookEvent / TmuxInfo / ClaudeInfo serialization round-trip.
- EventLogWriter atomic single-write appends (including concurrent stress).
- EventLogReader projection (last-event-wins per tmux_session_name).
- EventLogReader async poll loop picks up new appends.
- compact() collapses multi-event log to one event per tmux_session_name.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


from ccmux.event_log import (
    ClaudeInfo,
    CurrentClaudeBinding,
    EventLogReader,
    EventLogWriter,
    HookEvent,
    TmuxInfo,
    compact,
)


def _make_event(
    name: str,
    claude_id: str,
    ts: datetime,
    *,
    window_id: str = "@5",
    transcript_path: str = "",
    cwd: str = "/c",
    event: str = "UserPromptSubmit",
) -> HookEvent:
    return HookEvent(
        timestamp=ts,
        hook_event=event,
        tmux=TmuxInfo("$1", name, window_id, "1", "n", "%1", "1"),
        claude=ClaudeInfo(
            claude_id,
            transcript_path or f"/p/{claude_id}.jsonl",
            cwd,
            "default",
        ),
    )


# ------------------- Schema (Task 1) -------------------


class TestHookEventSchema:
    def test_serialize_in_tmux(self) -> None:
        e = HookEvent(
            timestamp=datetime(2026, 4, 28, 15, 43, 43, tzinfo=timezone.utc),
            hook_event="UserPromptSubmit",
            tmux=TmuxInfo(
                session_id="$574",
                session_name="ccmux",
                window_id="@5",
                window_index="1",
                window_name="wenruiwu",
                pane_id="%1",
                pane_index="1",
            ),
            claude=ClaudeInfo(
                session_id="a61a3a01-0cbb-48f1-8ba3-9cc0d9e53faf",
                transcript_path="/path/to/jsonl",
                cwd="/home/u",
                permission_mode="default",
            ),
        )
        line = e.to_jsonl()
        assert line.endswith("\n")
        assert len(line.encode("utf-8")) < 4096  # PIPE_BUF safety
        payload = json.loads(line)
        assert payload["hook_event"] == "UserPromptSubmit"
        assert payload["tmux"]["session_name"] == "ccmux"
        assert payload["tmux"]["window_id"] == "@5"
        assert payload["claude"]["session_id"] == "a61a3a01-0cbb-48f1-8ba3-9cc0d9e53faf"
        assert payload["claude"]["transcript_path"] == "/path/to/jsonl"

    def test_serialize_out_of_tmux(self) -> None:
        e = HookEvent(
            timestamp=datetime(2026, 4, 28, tzinfo=timezone.utc),
            hook_event="SessionStart",
            tmux=TmuxInfo.empty(),
            claude=ClaudeInfo(
                session_id="uuid",
                transcript_path="/p",
                cwd="/c",
                permission_mode="default",
            ),
        )
        payload = json.loads(e.to_jsonl())
        assert payload["tmux"]["session_name"] == ""
        assert payload["tmux"]["window_id"] == ""
        assert payload["tmux"]["pane_id"] == ""

    def test_parse_roundtrip(self) -> None:
        src = HookEvent(
            timestamp=datetime(2026, 4, 28, tzinfo=timezone.utc),
            hook_event="SessionStart",
            tmux=TmuxInfo("$1", "ccmux", "@5", "1", "n", "%1", "1"),
            claude=ClaudeInfo("uuid", "/p", "/c", "default"),
        )
        line = src.to_jsonl()
        parsed = HookEvent.from_jsonl(line)
        assert parsed == src


# ------------------- Writer (Task 2) -------------------


class TestEventLogWriter:
    def test_appends_one_line(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        writer = EventLogWriter(log)
        writer.append(
            _make_event("ccmux", "u1", datetime(2026, 4, 28, tzinfo=timezone.utc))
        )
        assert log.exists()
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        parsed = HookEvent.from_jsonl(lines[0] + "\n")
        assert parsed.tmux.session_name == "ccmux"
        assert parsed.claude.session_id == "u1"

    def test_appends_multiple_preserves_order(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        writer = EventLogWriter(log)
        for i in range(5):
            writer.append(
                _make_event(
                    "ccmux",
                    f"u{i}",
                    datetime(2026, 4, 28, 12, i, tzinfo=timezone.utc),
                )
            )
        lines = log.read_text().splitlines()
        assert len(lines) == 5
        ids = [HookEvent.from_jsonl(line + "\n").claude.session_id for line in lines]
        assert ids == ["u0", "u1", "u2", "u3", "u4"]

    def test_concurrent_appends_no_torn_writes(self, tmp_path: Path) -> None:
        """Spawn 20 subprocesses each writing one line; all 20 must come back intact."""
        log = tmp_path / "events.jsonl"
        helper = tmp_path / "helper.py"
        helper.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from datetime import datetime, timezone\n"
            "from ccmux.event_log import (\n"
            "    EventLogWriter, HookEvent, TmuxInfo, ClaudeInfo,\n"
            ")\n"
            "log = Path(sys.argv[1])\n"
            "uid = sys.argv[2]\n"
            "EventLogWriter(log).append(HookEvent(\n"
            "    timestamp=datetime.now(timezone.utc),\n"
            "    hook_event='UserPromptSubmit',\n"
            "    tmux=TmuxInfo('$1', 'ccmux', '@5', '1', 'n', '%1', '1'),\n"
            "    claude=ClaudeInfo(uid, '/p', '/c', 'default'),\n"
            "))\n"
        )
        procs = [
            subprocess.Popen([sys.executable, str(helper), str(log), f"u{i}"])
            for i in range(20)
        ]
        for p in procs:
            assert p.wait() == 0
        lines = [line for line in log.read_text().splitlines() if line.strip()]
        assert len(lines) == 20
        # Every line must parse — proves no torn writes interleaved.
        for line in lines:
            HookEvent.from_jsonl(line + "\n")


# ------------------- Reader projection (Task 4) -------------------


class TestEventLogReader:
    def test_empty_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.touch()
        r = EventLogReader(log)
        r.refresh()
        assert r.get("ccmux") is None
        assert r.all_alive() == []

    def test_one_event_projects(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        EventLogWriter(log).append(
            _make_event(
                "ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
            )
        )
        r = EventLogReader(log)
        r.refresh()
        b = r.get("ccmux")
        assert b is not None
        assert isinstance(b, CurrentClaudeBinding)
        assert b.tmux_session_name == "ccmux"
        assert b.window_id == "@5"
        assert b.claude_session_id == "uA"

    def test_last_write_wins_per_tmux_name(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        w = EventLogWriter(log)
        w.append(
            _make_event(
                "ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
            )
        )
        w.append(
            _make_event(
                "ccmux", "uB", datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc)
            )
        )
        r = EventLogReader(log)
        r.refresh()
        b = r.get("ccmux")
        assert b is not None
        assert b.claude_session_id == "uB"  # /clear-style overwrite

    def test_skips_empty_tmux_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        e = HookEvent(
            timestamp=datetime(2026, 4, 28, tzinfo=timezone.utc),
            hook_event="SessionStart",
            tmux=TmuxInfo.empty(),
            claude=ClaudeInfo("uX", "/p", "/c", "default"),
        )
        EventLogWriter(log).append(e)
        r = EventLogReader(log)
        r.refresh()
        assert r.all_alive() == []  # not routable

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        # Pre-seed a bad line, then append a good one.
        log.write_text("not json\n{invalid\n")
        EventLogWriter(log).append(
            _make_event("ccmux", "u1", datetime(2026, 4, 28, tzinfo=timezone.utc))
        )
        r = EventLogReader(log)
        r.refresh()
        assert r.get("ccmux") is not None
        assert r.get("ccmux").claude_session_id == "u1"

    def test_multi_tmux_independent_rows(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        w = EventLogWriter(log)
        w.append(
            _make_event(
                "ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
            )
        )
        w.append(
            _make_event(
                "daily",
                "uD",
                datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc),
                window_id="@6",
            )
        )
        r = EventLogReader(log)
        r.refresh()
        assert r.get("ccmux").claude_session_id == "uA"
        assert r.get("daily").claude_session_id == "uD"
        assert r.get("daily").window_id == "@6"
        names = {b.tmux_session_name for b in r.all_alive()}
        assert names == {"ccmux", "daily"}


# ------------------- Reader poll loop (Task 5) -------------------


class TestEventLogReaderPolling:
    async def test_picks_up_appends_during_poll(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.touch()
        r = EventLogReader(log, poll_interval=0.05)
        await r.start()
        try:
            EventLogWriter(log).append(
                _make_event(
                    "ccmux",
                    "uA",
                    datetime(2026, 4, 28, tzinfo=timezone.utc),
                )
            )
            # Wait up to 1 s for the poll to consume it.
            for _ in range(20):
                if r.get("ccmux") is not None:
                    break
                await asyncio.sleep(0.05)
            b = r.get("ccmux")
            assert b is not None
            assert b.claude_session_id == "uA"
        finally:
            await r.stop()


# ------------------- compact (Task 10) -------------------


class TestCompact:
    def test_collapses_to_one_per_tmux_name(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        w = EventLogWriter(log)
        w.append(
            _make_event(
                "ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
            )
        )
        w.append(
            _make_event(
                "ccmux", "uB", datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc)
            )
        )
        w.append(
            _make_event(
                "daily",
                "uC",
                datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc),
                window_id="@6",
            )
        )
        assert len(log.read_text().splitlines()) == 3

        n_before, n_after = compact(log)
        assert n_before == 3
        assert n_after == 2

        r = EventLogReader(log)
        r.refresh()
        assert r.get("ccmux").claude_session_id == "uB"
        assert r.get("daily").claude_session_id == "uC"

    def test_compact_missing_file_is_safe(self, tmp_path: Path) -> None:
        log = tmp_path / "absent.jsonl"
        n_before, n_after = compact(log)
        assert n_before == 0
        assert n_after == 0

<!-- markdownlint-disable MD024 -->

# Event Log Self-Heal Implementation Plan (v4.0.0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans-test-first to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `claude_instances.json` + override layer + `reconcile_instance` with an append-only JSONL event log written by `SessionStart` and `UserPromptSubmit` hooks. Backend derives current state by projecting the log on every read.

**Architecture:** Hook becomes append-only, no read-modify-write, no overwrite guard. New `EventLogReader` module in `ccmux-backend` tails the log and projects to `dict[tmux_session_name, CurrentClaudeBinding]`. Frontend (`ccmux-telegram`) drops the override-layer call sites and the `/rebind_window` command. Both repos ship as `v4.0.0`.

**Tech Stack:** Python 3.12+, asyncio, `python-telegram-bot`, `libtmux`, `uv`. No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-04-28-event-log-self-heal-design.md`](../specs/2026-04-28-event-log-self-heal-design.md)

**Repos affected:**

- `ccmux-backend` at `/mnt/md0/home/wenruiwu/projects/ccmux/ccmux-backend` (major: hook + reader + API)
- `ccmux-telegram` at `/mnt/md0/home/wenruiwu/projects/ccmux/ccmux-telegram` (major: drop override usage, drop `/rebind_window`)

**Branch strategy:** git-flow. Backend on `feature/event-log-self-heal` branched off `dev`. Telegram on `feature/event-log-self-heal` branched off `dev`. Two separate `release/v4.0.0` branches at the end (backend ships first).

---

## Phase 1: Additive — Event log writing alongside existing

After this phase, hook writes BOTH `claude_instances.json` (legacy) AND `claude_events.jsonl` (new). Frontend still uses old code path. Bot keeps working. Safe to land on `dev`.

### Task 1: Branch + Event schema dataclass

**Files:**

- Create: `src/ccmux/event_log.py` (new module — schema + writer)
- Test: `tests/test_event_log.py`

- [ ] **Step 1: Create feature branch**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-backend
git checkout dev
git pull
git checkout -b feature/event-log-self-heal
```

- [ ] **Step 2: Write the failing test**

`tests/test_event_log.py`:

```python
"""Tests for the event log schema and serialization."""

import json
from datetime import datetime, timezone

from ccmux.event_log import HookEvent, TmuxInfo, ClaudeInfo


def test_hook_event_serialize_in_tmux():
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
    assert len(line) < 4096  # PIPE_BUF safety
    payload = json.loads(line)
    assert payload["hook_event"] == "UserPromptSubmit"
    assert payload["tmux"]["session_name"] == "ccmux"
    assert payload["claude"]["session_id"] == "a61a3a01-0cbb-48f1-8ba3-9cc0d9e53faf"


def test_hook_event_serialize_out_of_tmux():
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


def test_hook_event_parse_roundtrip():
    src = HookEvent(
        timestamp=datetime(2026, 4, 28, tzinfo=timezone.utc),
        hook_event="SessionStart",
        tmux=TmuxInfo("$1", "ccmux", "@5", "1", "n", "%1", "1"),
        claude=ClaudeInfo("uuid", "/p", "/c", "default"),
    )
    line = src.to_jsonl()
    parsed = HookEvent.from_jsonl(line)
    assert parsed == src
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_event_log.py -v
```

Expected: `ImportError: cannot import name 'HookEvent' from 'ccmux.event_log'`.

- [ ] **Step 4: Write minimal implementation**

`src/ccmux/event_log.py`:

```python
"""Append-only event log: schema, writer, reader.

The hook writes one JSONL line per Claude Code lifecycle event
(SessionStart, UserPromptSubmit). Backend projects the log into
an in-memory dict[tmux_session_name, CurrentClaudeBinding].

Each line is one self-contained JSON object terminated by '\\n'
and kept under 4 KB so POSIX O_APPEND single-write atomicity holds
across concurrent hooks without explicit locking.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class TmuxInfo:
    session_id: str
    session_name: str
    window_id: str
    window_index: str
    window_name: str
    pane_id: str
    pane_index: str

    @classmethod
    def empty(cls) -> "TmuxInfo":
        return cls("", "", "", "", "", "", "")

    @classmethod
    def from_dict(cls, d: dict) -> "TmuxInfo":
        return cls(
            session_id=d.get("session_id", ""),
            session_name=d.get("session_name", ""),
            window_id=d.get("window_id", ""),
            window_index=d.get("window_index", ""),
            window_name=d.get("window_name", ""),
            pane_id=d.get("pane_id", ""),
            pane_index=d.get("pane_index", ""),
        )


@dataclass(frozen=True)
class ClaudeInfo:
    session_id: str
    transcript_path: str
    cwd: str
    permission_mode: str

    @classmethod
    def from_dict(cls, d: dict) -> "ClaudeInfo":
        return cls(
            session_id=d.get("session_id", ""),
            transcript_path=d.get("transcript_path", ""),
            cwd=d.get("cwd", ""),
            permission_mode=d.get("permission_mode", ""),
        )


@dataclass(frozen=True)
class HookEvent:
    timestamp: datetime
    hook_event: str
    tmux: TmuxInfo
    claude: ClaudeInfo

    def to_jsonl(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "hook_event": self.hook_event,
            "tmux": asdict(self.tmux),
            "claude": asdict(self.claude),
        }
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @classmethod
    def from_jsonl(cls, line: str) -> "HookEvent":
        d = json.loads(line)
        return cls(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            hook_event=d["hook_event"],
            tmux=TmuxInfo.from_dict(d.get("tmux", {})),
            claude=ClaudeInfo.from_dict(d.get("claude", {})),
        )
```

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_event_log.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/event_log.py tests/test_event_log.py
git commit -m "$(cat <<'EOF'
feat(event-log): add HookEvent schema dataclasses

Schema for the v4.0.0 append-only JSONL event log: TmuxInfo,
ClaudeInfo, HookEvent. Round-trips JSON, terminates with newline,
stays under PIPE_BUF (4 KB) for atomic O_APPEND writes.

Part of v4.0.0 event-log-self-heal redesign (see spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Event log writer (atomic append)

**Files:**

- Modify: `src/ccmux/event_log.py` (add `EventLogWriter`)
- Modify: `tests/test_event_log.py` (add writer tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_log.py`:

```python
import os
from datetime import datetime, timezone
from pathlib import Path

from ccmux.event_log import EventLogWriter, HookEvent, TmuxInfo, ClaudeInfo


def _make_event(name: str, claude_id: str, ts: datetime) -> HookEvent:
    return HookEvent(
        timestamp=ts,
        hook_event="UserPromptSubmit",
        tmux=TmuxInfo("$1", name, "@5", "1", "n", "%1", "1"),
        claude=ClaudeInfo(claude_id, f"/p/{claude_id}.jsonl", "/c", "default"),
    )


def test_writer_appends_one_line(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    writer = EventLogWriter(log)
    writer.append(_make_event("ccmux", "u1", datetime(2026, 4, 28, tzinfo=timezone.utc)))
    assert log.exists()
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    parsed = HookEvent.from_jsonl(lines[0] + "\n")
    assert parsed.tmux.session_name == "ccmux"


def test_writer_appends_multiple_preserves_order(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    writer = EventLogWriter(log)
    for i in range(5):
        writer.append(_make_event("ccmux", f"u{i}", datetime(2026, 4, 28, 12, i, tzinfo=timezone.utc)))
    lines = log.read_text().splitlines()
    assert len(lines) == 5
    ids = [HookEvent.from_jsonl(l + "\n").claude.session_id for l in lines]
    assert ids == ["u0", "u1", "u2", "u3", "u4"]


def test_writer_concurrent_appends_no_torn_writes(tmp_path: Path):
    """Spawn N processes that each write one line; verify N intact lines."""
    import subprocess
    import sys

    log = tmp_path / "events.jsonl"
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from datetime import datetime, timezone\n"
        "from ccmux.event_log import EventLogWriter, HookEvent, TmuxInfo, ClaudeInfo\n"
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
    lines = [l for l in log.read_text().splitlines() if l.strip()]
    assert len(lines) == 20
    for line in lines:
        HookEvent.from_jsonl(line + "\n")  # parse must succeed
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_event_log.py::test_writer_appends_one_line -v
```

Expected: `ImportError: cannot import name 'EventLogWriter'`.

- [ ] **Step 3: Implement EventLogWriter**

Append to `src/ccmux/event_log.py`:

```python
import os
from pathlib import Path


class EventLogWriter:
    """Atomic single-line appender.

    Writes each HookEvent as one O_APPEND write() syscall on a regular
    file. Lines under PIPE_BUF (~4 KB) interleave atomically across
    concurrent hooks without explicit locking.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: HookEvent) -> None:
        line = event.to_jsonl()
        # NOTE: must be < PIPE_BUF for atomic concurrent appends.
        # HookEvent's fixed-shape payload is well under 1 KB.
        assert len(line.encode("utf-8")) < 4096, "event line exceeds PIPE_BUF"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open with O_APPEND so the kernel ensures each write goes to EOF.
        # Single write() of the full line is the atomic unit.
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
```

- [ ] **Step 4: Run all event_log tests**

```bash
uv run pytest tests/test_event_log.py -v
```

Expected: 6 passed (3 schema + 3 writer including concurrent).

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/event_log.py tests/test_event_log.py
git commit -m "$(cat <<'EOF'
feat(event-log): add EventLogWriter atomic single-write appender

O_APPEND + single os.write() per event is atomic across concurrent
hooks for lines under PIPE_BUF. No fcntl needed.

The concurrent-appends test spawns 20 subprocesses writing to the
same file and verifies 20 intact parseable lines come back.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Hook writes to event log (alongside legacy)

Hook keeps writing `claude_instances.json` (no behavior change for existing readers) AND now also writes `claude_events.jsonl`. Hook starts dispatching on `UserPromptSubmit` too.

**Files:**

- Modify: `src/ccmux/hook.py:212-406` (the `hook_main` body)
- Test: `tests/test_hook.py` (existing — extend)

- [ ] **Step 1: Read the existing hook tests to learn fixtures**

```bash
ls tests/ | grep hook
uv run pytest tests/test_hook.py --collect-only 2>&1 | head -40
```

Note the fixture pattern (likely uses `monkeypatch` for env, `subprocess.run` mocked for tmux, JSON fed via `sys.stdin`).

- [ ] **Step 2: Write failing test for UserPromptSubmit dispatch**

Append to `tests/test_hook.py` (match existing fixture style):

```python
def test_hook_dispatches_user_prompt_submit_to_event_log(
    tmp_path, monkeypatch
):
    """UserPromptSubmit fires -> event_log gets a new line."""
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%1")

    # Mock tmux display-message (returns 'ccmux:@5')
    import subprocess
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["tmux", "display-message"]:
            class R:
                stdout = "ccmux:@5\n"
                returncode = 0
            return R()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("ccmux.hook.subprocess.run", fake_run)

    payload = {
        "session_id": "uuid-1234",
        "cwd": "/home/u",
        "transcript_path": "/p/t.jsonl",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    from ccmux.hook import _hook_main_impl
    _hook_main_impl()

    log = tmp_path / "claude_events.jsonl"
    assert log.exists()
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    from ccmux.event_log import HookEvent
    e = HookEvent.from_jsonl(lines[0] + "\n")
    assert e.hook_event == "UserPromptSubmit"
    assert e.tmux.session_name == "ccmux"
    assert e.tmux.window_id == "@5"
    assert e.claude.session_id == "uuid-1234"
    assert e.claude.transcript_path == "/p/t.jsonl"


def test_hook_session_start_writes_both_legacy_and_new(tmp_path, monkeypatch):
    """SessionStart still writes claude_instances.json; ALSO writes event log."""
    # ... same setup as above with hook_event_name=SessionStart
    # assert claude_instances.json has the row
    # assert claude_events.jsonl has the line
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_hook.py::test_hook_dispatches_user_prompt_submit_to_event_log -v
```

Expected: today's hook returns early on non-SessionStart, so the log stays empty.

- [ ] **Step 4: Modify hook to dispatch + write event log**

In `src/ccmux/hook.py:_hook_main_impl`, replace the `if event != "SessionStart": return` early-out (line 274) and add an event-log write call after the existing `claude_instances.json` block.

```python
# At the top of _hook_main_impl, after parsing payload:
ACCEPTED_EVENTS = {"SessionStart", "UserPromptSubmit"}
if event not in ACCEPTED_EVENTS:
    logger.debug("Ignoring event: %s", event)
    return

# transcript_path is provided directly by Claude Code in the payload
transcript_path = payload.get("transcript_path", "") if "payload" in dir() else ""
permission_mode = payload.get("permission_mode", "") if "payload" in dir() else ""

# (existing tmux session_name + window_id resolution stays unchanged)

# Existing claude_instances.json read-modify-write block stays (do not delete yet).
# It only fires for SessionStart to preserve legacy behavior.
if event == "SessionStart":
    # ... existing block writing claude_instances.json ...
    pass

# NEW: also append to event log (every accepted event)
from datetime import datetime, timezone
from .event_log import EventLogWriter, HookEvent, TmuxInfo, ClaudeInfo
from .util import ccmux_dir

writer = EventLogWriter(ccmux_dir() / "claude_events.jsonl")
writer.append(HookEvent(
    timestamp=datetime.now(timezone.utc),
    hook_event=event,
    tmux=TmuxInfo(
        session_id="",  # tmux's $-id is not currently captured; lift later if needed
        session_name=tmux_session_name,
        window_id=window_id,
        window_index="",
        window_name="",
        pane_id=pane_id,
        pane_index="",
    ),
    claude=ClaudeInfo(
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=cwd,
        permission_mode=permission_mode,
    ),
))
logger.debug("Appended event log entry: event=%s session=%s", event, session_id)
```

Where the tmux $-id and indexes are needed: extend the `tmux display-message` call to fetch `#{session_id}:#{window_id}:#{window_index}:#{window_name}:#{pane_index}` and split. Update the parsing block accordingly. Leave `tmux.session_id` empty (`""`) for v4.0.0 if pulling all fields is too invasive in this task; field is recorded but not consumed by the projection.

- [ ] **Step 5: Run all hook tests**

```bash
uv run pytest tests/test_hook.py -v
```

Expected: existing tests still pass (legacy behavior preserved), 2 new tests pass.

- [ ] **Step 6: Update --install to register UserPromptSubmit**

In `src/ccmux/hook.py:_install_hook`, replace the SessionStart-only registration with a loop over both events:

```python
EVENTS_TO_REGISTER = ["SessionStart", "UserPromptSubmit"]

for ev in EVENTS_TO_REGISTER:
    if ev not in settings["hooks"]:
        settings["hooks"][ev] = []
    # idempotency: skip if a matching command is already there
    already = any(
        h.get("command", "") == hook_command or h.get("command", "").endswith("/" + _HOOK_COMMAND_SUFFIX)
        for entry in settings["hooks"][ev]
        if isinstance(entry, dict)
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )
    if already:
        continue
    settings["hooks"][ev].append({"hooks": [hook_config]})
```

Update `_is_hook_installed` to take an `event` argument and check the specified event's section.

- [ ] **Step 7: Add test for --install registers both events**

```python
def test_install_registers_session_start_and_user_prompt_submit(tmp_path, monkeypatch):
    monkeypatch.setattr("ccmux.hook._CLAUDE_SETTINGS_FILE", tmp_path / "settings.json")
    from ccmux.hook import _install_hook
    assert _install_hook() == 0
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]
```

- [ ] **Step 8: Run hook + install tests**

```bash
uv run pytest tests/test_hook.py -v
```

Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "$(cat <<'EOF'
feat(hook): dispatch on UserPromptSubmit and append to event log

Hook now accepts SessionStart + UserPromptSubmit. Each accepted event
appends one line to ~/.ccmux/claude_events.jsonl via EventLogWriter
(atomic O_APPEND).

claude_instances.json read-modify-write (with overwrite guard) is
preserved unchanged so existing readers keep working. Phase 3 will
remove it once the new reader is in place.

ccmux hook --install now also registers UserPromptSubmit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2: Reader + backend wiring (still non-breaking)

`EventLogReader` exists alongside `ClaudeInstanceRegistry`. `Backend.get_instance` queries the reader and falls back to the registry if the reader has no row (so older Claudes still findable until their next prompt fires the new hook). Self-healing kicks in here.

### Task 4: EventLogReader projection (initial read)

**Files:**

- Modify: `src/ccmux/event_log.py` (add `EventLogReader`, `CurrentClaudeBinding`)
- Test: `tests/test_event_log.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_event_log.py`:

```python
from ccmux.event_log import EventLogReader, CurrentClaudeBinding


def test_reader_empty_log_returns_none(tmp_path):
    log = tmp_path / "events.jsonl"
    log.touch()
    r = EventLogReader(log)
    r.refresh()
    assert r.get("ccmux") is None
    assert r.all_alive() == []


def test_reader_one_session_start(tmp_path):
    log = tmp_path / "events.jsonl"
    EventLogWriter(log).append(_make_event("ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)))
    r = EventLogReader(log)
    r.refresh()
    b = r.get("ccmux")
    assert b is not None
    assert b.tmux_session_name == "ccmux"
    assert b.window_id == "@5"
    assert b.claude_session_id == "uA"


def test_reader_last_write_wins_per_tmux_name(tmp_path):
    log = tmp_path / "events.jsonl"
    w = EventLogWriter(log)
    w.append(_make_event("ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)))
    w.append(_make_event("ccmux", "uB", datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc)))
    r = EventLogReader(log)
    r.refresh()
    b = r.get("ccmux")
    assert b.claude_session_id == "uB"  # /clear overwrote uA


def test_reader_skips_empty_tmux_lines(tmp_path):
    log = tmp_path / "events.jsonl"
    e = HookEvent(
        timestamp=datetime(2026, 4, 28, tzinfo=timezone.utc),
        hook_event="SessionStart",
        tmux=TmuxInfo.empty(),  # out-of-tmux
        claude=ClaudeInfo("uX", "/p", "/c", "default"),
    )
    EventLogWriter(log).append(e)
    r = EventLogReader(log)
    r.refresh()
    assert r.all_alive() == []  # nothing routable


def test_reader_skips_malformed_lines(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text("not json\n")
    EventLogWriter(log).append(_make_event("ccmux", "u1", datetime(2026, 4, 28, tzinfo=timezone.utc)))
    r = EventLogReader(log)
    r.refresh()
    # Bad line skipped, good line projected
    assert r.get("ccmux") is not None
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/test_event_log.py -v
```

Expected: ImportError on `EventLogReader` and `CurrentClaudeBinding`.

- [ ] **Step 3: Implement reader and binding type**

Append to `src/ccmux/event_log.py`:

```python
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentClaudeBinding:
    tmux_session_name: str
    window_id: str
    claude_session_id: str
    cwd: str
    transcript_path: str
    last_seen: datetime


class EventLogReader:
    """Tail the event log and project to dict[tmux_session_name, binding].

    Last-event-wins per tmux_session_name. Out-of-tmux events
    (empty tmux.session_name) are skipped. Malformed lines are
    logged and skipped without raising.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._current: dict[str, CurrentClaudeBinding] = {}

    def refresh(self) -> None:
        """Read any new bytes since last refresh and update projection."""
        if not self._path.exists():
            return
        size = self._path.stat().st_size
        if size <= self._offset:
            return  # truncated or unchanged; for simplicity, never re-read on truncate
        with self._path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        # Only consume up through the last newline; partial trailing line stays.
        last_nl = text.rfind("\n")
        if last_nl < 0:
            return
        consumable = text[: last_nl + 1]
        self._offset += len(consumable.encode("utf-8"))
        for line in consumable.splitlines():
            self._project_line(line)

    def get(self, tmux_session_name: str) -> CurrentClaudeBinding | None:
        return self._current.get(tmux_session_name)

    def all_alive(self) -> list[CurrentClaudeBinding]:
        return list(self._current.values())

    def _project_line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            ev = HookEvent.from_jsonl(line + "\n")
        except (ValueError, KeyError) as e:
            logger.warning("event_log: skipping malformed line: %s", e)
            return
        if not ev.tmux.session_name:
            return  # out-of-tmux event; not routable
        self._current[ev.tmux.session_name] = CurrentClaudeBinding(
            tmux_session_name=ev.tmux.session_name,
            window_id=ev.tmux.window_id,
            claude_session_id=ev.claude.session_id,
            cwd=ev.claude.cwd,
            transcript_path=ev.claude.transcript_path,
            last_seen=ev.timestamp,
        )
```

- [ ] **Step 4: Run reader tests**

```bash
uv run pytest tests/test_event_log.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/event_log.py tests/test_event_log.py
git commit -m "$(cat <<'EOF'
feat(event-log): EventLogReader projection + CurrentClaudeBinding

Reads append-only JSONL log, projects to dict[tmux_session_name]
with last-write-wins semantics per tmux session. Skips out-of-tmux
events and malformed lines without raising.

Tail-read tracks byte offset; only fully-newline-terminated lines
are consumed (partial trailing lines wait for next refresh).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Reader poll loop (continuous tail)

**Files:**

- Modify: `src/ccmux/event_log.py` (add `start`/`stop` async lifecycle)
- Test: `tests/test_event_log.py`

- [ ] **Step 1: Write failing test**

```python
import asyncio


@pytest.mark.asyncio
async def test_reader_picks_up_appends_during_poll(tmp_path):
    log = tmp_path / "events.jsonl"
    log.touch()
    r = EventLogReader(log, poll_interval=0.05)
    await r.start()
    try:
        EventLogWriter(log).append(
            _make_event("ccmux", "uA", datetime(2026, 4, 28, tzinfo=timezone.utc))
        )
        # Wait up to 1 s for the poll to consume it
        for _ in range(20):
            if r.get("ccmux") is not None:
                break
            await asyncio.sleep(0.05)
        assert r.get("ccmux") is not None
        assert r.get("ccmux").claude_session_id == "uA"
    finally:
        await r.stop()
```

- [ ] **Step 2: Run, verify fails**

```bash
uv run pytest tests/test_event_log.py::test_reader_picks_up_appends_during_poll -v
```

Expected: `EventLogReader.__init__() got unexpected keyword 'poll_interval'`.

- [ ] **Step 3: Add async lifecycle**

Modify `EventLogReader` in `src/ccmux/event_log.py`:

```python
import asyncio


class EventLogReader:
    def __init__(self, path: Path, poll_interval: float = 0.5) -> None:
        self._path = path
        self._offset = 0
        self._current: dict[str, CurrentClaudeBinding] = {}
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self.refresh()  # initial full read
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                self.refresh()
            except Exception:
                logger.exception("event_log: poll iteration failed")
            await asyncio.sleep(self._poll_interval)

    # refresh / get / all_alive / _project_line unchanged from Task 4
```

- [ ] **Step 4: Run all event log tests**

```bash
uv run pytest tests/test_event_log.py -v
```

Expected: all green (existing sync tests + new async test).

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/event_log.py tests/test_event_log.py
git commit -m "$(cat <<'EOF'
feat(event-log): EventLogReader async start/stop + poll loop

Default poll interval 0.5 s matches CCMUX_MONITOR_POLL_INTERVAL.
Initial refresh on start() does a full read; the loop tails new
bytes thereafter. Exceptions inside an iteration are logged and
the loop continues.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Wire reader into DefaultBackend

**Files:**

- Modify: `src/ccmux/backend.py` (add `event_reader` parameter, use in `get_instance`)
- Modify: `src/ccmux/api.py` (re-export `EventLogReader`, `CurrentClaudeBinding`)
- Test: `tests/test_backend.py` or new `tests/test_backend_event_reader.py`

- [ ] **Step 1: Read current `DefaultBackend.__init__` + `get_instance`**

```bash
uv run grep -n "class DefaultBackend\|get_instance\|claude_instances" src/ccmux/backend.py | head -20
```

Note line numbers; you'll modify `__init__` to accept `event_reader`, `start`/`stop` to drive its lifecycle, and `get_instance` to consult the reader first.

- [ ] **Step 2: Write failing integration test**

`tests/test_backend_event_reader.py`:

```python
"""Backend uses EventLogReader as primary instance source."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccmux.event_log import EventLogReader, EventLogWriter, HookEvent, TmuxInfo, ClaudeInfo


@pytest.mark.asyncio
async def test_get_instance_returns_reader_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    log = tmp_path / "claude_events.jsonl"
    EventLogWriter(log).append(HookEvent(
        timestamp=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        hook_event="UserPromptSubmit",
        tmux=TmuxInfo("$1", "ccmux", "@5", "1", "n", "%1", "1"),
        claude=ClaudeInfo("uA", "/p", "/c", "default"),
    ))

    from ccmux.api import DefaultBackend, ClaudeInstanceRegistry, tmux_registry
    backend = DefaultBackend(
        tmux_registry=tmux_registry,
        claude_instances=ClaudeInstanceRegistry(),
        event_reader=EventLogReader(log, poll_interval=0.05),
    )
    await backend.start(on_state=lambda *a, **kw: None, on_message=lambda *a, **kw: None)
    try:
        # Reader pre-loaded the event during start(); query immediately.
        inst = backend.get_instance("ccmux")
        assert inst is not None
        assert inst.window_id == "@5"
        assert inst.claude_session_id == "uA"
    finally:
        await backend.stop()
```

- [ ] **Step 3: Run, verify fails**

```bash
uv run pytest tests/test_backend_event_reader.py -v
```

Expected: `TypeError: __init__() got unexpected keyword 'event_reader'`.

- [ ] **Step 4: Modify DefaultBackend**

In `src/ccmux/backend.py`:

```python
from .event_log import EventLogReader, CurrentClaudeBinding


class DefaultBackend:
    def __init__(
        self,
        tmux_registry,
        claude_instances,
        *,
        event_reader: EventLogReader | None = None,
        show_user_messages: bool | None = None,
    ) -> None:
        self.tmux = tmux_registry
        self.claude_instances = claude_instances
        if event_reader is None:
            from .util import ccmux_dir
            event_reader = EventLogReader(ccmux_dir() / "claude_events.jsonl")
        self.event_reader = event_reader
        # ... existing init logic ...

    async def start(self, *, on_state, on_message) -> None:
        await self.event_reader.start()
        # ... existing start logic ...

    async def stop(self) -> None:
        # ... existing stop logic ...
        await self.event_reader.stop()

    def get_instance(self, instance_id: str):
        # NEW path: reader first
        b = self.event_reader.get(instance_id)
        if b is not None:
            # Adapter back to the existing ClaudeInstance shape so
            # frontend callers don't break in Phase 2.
            from .claude_instance import ClaudeInstance
            return ClaudeInstance(
                instance_id=instance_id,
                window_id=b.window_id,
                session_id=b.claude_session_id,
                cwd=b.cwd,
            )
        # FALLBACK: legacy registry (covers Claude sessions launched
        # before the new hook-event registration was applied).
        return self.claude_instances.get(instance_id)
```

In `src/ccmux/api.py`, re-export:

```python
from .event_log import (
    EventLogReader,
    CurrentClaudeBinding,
    EventLogWriter,
    HookEvent,
)

__all__ = [
    # ... existing ...
    "EventLogReader",
    "CurrentClaudeBinding",
    "EventLogWriter",
    "HookEvent",
]
```

- [ ] **Step 5: Run integration + smoke + existing backend tests**

```bash
uv run pytest tests/test_backend_event_reader.py tests/test_api_smoke.py -v
```

Expected: green. The smoke test pinned the v1.0 API surface; new symbols add but old ones still resolve.

- [ ] **Step 6: Run the whole suite**

```bash
uv run pytest -v
```

Expected: green (or pre-existing flakes flagged).

- [ ] **Step 7: Commit**

```bash
git add src/ccmux/backend.py src/ccmux/api.py tests/test_backend_event_reader.py
git commit -m "$(cat <<'EOF'
feat(backend): wire EventLogReader into DefaultBackend.get_instance

Backend.get_instance consults the event-log reader first; falls back
to the legacy ClaudeInstanceRegistry only when the reader has no row
for that tmux session name (covers Claudes launched before users
re-installed the hook). The override layer is unchanged in this
phase.

Phase 3 will delete the legacy fallback and the registry entirely.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 8: End-of-Phase-2 manual smoke**

Bot is currently running pre-Phase-1 code (loaded into memory). To smoke-test the changes:

```bash
# In the bot's tmux pane (__ccmux__:1)
# 1. Stop the bot
tmux send-keys -t __ccmux__:1 C-c
# 2. Restart
tmux send-keys -t __ccmux__:1 "ccmux-telegram" Enter
# 3. Send a message in any bound topic. Verify it arrives.
# 4. Inspect ~/.ccmux/claude_events.jsonl — should have new lines.
# 5. /clear in a Claude pane — next prompt should refresh the log row.
```

If smoke passes, Phase 2 is shippable as a backend point release (no breaking changes). Decision point: ship `v3.2.0` here, OR continue to Phase 3 and ship as `v4.0.0`. **Default: continue to Phase 3.**

---

## Phase 3: Breaking — delete old code paths

After Phase 3, `claude_instances.json` is no longer written or read. Override layer is gone. `reconcile_instance` is gone. `pid_session_resolver` collapses into `hook.py`.

### Task 7: Hook stops writing claude_instances.json + drop overwrite guard

**Files:**

- Modify: `src/ccmux/hook.py` (delete the read-modify-write block, ~50 lines)
- Modify: `tests/test_hook.py` (drop tests that asserted on `claude_instances.json`)

- [ ] **Step 1: Identify the legacy block**

```bash
uv run grep -n "claude_instances" src/ccmux/hook.py
```

Lines ~330-405 (the `with open(lock_path, ...)` fcntl block) are the target. The PID-fallback block above stays (still needed for empty-stdin cases).

- [ ] **Step 2: Delete the legacy block**

In `src/ccmux/hook.py:_hook_main_impl`, remove from `# Read-modify-write with file locking ...` (around line 330) through the matching `except OSError as e: logger.error(...)` close. The event-log append (added in Task 3) stays.

Also remove the `from .util import atomic_write_json` import if it's now unused.

- [ ] **Step 3: Update tests**

Delete or rewrite tests that asserted on `claude_instances.json` contents. Replace with assertions on `claude_events.jsonl`. Specifically:

- `test_same_session_resume_updates_window` (v2.2.0 era) → reframe as "event log row updates window_id when /clear or resume happens".
- `test_different_window_refuses_overwrite` (the overwrite-guard regression test) → **delete entirely**. The guard is gone.

Run:

```bash
uv run pytest tests/test_hook.py -v
```

Expected: green after deletes/rewrites.

- [ ] **Step 4: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "$(cat <<'EOF'
refactor(hook)!: remove claude_instances.json read-modify-write

BREAKING. Hook is now append-only into claude_events.jsonl. The
legacy ~/.ccmux/claude_instances.json + .lock files are no longer
written; the overwrite guard ('one Claude per tmux session') is
deleted with them.

The PID-fallback path for empty-stdin invocations is kept in
hook.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Delete ClaudeInstanceRegistry, override layer, reconcile_instance

**Files:**

- Delete: `src/ccmux/claude_instance.py` (entire file)
- Modify: `src/ccmux/backend.py` (drop registry attr, drop `reconcile_instance`)
- Modify: `src/ccmux/api.py` (drop deleted exports, rename `ClaudeInstance` alias)
- Modify: tests touching the registry / reconcile

- [ ] **Step 1: List call sites**

```bash
uv run grep -rn "ClaudeInstanceRegistry\|set_override\|clear_override\|reconcile_instance\|from .claude_instance\|from ccmux.claude_instance" src/ tests/
```

Expect a handful of internal usages plus tests.

- [ ] **Step 2: Adjust DefaultBackend.get_instance to return CurrentClaudeBinding**

In `src/ccmux/backend.py`:

```python
def get_instance(self, instance_id: str) -> CurrentClaudeBinding | None:
    return self.event_reader.get(instance_id)

# Remove reconcile_instance method entirely.
# Remove the self.claude_instances attribute and the constructor parameter.
```

Update `Backend` Protocol in the same file (or wherever it lives) to drop `claude_instances`, `reconcile_instance` from the protocol.

- [ ] **Step 3: Delete `src/ccmux/claude_instance.py`**

```bash
git rm src/ccmux/claude_instance.py
```

- [ ] **Step 4: Update `src/ccmux/api.py`**

Remove from `__all__`: `ClaudeInstance`, `ClaudeInstanceRegistry`. Drop the `ClaudeInstance` class entirely. `CurrentClaudeBinding` already exported (Task 6 step 4) takes its place.

- [ ] **Step 5: Adjust call sites in backend / state_monitor / message_monitor**

`src/ccmux/state_monitor.py`, `src/ccmux/message_monitor.py`: replace any `ClaudeInstance` type annotation with `CurrentClaudeBinding`. Replace `inst.session_id` with `inst.claude_session_id`. Replace any `claude_instances.get(...)` with `event_reader.get(...)`.

`src/ccmux/message_monitor.py`: when starting tracking for a session, read `transcript_path` from the binding directly instead of computing it from cwd. Drop the cwd-encoding helper if it's now unused.

- [ ] **Step 6: Run the full suite, expect failures**

```bash
uv run pytest -v 2>&1 | tail -40
```

Expected: tests that imported deleted symbols fail. Fix them by removing imports / updating to new names.

- [ ] **Step 7: Update `tests/test_api_smoke.py`**

Remove `ClaudeInstance`, `ClaudeInstanceRegistry`, `Backend.reconcile_instance` from the pinned-surface assertions. Add `CurrentClaudeBinding`, `EventLogReader`.

- [ ] **Step 8: Run suite, expect green**

```bash
uv run pytest -v
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor!: delete ClaudeInstanceRegistry, override layer, reconcile_instance

BREAKING. The v3.1 override layer (set_override / clear_override) is
removed. Backend.reconcile_instance is removed; backend.get_instance
delegates directly to EventLogReader. ClaudeInstance is renamed to
CurrentClaudeBinding (no alias).

Removed file: src/ccmux/claude_instance.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Collapse pid_session_resolver into hook.py

**Files:**

- Delete: `src/ccmux/pid_session_resolver.py`
- Modify: `src/ccmux/hook.py` (inline the helpers as private functions)
- Modify: tests that imported the public module

- [ ] **Step 1: Read pid_session_resolver, identify what hook.py actually uses**

```bash
uv run grep -n "from .pid_session_resolver\|import pid_session_resolver" src/ tests/
```

Hook today imports: `_PANE_RE`, `_SESSION_FILE_RE`, `_UUID_RE`, `_encode_project_dir`, `_find_claude_pid`, and `resolve_for_pane` (aliased as `_resolve_session_via_pid`).

- [ ] **Step 2: Inline into hook.py as private helpers**

Move the bodies of those functions into `src/ccmux/hook.py`. Keep their names with leading underscore. Rename `resolve_for_pane` to `_resolve_session_via_pid` (the alias today). Keep them tested via `tests/test_hook.py` (the resolver tests can move to be private-helper tests in the same file).

- [ ] **Step 3: Delete the public module**

```bash
git rm src/ccmux/pid_session_resolver.py
```

If `tests/test_pid_session_resolver.py` exists, fold its tests into `tests/test_hook.py` and delete the file.

- [ ] **Step 4: Run suite**

```bash
uv run pytest -v
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor!: collapse pid_session_resolver into hook.py

BREAKING. The public ccmux.pid_session_resolver module is deleted.
Its helpers (_find_claude_pid, _resolve_session_via_pid, the regex
constants, _encode_project_dir) move into hook.py as private
functions.

The resolver was only used by hook.py's empty-stdin fallback path
and by the now-deleted reconcile_instance.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Add ccmux compact-events CLI subcommand

**Files:**

- Modify: `src/ccmux/cli.py` (add subcommand)
- Modify: `src/ccmux/event_log.py` (add `compact` function)
- Test: `tests/test_event_log.py`

- [ ] **Step 1: Write failing test**

```python
def test_compact_collapses_to_one_per_tmux_name(tmp_path):
    log = tmp_path / "events.jsonl"
    w = EventLogWriter(log)
    w.append(_make_event("ccmux", "uA", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)))
    w.append(_make_event("ccmux", "uB", datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc)))
    w.append(_make_event("daily", "uC", datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc)))
    assert len(log.read_text().splitlines()) == 3

    from ccmux.event_log import compact
    n_before, n_after = compact(log)
    assert n_before == 3
    assert n_after == 2  # one per tmux_session_name (latest)

    r = EventLogReader(log)
    r.refresh()
    assert r.get("ccmux").claude_session_id == "uB"
    assert r.get("daily").claude_session_id == "uC"
```

- [ ] **Step 2: Run, verify fails**

```bash
uv run pytest tests/test_event_log.py::test_compact_collapses_to_one_per_tmux_name -v
```

- [ ] **Step 3: Implement compact**

Append to `src/ccmux/event_log.py`:

```python
def compact(path: Path) -> tuple[int, int]:
    """Rewrite the log keeping only the latest event per tmux_session_name.

    Returns (lines_before, lines_after). Atomic: writes a temp file
    and renames into place.
    """
    if not path.exists():
        return (0, 0)
    by_name: dict[str, HookEvent] = {}
    n_before = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            n_before += 1
            try:
                ev = HookEvent.from_jsonl(line)
            except (ValueError, KeyError):
                continue
            if not ev.tmux.session_name:
                continue
            existing = by_name.get(ev.tmux.session_name)
            if existing is None or ev.timestamp >= existing.timestamp:
                by_name[ev.tmux.session_name] = ev
    tmp = path.with_suffix(path.suffix + ".compact.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for ev in sorted(by_name.values(), key=lambda e: e.timestamp):
            f.write(ev.to_jsonl())
    tmp.replace(path)
    return (n_before, len(by_name))
```

- [ ] **Step 4: Hook into CLI**

In `src/ccmux/cli.py`, add a `compact-events` subcommand:

```python
def _compact_events_main() -> int:
    from .event_log import compact
    from .util import ccmux_dir
    log = ccmux_dir() / "claude_events.jsonl"
    before, after = compact(log)
    print(f"Compacted {log}: {before} -> {after} lines")
    return 0
```

Register it in the argparse / dispatch logic alongside `hook`.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_event_log.py -v
```

- [ ] **Step 6: Smoke test the CLI**

```bash
uv run ccmux compact-events
```

Expected output: `Compacted /path/to/claude_events.jsonl: N -> M lines`.

- [ ] **Step 7: Commit**

```bash
git add src/ccmux/event_log.py src/ccmux/cli.py tests/test_event_log.py
git commit -m "$(cat <<'EOF'
feat(cli): add ccmux compact-events subcommand

Compaction keeps only the latest event per tmux_session_name and
rewrites the log via temp file + atomic rename. Manual; no runtime
GC.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4: Frontend migration (`ccmux-telegram`)

After Phase 3, backend `dev` is at the v4.0.0 API. Frontend on a feature branch updates against it.

### Task 11: Frontend branch + dependency bump

**Files:**

- Modify: `pyproject.toml`

- [ ] **Step 1: Branch + push backend feature for local install**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-backend
git push -u origin feature/event-log-self-heal

cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-telegram
git checkout dev
git pull
git checkout -b feature/event-log-self-heal
```

- [ ] **Step 2: Bump backend dep**

Edit `pyproject.toml`:

```toml
dependencies = [
    "ccmux @ git+https://github.com/wuwenrui555/ccmux-backend.git@feature/event-log-self-heal",
    # ... rest unchanged ...
]
```

(Once backend hits `v4.0.0` tag, this gets repinned to `@v4.0.0` in Task 14.)

```bash
uv sync
```

- [ ] **Step 3: Verify imports still resolve**

```bash
uv run python -c "from ccmux.api import DefaultBackend, CurrentClaudeBinding, EventLogReader; print('ok')"
```

Expected: `ok`. Failures here mean re-export adjustments are needed in backend's `api.py`.

- [ ] **Step 4: Commit dep bump**

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
chore(deps): bump ccmux to event-log-self-heal feature branch

Tracks ccmux-backend feature/event-log-self-heal during Phase 4
frontend migration. Will be repinned to v4.0.0 tag at release.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Drop set_override / clear_override / startup reconcile

**Files:**

- Modify: `ccmux-telegram/src/ccmux_telegram/main.py` (drop startup reconcile pass)
- Modify: `ccmux-telegram/src/ccmux_telegram/binding_callbacks.py` (drop reconcile-and-override)
- Modify: `ccmux-telegram/src/ccmux_telegram/command_basic.py` (delete `/rebind_window` handler)

- [ ] **Step 1: Find call sites**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-telegram
uv run grep -rn "set_override\|clear_override\|reconcile_instance\|rebind_window" src/ tests/
```

- [ ] **Step 2: Delete the startup reconcile pass**

In `src/ccmux_telegram/main.py`, find the `for binding in _topics.all(): ... await backend.reconcile_instance(...) ... set_override(...)` block and delete it. The reader's `start()` does the equivalent on its own.

- [ ] **Step 3: Delete `/rebind_window` handler**

In `src/ccmux_telegram/command_basic.py`, delete the `rebind_window_command` function and its `CommandHandler("rebind_window", ...)` registration. Update `/help` text and BotFather command list block to remove the entry.

- [ ] **Step 4: Update message_out.py wording**

In `src/ccmux_telegram/message_out.py`, replace every:

> `Use /rebind_window to refresh, or /rebind_topic to switch.`

with:

> `Use /rebind_topic to switch to a different session, or check that Claude is alive in tmux.`

(9 call sites per the v3.1 spec — verify with grep.)

- [ ] **Step 5: Drop set_override / clear_override sites**

Delete `binding_callbacks.py`'s reconcile-and-override sequence. Anywhere the frontend mutated `claude_instances`, just delete those lines (state is now reader-derived).

- [ ] **Step 6: Run tests, expect failures, fix**

```bash
uv run pytest -v 2>&1 | tail -30
```

Update tests that referenced removed symbols. The `BindingHealth` `RECOVERED` notice path stays — keep its tests.

- [ ] **Step 7: Manual smoke**

```bash
# Stop running bot, start the new one from the feature branch
tmux send-keys -t __ccmux__:1 C-c
sleep 1
tmux send-keys -t __ccmux__:1 "ccmux-telegram" Enter
sleep 4
# Confirm new PID + clean startup
pgrep -af ccmux-telegram
```

Send a message in any bound topic. Verify bidirectional flow. `/clear` in a Claude pane and verify next message routes correctly (event log refreshes the binding).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor!: drop override layer and /rebind_window

BREAKING. Frontend no longer calls set_override/clear_override or
reconcile_instance — backend's EventLogReader auto-refreshes on
every UserPromptSubmit hook fire.

/rebind_window command is removed (no alias). /rebind_topic
remains as the one rebind action.

Warning text in message_out.py updated to drop the /rebind_window
mention.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 5: Release

### Task 13: Backend release v4.0.0

**Files:**

- Modify: `ccmux-backend/pyproject.toml` (version bump)
- Modify: `ccmux-backend/src/ccmux/__init__.py` (`__version__`)
- Modify: `ccmux-backend/CHANGELOG.md`

- [ ] **Step 1: Merge feature into dev, run full suite**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-backend
git checkout dev
git merge --no-ff feature/event-log-self-heal -m "Merge feature/event-log-self-heal into dev"
uv run pytest -v
```

Expected: all green.

- [ ] **Step 2: Open release branch (managing-git-branches skill)**

Use the skill. Branch: `release/v4.0.0` off `dev`.

```bash
git checkout -b release/v4.0.0
```

- [ ] **Step 3: Bump version**

`pyproject.toml`: `version = "4.0.0"`. `src/ccmux/__init__.py`: `__version__ = "4.0.0"`.

- [ ] **Step 4: Write CHANGELOG entry**

Prepend to `CHANGELOG.md` under the existing `## [Unreleased]` section, then move it under `## 4.0.0 — 2026-04-28` (or release date):

```markdown
## 4.0.0 — 2026-04-28

### Changed (BREAKING)

- Layer 2 binding state replaced. `~/.ccmux/claude_instances.json` +
  the v3.1 override layer + `Backend.reconcile_instance` are deleted.
  In their place: an append-only JSONL event log at
  `~/.ccmux/claude_events.jsonl`, written by `SessionStart` and
  `UserPromptSubmit` hooks; backend derives current state by
  projecting the log on every read. Stale `window_id` and stale
  `cwd` failure modes (the v3.1.x hotfix drivers) self-heal on the
  next user message.

- `ClaudeInstance` renamed to `CurrentClaudeBinding`. Field
  `session_id` renamed to `claude_session_id` to disambiguate from
  tmux's own `session_id`. New fields: `transcript_path`, `last_seen`.

- `ClaudeInstanceRegistry` removed. `Backend.claude_instances`
  accessor removed. Set/clear override APIs removed. Use
  `Backend.event_reader` (an `EventLogReader`) for all binding
  queries.

- `pid_session_resolver` public module removed. Helpers fold back
  into `hook.py` as private functions.

- `ccmux hook --install` now registers two events:
  `SessionStart` and `UserPromptSubmit`. Run `ccmux hook --install`
  on upgrade to refresh `~/.claude/settings.json`.

### Added

- `ccmux.api.EventLogReader` — async polling reader over the event
  log; `start()`, `stop()`, `get(name)`, `all_alive()`, `refresh()`.

- `ccmux.api.CurrentClaudeBinding` dataclass.

- `ccmux.api.HookEvent` / `EventLogWriter` / `TmuxInfo` / `ClaudeInfo`
  for callers building or testing event lines.

- `ccmux compact-events` CLI subcommand. Manual log compaction;
  collapses to one event per `tmux_session_name`. No runtime GC.

### Removed

- File: `~/.ccmux/claude_instances.json` (deleted on first v4 hook
  fire; also safe for users to remove manually).
- File: `~/.ccmux/claude_instances.lock` (no longer used).
- API: `ClaudeInstance`, `ClaudeInstanceRegistry`,
  `Backend.reconcile_instance`, `set_override`, `clear_override`,
  `pid_session_resolver` module.

### Migration

No automatic migration. On upgrade:

1. Run `ccmux hook --install` to register the new event handlers.
2. Old `claude_instances.json` is ignored. The first `SessionStart`
   or `UserPromptSubmit` fire from each Claude repopulates state.
3. If running `ccmux-telegram`, upgrade to `v4.0.0` in lockstep.
```

- [ ] **Step 5: Commit and finish release**

```bash
git add CHANGELOG.md pyproject.toml src/ccmux/__init__.py
git commit -m "chore: bump version to 4.0.0 and update CHANGELOG"
```

Then follow `managing-git-branches` to merge `release/v4.0.0` into `main` + `dev`, tag `v4.0.0`, push.

- [ ] **Step 6: Verify install path**

```bash
uv tool upgrade ccmux
ccmux hook --install   # idempotent; adds UserPromptSubmit registration
```

---

### Task 14: Telegram release v4.0.0

**Files:**

- Modify: `ccmux-telegram/pyproject.toml` (repin backend dep + version bump)
- Modify: `ccmux-telegram/CHANGELOG.md`

- [ ] **Step 1: Merge feature into dev, full suite**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-telegram
git checkout dev
git merge --no-ff feature/event-log-self-heal -m "Merge feature/event-log-self-heal into dev"
uv run pytest -v
```

- [ ] **Step 2: Release branch + version bump + repin backend**

```bash
git checkout -b release/v4.0.0
```

`pyproject.toml`:

```toml
dependencies = [
    "ccmux @ git+https://github.com/wuwenrui555/ccmux-backend.git@v4.0.0",
    # ...
]
```

Bump telegram version to `4.0.0`.

```bash
uv sync
```

- [ ] **Step 3: CHANGELOG**

```markdown
## 4.0.0 — 2026-04-28

### Changed (BREAKING)

- Requires `ccmux >= 4.0.0`. Frontend uses the new `EventLogReader`
  + `CurrentClaudeBinding` API; the v3.1 override layer is gone.

- `/rebind_window` command removed (no alias). The reader
  auto-refreshes the binding on every user message; manual refresh
  is no longer meaningful. Use `/rebind_topic` to switch a topic to
  a different tmux session.

- "Binding not alive" warning text updated to drop the
  `/rebind_window` reference.

### Removed

- Startup reconcile pass in `main.py`. `EventLogReader.start()` does
  the initial full read internally.
- `set_override` / `clear_override` call sites in
  `binding_callbacks.py` and `command_basic.py`.
```

- [ ] **Step 4: Commit, finish release, tag**

```bash
git add -A
git commit -m "chore: bump version to 4.0.0, repin ccmux to v4.0.0, update CHANGELOG"
```

Follow `managing-git-branches` to merge into `main` + `dev`, tag `v4.0.0`, push.

- [ ] **Step 5: Final smoke on installed version**

```bash
uv tool upgrade ccmux-telegram
tmux send-keys -t __ccmux__:1 C-c
sleep 1
tmux send-keys -t __ccmux__:1 "ccmux-telegram" Enter
```

Send a message in a bound topic, `/clear` mid-conversation, verify routing follows. Inspect `~/.ccmux/claude_events.jsonl` to confirm growth.

---

## Self-review checklist (run before handoff)

**Spec coverage:**

- ✅ Append-only JSONL log + atomic O_APPEND (Task 2)
- ✅ Hook registers SessionStart + UserPromptSubmit, dispatches by event (Task 3)
- ✅ `EventLogReader` with `_current` projection (Tasks 4, 5)
- ✅ `CurrentClaudeBinding` schema (Task 4)
- ✅ Backend wiring + adapter for legacy callers during Phase 2 (Task 6)
- ✅ Hook stops writing claude_instances.json + drops overwrite guard (Task 7)
- ✅ Delete registry / override / reconcile_instance (Task 8)
- ✅ Collapse pid_session_resolver into hook.py (Task 9)
- ✅ `ccmux compact-events` CLI (Task 10)
- ✅ Frontend dep bump + drop overrides + drop `/rebind_window` (Tasks 11, 12)
- ✅ CHANGELOG + version bump + git-flow release (Tasks 13, 14)

**Type consistency:**

- `CurrentClaudeBinding` field `claude_session_id` (not `session_id`) — used consistently from Task 4 onward.
- `EventLogReader.refresh()` is sync; `start()` / `stop()` are async — used consistently in Tasks 5, 6.
- `Backend.event_reader` (not `Backend.events`) — used consistently in Tasks 6, 8.

**Gaps:** None identified. The "tmux $-id and indexes" capture in Task 3 step 4 is intentionally permissive (records empty strings if the engineer chooses not to lift the `display-message` format string in this task — they're not consumed by the projection).

---

## Out of scope for v4.0.0

- inotify / fsevents-driven reader (polling at 0.5 s is fine).
- Per-Claude addressing in `topic_bindings` (still keyed by `tmux_session_name`).
- Daemon process (single-writer-IPC alternative).
- Cross-machine state sharing (Linux + macOS each maintain their own log).
- Storing `prompt` content in the log (rejected for atomicity; out of scope for telemetry).

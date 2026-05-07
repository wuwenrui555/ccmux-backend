<!-- markdownlint-disable MD024 -->

# ccmux state log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans-test-first to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every `(pane_text, state)` pair from `state_monitor.fast_tick` to a JSONL file, with adjacent dedup, opt-in via the `CCMUX_STATE_LOG_PATH` env var. Builds a corpus we can mine offline for new parser patterns.

**Architecture:** New module `ccmux/state_log.py` defines `StateLog` (file open + dedup state + JSONL writer) and a pure `_serialize_state` helper. `StateMonitor` accepts an optional `state_log` parameter; when present, it calls `state_log.record(...)` after `parse_pane`. `DefaultBackend` reads the env var, constructs `StateLog` if set, injects it into `StateMonitor`, and awaits `state_log.close()` from its `stop()` shutdown path. `claude-code-state` is not modified.

**Tech Stack:** Python 3.12+, asyncio, `dataclasses`, stdlib only. No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-05-07-ccmux-state-log-design.md`](../specs/2026-05-07-ccmux-state-log-design.md)

**Repos affected:**

- `ccmux-backend` at `/mnt/nfs/home/wenruiwu/ccmux/ccmux-backend` (minor: new module + small wiring changes)

**Branch strategy:** Branch is already created at `feature/state-log` (branched off `dev`); the spec is committed there. All tasks below land on this branch. Final merge to `dev` is the user's call after review.

---

## Task 1: `_serialize_state` helper

Pure function that turns a `ClaudeState` into a JSON-ready dict with a top-level `type` discriminator. Lands first because it's pure, easy to test, and reused by every later task.

**Files:**

- Create: `src/ccmux/state_log.py`
- Test: `tests/test_state_log.py`

- [ ] **Step 1: Confirm branch is `feature/state-log`**

```bash
cd /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend
git status
git branch --show-current
```

Expected: branch is `feature/state-log`, working tree clean.

- [ ] **Step 2: Write the failing test**

`tests/test_state_log.py`:

```python
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
```

- [ ] **Step 3: Run test, verify it fails**

```bash
uv run pytest tests/test_state_log.py -v
```

Expected: ImportError or ModuleNotFoundError on `ccmux.state_log`.

- [ ] **Step 4: Implement `_serialize_state` in `src/ccmux/state_log.py`**

```python
"""State log: append-only JSONL recorder for (pane_text, state) observations.

Opt-in via the ``CCMUX_STATE_LOG_PATH`` env var. When the path is set,
``DefaultBackend`` constructs a ``StateLog`` and injects it into
``StateMonitor``; ``fast_tick`` calls ``record(...)`` after every
``parse_pane`` classification.

Adjacent ticks with identical pane text for the same instance are
collapsed into a single record with ``first_seen``, ``last_seen``, and
``tick_count``. State only flushes to disk when the pane text changes
for that instance, or when ``close()`` is called at shutdown.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from claude_code_state import ClaudeState


def _serialize_state(state: ClaudeState) -> dict[str, Any]:
    """Serialize a ``ClaudeState`` to a JSON-ready dict.

    All variants are frozen dataclasses; ``dataclasses.asdict`` flattens
    them and ``BlockedUI`` (a ``StrEnum``) serializes as its string value.
    A ``type`` field with the variant class name is injected at the top
    level so log readers can branch on variant without duck typing.
    """
    payload: dict[str, Any] = {"type": type(state).__name__}
    payload.update(asdict(state))
    return payload
```

- [ ] **Step 5: Run test, verify it passes**

```bash
uv run pytest tests/test_state_log.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/state_log.py tests/test_state_log.py
git commit -m "$(cat <<'EOF'
feat(state-log): add _serialize_state helper

Pure function that turns a ClaudeState variant into a JSON-ready dict
with a top-level type discriminator. Lands first as the reusable
serialization primitive for the upcoming StateLog writer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `StateLog` class — single-instance dedup + file IO

Implements the core dedup-and-write logic for one instance. Opens the file in `__init__`, stages records in memory, flushes on pane-text change or `close()`. Multi-instance and concurrency are layered on in Task 3.

**Files:**

- Modify: `src/ccmux/state_log.py`
- Modify: `tests/test_state_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state_log.py`:

```python
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
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
uv run pytest tests/test_state_log.py::TestStateLogSingleInstance -v
```

Expected: ImportError on `StateLog`.

- [ ] **Step 3: Implement `StateLog` class in `src/ccmux/state_log.py`**

Append to `src/ccmux/state_log.py`:

```python
import asyncio
import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StagedRecord:
    instance_id: str
    window_id: str
    pane_text: str
    state: dict[str, Any]
    first_seen: datetime
    last_seen: datetime
    tick_count: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class StateLog:
    """Append-only JSONL writer for (pane_text, state) observations.

    Adjacent ticks with identical pane text for the same instance are
    collapsed; only when the pane text changes does the previous record
    get written to disk. ``close()`` flushes all in-memory staged
    records.

    Concurrency: ``record()`` may be invoked from multiple coroutines
    (``state_monitor.fast_tick`` fans out via ``asyncio.gather``). A
    single ``asyncio.Lock`` protects the staged-record dict and the
    file write. The critical section is short and contention is bounded
    by the number of instances, so a single lock is sufficient.

    The file handle is opened in ``__init__`` and held for the
    lifetime of the object. The parent directory must already exist;
    we do not silently ``mkdir`` because the caller may have typed the
    path wrong.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        if not p.parent.exists():
            raise FileNotFoundError(
                f"State log parent directory does not exist: {p.parent}"
            )
        self._path = p
        self._fh: IO[str] = open(p, "a", encoding="utf-8")
        self._staged: dict[str, _StagedRecord] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def record(
        self,
        *,
        instance_id: str,
        window_id: str,
        pane_text: str,
        state: ClaudeState,
    ) -> None:
        if self._closed:
            return
        now = _utcnow()
        async with self._lock:
            prev = self._staged.get(instance_id)
            if prev is not None and prev.pane_text == pane_text:
                self._staged[instance_id] = replace(
                    prev,
                    last_seen=now,
                    tick_count=prev.tick_count + 1,
                )
                return
            if prev is not None:
                self._write(prev)
            self._staged[instance_id] = _StagedRecord(
                instance_id=instance_id,
                window_id=window_id,
                pane_text=pane_text,
                state=_serialize_state(state),
                first_seen=now,
                last_seen=now,
                tick_count=1,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            for rec in self._staged.values():
                self._write(rec)
            self._staged.clear()
            try:
                self._fh.close()
            except Exception as e:
                logger.debug("state_log file close error: %s", e)
            self._closed = True

    def _write(self, rec: _StagedRecord) -> None:
        line = json.dumps(
            {
                "first_seen": _iso(rec.first_seen),
                "last_seen": _iso(rec.last_seen),
                "tick_count": rec.tick_count,
                "instance_id": rec.instance_id,
                "window_id": rec.window_id,
                "state": rec.state,
                "pane_text": rec.pane_text,
            },
            ensure_ascii=False,
        )
        self._fh.write(line + "\n")
        self._fh.flush()
```

- [ ] **Step 4: Run all tests in test_state_log.py, verify pass**

```bash
uv run pytest tests/test_state_log.py -v
```

Expected: 13 passed (5 from Task 1 + 8 new).

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/state_log.py tests/test_state_log.py
git commit -m "$(cat <<'EOF'
feat(state-log): add StateLog with adjacent-pane dedup

StateLog opens an append-only JSONL file in __init__, stages one
record per instance in memory, and only writes the previous record to
disk when the pane text changes for that instance (or close() is
called). first_seen/last_seen/tick_count capture how long an
identical pane persisted across consecutive ticks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Multi-instance dedup + concurrency tests

The `_staged` dict already keys by `instance_id`, so multi-instance behavior should already work. This task adds tests that pin it down and exercises the asyncio lock under concurrent access.

**Files:**

- Modify: `tests/test_state_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state_log.py`:

```python
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
```

- [ ] **Step 2: Run tests, verify they pass without further implementation**

```bash
uv run pytest tests/test_state_log.py -v
```

Expected: 15 passed (13 prior + 2 new). The class already supports multi-instance keying and locking; these tests pin down that behavior.

- [ ] **Step 3: Commit**

```bash
git add tests/test_state_log.py
git commit -m "$(cat <<'EOF'
test(state-log): cover multi-instance dedup and concurrent record()

Pins the per-instance staging behavior (one instance flushing does not
disturb another) and exercises the asyncio.Lock under 200 concurrent
record() calls across 50 instances. Catches future regressions if
someone reaches for a per-key lock or removes the lock entirely.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Plumb `state_log` through `StateMonitor`

`StateMonitor.__init__` gains an optional `state_log` parameter. `_classify_from_pane` is refactored to return `(pane_text, state) | None`. `fast_tick` unpacks the tuple, calls `state_log.record(...)` if non-None, then calls `on_state` as before. All existing tests construct `StateMonitor` without `state_log` (default `None`), so they keep passing.

**Files:**

- Modify: `src/ccmux/state_monitor.py`
- Modify: `tests/test_state_monitor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state_monitor.py` (after the existing `TestClassification` class):

```python
class TestStateLogIntegration:
    @pytest.mark.asyncio
    async def test_fast_tick_calls_state_log_record(self, chrome: str) -> None:
        from ccmux.state_log import StateLog  # noqa: F401  (import locality)

        b = _binding()
        pane = f"some output\n✽ Thinking… (3s)\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen_state: list[tuple[str, ClaudeState]] = []
        recorded: list[dict[str, Any]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen_state.append((instance_id, state))

        class _FakeStateLog:
            async def record(
                self,
                *,
                instance_id: str,
                window_id: str,
                pane_text: str,
                state: ClaudeState,
            ) -> None:
                recorded.append(
                    {
                        "instance_id": instance_id,
                        "window_id": window_id,
                        "pane_text": pane_text,
                        "state": state,
                    }
                )

            async def close(self) -> None:
                pass

        mon = StateMonitor(
            event_reader=reader,
            tmux_registry=tmux,
            on_state=on_state,
            state_log=_FakeStateLog(),
        )
        await mon.fast_tick()

        assert len(recorded) == 1
        assert recorded[0]["instance_id"] == "a"
        assert recorded[0]["window_id"] == "@1"
        assert recorded[0]["pane_text"] == pane
        assert isinstance(recorded[0]["state"], Working)
        # on_state still fires.
        assert len(seen_state) == 1

    @pytest.mark.asyncio
    async def test_fast_tick_with_no_state_log_works_unchanged(
        self, chrome: str
    ) -> None:
        b = _binding()
        pane = f"output\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        # No state_log argument — default is None.
        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()
        assert len(seen) == 1
```

- [ ] **Step 2: Run the new test, verify it fails**

```bash
uv run pytest tests/test_state_monitor.py::TestStateLogIntegration -v
```

Expected: TypeError on `StateMonitor.__init__` getting unexpected keyword `state_log`, OR no `record()` call observed.

- [ ] **Step 3: Modify `src/ccmux/state_monitor.py`**

Add a `Protocol` for the logger contract near the top of the file (next to `OnStateCallback`):

```python
from typing import Awaitable, Callable, Protocol, TYPE_CHECKING


class StateLogProtocol(Protocol):
    """Subset of ``StateLog`` consumed by ``StateMonitor``."""

    async def record(
        self,
        *,
        instance_id: str,
        window_id: str,
        pane_text: str,
        state: ClaudeState,
    ) -> None: ...
```

Update `StateMonitor.__init__`:

```python
class StateMonitor:
    def __init__(
        self,
        *,
        event_reader: "EventLogReader",
        tmux_registry: "TmuxSessionRegistry",
        on_state: OnStateCallback,
        state_log: "StateLogProtocol | None" = None,
    ) -> None:
        self._event_reader = event_reader
        self._tmux_registry = tmux_registry
        self._on_state = on_state
        self._state_log = state_log
```

Refactor `_classify_from_pane` to return a tuple:

```python
async def _classify_from_pane(
    self, b: "CurrentClaudeBinding"
) -> tuple[str, ClaudeState] | None:
    """Return (pane_text, ClaudeState) from pane text, or None to skip."""
    if not b.window_id:
        return None
    tm = self._tmux_registry.get_by_window_id(b.window_id)
    if tm is None:
        return None
    w = await tm.find_window_by_id(b.window_id)
    if w is None:
        return None
    pane_text = await tm.capture_pane(b.window_id)
    if not pane_text:
        return None
    state = parse_pane(pane_text)
    if state is None:
        return None
    return pane_text, state
```

Update the `fast_tick` loop body. Find the existing block:

```python
for b, result in zip(bindings, results):
    if isinstance(result, BaseException):
        if isinstance(result, Exception):
            logger.debug(...)
            continue
        raise result
    if result is not None:
        await self._on_state(b.tmux_session_name, result)
```

Replace with:

```python
for b, result in zip(bindings, results):
    if isinstance(result, BaseException):
        if isinstance(result, Exception):
            logger.debug(
                "fast_tick classify error for %s: %s",
                b.tmux_session_name,
                result,
            )
            continue
        # KeyboardInterrupt / SystemExit — propagate.
        raise result
    if result is None:
        continue
    pane_text, state = result
    if self._state_log is not None:
        try:
            await self._state_log.record(
                instance_id=b.tmux_session_name,
                window_id=b.window_id,
                pane_text=pane_text,
                state=state,
            )
        except Exception as e:
            logger.debug("state_log record error for %s: %s", b.tmux_session_name, e)
    await self._on_state(b.tmux_session_name, state)
```

The `try/except Exception` around `record()` matches the spec: a logger crash must never break observation. Errors are logged at `debug` and swallowed.

- [ ] **Step 4: Run all tests, verify pass**

```bash
uv run pytest tests/test_state_log.py tests/test_state_monitor.py -v
```

Expected: all green. Existing `state_monitor` tests still pass because `state_log` defaults to `None`.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/state_monitor.py tests/test_state_monitor.py
git commit -m "$(cat <<'EOF'
feat(state-monitor): plumb optional state_log into fast_tick

Adds an optional state_log parameter on StateMonitor. When non-None,
fast_tick calls state_log.record(instance_id, window_id, pane_text,
state) after parse_pane, before on_state. Errors from record() are
logged at debug and swallowed so logger faults can't break state
observation. _classify_from_pane now returns (pane_text, state) so
the outer loop has both pieces.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire env var in `DefaultBackend`, hook `close()` in `stop()`

`DefaultBackend.start` reads `CCMUX_STATE_LOG_PATH`; if non-empty, constructs a `StateLog` and passes it to `StateMonitor`. The `StateLog` instance is held on the backend so `stop()` can call `close()`. Errors during `close()` are logged at `debug` and swallowed (mirrors existing pattern).

**Files:**

- Modify: `src/ccmux/backend.py`
- Modify: `tests/test_claude_backend.py` (or wherever `DefaultBackend` lifecycle is exercised — verified in Step 1)

- [ ] **Step 1: Find the existing backend lifecycle test file**

```bash
grep -ln "DefaultBackend\|self\._fast_task\|backend\.stop()" /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend/tests/*.py
```

Confirm which test file already covers `DefaultBackend.start()` / `stop()`. Use that file for the new test. If no integration-style test exists, add a new file `tests/test_state_log_wiring.py`.

- [ ] **Step 2: Write the failing test**

Add a test (in the file identified above; if creating new, use `tests/test_state_log_wiring.py`):

```python
"""Wiring tests for CCMUX_STATE_LOG_PATH env-var driven state log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ccmux.state_log import StateLog


class TestEnvVarConstruction:
    def test_unset_env_var_yields_no_state_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CCMUX_STATE_LOG_PATH", raising=False)
        from ccmux.backend import _build_state_log

        assert _build_state_log() is None

    def test_empty_env_var_yields_no_state_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCMUX_STATE_LOG_PATH", "   ")
        from ccmux.backend import _build_state_log

        assert _build_state_log() is None

    def test_env_var_set_yields_state_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "state.jsonl"
        monkeypatch.setenv("CCMUX_STATE_LOG_PATH", str(log_path))
        from ccmux.backend import _build_state_log

        log = _build_state_log()
        assert isinstance(log, StateLog)
```

- [ ] **Step 3: Run the test, verify it fails**

```bash
uv run pytest tests/test_state_log_wiring.py -v
```

Expected: ImportError on `_build_state_log`.

- [ ] **Step 4: Modify `src/ccmux/backend.py`**

At module top (with the other imports), add:

```python
from ccmux.state_log import StateLog
```

Add a small private factory just above `class DefaultBackend:` (so it's importable for the wiring test):

```python
def _build_state_log() -> StateLog | None:
    """Return a StateLog if CCMUX_STATE_LOG_PATH is set and non-empty, else None."""
    path = os.getenv("CCMUX_STATE_LOG_PATH", "").strip()
    if not path:
        return None
    return StateLog(path)
```

In `DefaultBackend.__init__`, add:

```python
self._state_log: StateLog | None = None
```

In `DefaultBackend.start`, before constructing `StateMonitor`, build the logger and pass it through:

```python
self._state_log = _build_state_log()
state_monitor = StateMonitor(
    event_reader=self.event_reader,
    tmux_registry=self._tmux_registry,
    on_state=on_state_with_resume,
    state_log=self._state_log,
)
```

In `DefaultBackend.stop`, after `self._message_monitor.shutdown()` (so the logger closes last, mirroring the order of construction):

```python
if self._state_log is not None:
    try:
        await self._state_log.close()
    except Exception as e:
        logger.debug("state_log close error: %s", e)
    self._state_log = None
```

- [ ] **Step 5: Run wiring tests, verify pass**

```bash
uv run pytest tests/test_state_log_wiring.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the full test suite to confirm nothing else broke**

```bash
uv run pytest -v
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/ccmux/backend.py tests/test_state_log_wiring.py
git commit -m "$(cat <<'EOF'
feat(backend): wire CCMUX_STATE_LOG_PATH into DefaultBackend

When the env var is set and non-empty, DefaultBackend.start
constructs a StateLog and injects it into StateMonitor. stop() awaits
state_log.close() so all in-memory staged records flush to disk
before the process exits. close() errors are logged at debug and
swallowed (mirrors the existing message_monitor.shutdown pattern).

Unset / empty env var: state_log is None and behavior is bit-identical
to before this commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: README env-var documentation

Document the new `CCMUX_STATE_LOG_PATH` env var alongside `CCMUX_CLAUDE_PROC_NAMES`.

**Files:**

- Modify: `README.md`

- [ ] **Step 1: Find the env var section in README**

```bash
grep -n "CCMUX_CLAUDE_PROC_NAMES\|env var\|Environment\|Configuration" /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend/README.md
```

If a section already documents `CCMUX_CLAUDE_PROC_NAMES`, add the new var next to it. If no such section exists, add a small "Environment variables" section near the configuration / usage area.

- [ ] **Step 2: Add documentation**

Add an entry that reads (adjust formatting to match the surrounding section):

```markdown
- `CCMUX_STATE_LOG_PATH` — path to a JSONL file. When set, every
  `fast_tick` observation `(pane_text, state)` is recorded; consecutive
  ticks with identical pane text for the same instance are collapsed
  into a single record with `first_seen`, `last_seen`, and
  `tick_count`. Unset / empty: no logging, zero overhead. The parent
  directory must already exist. See
  [`docs/superpowers/specs/2026-05-07-ccmux-state-log-design.md`](docs/superpowers/specs/2026-05-07-ccmux-state-log-design.md)
  for the record schema and intended workflow.
```

- [ ] **Step 3: Verify markdownlint passes**

```bash
cd /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend
pre-commit run --files README.md
```

Expected: pass (or at most reformatting the engineer accepts).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): document CCMUX_STATE_LOG_PATH env var

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Final verification

End-to-end smoke check on the feature branch before handing back.

- [ ] **Step 1: Full test suite**

```bash
cd /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend
uv run pytest -v
```

Expected: all tests pass.

- [ ] **Step 2: Pre-commit on the whole tree**

```bash
pre-commit run --all-files
```

Expected: pass.

- [ ] **Step 3: Manual smoke (optional, recommended)**

In one terminal, set `CCMUX_STATE_LOG_PATH=/tmp/ccmux-state.jsonl` and start a backend session that drives `DefaultBackend.start`. After a few ticks, inspect:

```bash
tail -n 5 /tmp/ccmux-state.jsonl | jq .
```

Expected: well-formed JSONL records with `state.type` populated and `pane_text` matching observed panes. Stop the backend with normal shutdown and confirm the last in-memory records flushed.

- [ ] **Step 4: Branch state check**

```bash
git log --oneline dev..HEAD
```

Expected commits, in order:

1. `docs(spec): add design for ccmux state log` (already on branch)
2. `feat(state-log): add _serialize_state helper`
3. `feat(state-log): add StateLog with adjacent-pane dedup`
4. `test(state-log): cover multi-instance dedup and concurrent record()`
5. `feat(state-monitor): plumb optional state_log into fast_tick`
6. `feat(backend): wire CCMUX_STATE_LOG_PATH into DefaultBackend`
7. `docs(readme): document CCMUX_STATE_LOG_PATH env var`

Branch `feature/state-log` is ready for the user's review and merge.

---

## Done criteria

- All tests in `tests/test_state_log.py`, `tests/test_state_monitor.py`, `tests/test_state_log_wiring.py` (or wherever the wiring test landed) pass.
- `pre-commit run --all-files` passes.
- With `CCMUX_STATE_LOG_PATH` unset, `pytest -v` output is identical to before this branch (modulo new tests).
- With the env var set, a real backend run produces a valid JSONL file whose lines parse and contain the documented schema.

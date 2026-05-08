<!-- markdownlint-disable MD024 -->

# ccmux state snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans-test-first to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, separately-toggled state observer (`StateSnapshot` → `$CCMUX_DIR/state_current.json`) for live polling by an external monitoring tool, alongside the existing `StateLog` corpus log.

**Architecture:** Introduce a `StateObserver` Protocol in `state_log.py`. Generalize `StateMonitor`'s `state_log` parameter to a tuple of observers. Add `StateSnapshot` class (atomic-rewrite JSON map of `instance_id -> {state, window_id, last_seen}`, no `pane_text`). `DefaultBackend` reads `CCMUX_STATE_LOG` and `CCMUX_STATE_SNAPSHOT` independently and constructs whichever observers are enabled.

**Tech Stack:** Python 3.12+, asyncio, stdlib only. Reuses existing `ccmux.util.atomic_write_json`.

**Spec:** [`docs/superpowers/specs/2026-05-07-ccmux-state-log-design.md`](../specs/2026-05-07-ccmux-state-log-design.md) (revised "split" section)

**Repos affected:**

- `ccmux-backend` at `/mnt/nfs/home/wenruiwu/ccmux/ccmux-backend`

**Branch strategy:** Continue on existing `feature/state-log` branch. Spec update already committed (`eb2372e`).

---

## Task 1: `StateObserver` Protocol + `StateSnapshot` class

Add the shared Protocol and the new snapshot writer to `state_log.py`. TDD on the snapshot writer's behavior in isolation.

**Files:**

- Modify: `src/ccmux/state_log.py`
- Create: `tests/test_state_snapshot.py`

- [ ] **Step 1: Confirm branch state**

```bash
cd /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend
git status
git branch --show-current
git log --oneline -3
```

Expected: branch `feature/state-log`, working tree clean, recent commit is the spec update.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_state_snapshot.py`:

```python
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
    async def test_repeated_record_updates_last_seen(
        self, snap_path: Path
    ) -> None:
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
    async def test_concurrent_records_produce_valid_json(
        self, snap_path: Path
    ) -> None:
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
        await snap.record(
            instance_id="a", window_id="@1", pane_text="x", state=Idle()
        )
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
        snap_path.write_text('{"old": {"state": {"type": "Idle"}, "window_id": "@x", "last_seen": "2020-01-01T00:00:00+00:00"}}')
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
```

- [ ] **Step 3: Run tests, verify they fail**

```bash
uv run pytest tests/test_state_snapshot.py -v
```

Expected: ImportError on `StateSnapshot`.

- [ ] **Step 4: Add `StateObserver` Protocol + `StateSnapshot` class**

Edit `src/ccmux/state_log.py`. After the `_serialize_state` function (and before the existing `_StagedRecord` dataclass), add:

```python
from typing import IO, Any, Protocol, runtime_checkable


@runtime_checkable
class StateObserver(Protocol):
    """Common interface for any sink that wants per-tick state observations.

    StateMonitor fans observations out to a tuple of these. Both StateLog
    (corpus) and StateSnapshot (live) implement this Protocol.
    """

    async def record(
        self,
        *,
        instance_id: str,
        window_id: str,
        pane_text: str,
        state: ClaudeState,
    ) -> None: ...

    async def close(self) -> None: ...
```

The existing `from typing import IO, Any` import line should be replaced with the line above. (If the original line read `from typing import IO, Any`, replace it with the expanded form including `Protocol, runtime_checkable`. If it's already in a different shape, merge.)

Then, at the end of the file (after the existing `StateLog` class), add:

```python
import tempfile

from .util import atomic_write_json


class StateSnapshot:
    """Atomic-rewrite JSON map of instance_id -> latest observation.

    On every record() call:
      1. Update the in-memory map for that instance_id.
      2. Atomically rewrite the file (write tmp, rename).

    pane_text is intentionally NOT stored: consumers that need raw pane
    contents can run `tmux capture-pane` themselves; keeping the file small
    keeps rewrite IO bounded.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._path = p
        self._current: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        instance_id: str,
        window_id: str,
        pane_text: str,  # accepted for interface symmetry; deliberately ignored
        state: ClaudeState,
    ) -> None:
        del pane_text  # not stored in the snapshot file
        now = _utcnow()
        async with self._lock:
            self._current[instance_id] = {
                "state": _serialize_state(state),
                "window_id": window_id,
                "last_seen": _iso(now),
            }
            atomic_write_json(self._path, self._current, indent=2)

    async def close(self) -> None:
        # No-op. The file is always at-rest after the most recent
        # atomic_write_json. close() exists for StateObserver Protocol
        # symmetry with StateLog.
        return
```

- [ ] **Step 5: Run snapshot tests, verify they pass**

```bash
uv run pytest tests/test_state_snapshot.py -v
```

Expected: 9 passed.

- [ ] **Step 6: Run StateLog tests to confirm no regression**

```bash
uv run pytest tests/test_state_log.py -v
```

Expected: existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/ccmux/state_log.py tests/test_state_snapshot.py
git commit -m "$(cat <<'EOF'
feat(state-snapshot): add StateSnapshot atomic-rewrite observer

StateSnapshot implements the StateObserver Protocol alongside
StateLog. record() updates an in-memory dict keyed by instance_id
and atomically rewrites a small JSON file at the configured path.
pane_text is accepted but deliberately not persisted — the file is
meant for live polling by an external monitoring tool that can run
tmux capture-pane itself if it needs raw pane contents.

close() is a no-op; the file is always at-rest after the last
record().

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Generalize `StateMonitor` to multiple observers

Replace `state_log: StateLogProtocol | None` with `observers: tuple[StateObserver, ...]`. Update fast_tick to fan out. Errors from one observer don't block others.

**Files:**

- Modify: `src/ccmux/state_monitor.py`
- Modify: `tests/test_state_monitor.py`

- [ ] **Step 1: Find the existing `state_log` references in state_monitor**

```bash
grep -n "state_log\|StateLogProtocol" src/ccmux/state_monitor.py
```

Note all locations. Expected: constructor parameter, attribute, fast_tick call site, type alias / Protocol definition.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_state_monitor.py` (or replace the existing `TestStateLogIntegration` class if its tests use the old single-observer API):

```python
class TestStateObserverFanout:
    @pytest.mark.asyncio
    async def test_multiple_observers_all_called(self, chrome: str) -> None:
        b = _binding()
        pane = f"output\n✽ Thinking… (3s)\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reader = _FakeReader(bindings=[b])
        seen: list[tuple[str, ClaudeState]] = []
        recorded_a: list[dict[str, Any]] = []
        recorded_b: list[dict[str, Any]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        class _FakeObs:
            def __init__(self, sink: list[dict[str, Any]]) -> None:
                self._sink = sink

            async def record(self, **kwargs: Any) -> None:
                self._sink.append(kwargs)

            async def close(self) -> None:
                pass

        mon = StateMonitor(
            event_reader=reader,
            tmux_registry=tmux,
            on_state=on_state,
            observers=(_FakeObs(recorded_a), _FakeObs(recorded_b)),
        )
        await mon.fast_tick()
        assert len(recorded_a) == 1
        assert len(recorded_b) == 1
        assert recorded_a[0]["instance_id"] == recorded_b[0]["instance_id"] == "a"
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_one_observer_failure_does_not_block_others(
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
        recorded: list[dict[str, Any]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        class _BoomObs:
            async def record(self, **kwargs: Any) -> None:
                raise RuntimeError("simulated observer failure")

            async def close(self) -> None:
                pass

        class _OkObs:
            async def record(self, **kwargs: Any) -> None:
                recorded.append(kwargs)

            async def close(self) -> None:
                pass

        mon = StateMonitor(
            event_reader=reader,
            tmux_registry=tmux,
            on_state=on_state,
            observers=(_BoomObs(), _OkObs()),
        )
        await mon.fast_tick()
        # _OkObs still got called even though _BoomObs raised.
        assert len(recorded) == 1
        # on_state still fired.
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_no_observers_works_unchanged(self, chrome: str) -> None:
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

        # No observers argument — default empty tuple.
        mon = StateMonitor(event_reader=reader, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()
        assert len(seen) == 1
```

- [ ] **Step 3: Run new tests, verify they fail**

```bash
uv run pytest tests/test_state_monitor.py::TestStateObserverFanout -v
```

Expected: TypeError on unexpected keyword `observers`.

- [ ] **Step 4: Update `src/ccmux/state_monitor.py`**

Replace the `StateLogProtocol` class definition with:

```python
from .state_log import StateObserver
```

Replace the `state_log` constructor parameter and attribute with:

```python
class StateMonitor:
    def __init__(
        self,
        *,
        event_reader: "EventLogReader",
        tmux_registry: "TmuxSessionRegistry",
        on_state: OnStateCallback,
        observers: "tuple[StateObserver, ...]" = (),
    ) -> None:
        self._event_reader = event_reader
        self._tmux_registry = tmux_registry
        self._on_state = on_state
        self._observers = observers
```

Replace the fast_tick fanout block (the `if self._state_log is not None:` body) with:

```python
for obs in self._observers:
    try:
        await obs.record(
            instance_id=b.tmux_session_name,
            window_id=b.window_id,
            pane_text=pane_text,
            state=state,
        )
    except Exception as e:
        logger.debug(
            "observer %s record error for %s: %s",
            type(obs).__name__,
            b.tmux_session_name,
            e,
        )
await self._on_state(b.tmux_session_name, state)
```

Remove the now-unused `StateLogProtocol` class definition entirely from `state_monitor.py`.

- [ ] **Step 5: Run state_monitor tests, verify they pass**

```bash
uv run pytest tests/test_state_monitor.py -v
```

Expected: all green. Old `TestStateLogIntegration` tests may need to be removed or updated — if they constructed `StateMonitor` with `state_log=fake`, change to `observers=(fake,)`.

- [ ] **Step 6: Run full state_log tests to confirm no break**

```bash
uv run pytest tests/test_state_log.py tests/test_state_snapshot.py -v
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/ccmux/state_monitor.py tests/test_state_monitor.py
git commit -m "$(cat <<'EOF'
refactor(state-monitor): generalize to tuple of StateObservers

StateMonitor now accepts observers: tuple[StateObserver, ...] in
place of the single state_log parameter. fast_tick fans observations
out to each observer in turn; one observer's failure is logged at
debug and never blocks the others. The StateObserver Protocol
(defined in state_log.py) is implemented by both StateLog and
StateSnapshot, so they're interchangeable from StateMonitor's
perspective.

The previous StateLogProtocol class in state_monitor.py is removed;
the public Protocol is now StateObserver in state_log.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire `CCMUX_STATE_SNAPSHOT` into `DefaultBackend`

Add the env-var-driven snapshot construction alongside the existing log construction. Both go into `self._state_observers` tuple.

**Files:**

- Modify: `src/ccmux/backend.py`
- Modify: `tests/test_state_log_wiring.py`

- [ ] **Step 1: Write the failing test**

Replace `tests/test_state_log_wiring.py` with:

```python
"""Wiring tests for CCMUX_STATE_LOG and CCMUX_STATE_SNAPSHOT env-var toggles."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ccmux.state_log import StateLog, StateSnapshot


class TestEnvVarToggles:
    def test_both_unset_yields_empty_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CCMUX_STATE_LOG", raising=False)
        monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert observers == ()
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_only_log_yields_one_state_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.setenv("CCMUX_STATE_LOG", "1")
        monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert len(observers) == 1
            assert isinstance(observers[0], StateLog)
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_only_snapshot_yields_one_state_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.delenv("CCMUX_STATE_LOG", raising=False)
        monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", "1")
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert len(observers) == 1
            assert isinstance(observers[0], StateSnapshot)
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_both_set_yields_log_then_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.setenv("CCMUX_STATE_LOG", "1")
        monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", "1")
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert len(observers) == 2
            assert isinstance(observers[0], StateLog)
            assert isinstance(observers[1], StateSnapshot)
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_falsy_values_keep_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("", "   ", "0", "false", "no", "off", "garbage"):
            monkeypatch.setenv("CCMUX_STATE_LOG", value)
            monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", value)
            from ccmux.backend import _build_state_observers

            observers = _build_state_observers()
            try:
                assert observers == (), f"value {value!r} should disable both"
            finally:
                for obs in observers:
                    asyncio.run(obs.close())

    def test_truthy_variants(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        for value in ("1", "true", "yes", "on", "TRUE", "On"):
            monkeypatch.setenv("CCMUX_STATE_LOG", value)
            monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)
            from ccmux.backend import _build_state_observers

            observers = _build_state_observers()
            try:
                assert len(observers) == 1
                assert isinstance(observers[0], StateLog)
            finally:
                for obs in observers:
                    asyncio.run(obs.close())
```

- [ ] **Step 2: Run, verify they fail**

```bash
uv run pytest tests/test_state_log_wiring.py -v
```

Expected: ImportError on `_build_state_observers`.

- [ ] **Step 3: Update `src/ccmux/backend.py`**

Add `StateSnapshot` to the import:

```python
from .state_log import StateLog, StateSnapshot
```

Replace the existing `_build_state_log()` function with:

```python
_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in _TRUTHY_ENV_VALUES


def _build_state_observers() -> tuple:
    """Build the state observer tuple from CCMUX_STATE_LOG / CCMUX_STATE_SNAPSHOT.

    Returns observers in declared order: StateLog first (if enabled), then
    StateSnapshot (if enabled). Empty tuple if both disabled.
    """
    observers: list = []
    if _truthy(os.getenv("CCMUX_STATE_LOG", "")):
        observers.append(StateLog(ccmux_dir() / "state.jsonl"))
    if _truthy(os.getenv("CCMUX_STATE_SNAPSHOT", "")):
        observers.append(StateSnapshot(ccmux_dir() / "state_current.json"))
    return tuple(observers)
```

In `DefaultBackend.__init__`, replace `self._state_log: StateLog | None = None` with:

```python
self._state_observers: tuple = ()
```

In `DefaultBackend.start`, replace the existing state_log construction block with:

```python
self._state_observers = _build_state_observers()
state_monitor = StateMonitor(
    event_reader=self.event_reader,
    tmux_registry=self._tmux_registry,
    on_state=on_state_with_resume,
    observers=self._state_observers,
)
```

In `DefaultBackend.stop`, replace the existing state_log close block with:

```python
for obs in self._state_observers:
    try:
        await obs.close()
    except Exception as e:
        logger.debug("observer %s close error: %s", type(obs).__name__, e)
self._state_observers = ()
```

- [ ] **Step 4: Run wiring tests, verify pass**

```bash
uv run pytest tests/test_state_log_wiring.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full suite to confirm nothing else broke**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/backend.py tests/test_state_log_wiring.py
git commit -m "$(cat <<'EOF'
feat(backend): independently toggle state log + state snapshot

DefaultBackend now reads CCMUX_STATE_LOG and CCMUX_STATE_SNAPSHOT as
two independent boolean env vars, constructs whichever observers
are enabled, and passes them to StateMonitor as a tuple. Each
observer's close() is awaited individually during stop().

Common configurations:
- CCMUX_STATE_LOG=1                          → corpus only
- CCMUX_STATE_SNAPSHOT=1                     → live snapshot only
- CCMUX_STATE_LOG=1 + CCMUX_STATE_SNAPSHOT=1 → both
- (neither set)                              → unchanged behavior

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: README docs

Document the new env var alongside the existing one and add `state_current.json` to the state-files list.

**Files:**

- Modify: `README.md`

- [ ] **Step 1: Update the env-var section**

Find the existing `CCMUX_STATE_LOG` bullet in `README.md` and add `CCMUX_STATE_SNAPSHOT` immediately after. Match the surrounding bullet style. Suggested text:

```markdown
- `CCMUX_STATE_SNAPSHOT` — set to `1` / `true` / `yes` / `on` to enable real-time state snapshot. When enabled, every `fast_tick` observation overwrites `$CCMUX_DIR/state_current.json` (default `~/.ccmux/state_current.json`) with a JSON map `{instance_id -> {state, window_id, last_seen}}`. `pane_text` is intentionally omitted; consumers that need raw pane content can run `tmux capture-pane` themselves. Independent of `CCMUX_STATE_LOG`. Unset / falsy: no snapshot, zero overhead.
```

- [ ] **Step 2: Update the state-files section**

Find the existing `state.jsonl` bullet (added in the previous commit) and add a sibling for the snapshot file:

```markdown
- `state_current.json` — only created when `CCMUX_STATE_SNAPSHOT=1`; atomic-rewrite snapshot of every tracked instance's current state, keyed by `instance_id`. Polled by external monitoring tools.
```

- [ ] **Step 3: Run pre-commit on README**

```bash
pre-commit run --files README.md
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): document CCMUX_STATE_SNAPSHOT and state_current.json

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Final verification + bot restart

End-to-end: confirm tests pass, set the env var, restart the bot, and inspect `state_current.json`.

- [ ] **Step 1: Full test suite**

```bash
cd /mnt/nfs/home/wenruiwu/ccmux/ccmux-backend
uv run pytest -q
```

Expected: all green.

- [ ] **Step 2: Pre-commit on full tree**

```bash
pre-commit run --all-files
```

Pre-existing markdownlint failures on historical plan docs are acceptable (they exist on `dev` already). Touched files must pass.

- [ ] **Step 3: Add the snapshot env var to settings.env**

`~/.ccmux/settings.env` is a symlink into the user's dotfiles repo (`~/dotfiles/_linux/backup/.ccmux/settings.env`). Read the current contents:

```bash
cat ~/.ccmux/settings.env
```

Then append the new line by editing the symlinked target file directly (the Edit tool refuses to write through symlinks):

Edit `~/dotfiles/_linux/backup/.ccmux/settings.env` to add:

```env
CCMUX_STATE_SNAPSHOT=1
```

Verify via the symlink:

```bash
cat ~/.ccmux/settings.env
```

Expected output includes both `CCMUX_STATE_LOG=1` and `CCMUX_STATE_SNAPSHOT=1`.

- [ ] **Step 4: Restart the bot**

```bash
# Find current bot pid
ps -ef | grep ccmux-telegram | grep -v grep
# Send SIGINT
tmux send-keys -t __ccmux__:1 C-c
sleep 5
# Verify session/process gone
tmux ls 2>&1 | grep __ccmux__ || echo "(session ended)"
ps -ef | grep ccmux-telegram | grep -v grep || echo "(process ended)"
# Relaunch
tmux new-session -d -s __ccmux__ -c /mnt/beegfs/home/wenruiwu/ccmux/ccmux-telegram 'ccmux-telegram'
sleep 5
tmux ls | grep __ccmux__
ps -ef | grep ccmux-telegram | grep -v grep
```

Expected: new bot process running.

- [ ] **Step 5: Verify both files appear**

```bash
ls -la ~/.ccmux/state.jsonl ~/.ccmux/state_current.json
```

Expected: both files exist with recent mtime.

```bash
python3 -c 'import json; d = json.load(open("/mnt/nfs/home/wenruiwu/.ccmux/state_current.json")); print(f"snapshot has {len(d)} instances"); [print(f"  {k}: {v[\"state\"][\"type\"]} (last_seen={v[\"last_seen\"]})") for k, v in d.items()]'
```

Expected: snapshot has multiple instances (one per active Claude Code session) with their current state + last_seen.

- [ ] **Step 6: Branch state check**

```bash
git log --oneline dev..HEAD
```

Expected commits, in order from oldest to newest:

1. `docs(spec): add design for ccmux state log` (existing)
2. `docs(plan): add implementation plan for ccmux state log` (existing)
3. `feat(state-log): add _serialize_state helper` (existing)
4. `feat(state-log): add StateLog with adjacent-pane dedup` (existing)
5. `test(state-log): cover multi-instance dedup and concurrent record()` (existing)
6. `feat(state-monitor): plumb optional state_log into fast_tick` (existing)
7. `feat(backend): wire CCMUX_STATE_LOG_PATH into DefaultBackend` (existing)
8. `docs(readme): document CCMUX_STATE_LOG_PATH env var` (existing)
9. `chore(state-log): consolidate imports and apply ruff format` (existing)
10. `refactor(state-log): toggle via CCMUX_STATE_LOG, default path under $CCMUX_DIR` (existing)
11. `docs(spec): expand state-log design with state_current.json snapshot` (existing)
12. `feat(state-snapshot): add StateSnapshot atomic-rewrite observer` (new)
13. `refactor(state-monitor): generalize to tuple of StateObservers` (new)
14. `feat(backend): independently toggle state log + state snapshot` (new)
15. `docs(readme): document CCMUX_STATE_SNAPSHOT and state_current.json` (new)

---

## Done criteria

- All tests in `tests/test_state_log.py`, `tests/test_state_snapshot.py`, `tests/test_state_log_wiring.py`, `tests/test_state_monitor.py` pass.
- `pre-commit run --files <touched files>` passes.
- After bot restart with `CCMUX_STATE_SNAPSHOT=1`:
  - `~/.ccmux/state.jsonl` continues to grow (corpus log unaffected).
  - `~/.ccmux/state_current.json` exists and contains one entry per active Claude Code session, with `state.type`, `window_id`, `last_seen` fields, no `pane_text`.
- Setting only `CCMUX_STATE_LOG=1` (and not `CCMUX_STATE_SNAPSHOT=1`) produces only `state.jsonl`. Setting only the snapshot var produces only the snapshot. Setting neither produces neither.

<!-- markdownlint-disable MD024 -->

# ccmux state log + snapshot design

- **Date**: 2026-05-07
- **Repos affected**: `ccmux-backend` (minor)
- **Status**: design accepted; state.jsonl shipped, state_current.json pending

## Revision history

- **2026-05-07 (initial)** — single component: `state.jsonl`, opt-in via
  `CCMUX_STATE_LOG_PATH=/some/path`.
- **2026-05-07 (alignment)** — moved to `$CCMUX_DIR/state.jsonl`, replaced the
  path env var with a boolean `CCMUX_STATE_LOG=1` to mirror the
  `hook.log` / `ccmux.log` / `drift.log` convention.
- **2026-05-07 (split)** — empirical observation showed that `state.jsonl` is a
  poor data source for "what state is each session in *right now*?" because
  idle sessions stay staged in memory until pane changes. Added a second,
  separately-toggled component (`state_current.json`) for that real-time
  snapshot use case. The historical log keeps its current dedup behavior; the
  snapshot file is purpose-built for live polling by an external monitoring
  tool.

## Problem

`claude-code-state.parse_pane(text) -> ClaudeState` is a pure parser that
classifies a Claude Code TUI pane into `Working / Idle / Blocked / Dead`. Its
coverage is grown empirically: every time we notice a Claude UI variant the
parser doesn't classify well, someone copies the offending pane text into a
test fixture and adds a regex / structural rule.

Today this loop is manual and depends on the maintainer happening to look at
the right pane at the right time. There is no recording of "what panes did the
parser actually see in production, and what did it call them?", so:

- **Rare UIs are easy to miss.** A Blocked variant that fires once a week never
  gets captured unless someone is watching that exact tick.
- **Pattern coverage is hard to measure.** When we add a new spinner regex like
  `[spinner] \w+… (\d+s · …)`, we have no way to ask "how many of the panes the
  bot saw last week would have matched this?" — we only have the small fixture
  set.
- **Regressions are silent.** Changing a parser rule may flip the state of
  panes we never deliberately tested. We won't notice until someone reports
  bad behavior in chat.

The first fix is to record `(pane_text, state)` for every `fast_tick`
observation, build up a corpus, and use it offline to mine new patterns and
run regression tests against parser changes.

A separate problem surfaced once the corpus log was running: external tools
(e.g. a status-monitoring software the user wants to build) need a way to ask
"what state is each tracked session in *right now*?" The corpus log is
ill-suited for this because (a) it is append-only, so the "current" record
per instance is buried at varying tail offsets and (b) the dedup design holds
unchanged-pane records in memory and only flushes on pane change, so an idle
session may not appear in the file at all. A separate snapshot file, keyed by
`instance_id` and rewritten atomically per tick, is the natural fit for live
polling.

## Goals

### Corpus log (`state.jsonl`)

1. Record every `(pane_text, state)` pair that `state_monitor.fast_tick`
   observes, including the instance and tmux window context, while the bot is
   running.
2. Keep the recording cheap enough that it can stay on for hours/days without
   producing absurd file sizes — primarily by collapsing consecutive ticks with
   identical pane text.
3. Preserve enough of `ClaudeState` (variant + variant-specific fields like
   `Blocked.ui`) that the corpus can be queried, e.g. "all records where
   `state.type == 'Blocked'` and `state.ui` is missing".

### Real-time snapshot (`state_current.json`)

4. Maintain a small, always-current map `instance_id -> latest observation`
   that an external monitoring tool can poll cheaply (read whole file, parse
   one JSON object) without scanning history.
5. Rewrite atomically so a concurrent reader never sees a partial file.
6. Exclude `pane_text` to keep the file small; consumers that need raw pane
   contents can run `tmux capture-pane` themselves.

### Common

7. Each component is **independently toggled** by its own env var. Default
   off for both; turning one on does not turn the other on.
8. Keep `claude-code-state` untouched. It stays a pure parser; only
   `ccmux-backend` knows about polling, instances, and timestamps, so both
   writers live there.

## Non-goals

- **Not** building a UI / dashboard / live tail viewer. JSONL + `jq` is enough.
- **Not** building a "replay" CLI inside the repo. Feeding records back through
  `parse_pane` is a 10-line script; it can stay outside the package until we
  actually need it.
- **Not** doing rotation, compression, or upload. The user manages the file.
  When it gets too big, they delete it or point the env var elsewhere.
- **Not** offering a fully-deduplicated "unique panes" view as part of the
  runtime. That can be derived offline from the JSONL with `jq | sort -u` or
  similar; baking it into the writer would lose temporal information that we
  may want later.
- **Not** logging panes from the `slow_tick` Dead probe. That tick doesn't
  produce a `(pane_text, state)` pair — it only checks the foreground process.
- **Not** multi-process safety. One backend process writes one file. We rely
  on append-mode writes from a single process.

## Design

### Component placement

```text
ccmux-backend/
  src/ccmux/
    state_log.py          MODIFIED. Adds StateSnapshot class alongside
                          StateLog. Both expose async record() and close().
    state_monitor.py      MODIFIED. Accepts a list of "observers"
                          (StateLog / StateSnapshot). Calls record() on
                          each after parse_pane.
    backend.py            MODIFIED. Reads CCMUX_STATE_LOG and
                          CCMUX_STATE_SNAPSHOT independently; constructs
                          whichever observer(s) are enabled.
  tests/
    test_state_log.py            EXISTING. StateLog unit tests.
    test_state_snapshot.py       NEW. StateSnapshot unit tests.
    test_state_log_wiring.py     MODIFIED. Tests for both env vars and
                                 the independent toggle behavior.
    test_state_monitor.py        MODIFIED. Verifies multi-observer fanout.
  docs/superpowers/specs/
    2026-05-07-ccmux-state-log-design.md   THIS DOCUMENT.
```

`claude-code-state` is **not** modified. Both components live downstream of
the parser.

### Public API

Both observers implement the same async-record interface so `StateMonitor`
treats them uniformly:

```python
# ccmux/state_log.py

class StateObserver(Protocol):
    """Common interface for any sink that wants per-tick state observations."""

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

#### `StateLog` (existing — unchanged from prior revision)

```python
@dataclass(frozen=True)
class _StagedRecord:
    instance_id: str
    window_id: str
    pane_text: str
    state: dict
    first_seen: datetime
    last_seen: datetime
    tick_count: int


class StateLog:
    """Append-only JSONL writer that collapses consecutive identical panes."""

    def __init__(self, path: str | os.PathLike[str]) -> None: ...
```

Adjacent dedup behavior is unchanged. State.jsonl stays a corpus log.

#### `StateSnapshot` (new)

```python
class StateSnapshot:
    """Atomic-rewrite JSON map of instance_id -> latest observation.

    On every record() call:
      1. Update the in-memory map for that instance_id with the latest state,
         window_id, last_seen, claude_session_id (if available).
      2. Atomically rewrite the snapshot file (write tmp, rename).

    pane_text is intentionally NOT stored: consumers that need raw pane
    contents can run `tmux capture-pane` themselves; keeping the file small
    keeps rewrite IO bounded.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None: ...
```

`record()` for `StateSnapshot` ignores `pane_text`. It accepts the same
keyword arguments as `StateLog.record` so the two are interchangeable from
`StateMonitor`'s perspective.

`close()` for `StateSnapshot` is a no-op (the file is always at-rest after
the last rewrite). It's defined so `StateMonitor` can call it uniformly.

The `record()` method is `async` for two reasons: (1) the call site in
`fast_tick` is already inside an async coroutine, so making it async keeps the
shape regular, and (2) the underlying writes (file IO, atomic rename) live
behind an `asyncio.Lock` to serialize across the `asyncio.gather` fan-out in
`fast_tick`.

Both observers are constructed in one place — `DefaultBackend` — based on
their respective env vars; they are never wired by application code directly.
Tests construct them explicitly with `tmp_path`.

### Wiring

`StateMonitor`'s previous `state_log: StateLog | None` parameter generalizes
to a tuple of observers:

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
        ...
        self._observers = observers
```

`fast_tick` calls `record()` on each observer in turn after `parse_pane`:

```python
# inside fast_tick loop, success branch:
pane_text, state = result
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

A single observer's failure is logged at debug and never blocks others. The
existing `_classify_from_pane` refactor (returning `(pane_text, state)`) is
unchanged.

`DefaultBackend.start()` reads each env var independently and constructs the
observers it should enable:

```python
observers: list[StateObserver] = []
if _truthy(os.getenv("CCMUX_STATE_LOG", "")):
    observers.append(StateLog(ccmux_dir() / "state.jsonl"))
if _truthy(os.getenv("CCMUX_STATE_SNAPSHOT", "")):
    observers.append(StateSnapshot(ccmux_dir() / "state_current.json"))
self._state_observers = tuple(observers)
state_monitor = StateMonitor(
    event_reader=...,
    tmux_registry=...,
    on_state=on_state_with_resume,
    observers=self._state_observers,
)
```

Helper `_truthy(s)` returns `s.strip().lower() in {"1", "true", "yes", "on"}`
(centralizes the boolean parsing previously inline in `_build_state_log`).

Both `StateLog.__init__` and `StateSnapshot.__init__` call
`parent.mkdir(parents=True, exist_ok=True)` on their target path. The path is
always under `$CCMUX_DIR`, which the rest of the system also creates on
demand (`hook.log`, `claude_events.jsonl`, etc. follow the same pattern), so
the caller does not need to pre-provision the directory.

`DefaultBackend.stop()` calls `close()` on each observer in the order they
were appended:

```python
async def stop(self) -> None:
    # ... existing task cancellation + event_reader.stop() ...
    for obs in self._state_observers:
        try:
            await obs.close()
        except Exception as e:
            logger.debug("observer %s close error: %s", type(obs).__name__, e)
```

The `try/except` mirrors the existing pattern around `message_monitor.shutdown()`
in the same method: shutdown errors are logged at `debug`, never raised, so
one cleanup failure cannot break the rest of the shutdown sequence.

### Record schema — `state.jsonl`

One JSONL line per "screen segment" (run of identical pane_text for one
instance):

```jsonl
{"first_seen":"2026-05-07T10:23:14.512Z","last_seen":"2026-05-07T10:23:47.108Z","tick_count":34,"instance_id":"telegram:abc123","window_id":"@7","state":{"type":"Blocked","ui":"PermissionPrompt","content":"Allow Bash..."},"pane_text":"...raw pane string..."}
```

Field semantics:

| Field | Type | Notes |
|---|---|---|
| `first_seen` | ISO8601 UTC string | When this pane_text was first observed for this instance. |
| `last_seen` | ISO8601 UTC string | When the pane_text last matched. Equal to `first_seen` if `tick_count == 1`. |
| `tick_count` | int ≥ 1 | How many consecutive ticks observed this exact pane_text. |
| `instance_id` | string | The ccmux instance id passed in by `state_monitor`. |
| `window_id` | string | The tmux window id (e.g. `@7`). Useful for offline debugging. |
| `state` | object | Tagged variant. Always has `type` ∈ `{"Working","Idle","Blocked","Dead"}`. Other fields per variant. |
| `pane_text` | string | Exactly what was passed to `parse_pane`. No re-encoding. |

`state` serialization: use `dataclasses.asdict(state)` and inject
`type=type(state).__name__` at the top level. `BlockedUI` is a `StrEnum`, so it
serializes naturally as a string. Concretely:

- `Working(status_text="Thinking… (16s)")` → `{"type":"Working","status_text":"Thinking… (16s)"}`
- `Idle()` → `{"type":"Idle"}`
- `Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="...")` → `{"type":"Blocked","ui":"permission_prompt","content":"..."}` (StrEnum serializes as its lowercase value, not the Python attribute name)
- `Dead()` → `{"type":"Dead"}` (in practice we never log Dead — see Non-goals — but the serializer handles it for completeness)

**`instance_id` field source**: populated from `binding.tmux_session_name` at the call site. `CurrentClaudeBinding` has no `instance_id` attribute; "instance id" is the public-API name (`Backend.get_instance(instance_id)`) for what is internally `tmux_session_name`. The JSONL field name follows the public vocabulary so log readers don't need to know the internal mapping.

### File schema — `state_current.json`

A single JSON object, keyed by `instance_id`. Rewritten atomically on every
record() that mutates the map. Example:

```json
{
  "claude-code-state": {
    "state": {"type": "Working", "status_text": "Thinking… (3s)"},
    "window_id": "@101",
    "last_seen": "2026-05-07T22:58:39.512Z"
  },
  "ccmux": {
    "state": {"type": "Idle"},
    "window_id": "@80",
    "last_seen": "2026-05-07T22:58:39.502Z"
  }
}
```

Field semantics for each entry:

| Field | Type | Notes |
|---|---|---|
| `state` | object | Same tagged variant shape as in `state.jsonl`. |
| `window_id` | string | Latest observed tmux window_id for this instance. |
| `last_seen` | ISO8601 UTC string | Wall-clock timestamp of the most recent `record()` call. |

`pane_text` is **not** included (see Goal 6). `tick_count` is also omitted —
this file describes "current state", not history.

**Dead instances**: when a `Dead` ClaudeState is observed (slow_tick path),
the entry's `state.type` becomes `"Dead"`. Entries are **not** removed from
the file — a stale entry with old `last_seen` is informative ("we last saw
this instance at T"). Consumers that want to filter to "live" can compare
`last_seen` against now.

**Atomic rewrite**: write to `<path>.tmp` (in the same directory), then
`os.replace(tmp, path)`. POSIX guarantees `os.replace` is atomic on the same
filesystem, so a concurrent reader either sees the old file or the new file —
never a partial write. The codebase already uses `atomic_write_json` (in
`util.py`) for the same pattern; we reuse it here.

### Dedup logic — `state.jsonl` only

Per-instance state held in a `dict[str, _StagedRecord]`:

```text
record(instance_id, window_id, pane_text, state):
    async with self._lock:
        prev = self._staged.get(instance_id)
        now = datetime.now(timezone.utc)
        if prev is not None and prev.pane_text == pane_text:
            # Same pane: bump counter, do not write.
            prev = replace(prev, last_seen=now, tick_count=prev.tick_count + 1)
            self._staged[instance_id] = prev
            return
        # Different pane (or first time): flush prev, start a new staged record.
        if prev is not None:
            self._write(prev)
        self._staged[instance_id] = _StagedRecord(
            instance_id=instance_id,
            window_id=window_id,
            pane_text=pane_text,
            state=_serialize(state),
            first_seen=now,
            last_seen=now,
            tick_count=1,
        )
```

Notes:

- **Dedup key is `pane_text` only, not `(pane_text, state)`.** If two ticks
  produce identical pane text but different `state`, that is parser
  non-determinism — a bug, not something the logger should hide.
- **`window_id` change does not trigger a flush** in practice because
  `instance_id` is the dedup partition; if a window_id ever changed for the
  same instance the staged record's `window_id` would stay at its first
  observation. This is acceptable: window_id is contextual metadata, not part
  of identity.
- **`close()` flushes all staged records.** If the process crashes hard
  (SIGKILL, OOM), at most one record per instance is lost — acceptable for a
  research tool.

### Snapshot logic — `state_current.json`

```text
record(instance_id, window_id, pane_text, state):
    async with self._lock:
        self._current[instance_id] = {
            "state": _serialize(state),
            "window_id": window_id,
            "last_seen": now_iso(),
        }
        self._atomic_write(self._current)

close():
    # No-op. Last record() already wrote a complete snapshot to disk.
    return
```

`pane_text` is accepted but ignored. The snapshot file always reflects the
result of the most recent `record()` call across all instances — there is no
"staged" intermediate state to flush.

### Concurrency

`fast_tick` calls `_classify_from_pane` for all bindings concurrently via
`asyncio.gather`, so `record()` may be invoked concurrently from multiple
coroutines on each observer.

- **`StateLog`** holds a single `asyncio.Lock`, taken for the whole
  read-modify-write of `_staged` and the `_write()` call. The critical section
  is short (one dict lookup + one short append) and contention is bounded by
  the number of instances (typically single digits), so a single lock is
  plenty.
- **`StateSnapshot`** holds its own `asyncio.Lock`, taken for the dict update
  and the atomic-rewrite. The critical section is slightly longer (full JSON
  serialize + tmp write + rename) but still bounded.

The two observers' locks are independent; one slow record() does not block
the other observer.

### File handling

#### `StateLog`

- `StateLog.__init__` opens the path with `open(path, "a", encoding="utf-8")`
  and stores the file handle.
- Every `_write()` call serializes the staged record to JSON, appends a `\n`,
  writes, and flushes.
- `close()` flushes any in-memory staged records, then closes the file handle.
- Parent directory is created on demand (`mkdir(parents=True, exist_ok=True)`).
- No rotation, no size cap.

#### `StateSnapshot`

- `StateSnapshot.__init__` does NOT open a long-lived file handle. Each
  rewrite creates a fresh tmp file via `tempfile.mkstemp` in the target's
  parent directory and renames over the target.
- Atomic rewrite reuses `ccmux.util.atomic_write_json` (existing).
- Parent directory is created on demand.
- File size is bounded by `len(instances) * (state json + last_seen)`,
  typically a few KB.
- `close()` is a no-op.

## Activation

Each component has its own boolean env var. Unset / falsy = off. Truthy
values: `1`, `true`, `yes`, `on` (case-insensitive).

| Env var | Effect |
|---|---|
| `CCMUX_STATE_LOG=1` | Construct `StateLog`; append to `$CCMUX_DIR/state.jsonl`. |
| `CCMUX_STATE_LOG` unset or falsy | No state log. |
| `CCMUX_STATE_SNAPSHOT=1` | Construct `StateSnapshot`; atomic-rewrite `$CCMUX_DIR/state_current.json`. |
| `CCMUX_STATE_SNAPSHOT` unset or falsy | No snapshot file. |

The two toggles are **independent**. Common configurations:

| Goal | Settings |
|---|---|
| Build parser corpus only (original use case) | `CCMUX_STATE_LOG=1` |
| Power an external "current state" monitoring tool | `CCMUX_STATE_SNAPSHOT=1` |
| Both: corpus + live polling | `CCMUX_STATE_LOG=1` and `CCMUX_STATE_SNAPSHOT=1` |
| Default ccmux user who cares about neither | (both unset) |

Both toggles align with the `hook.log` / `ccmux.log` / `drift.log` convention:
filename is fixed, location is `$CCMUX_DIR`, and the directory is created on
demand. The toggle being boolean (rather than always-on like `hook.log`) is
because — unlike sparse log files — these per-tick writers accumulate fast
enough that always-on would surprise users on disk usage.

Reading priority for both env vars (already wired up by `Config`; no
additional code): process env → cwd `settings.env` → `$CCMUX_DIR/settings.env`.

## Testing

`tests/test_state_log.py` (existing, unchanged): behavior of the corpus log.

`tests/test_state_snapshot.py` (new):

- First `record()` for an instance: file exists, contains one entry with
  `state`, `window_id`, `last_seen`.
- Second `record()` with same pane_text/state for same instance: file
  contains one entry, `last_seen` updated.
- Second `record()` for a different instance: file contains both entries.
- Re-instantiating `StateSnapshot` on an existing file does NOT clobber it
  on construction; subsequent `record()` rewrites it (so loading-from-disk
  on init is not implemented; observers of bot restart should expect the
  file to start empty until the first `record()` arrives).
  - Documenting this in the test makes the behavior explicit.
- File is valid JSON after every record() (no torn writes — concurrency test
  fans out 50 instances × 4 calls and asserts the final file parses).
- `state` field serialization matches `state.jsonl`'s tagged-variant shape.
- `pane_text` is NOT in the file even when passed.
- Path with non-existent parent dir → `__init__` creates it.
- `close()` is a no-op (test that calling it does not raise and does not
  modify the file).

`tests/test_state_log_wiring.py` (modified):

- Rename / restructure tests to cover both env vars independently:
  - Both unset → `observers == ()`.
  - Only `CCMUX_STATE_LOG=1` → 1 observer, a `StateLog`.
  - Only `CCMUX_STATE_SNAPSHOT=1` → 1 observer, a `StateSnapshot`.
  - Both set → 2 observers in declared order: `StateLog` first, then
    `StateSnapshot`.
  - Falsy values still recognized as off (`""`, `"0"`, `"false"`, `"no"`,
    `"off"`, `"garbage"`).

`tests/test_state_monitor.py` (modified):

- Replace the `StateLog` integration test with one that uses a list of fake
  observers and asserts `record()` was called on each in order.
- Verify single-observer failure is logged + swallowed: feed a fake observer
  whose `record()` raises; assert `on_state` still fires and the next
  observer in the list still gets called.

`tests/test_api_smoke.py`: nothing to add. Smoke test stays parser-free.

## Open questions

None at design time. The async shutdown hook (`DefaultBackend.stop()`) was
confirmed during the first revision. The new `StateSnapshot.close()` is a
no-op, so adding it to the shutdown sequence is purely structural symmetry.

## Related followups (out of scope here)

- Offline corpus tools: a tiny `scripts/state_log_replay.py` that re-runs
  `parse_pane` over the JSONL and reports diffs vs the recorded state. Worth
  building once we actually have a corpus to point it at.
- Adding a "skipped" reason column when `_classify_from_pane` returns `None`
  (no window, empty pane). Today those silent skips are invisible; the corpus
  could include them for completeness. Not built in v1 because every skip path
  has its own log line in `state_monitor` already.
- Bringing `slow_tick` Dead observations into the same log file. Currently the
  Dead path doesn't have a `pane_text`, so the schema would need a nullable
  `pane_text` and a synthetic state record. Punting until we actually want
  Dead-frequency analysis.

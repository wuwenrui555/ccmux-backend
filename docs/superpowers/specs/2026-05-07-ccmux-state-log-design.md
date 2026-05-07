<!-- markdownlint-disable MD024 -->

# ccmux state log design

- **Date**: 2026-05-07
- **Repos affected**: `ccmux-backend` (minor)
- **Status**: design accepted; implementation pending

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

The fix is to record `(pane_text, state)` for every `fast_tick` observation,
build up a corpus, and use it offline to mine new patterns and run regression
tests against parser changes.

## Goals

1. Record every `(pane_text, state)` pair that `state_monitor.fast_tick`
   observes, including the instance and tmux window context, while the bot is
   running.
2. Keep the recording cheap enough that it can stay on for hours/days without
   producing absurd file sizes — primarily by collapsing consecutive ticks with
   identical pane text.
3. Preserve enough of `ClaudeState` (variant + variant-specific fields like
   `Blocked.ui`) that the corpus can be queried, e.g. "all records where
   `state.type == 'Blocked'` and `state.ui` is missing".
4. Be entirely opt-in: when the env var is unset, behavior is bit-identical to
   today.
5. Keep `claude-code-state` untouched. It stays a pure parser; only
   `ccmux-backend` knows about polling, instances, and timestamps, so the
   logger lives there.

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
    state_log.py          NEW. StateLog class: open file, dedup, write JSONL.
    state_monitor.py      MODIFIED. Optional state_log parameter; calls record().
    backend.py            MODIFIED. Reads CCMUX_STATE_LOG_PATH env var; injects.
  tests/
    test_state_log.py     NEW. Unit tests for the StateLog class.
    test_state_monitor.py MODIFIED. One additional test verifying the
                          state_log hook fires with the right arguments.
  docs/superpowers/specs/
    2026-05-07-ccmux-state-log-design.md   THIS DOCUMENT.
```

`claude-code-state` is **not** modified. The whole feature lives downstream of
the parser.

### Public API

```python
# ccmux/state_log.py

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

    async def record(
        self,
        *,
        instance_id: str,
        window_id: str,
        pane_text: str,
        state: ClaudeState,
    ) -> None: ...

    async def close(self) -> None:
        """Flush all per-instance staged records and close the file."""
```

The `record()` method is `async` for two reasons: (1) the call site in
`fast_tick` is already inside an async coroutine, so making it async keeps the
shape regular, and (2) we use an `asyncio.Lock` internally to serialize writes
across the `asyncio.gather` fan-out.

`StateLog` is constructed in one place — `DefaultBackend` — based on env var
presence; it is never wired by application code directly. Tests construct it
explicitly with a `tmp_path`.

### Wiring

`StateMonitor` gains an optional constructor parameter:

```python
class StateMonitor:
    def __init__(
        self,
        *,
        event_reader: "EventLogReader",
        tmux_registry: "TmuxSessionRegistry",
        on_state: OnStateCallback,
        state_log: "StateLog | None" = None,
    ) -> None:
        ...
        self._state_log = state_log
```

`fast_tick`'s per-binding `_classify_from_pane` is split so that `pane_text` is
returned alongside the state, and the outer loop calls
`self._state_log.record(...)` after `parse_pane`:

```python
async def _classify_from_pane(
    self, b: "CurrentClaudeBinding"
) -> tuple[str, ClaudeState] | None:
    # ... unchanged tmux lookup ...
    pane_text = await tm.capture_pane(b.window_id)
    if not pane_text:
        return None
    state = parse_pane(pane_text)
    if state is None:
        return None
    return pane_text, state

# inside fast_tick loop:
# (current code is gather + zip + isinstance(result, BaseException) handling;
# the unpack and state_log call land in the success branch.)
pane_text, state = result
if self._state_log is not None:
    await self._state_log.record(
        instance_id=b.tmux_session_name,
        window_id=b.window_id,
        pane_text=pane_text,
        state=state,
    )
await self._on_state(b.tmux_session_name, state)
```

`DefaultBackend.__init__` (or wherever `StateMonitor` is constructed today)
reads the env var and injects:

```python
log_path = os.getenv("CCMUX_STATE_LOG_PATH", "").strip()
state_log = StateLog(log_path) if log_path else None
self._state_monitor = StateMonitor(
    event_reader=...,
    tmux_registry=...,
    on_state=...,
    state_log=state_log,
)
```

If the env var path's parent directory does not exist, `StateLog.__init__`
raises `FileNotFoundError` immediately — we don't silently `mkdir` because the
caller may have typed the path wrong.

`StateLog.close()` is awaited at the end of `DefaultBackend.stop()`
(`src/ccmux/backend.py:293`), after the fast/slow tasks are cancelled and the
event reader is stopped. The new line is roughly:

```python
async def stop(self) -> None:
    # ... existing task cancellation + event_reader.stop() ...
    if self._state_log is not None:
        try:
            await self._state_log.close()
        except Exception as e:
            logger.debug("state_log close error: %s", e)
```

The `try/except` mirrors the existing pattern around `message_monitor.shutdown()`
in the same method: shutdown errors are logged at `debug`, never raised, so
one cleanup failure cannot break the rest of the shutdown sequence.

### Record schema

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

### Dedup logic

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

### Concurrency

`fast_tick` calls `_classify_from_pane` for all bindings concurrently via
`asyncio.gather`, so `record()` may be invoked concurrently from multiple
coroutines. `StateLog` holds a single `asyncio.Lock`, taken for the whole
read-modify-write of `_staged` and the `_write()` call. The critical section
is short (one dict lookup + one short append) and contention is bounded by the
number of instances (typically single digits), so a single lock is plenty.

### File handling

- `StateLog.__init__` opens the path with `open(path, "a", encoding="utf-8")`
  and stores the file handle.
- Every `_write()` call serializes the staged record to JSON, appends a `\n`,
  writes, and flushes.
- `close()` flushes any in-memory staged records, then closes the file handle.
- Parent directory must already exist; otherwise `__init__` raises.
- No rotation, no size cap.

## Activation

| Env var | Effect |
|---|---|
| `CCMUX_STATE_LOG_PATH` unset or empty | `state_log=None`, no logging, zero overhead. |
| `CCMUX_STATE_LOG_PATH=/path/to/log.jsonl` | Logger constructed; appends to that file. |

This matches the existing pattern set by `CCMUX_CLAUDE_PROC_NAMES` in
`state_monitor.py`. Documented in the README under the existing "Configuration
via env vars" section (if present; otherwise added there).

## Testing

`tests/test_state_log.py` (new):

- First `record()` for an instance: file is empty, record is staged in memory.
- Second `record()` with identical pane_text for the same instance:
  `tick_count == 2`, `last_seen` advanced, file still empty.
- Second `record()` with a different pane_text: previous record is in the
  file with `tick_count == 1`, new record staged.
- Two instances interleaved: each has its own staged record, neither flushes
  the other.
- `close()` flushes all staged records to file.
- Each line of the file is valid JSON with the expected schema (sample-check
  one of each `ClaudeState` variant).
- `BlockedUI` enum value serializes as its string name, not its int.
- Path with non-existent parent dir → `FileNotFoundError` from `__init__`.
- Re-opening an existing file appends rather than truncating.

`tests/test_state_monitor.py` (modified):

- One new test: construct `StateMonitor` with a fake `StateLog` (records
  arguments to a list), drive a `fast_tick`, assert the fake saw the expected
  `(instance_id, window_id, pane_text, state)`.
- Existing tests construct `StateMonitor` with `state_log=None` (default), so
  none of them need to change.

`tests/test_api_smoke.py`: nothing to add. Smoke test stays parser-free.

## Open questions

None at design time. If `DefaultBackend` doesn't already have an async
shutdown hook that we can hang `StateLog.close()` off of, the implementation
plan will surface that and propose the smallest fix.

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

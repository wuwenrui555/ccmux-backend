<!-- markdownlint-disable MD024 -->

# Event Log Self-Heal Design (v4.0.0)

- **Date**: 2026-04-28
- **Repos affected**: `ccmux-backend` (major), `ccmux-telegram` (follow-up)
- **Status**: design accepted; implementation pending
- **Supersedes**: 2026-04-27 binding-self-heal design (override layer + reconcile_instance — entire mechanism is replaced)

## Problem

`~/.ccmux/claude_instances.json` is a `tmux_session_name → ClaudeInstance` map written by the `ccmux hook` CLI on Claude Code `SessionStart`. The hook is the only writer and uses an "overwrite guard" that refuses to replace an existing entry when `session_id` differs. The bot frontend reads this file and uses an in-memory override layer (v3.1.x) to compensate when the file goes stale.

This architecture has produced three classes of failure, all observed in production within one week:

1. **Stale window_id from tmux-continuum respawn.** Continuum re-spawns Claude in a new window with a new `session_id`; hook fires but the overwrite guard rejects the update because `session_id` differs from the recorded one. File now points at a window that no longer exists.

2. **Stale cwd from project directory reorganization.** A `claude_instances.json` entry from days ago records `cwd=/projects/ccmux-backend`. The user reorganizes to `/projects/ccmux/ccmux-backend`. The old entry is never refreshed because the recorded session_id matches no live Claude. Backend's auto-resume reads the stale cwd and fails (`Directory does not exist`), wedging downstream tracking.

3. **Three-layer state is hard to reason about.** v3.1 added an in-memory override layer plus a `reconcile_instance` resolver to patch over (1) and (2). Three patch releases later, the question "which window is the bot talking to right now?" requires checking three places (file, override dict, reconcile result) and manually reconstructing precedence.

The root cause is identity-confused single-row schema plus mutable read-modify-write. **Hook tries to be authoritative for one row per tmux session; that row's lifetime is shorter than the tmux session's.**

## Goals

1. Replace `claude_instances.json` with an **append-only event log** as Layer 2's source of truth.
2. Hook writes one event per Claude Code lifecycle hook fire (no read-modify-write, no overwrite guard).
3. Backend exposes the same `Backend.get_instance(tmux_session_name)` query shape, but the answer is **derived** from the log on every read (cached in memory, refreshed on each new event).
4. Override layer (`set_override` / `clear_override`) is **deleted**: state self-heals on the next hook fire.
5. cwd, window_id, transcript_path are refreshed automatically by `UserPromptSubmit` events, so stale-cwd-style bugs cannot persist past the next user message.
6. Single-user personal tool; no daemon, no DB, no new dependencies; everything still `cat`-able and `vim`-editable.

## Non-goals

- **Layer 1 (`topic_bindings.json`) is not touched.** Topic ↔ tmux_session_name binding remains frontend-owned and identical to today.
- **No multi-Claude-per-tmux support.** Explicit assumption (see below). Frontends address Claudes by tmux session name; the deepest projection collapses to one row per tmux session.
- **No migration of `claude_instances.json`.** v4.0.0 deletes the file on upgrade; the next hook fire re-populates Layer 2 from scratch (same approach v2.0 → v3.0 took for `window_bindings.json`).
- **No daemon / IPC.** Hook stays a short-lived CLI; backend stays in-process with the frontend.
- **No persistence of prompt content for analytics.** Hook does not store the `prompt` field; the log is for routing, not transcript replay.

## Assumptions

- **One tmux session contains at most one Claude Code window.** Multi-Claude-per-tmux is not supported. If it happens accidentally, the most recently-active Claude wins the projection; older ones become invisible to the bot until they emit a fresh event (which then takes over).
- **Hook events arrive in causal order per tmux session.** The kernel's `O_APPEND` semantics on a regular file guarantee ordered atomic appends for each `write()` of size < `PIPE_BUF` (4096 bytes on Linux/macOS). Hook lines are kept under this limit.
- **Polling is acceptable.** The existing backend already polls tmux at `CCMUX_MONITOR_POLL_INTERVAL` (0.5 s default). The reader piggybacks on the same tick.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│ Claude Code process                                              │
│   ↓ fires SessionStart / UserPromptSubmit                        │
│ ccmux hook (short-lived subprocess)                              │
│   ↓ appends one line                                             │
│ ~/.ccmux/claude_events.jsonl  ◄── single source of truth         │
│   ↑ tail-reads new lines                                         │
│ EventLogReader (in-process, in backend)                          │
│   ↓ projects                                                     │
│ _current: dict[tmux_session_name, CurrentClaudeBinding]          │
│   ↑ queries                                                      │
│ Backend.get_instance(name) / all_alive_bindings()                │
│   ↑ used by                                                      │
│ ccmux-telegram frontend                                          │
└─────────────────────────────────────────────────────────────────┘
```

Data flow is one-way left-to-right. The dict is a pure projection of the log. Bot crash → restart → re-derive the dict from the log → consistent.

## Event log format

Path: `$CCMUX_DIR/claude_events.jsonl` (default `~/.ccmux/claude_events.jsonl`).

Format: JSONL, one event per line, append-only.

Schema per line:

```json
{
  "timestamp": "2026-04-28T15:43:43.889+00:00",
  "hook_event": "UserPromptSubmit",
  "tmux": {
    "session_id": "$574",
    "session_name": "ccmux-2",
    "window_id": "@455",
    "window_index": "1",
    "window_name": "wenruiwu",
    "pane_id": "%594",
    "pane_index": "1"
  },
  "claude": {
    "session_id": "a61a3a01-0cbb-48f1-8ba3-9cc0d9e53faf",
    "transcript_path": "/home/.../-mnt-md0-.../<uuid>.jsonl",
    "cwd": "/mnt/md0/home/wenruiwu",
    "permission_mode": "default"
  }
}
```

- `timestamp` is ISO-8601 with timezone, written by the hook.
- `tmux.*` fields are read from `$TMUX_PANE` + `tmux display-message`. When the hook is invoked outside tmux, every `tmux.*` field is the empty string; the reader filters such lines out (no projection update).
- `claude.*` fields come from the hook's stdin payload. `transcript_path` is provided directly by Claude Code (no more cwd-encoding path computation).
- **`prompt` content is not stored.** UserPromptSubmit's payload includes a `prompt` field, but writing it can blow past `PIPE_BUF` and break atomic appends, and ccmux does not read it for routing.

### Atomicity (no file locking)

Each hook invocation writes exactly one line ending in `\n` via a single `write()` syscall on a file opened with `O_APPEND`. POSIX guarantees atomic interleaving for writes ≤ `PIPE_BUF`. Lines are kept under 1 KB by construction (no `prompt`, fixed-shape payload), well under the 4 KB safe limit. **No fcntl lock is needed** — the kernel serializes appends from concurrent hooks.

## Hook changes (`ccmux-backend`)

### Events registered

`ccmux hook --install` writes two entries into `~/.claude/settings.json` `hooks`:

```json
{
  "hooks": {
    "SessionStart":      [{"hooks": [{"type": "command", "command": ".../ccmux hook", "timeout": 5}]}],
    "UserPromptSubmit":  [{"hooks": [{"type": "command", "command": ".../ccmux hook", "timeout": 5}]}]
  }
}
```

Both events call the same `ccmux hook` binary; the dispatcher inside reads `hook_event_name` from stdin to decide. `Stop`, `PreToolUse`, etc. are deliberately not registered — they would add I/O without ever updating fields the projection reads (Claude finishing a turn changes neither `session_id` nor `window_id`).

### `hook_main` rewrite

Today's `hook.py:_hook_main_impl` does:

1. Parse stdin payload.
2. **Reject if event != SessionStart.**
3. Read tmux info via `$TMUX_PANE` + `display-message`.
4. **Read-modify-write `claude_instances.json` with fcntl lock.**
5. **Apply overwrite guard.**

The new path:

1. Parse stdin payload (same as today).
2. **Accept any of {SessionStart, UserPromptSubmit}; ignore others.**
3. Read tmux info (same as today; out-of-tmux yields empty strings, still written).
4. **Append one JSONL line to `claude_events.jsonl` via `O_APPEND`.** No lock, no read.
5. **Overwrite guard is deleted.**

PID fallback (when stdin is empty/malformed) is kept verbatim for the SessionStart case; that branch is the only path where `pid_session_resolver` is still consulted.

### Removed code

- The `existing` lookup in `_hook_main_impl` (lines 362–382 in current `hook.py`) — guard logic.
- `atomic_write_json(map_file, session_map)` call site — entire read-modify-write block.
- `claude_instances.lock` file path; `fcntl.flock` calls.
- `--install`'s SessionStart-only registration; replaced with the three-event dict above.

## Reader (`ccmux.event_log_reader`, new module)

### Public surface (re-exported from `ccmux.api`)

```python
@dataclass(frozen=True)
class CurrentClaudeBinding:
    tmux_session_name: str       # primary key
    window_id: str               # @5
    claude_session_id: str       # uuid; flips on /clear
    cwd: str
    transcript_path: str
    last_seen: datetime

class EventLogReader:
    def __init__(self, log_path: Path) -> None: ...
    async def start(self) -> None: ...   # initial full read + spawn poll task
    async def stop(self) -> None: ...
    def get(self, tmux_session_name: str) -> CurrentClaudeBinding | None: ...
    def all_alive(self) -> list[CurrentClaudeBinding]: ...
```

### Internals

```python
class EventLogReader:
    _path: Path
    _offset: int                                          # bytes read so far
    _current: dict[str, CurrentClaudeBinding]             # tmux_session_name → row
```

- **Initial read**: open file, read everything, project line-by-line, set `_offset = file_size`.
- **Polling loop**: every `CCMUX_MONITOR_POLL_INTERVAL` seconds, `seek(_offset)`, read new bytes, split on `\n`, project each complete line, advance `_offset`. Partial trailing line stays unread until next tick.
- **Projection rule**: for each event line where `tmux.session_name != ""`, set `_current[tmux.session_name] = CurrentClaudeBinding(...)`. Last write wins.
- **Empty tmux**: lines with empty `tmux.session_name` are skipped (out-of-tmux Claudes are not routable through ccmux).
- **Malformed line**: `JSONDecodeError` or missing required fields → log warning, skip, continue.

### Query semantics

- `get("ccmux")` → `_current.get("ccmux")` → `CurrentClaudeBinding | None`. O(1).
- `all_alive()` → `list(_current.values())`. Caller filters as needed.

The reader does **not** verify window-still-exists in tmux. That check belongs to the consumer (the existing `state_monitor` already does it via `find_window_by_id`). The reader's contract is "what does the log say about this tmux session's most recent Claude" — not "is that Claude actually alive right now". Today's `LivenessChecker` semantics are unchanged.

### Composition into `DefaultBackend`

`DefaultBackend.__init__` gains an `event_reader: EventLogReader` parameter, defaulting to `EventLogReader(ccmux_dir() / "claude_events.jsonl")`. `Backend.get_instance(name)` delegates to `event_reader.get(name)`.

## Backend API changes (`ccmux.api`)

### Removed

- `ClaudeInstanceRegistry` (entire class)
- `Backend.reconcile_instance`
- `set_override` / `clear_override` (live on `ClaudeInstanceRegistry`)
- `pid_session_resolver` module — except for the empty-stdin fallback used inside `hook.py`. The two fallback helpers (`_find_claude_pid`, JSONL-mtime correlation) move into `hook.py` itself as private functions; the public module is deleted.

### Added

- `EventLogReader` class
- `CurrentClaudeBinding` dataclass
- `Backend.event_reader: EventLogReader` accessor (typed handle on the protocol)

### Changed

- `Backend.get_instance(name)` returns `CurrentClaudeBinding | None`. The old `ClaudeInstance` type is removed; v4.0.0 is breaking anyway and a clean rename matches the major bump.
- `DefaultBackend.start` / `stop` lifecycle now also starts/stops `event_reader`.

### Internal cleanup (no API impact)

- `claude_instance.py` deleted (entire module).
- `state_monitor.py` continues to import the new binding type; only the import path changes.
- `message_monitor.py` reads `transcript_path` directly from the binding rather than computing it from cwd. This removes one Claude-Code-coupling point.

## Frontend impact (`ccmux-telegram`)

The current binding-self-heal frontend code was designed against the override API. Most of it goes away.

### Removed

- Startup reconcile pass in `main.py` (the reader's initial read does this work).
- `claude_instances.set_override` / `clear_override` call sites in `command_basic.py` and `main.py`.
- `binding_callbacks.py`'s reconcile-and-override sequence.
- `BindingHealth.observe` for the `RECOVERED` posting **stays** — it's still useful when the user manually fixes a stale binding via JSON edit (now an event-log edit) or when a long-stale Claude comes back online.

### Changed

- `/rebind_window` is removed entirely. The command was a workaround for the override layer's brittleness; with the reader auto-refreshing on every user message it is pure UX clutter.
- `/rebind_topic` (the existing topic ↔ tmux session picker) is unchanged.
- `message_out.py` ⚠️ wording updated to drop the `/rebind_window` mention.

### Version bumps

- `ccmux-backend` → `4.0.0` (breaking API)
- `ccmux-telegram` → `4.0.0` (depends on backend `>=4.0.0,<5.0.0`; user-facing `/rebind_window` removal is breaking for the Telegram menu)

## GC and log lifecycle

### Runtime: do nothing

The log grows by ~1 line per hook fire. Realistic usage: 20 active Claude sessions × 50 user prompts/day × 7 days ≈ 7 000 lines × ~700 bytes ≈ 5 MB / week. Reading 5 MB on bot startup is < 100 ms. **No runtime GC.**

### Startup compaction (optional, manual)

A new CLI subcommand `ccmux compact-events` (run by user when they feel like it):

1. Read entire log into memory.
2. Compute `_current` projection (same as reader).
3. Write a fresh log containing **one synthetic `compaction-snapshot` event per entry in `_current`**, plus header line `{"hook_event": "compaction", "timestamp": "...", "previous_lines": N}`.
4. Atomic rename.

Frontend never has to run compaction; manual is fine for personal tool.

### Log corruption recovery

If the log is partially truncated (power loss mid-write — unlikely with `O_APPEND` + atomic single-line writes, but possible if the disk fills):

- Reader's per-line `JSONDecodeError` handler skips the bad line.
- If the entire file is unreadable, reader starts with empty `_current`. The next hook fire repopulates it.
- User can also manually edit the file (it's plain JSONL).

## Migration / rollout

1. **Pre-merge**: design accepted (this doc), implementation plan written via writing-plans skill.
2. **`ccmux-backend` v4.0.0** ships with: new hook, new reader, deleted modules. CHANGELOG documents:
   - Removed: `ClaudeInstanceRegistry`, `Backend.reconcile_instance`, `set_override`/`clear_override`, `pid_session_resolver` public module.
   - Added: `EventLogReader`, `CurrentClaudeBinding`, `claude_events.jsonl`.
   - Renamed: `ClaudeInstance` → `CurrentClaudeBinding`.
   - Removed file: `~/.ccmux/claude_instances.json`, `~/.ccmux/claude_instances.lock` (delete on upgrade by `ccmux hook --install` running first time on v4).
3. **`ccmux-telegram` v4.0.0** ships next, depending on backend `>=4.0.0`. CHANGELOG documents `/rebind_window` removal and the simplified ⚠️ wording.
4. **First-run on v4**: user reinstalls the hook (`ccmux hook --install`); it overwrites `~/.claude/settings.json` `hooks` to register the three events. Old `SessionStart`-only registration is replaced.
5. **No data migration of `claude_instances.json`**. The first SessionStart / UserPromptSubmit fires populate the new log organically.

## Testing

### Backend

- `EventLogReader.get` after replaying a fixture log: zero events / one SessionStart / multiple events same tmux (last wins) / `/clear` mid-stream (session_id flips, row otherwise unchanged) / multi-tmux interleaved.
- `EventLogReader` tail-poll: write a partial line, poll, ensure not consumed; complete the line, poll, ensure consumed.
- Hook unit tests: malformed stdin → PID fallback path; in-tmux happy path → exact JSON shape; out-of-tmux → empty `tmux` block written; line size assertion (< 4 KB).
- Concurrent hook stress: spawn N hooks in parallel writing to the same file, verify line count == N and every line is valid JSON.

### Frontend

- `/rebind_topic` still binds correctly with the new backend.
- ⚠️ wording on stale binding (no `/rebind_window` reference).
- BindingHealth `RECOVERED` notice fires when a manually-edited log row brings a topic back to a live window.

### Integration / smoke

- End-to-end: send message in topic, verify Claude receives, verify response routes back. Single-Claude single-tmux case.
- tmux-continuum simulation: kill Claude window, spawn new Claude in new window, send another message. Reader should pick up the new window_id by the second user message at the latest.
- /clear simulation: claude_session_id changes; message_monitor switches JSONL tail by the next user message.
- Reorganize cwd simulation: rename project directory, send a user message in topic; auto-resume should never fire against the stale cwd because the next UserPromptSubmit refreshed it.

## Out of scope

- Watching the log via inotify / fsevents (polling is fine at 0.5 s).
- Per-Claude addressing (binding a topic to a `claude_session_id` rather than tmux session). Possible future feature; not in v4.0.
- Daemon process owning the log (single-writer-daemon alternative discussed and rejected: hooks must work when bot is offline).
- Compaction at runtime; manual `ccmux compact-events` is enough.
- Sharing state across machines (Linux + macOS each maintain their own log).

## Open questions

(none at design time; resolve at implementation if surprises emerge)

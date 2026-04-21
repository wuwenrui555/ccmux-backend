# Design: unify ccmux-backend around ClaudeState

Status: draft, pending user review
Target release: ccmux-backend v2.0.0 (breaking)
Date: 2026-04-20

## Problem

v1.3.x introduced `PaneState` (WORKING / IDLE / BLOCKED / UNKNOWN) on
`WindowStatus` to classify captured panes. The feature landed, but the
data model stayed incoherent:

- `WindowStatus` still exposes five flat fields (`window_exists`,
  `pane_captured`, `status_text`, `interactive_ui`, `pane_state`) that
  are not independent. `pane_state` is a lossless classification of
  the other four, yet every consumer bypasses it and re-derives the
  classification from the raw fields.
- `ccmux-telegram/src/ccmux_telegram/watcher.py::classify()` hand-rolls
  a two-state reduction (`working` / `waiting`) from ambiguous flags.
- `ccmux-telegram/src/ccmux_telegram/status_line.py::_consume_one()`
  walks four `if` layers over the same fields that `pane_state` already
  encodes.
- The root type is named "pane state" but actually describes the
  running Claude Code instance; the tmux pane is only one of several
  observation channels (JSONL transcript is the other).

The consequence: a parser-level fix can get merged (e.g. `⎿` elbow
skip, `a692a93`) while the frontend's interpretation of the result is
still a brittle chain of truthiness checks. There is no single type
the codebase can dispatch on.

## Goal

Organize the entire backend around a sealed four-case `ClaudeState`
union, keyed per `ClaudeInstance`. After the refactor:

- A running Claude Code session is a named entity (`ClaudeInstance`)
  with two observable axes: its **state** (`ClaudeState`) and its
  **messages** (`ClaudeMessage`, already defined).
- The backend emits exactly two kinds of observation via two
  callbacks: `on_state(instance_id, ClaudeState)` and
  `on_message(instance_id, ClaudeMessage)`.
- The backend is a pure producer: no state cache, no edge detection,
  no bus. Frontends that care about transitions maintain their own
  `{instance_id: last_state}`.
- `WindowStatus` and the old `PaneState` StrEnum are deleted. Every
  consumer pattern-matches on the sealed union instead of re-deriving
  from flat fields.
- Liveness detection (tmux alive + `claude` foreground) folds into
  the state axis as the `Dead` variant — no longer a separate module.

The refactor is internally breaking; external API (`ccmux.api`) ships
a new type family and drops the old one in the same release.

### Compatibility principle

**No backward compatibility is considered anywhere in this refactor.**
On upgrade to v2.0.0:

- Old `window_bindings.json` is ignored (does not migrate).
- Existing frontends pinned to v1.x must update in lockstep.
- No deprecation shims, no dual-emit callbacks, no alias imports.

This applies to every module, every type, every persistence file, and
every field name. Rename freely.

## Non-goals

- No behaviour change to `tmux_pane_parser` functions
  (`parse_status_line`, `extract_interactive_content`, `_has_input_chrome`)
  or their detection accuracy. Pure parser logic is re-used intact.
- No change to `parser_config.json` or its schema (that is a
  user-authored override file, not backend-managed state).
- No change to how fast/slow poll loops are scheduled.
- Not in scope: push-based transition events, state-transition logs
  to disk, completion-notification dispatch helpers. Frontends compute
  edges themselves.

## Breaking changes

| Item removed | Replacement |
|---|---|
| `ccmux.api.WindowStatus` | Two callback signatures — `on_state` gets `(instance_id, ClaudeState)`; `on_message` gets `(instance_id, ClaudeMessage)`. |
| `ccmux.api.PaneState` (StrEnum) | `ccmux.api.ClaudeState` sealed union: `Working \| Idle \| Blocked \| Dead`. |
| `ccmux.api.InteractiveUIContent.name: str` | `InteractiveUIContent.ui: BlockedUI` (StrEnum, 6 members matching existing UI patterns). |
| `ccmux.api.WindowBinding` | `ccmux.api.ClaudeInstance` (same four fields, `session_name` renamed to `instance_id`). |
| `ccmux.api.WindowBindings` | `ccmux.api.ClaudeInstanceRegistry`. |
| `Backend.is_alive(window_id)` | No direct replacement. `on_state` emits `Dead()` when the process dies; consumers track last state if they care. |
| `Backend.get_window_binding(window_id)` | `Backend.get_instance(instance_id)`. |
| `Backend.start(on_message, on_status)` signature | `Backend.start(on_state, on_message)`. Argument order changes (state first for parallelism with the two-axis model). |
| `$CCMUX_DIR/window_bindings.json` | `$CCMUX_DIR/claude_instances.json`. Old file is ignored on upgrade; users re-bind their Claude sessions (aligns with the existing "bindings are manually managed" policy). |
| JSON top-level key `session_name` (inside the persistence file) | `instance_id`. |

Internal (not exposed via `ccmux.api`):

| Item removed | Replacement |
|---|---|
| `ccmux/status_monitor.py` | `ccmux/state_monitor.py` (different output type; absorbs liveness). |
| `ccmux/liveness.py` (module) | Folded into `state_monitor.py` as a slow-tick sub-probe. |
| `ccmux/window_bindings.py` (module) | `ccmux/claude_instance.py`. |
| `LivenessChecker._window_alive` cache | Deleted. Consumers infer liveness from last observed `ClaudeState`. |

## Design

### Entity model

```text
ClaudeInstance                            # the subject
   ├── instance_id: str     [stable]      # binding key, survives resume
   ├── window_id: str       [mutable]     # tmux window, changes on resume
   ├── session_id: str      [mutable]     # Claude JSONL UUID, changes on /clear
   └── cwd: str             [stable]      # launch directory

ClaudeState                               # axis 1: what Claude is doing now
   = Working(status_text)                 # chrome + spinner + "…"
   | Idle()                               # chrome + no spinner
   | Blocked(ui, content)                 # chrome replaced by blocking UI
   | Dead()                               # tmux alive, claude process not

ClaudeMessage                             # axis 2: what Claude said
   = (existing type from claude_transcript_parser — unchanged)
```

`BlockedUI` is a `StrEnum` mirroring the six detection patterns
already in `parser_config.UI_PATTERNS`:

```text
PERMISSION_PROMPT | ASK_USER_QUESTION | EXIT_PLAN_MODE |
BASH_APPROVAL | RESTORE_CHECKPOINT | SETTINGS
```

### Type invariants

- `Working.status_text` is non-empty and contains U+2026 (`…`). The
  parser's existing contract for running status lines is reflected
  in the type.
- `Blocked` always carries both `ui` and `content`; no bare
  `Blocked()`.
- `Dead()` and `Idle()` carry no payload.
- The union is sealed. Adding a fifth state requires touching every
  match site — intentional.

### Monitor behaviour

**`state_monitor`** (replaces `status_monitor` and `liveness`):

| Observation | Source | Produces | Side effect |
|---|---|---|---|
| chrome + spinner with `…` | pane text (fast tick ~500ms) | `Working(status_text)` | `on_state(id, Working(...))` |
| chrome + no spinner | pane text (fast tick) | `Idle()` | `on_state(id, Idle())` |
| no chrome | pane text (fast tick) | `Blocked(ui, content)` | `on_state(id, Blocked(...))` |
| tmux alive, `claude` not foreground | process probe (slow tick ~60s) | `Dead()` | `on_state(id, Dead())` + `backend` triggers auto-resume |
| tmux window missing | tmux capture attempt | — | **skip**: no callback, bindings untouched |
| transient capture failure | tmux capture attempt | — | **skip**: no callback |

Skip semantics: the monitor does not emit anything on skip. The
frontend sees no event and leaves its displayed state alone. This
matches the project-wide policy that bindings are never auto-cleared;
temporary tmux absences (reboots, detach) do not propagate as UI
churn.

**`message_monitor`** (renamed callback, otherwise unchanged):
tails JSONL files via existing byte-offset logic, invokes
`on_message(instance_id, ClaudeMessage)` for each new line.

### Auto-resume

When `state_monitor` emits `Dead`, the backend — not the monitor —
issues the resume via `TmuxOps.create_window(resume_session_id=...)`.
The SessionStart hook asynchronously updates `ClaudeInstanceRegistry`
with the new `window_id`; the next fast tick observes the fresh
instance and emits `Working` or `Idle` normally. The frontend sees
two state callbacks: `Dead()` then (after some seconds) a live state.

### Backend API

```python
class Backend(Protocol):
    tmux: TmuxOps
    claude: ClaudeOps

    def get_instance(self, instance_id: str) -> ClaudeInstance | None: ...

    async def start(
        self,
        on_state:   Callable[[str, ClaudeState],   Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None: ...

    async def stop(self) -> None: ...
```

`TmuxOps` and `ClaudeOps` sub-protocols stay unchanged. The
module-level default singleton (`get_default_backend` /
`set_default_backend`) retains the same shape; only its `start`
signature changes.

### Statelessness

The backend maintains no per-instance state beyond
`ClaudeInstanceRegistry` (the binding persistence layer, unchanged in
behaviour). Every state observation is emitted fresh each tick; the
backend does not dedupe, does not compute deltas, does not remember
what it last sent. Frontends that dispatch on edges maintain
`{instance_id: last_state}` themselves — one dict lookup per
callback.

### Module layout

```text
ccmux/
├── __init__.py                    [-]
├── util.py                        [-]
├── config.py                      [-]
├── cli.py                         [-]
├── parser_config.py               [-]
├── tmux.py                        [-]
├── claude_files.py                [-]
├── claude_transcript_parser.py    [-]
├── hook.py                        [M]    persistence file + key rename
├── tmux_pane_parser.py            [M]    BlockedUI import; InteractiveUIContent.ui type
├── message_monitor.py             [M]    callback signature only
├── api.py                         [M]    re-exports new family
├── backend.py                     [M]    dual callback; auto-resume orchestration
├── claude_instance.py             [+]    ClaudeInstance + ClaudeInstanceRegistry
├── claude_state.py                [+]    sealed union + BlockedUI
├── state_monitor.py               [+]    pane fast-tick + process slow-tick
├── window_bindings.py             [-]    → claude_instance.py
├── status_monitor.py              [-]    → state_monitor.py + claude_state.py
└── liveness.py                    [-]    → absorbed into state_monitor.py
```

Net: 16 files → 16 files (3 deleted, 3 added, 6 modified).

### Field-level renames

Applies everywhere inside ccmux-backend and once inside
ccmux-telegram:

- `WindowBinding` → `ClaudeInstance`
- `WindowBindings` → `ClaudeInstanceRegistry`
- `session_name` (field, parameter, and persistence-file key) → `instance_id`
- `get_window_binding` → `get_instance`
- `$CCMUX_DIR/window_bindings.json` → `$CCMUX_DIR/claude_instances.json`
- `hook.py`'s JSON writes switch to the new filename and key

No aliases, no shims, no migration helper. Per the compatibility
principle above, old state is abandoned on upgrade.

## Frontend impact (ccmux-telegram)

Three consumer surfaces adapt.

**`status_line.py`** becomes a `match` over `ClaudeState`:

```python
async def on_state(instance_id: str, state: ClaudeState) -> None:
    match state:
        case Working(text):        await enqueue_status(instance_id, text)
        case Idle():               await clear_to_idle(instance_id)
        case Blocked(ui, content): await handle_interactive_ui(instance_id, ui, content)
        case Dead():               await show_resuming(instance_id)
```

**`watcher.py::classify()`** simplifies:

- `Working` → `working`
- `Idle` or `Blocked` → `waiting`
- `Dead` → new `resuming` display state

**`topic_bindings.py::is_alive()` + hot-path gates.**
`backend.is_alive()` is gone. The frontend grows a small last-state
cache (`{instance_id: ClaudeState}`, populated in `on_state`) and
rewrites `topic_bindings.is_alive()` to read from it —
`last_state.get(id) not in (None, Dead())`. The ~10 call sites in
`message_out.py` / `command_basic.py` continue to call
`_topics.is_alive(topic)` unchanged; only the internal implementation
switches source of truth.

Observable behaviour changes (visible to end users):

| Scenario | Before | After |
|---|---|---|
| User closes the tmux window manually | Status message cleared to empty. | Last state persists in the topic (matches the "bindings are never auto-cleared" policy). |
| Transient `capture-pane` hiccup | Could briefly clear status. | Silent; no callback fires. |
| Claude process dies | Repeated churn as liveness and pane observations fight. | One `Dead` callback; resume happens; next live state arrives as a fresh callback. |
| Same state persists for N ticks | Each tick re-sends `WindowStatus`; frontend dedupes. | Each tick re-sends the same state; frontend either ignores or dedupes via its own `last_state` dict. |

The "transient capture fail" and "window closed" behaviour changes
are deliberate: they follow from the rule "skip observations do not
emit". Frontends that want to show "session disconnected" can render
it from time-since-last-callback themselves.

## Testing

**New backend tests:**

- `tests/test_claude_state.py` — type contracts: `Working.status_text`
  non-empty and contains `…`; `Blocked` requires both fields; the
  union is exhaustive under `match`.
- `tests/test_state_monitor.py` — pane fixture → expected `ClaudeState`
  classification (covers each variant); skip rules (window gone /
  capture fail → no callback); slow-tick `Dead` detection.
- `tests/test_claude_instance.py` — registry load/save round-trip;
  `get_instance` / iteration; `instance_id` as stable key.

**Rewritten / retargeted:**

- `tests/test_tmux_pane_parser.py` — assertions on
  `InteractiveUIContent.ui` use `BlockedUI` enum values; string
  comparisons removed. Parser behaviour tests unchanged.
- `tests/test_pane_state.py` — retire; merged into
  `test_claude_state.py` + `test_state_monitor.py`.
- `tests/test_hook.py` — assertions switch from
  `window_bindings.json` + `session_name` to
  `claude_instances.json` + `instance_id`.
- `tests/test_verify_all.py` — retire or merge into
  `test_state_monitor.py` (the `verify_all` flow becomes
  the slow-tick sub-probe of `state_monitor`).
- `tests/fake_backend.py` — drop `is_alive` / `get_window_binding`;
  emit `on_state` / `on_message` callbacks matching the new
  `Backend` protocol.
- ccmux-telegram `tests/fake_backend.py`,
  `tests/test_status_monitor.py`, `tests/test_watcher.py`,
  `tests/test_bindings.py` — update to the new callback shape and
  type family; rewrite tests that relied on `backend.is_alive()`
  to exercise the frontend-side last-state cache instead.

**Fixture reuse:** every pane-text fixture
(`sample_pane_*.txt` et al.) stays usable; they feed the new
`state_monitor` unchanged.

## Release

**v2.0.0, hard break.**

- Single backend PR: delete old types, add new, update internal
  consumers, bump version in `pyproject.toml` and `CHANGELOG.md`.
- ccmux-telegram PR lands same working day after the backend tag,
  updating imports and consumer logic.
- `CHANGELOG.md` entry for v2.0.0 includes the full breaking-change
  table from this doc.
- README API table regenerated from the new `api.py` exports.

No deprecation period. Running frontends pinned to v1.x keep working
against v1.x backends; the major bump is the signal to coordinate
the upgrade.

## Open design questions

None pending. All design decisions above were ratified during
brainstorming.

## Follow-ups (explicitly not in this refactor)

- Push-based state transition events (backend emits `Transition(from, to)`
  instead of tick snapshots). Deferred because frontend-side edge
  detection is trivial and the backend's statelessness is a simpler
  starting point.
- Completion-notification helpers in ccmux-telegram that fire on
  `Working → Idle` edges. Belongs in the frontend after this refactor
  gives it a clean state type to dispatch on.
(Previously listed "retire `window_bindings.json`" as a follow-up —
now folded into this refactor per the compatibility principle.)

# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to `ccmux` are documented here. The project follows
[Semantic Versioning](https://semver.org/): the public surface
(`ccmux.api`) is stable across minor and patch releases; breaking changes
require a major bump.

## [Unreleased]

## 2.5.1 — 2026-04-21

### Fixed

- Apply `ruff format` to `tests/test_tmux_pane_parser.py`. v2.5.0
  landed on `main` with the file flagged by the CI
  `ruff format --check` step; this hotfix reformats so CI on `main`
  is green again. No behaviour change.

## 2.5.0 — 2026-04-21

### Changed (BREAKING)

- `parse_status_line` returns raw TodoWrite rows, exactly as
  rendered in the pane. The ASCII-bracket translation, `~~…~~`
  strikethrough wrap, 2-space indent normalization, and 50-char
  truncation that v2.4.0 baked into the parser are all removed.
  Rows retain the original `⎿` elbow connector, CC-supplied
  indentation, Unicode checkbox glyphs (`◻` / `◼` / `✔`), and the
  indented `… +N pending[, M completed]` overflow tail verbatim.

  Rationale: those transformations were Telegram-specific UX
  decisions that leaked into `ccmux.api.Working.status_text`.
  Splitting concerns: the parser extracts faithful pane content;
  each frontend renders it to its own markup.

  Migration for frontends that inlined v2.4.0's formatted output:
  apply your own normalization (drop `⎿`, translate checkbox glyphs,
  truncate for your message-size budget, wrap completed rows in
  your renderer's strikethrough syntax). See `ccmux-telegram`
  v2.2.0 for a reference implementation.

### Removed

- `_normalize_todo_row`, `_truncate_todo_row`, `_BRACKET_MAP`,
  `_STRIKE_OPEN`/`_STRIKE_CLOSE`, and all related frontend-facing
  constants. Unused after the parser stopped formatting.

## 2.4.0 — 2026-04-21

### Changed

- `parse_status_line` reshapes the TodoWrite rows it appends to the
  returned status text so frontends can render them without a
  monospace code block. The elbow connector `⎿` is dropped; leading
  whitespace is normalized to two spaces on every row (including the
  `… +N pending` overflow tail); and the Unicode checkbox glyphs are
  replaced with ASCII bracket markers:
  - `◻` / `☐` → `[ ]` (pending)
  - `◼`       → `[>]` (in progress)
  - `✔` / `✓` / `☒` → `[x]` (completed)
  Completed rows are additionally wrapped in GitHub-flavored
  `~~...~~` so the Telegram frontend's markdown pipeline renders them
  with native MarkdownV2 strikethrough. ASCII brackets never trigger
  emoji-style rendering on any client, so columns line up without
  font tricks.

- Row truncation (`_TODO_ROW_MAX_LEN`) now accounts for the `~~` wrap
  on completed rows so closing tildes stay balanced after truncation;
  an unbalanced wrap would have forced the frontend's MarkdownV2
  parse to fall back to plain text and lose formatting on every
  status update.

### Removed

- `_force_text_style` (the U+FE0E VS-15 approach) is gone. Telegram's
  mobile pre-block renderer ignored the variation selector anyway, so
  it bought nothing.

### Docs

- `docs/integration-prompts.md` is updated to match: the prompt is
  tool-agnostic (some CC harnesses expose `TaskCreate` instead of
  `TodoWrite`), and the Verify block runs the parser against a
  temp-file path so `uv run` stderr noise no longer leaks into the
  captured parse result. Layer 2's success check now gates on both a
  spinner ellipsis AND at least one ASCII-bracket task row.

### Tests

- Pre-existing TodoWrite tests updated to assert the new normalized
  shape; new cases cover the strikethrough wrap
  (`test_done_row_wrapped_in_markdown_strikethrough`) and the
  truncation-preserves-closing-tilde invariant.

## 2.3.0 — 2026-04-21

### Added

- `parse_status_line` now appends TodoWrite rows to the returned
  status text so frontends can render what Claude is working on
  alongside the spinner. The returned string is the spinner text on
  the first line followed by every checkbox row, the first-row
  `⎿  <checkbox>` elbow connector, and the `… +N pending[, M
  completed]` overflow tail — each on its own line, in top-to-bottom
  visual order, preserving original indentation. Rows longer than 50
  characters are truncated with an ellipsis so a verbose multi-task
  plan stays within Telegram's status message budget. Panes without
  TodoWrite content keep returning a single-line spinner text, same
  as before.

### Changed

- `parser_config` splits the previously merged `SKIPPABLE_PATTERNS`
  into two named buckets with distinct disposition during the
  spinner scan:
  - `OVERLAY_PATTERNS` — skipped but not collected (session-rating
    modal and similar overlays that should not surface in the status
    text). User-supplied `skippable_patterns` JSON overrides land
    here by default.
  - `TODO_PATTERNS` — skipped AND collected (TodoWrite checkbox
    rows, the elbow connector, and the overflow tail).
  `SKIPPABLE_PATTERNS` remains exported as the union for callers
  that only need a single "is this skippable?" check.

- `$CCMUX_DIR/parser_config.json` schema is unchanged: user
  `skippable_patterns` entries are still accepted and now merged
  into `OVERLAY_PATTERNS`.

### Tests

- Seven new tests cover the appending behaviour, 50-character
  truncation, rating-modal overlay disposition, and
  backward-compatible single-line return when no TodoWrite is
  present. Pre-existing TodoWrite tests updated to assert on the
  new multi-line shape.

## 2.2.2 — 2026-04-21

Tests and tooling only. No code or API changes.

### Tests

- `test_realistic_long_todowrite_pane` in `test_tmux_pane_parser.py`
  exercises the status-line skip stack against a verbatim pane
  captured from a live CC 2.1.116 session building a 12-task
  TodoWrite plan. Guards the v2.2.1 fix against regression on real
  CC output rather than hand-forged fixture strings.

### Docs

- New `docs/integration-prompts.md` catalogues prompts that drive a
  live CC session into specific UI states for end-to-end validation
  of parser changes. Includes the 12-task TodoWrite overflow prompt
  and a sampling-based three-layer verify script (pane, parser,
  frontend log) with automatic `/clear` cleanup on exit. Workflow
  is "Claude executes, user reads the summary".

## 2.2.1 — 2026-04-21

### Fixed

- `parse_status_line` bailed when CC's TodoWrite list overflowed the
  render window and rendered a `… +N pending[, M completed]` tail
  (indented) between the last checkbox and the chrome. The tail line
  matched no existing skip rule, so the upward scan terminated on it
  and the spinner never surfaced — the frontend saw the pane as
  IDLE even though Claude was actively working.

### Changed

- Unified the three status-skip mechanisms (`STATUS_SKIP_GLYPHS`
  frozenset, `SKIPPABLE_OVERLAY_PATTERNS` regex list, and the
  inline `⎿+checkbox` compound check in `parse_status_line`) into a
  single `SKIPPABLE_PATTERNS` regex tuple in `parser_config`. The
  scanner now does one regex pass over each candidate line. Skip
  semantics are unchanged for every case except the new overflow
  tail.

- `$CCMUX_DIR/parser_config.json` override schema collapses
  `skippable_overlays` + `status_skip_glyphs` into a single
  `skippable_patterns` regex list. No backwards compatibility — user
  configs must migrate.

### Tests

- `test_skips_todowrite_pending_tail` and
  `test_skips_todowrite_pending_and_completed_tail` cover the new
  skip rule; existing checklist / elbow / rating-modal tests
  continue to exercise the unified path.

## 2.2.0 — 2026-04-21

### Fixed

- SessionStart hook's overwrite guard blocked `_try_resume` from
  updating `claude_instances.json` after Claude exited and Backend
  opened a fresh tmux window running `claude --resume <session_id>`.
  The registry stayed pointing at the dead window, so StateMonitor
  kept emitting `Dead`, auto-resume fired again, a new tmux window
  was created every poll tick, and the user's tmux session
  accumulated a zombie window per minute until manual cleanup.

  Split the guard: reject only when `window_id` AND `session_id`
  both differ (the original intent: a distinct Claude taking over
  the same tmux session). When the new `session_id` matches, treat
  the hook as a resume report and overwrite the entry so the
  registry follows the live window.

### Tests

- `test_same_session_resume_updates_window` covers the new allow
  path; `test_different_window_refuses_overwrite` continues to
  cover the multi-Claude reject path.

## 2.1.0 — 2026-04-21

### Added

- `UIPattern.walkback: bool`: when True, `extract_interactive_content`
  expands the extracted region upward from the top anchor to the line
  after the nearest full-width `────` separator. Captures the tool
  preview block (`Read file` / `Read(/etc/passwd)`, `Bash command` /
  `<command>`, `Enable auto mode?` + description) that sits above the
  approval question. Enabled on the three permission patterns —
  `PERMISSION_PROMPT` ("Do you want to …?" variants), the numbered-
  options fallback, and `BASH_APPROVAL`.
- New `PERMISSION_PROMPT` pattern anchored on `Enable \w+ mode\?` for
  the Shift+Tab mode-toggle confirmation (auto / plan / …). Walkback
  naturally carries back the banner and description.

### Rationale

Claude Code 2.1.x does not flush `tool_use` to JSONL until the turn
completes, i.e. only after the user approves the tool. Frontends
relying on a JSONL lookup for permission-prompt UI injection (see
`ccmux-telegram` 2.1.0 drop of `tool_context`) find the cache empty
and have no tool preview to render. The pane is the only source of
truth during an active permission dialog; walkback makes the parser
return it.

## 2.0.0 — 2026-04-20

Refactor organizes the entire backend around a sealed four-case
`ClaudeState` union keyed per `ClaudeInstance`. Replaces the flat
`WindowStatus` + old `PaneState` StrEnum with a pattern-matchable type
family and a two-callback Backend protocol. Frontends pinned to
v1.x are not compatible and must upgrade in lockstep.

### Breaking changes — `ccmux.api`

| Removed | Replacement |
|---|---|
| `WindowStatus` | two callbacks: `on_state(instance_id, ClaudeState)` + `on_message(instance_id, ClaudeMessage)` |
| `PaneState` (StrEnum) | `ClaudeState` sealed union: `Working \| Idle \| Blocked \| Dead` |
| `InteractiveUIContent.name: str` | `InteractiveUIContent.ui: BlockedUI` |
| `WindowBinding` | `ClaudeInstance` (`session_name` → `instance_id`, `claude_session_id` → `session_id`) |
| `WindowBindings` | `ClaudeInstanceRegistry` (primary getter is `get(instance_id)`) |
| `Backend.is_alive(window_id)` | no direct replacement; consumers maintain `{instance_id: last_state}` and treat anything other than `Dead` as alive |
| `Backend.get_window_binding(window_id)` | `Backend.get_instance(instance_id)` |
| `Backend.start(on_message, on_status)` | `Backend.start(on_state, on_message)` (argument order changed) |

### Breaking changes — persistence

- `$CCMUX_DIR/window_bindings.json` → `$CCMUX_DIR/claude_instances.json`
- Inside the file, outer keys that were conceptually `session_name`
  are now `instance_id`. The inner dict shape (`window_id`,
  `session_id`, `cwd`) is unchanged.
- **No migration.** On upgrade the old file is ignored; users re-bind
  their Claude sessions.

### Internal changes

- `status_monitor.py` and `liveness.py` merged into `state_monitor.py`.
- `window_bindings.py` renamed to `claude_instance.py`.
- New `claude_state.py` hosts the sealed union and `BlockedUI` enum.
- `LivenessChecker._window_alive` cache deleted — liveness is now
  expressed as the `Dead` variant on `ClaudeState`.
- `MessageMonitor.poll()` returns `(instance_id, ClaudeMessage)` pairs
  so backends can route per-instance without a separate lookup.
- `DefaultBackend` owns the auto-resume coordinator directly; it
  subscribes to `Dead` state observations from `StateMonitor` and
  retries `claude --resume` with an idempotency guard against
  concurrent fires.

## 1.3.1 — 2026-04-20

### Changed

- Apply `ruff format` across `src/` and `tests/`. v1.3.0 landed on
  `main` with 5 files flagged by the CI `ruff format --check` step;
  this hotfix reformats them so CI on `main` is green again. No
  behaviour change.

## 1.3.0 — 2026-04-20

### Added

- `ccmux.api.PaneState` enum (`UNKNOWN` / `WORKING` / `IDLE` / `BLOCKED`).
  Classifies a captured pane using input-chrome presence and spinner
  state. Downstream features (completion notifications, waiting-topic
  dashboards, smart input routing) can dispatch on this instead of
  reconstructing the same signals from `status_text` /
  `interactive_ui` independently.
- `WindowStatus.pane_state` field, populated by `StatusMonitor._observe`.
  Defaults to `PaneState.UNKNOWN` so existing callers keep working.
- `parser_config.STATUS_SKIP_GLYPHS` — glyph set of task-checklist
  bullets (`◼ ◻ ☐ ☒ ✔ ✓`) that are free-skipped between spinner and
  chrome. Overridable via `parser_config.json`'s new
  `status_skip_glyphs` array.

### Fixed

- `extract_interactive_content` no longer matches UI patterns in pane
  scrollback. Live blocking UIs (permission prompts, AskUserQuestion,
  ExitPlanMode, Settings panels) always replace Claude's input chrome;
  the presence of the `────\n❯\n────\nstatusbar` sandwich at the pane
  bottom is now a hard gate that short-circuits detection. Without
  this, pasted transcripts containing UI-looking text triggered the
  status poller every tick and the Telegram frontend spammed the
  bound topic with fresh UI messages every few seconds.
- `parse_status_line` treats the turn-completion summary (`✻ Worked
  for 56s`, `· Cogitated for 1m 25s`) as not-running. It shares the
  spinner prefix but lacks the `…` ellipsis that marks work in
  progress. Returning the completion line as status leaked throwaway
  `Worked for 56s` bubbles into Telegram that were then overwritten
  by the next user message via status→content conversion.
- `TmuxSession.get_session()` catches `libtmux._internal.query_list
  .ObjectDoesNotExist`. That class is not a `LibTmuxException`
  subclass, so the previous `except _TMUX_ERRORS` let it propagate
  on every "probe for missing session" call — the common path when
  the picker tries to bind a brand-new session name.
- `TmuxSession(session_name="")` preserves the empty string instead of
  silently promoting it to `config.tmux_session_name`. The `or`
  fallback masked a bug where stale callbacks passed `""` and windows
  ended up written into the default session (`__ccmux__`) with the
  topic binding holding an empty session name, producing a permanent
  "Session '' has no window yet" error.
- `parse_status_line` now correctly detects the Claude Code spinner
  when a TodoWrite task checklist (`◼` / `◻` / etc.) sits between the
  spinner and the chrome separator. Previously the checklist rows
  were treated as unknown text and bailed the upward scan, leaving
  Telegram status messages stale during long subagent or multi-step
  runs.
- The upward scan budget (`_STATUS_SCAN_WINDOW`) is raised from `10`
  to `30` — empirically the real layout is spinner + ≤20 TodoWrite
  rows + blank + ≤2 overlay lines ≈ 24; 30 leaves headroom for
  subagent stacking. Unknown lines still bail the scan, so the larger
  window doesn't raise false-positive risk.
- Claude Code hook handler falls back to PID-based session lookup
  when stdin is empty. Previously the hook silently failed to
  register the window, leaving `window_bindings.json` incomplete and
  the Telegram topic unable to route messages.

## 1.2.1 — 2026-04-19

### Changed (internal only — no `ccmux.api` impact)

- Renamed `ccmux.parser_overrides` → `ccmux.parser_config`. Logger
  name follows (`ccmux.parser_overrides` → `ccmux.parser_config`).
- `parser_config` is now the single source of truth for
  Claude-Code-coupled parser constants. Built-in defaults, user
  override loading, merge composition, and shadow detection all
  live here. Parser modules (`tmux_pane_parser`,
  `claude_transcript_parser`) are pure consumers of
  `parser_config.UI_PATTERNS`, `parser_config.STATUS_SPINNERS`, etc.
- Removed `UIPattern` re-export from `tmux_pane_parser`. Import
  from `ccmux.parser_config` instead.
- Removed `_SIMPLE_SUMMARY_FIELDS` / `_BARE_SUMMARY_TOOLS` class
  attributes from `TranscriptParser`. Use
  `ccmux.parser_config.SIMPLE_SUMMARY_FIELDS` /
  `BARE_SUMMARY_TOOLS` directly.
- Removed module-level `UI_PATTERNS`, `STATUS_SPINNERS`, and
  `_SKIPPABLE_OVERLAY_PATTERNS` attributes from `tmux_pane_parser`.
  Import from `ccmux.parser_config` instead.

### Fixed

- Shadow detection no longer relies on a local duplicate of
  built-in names inside the override module. Adding a new built-in
  `UIPattern` or summary field now automatically participates in
  shadow detection without a second manual edit.

### Not affected

- `ccmux.api` surface is unchanged.
- `$CCMUX_DIR/parser_config.json` schema is unchanged.
- All user-observable behaviour (merge semantics, error handling,
  log output) is preserved.

## 1.2.0 — 2026-04-19

### Added

- `$CCMUX_DIR/parser_config.json` — optional JSON override for five
  Claude-Code-coupled parser constants (`UI_PATTERNS`,
  `_SKIPPABLE_OVERLAY_PATTERNS`, `STATUS_SPINNERS`,
  `TranscriptParser._SIMPLE_SUMMARY_FIELDS`,
  `TranscriptParser._BARE_SUMMARY_TOOLS`). Lets ops patch drift when
  a Claude Code update changes wording without waiting for a backend
  release. Merge semantics: user `ui_patterns` prepend to built-in so
  they match first; `simple_summary_fields` replaces built-in values
  per key; `skippable_overlays`, `status_spinners`, and
  `bare_summary_tools` take the union. Unknown schema version,
  malformed JSON, or per-entry errors degrade to empty overrides with
  warnings — the bot never fails to start because of a bad file. See
  [`docs/claude-code-compat.md`](docs/claude-code-compat.md) for the
  drift quick-fix procedure.
- INFO log on every successful override load summarising per-section
  counts plus an additional INFO per detected shadow (same name as a
  built-in `ui_patterns` entry, or same key as a built-in
  `simple_summary_fields`).

## 1.1.0 — 2026-04-19

### Added

- `hook.log` under `CCMUX_DIR` (default `~/.ccmux/hook.log`). The
  `ccmux hook` CLI now tees logs to a file handler alongside stderr so
  SessionStart invocations can be diagnosed after Claude Code's inline
  error banner scrolls away. Unhandled exceptions are recorded with a
  full traceback before the process exits 1. File logging is
  best-effort — a read-only state directory degrades to stderr-only
  without blocking the hook.

### Fixed

- `uv.lock` is now tracked in the repo. Previously it was gitignored,
  which broke the `astral-sh/setup-uv@v3` cache step in CI
  (`No file ... matched to [**/uv.lock]`). Tracking the lockfile is
  also the standard convention for uv projects: reproducible installs
  and diff review of dependency bumps.

## 1.0.0 — 2026-04-19

First stable release. The `ccmux.api` surface is now frozen.

### Changed (breaking vs. pre-1.0 snapshots)

- `ClaudeBackend` → `Backend`; `DefaultClaudeBackend` → `DefaultBackend`.
  Protocol split into `Backend.tmux: TmuxOps` and `Backend.claude:
  ClaudeOps` sub-protocols.
- `TmuxManagerRegistry` → `TmuxSessionRegistry`; `registry` module-level
  singleton → `tmux_registry`.
- `WindowRegistry` → `WindowBindings`; state file
  `~/.ccmux/tmux_claude_map.json` → `~/.ccmux/window_bindings.json`.
- `TranscriptParser` no longer injects Telegram-specific sentinel tokens
  into `ClaudeMessage.text`. Collapsible regions (tool output, thinking,
  diffs, long command results) are emitted as standard CommonMark
  blockquotes (lines prefixed with `>`). Frontends that want a
  collapsible UI detect the `>` prefix and render locally; plain-text
  consumers see readable quoted lines.
- `LivenessChecker.__init__` now takes an explicit `tmux_registry:
  TmuxSessionRegistry` argument. `StatusMonitor.__init__` accepts the
  same. The previous module-level `tmux_registry` import inside
  `liveness.py` / `status_monitor.py` has been removed so multiple
  isolated backends can coexist in one process.
- `DefaultBackend.__init__` accepts a new `show_user_messages: bool |
  None = None` kwarg. The old `CCMUX_SHOW_USER_MESSAGES` env var is
  still honored as the default.

### Fixed

- `LivenessChecker._check_claude` was a tautology (compared a session_id
  to itself) and never detected a dead Claude. The check now looks at
  the pane's foreground process (`pane_current_command` ∈ {`claude`,
  `node`}) and correctly triggers auto-resume when Claude has exited
  back to the shell.
- `LivenessChecker` now prunes cache entries for window_ids no longer
  present in `window_bindings.json`, preventing unbounded growth.
- `tests/test_integration_tmux.py` reformatted to satisfy
  `ruff format --check` (CI now passes from a clean checkout).
- `tmux.py` replaces the deprecated `Window.set_window_option` with
  `set_option` (libtmux 0.55+).

### Added

- `UsageInfo` is now re-exported from `ccmux.api` so frontends can
  type-annotate `parse_usage_output` return values without importing
  from submodules.
- `tests/test_api_smoke.py` locks the v1.0 API surface: every
  `__all__` symbol is verified importable, every event-payload dataclass
  has its fields pinned, and `DefaultBackend.start`/`stop` lifecycle is
  exercised.
- `CCMUX_CLAUDE_PROC_NAMES` env var (comma-separated, default
  `claude,node`) overrides the set of pane foreground process names the
  `LivenessChecker` treats as "Claude is alive". Lets ops recover
  without a backend release if a Claude Code update switches runtimes.
- `docs/claude-code-compat.md` catalogues every module that is coupled
  to Claude Code's UI / JSONL / hook contract, grouped by how often it
  breaks in practice, with a recommended upgrade ritual.

### Notes for frontend integrators

If you consumed `TranscriptParser.EXPANDABLE_QUOTE_START` /
`EXPANDABLE_QUOTE_END`, switch to detecting standard Markdown
blockquotes: any line starting with `>` (with a space after) is
part of a collapsible region. See `ccmux-telegram` v1.0+ for a reference
renderer that converts `>` blocks to Telegram MarkdownV2 expandable
blockquotes.

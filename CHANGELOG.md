# Changelog

All notable changes to `ccmux` are documented here. The project follows
[Semantic Versioning](https://semver.org/): the public surface
(`ccmux.api`) is stable across minor and patch releases; breaking changes
require a major bump.

## [Unreleased]

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
  blockquotes (lines prefixed with `> `). Frontends that want a
  collapsible UI detect the `> ` prefix and render locally; plain-text
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
blockquotes: any line starting with `> ` (with an optional space) is
part of a collapsible region. See `ccmux-telegram` v1.0+ for a reference
renderer that converts `> ` blocks to Telegram MarkdownV2 expandable
blockquotes.

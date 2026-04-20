# Changelog

All notable changes to `ccmux` are documented here. The project follows
[Semantic Versioning](https://semver.org/): the public surface
(`ccmux.api`) is stable across minor and patch releases; breaking changes
require a major bump.

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

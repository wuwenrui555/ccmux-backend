# Claude Code compatibility guide

This document catalogues the places in `ccmux` that are tightly coupled
to Claude Code's user-visible behaviour (prompt wording, JSONL schema,
hook API, runtime process names). When a Claude Code release breaks the
backend, the fix almost always lives in one of the modules listed below.

The list is ordered by how often the module breaks in practice — the
top of the page is what you should look at first.

## Fault tree — "what breaks when Claude Code updates?"

### 🔴 Most fragile — prompt wording in the pane

**Module:** [`src/ccmux/tmux_pane_parser.py`](../src/ccmux/tmux_pane_parser.py), the `UI_PATTERNS` list.

Every interactive UI that Telegram renders as a keyboard
(ExitPlanMode, AskUserQuestion, PermissionPrompt, BashApproval,
RestoreCheckpoint, Settings/model picker) is detected by a pair of
anchor regexes — top marker and bottom marker — matched against the
captured tmux pane. Example anchors currently in use:

| Pattern         | Top anchors (partial)                                       | Bottom anchors            |
|-----------------|-------------------------------------------------------------|---------------------------|
| ExitPlanMode    | `Would you like to proceed?` / `Claude has written up a plan` | `ctrl-g to edit in` / `Esc to (cancel\|exit)` |
| AskUserQuestion | `☐ ☒ ✔` bullets                                             | `Enter to select`         |
| PermissionPrompt| `Do you want to proceed?` / `Do you want to make this edit` | `Esc to cancel`           |
| BashApproval    | `Bash command` / `This command requires approval`           | `Esc to cancel`           |
| RestoreCheckpoint | `Restore the code`                                        | `Enter to continue`       |
| Settings        | `Status  Config  Usage  Stats` tab bar / `Select model`    | `Esc to (cancel\|exit\|close)` |

**Symptom of breakage:** user gets a prompt in Claude Code but the
Telegram keyboard does not appear; or a completely wrong keyboard
appears.

**Built-in safety net:** `drift_logger`. When a pane looks
prompt-shaped (contains `Esc to`, `Enter to`, `❯ 1.`, `Would you like
to`, `Do you want to`, `Type to filter`) but no `UI_PATTERNS` entry
matches, a one-line fingerprint is written to
`~/.ccmux/drift.log`. Grep it after every Claude Code upgrade — a new
entry is a drift signal.

**Fix:** add a new regex to the relevant `UIPattern` tuple (or a whole
new `UIPattern` for a new UI type). One-line change. Confirm with
`pytest tests/test_tmux_pane_parser.py`.

**Drift quick-fix (no backend release):** add a new entry to the
`ui_patterns` section of `$CCMUX_DIR/parser_config.json` and restart
the frontend. User entries are prepended to the built-in list so they
match first. See [`parser_config.json` schema in the v1.2.0 design
doc](superpowers/specs/2026-04-19-externalize-cc-constants-design.md).

### 🟠 Fragile — chrome / spinner / modal anchors

**Module:** same file.

- `STATUS_SPINNERS = {"·", "✻", "✽", "✶", "✳", "✢"}` — if Claude Code
  rotates to a new animation glyph, `parse_status_line` stops finding
  the spinner and the Telegram status line freezes.
- Chrome separator detection — `_find_chrome_separator` looks for a
  full-width run of `─` characters (≥20 chars). Any visual redesign of
  the pane footer breaks this, and every downstream parser that uses
  `_strip_pane_chrome` goes with it.
- `parse_usage_output` anchors on the literal strings `Current session`
  and `Current week` and stops at `Esc to`. Any `/usage` modal rewrite
  will need new anchor lines.
- `extract_bash_output` depends on Claude Code echoing `! <cmd>` and
  rendering results under `  ⎿  …`.

**Symptom of breakage:** status line ("Reading file…") stops updating;
`/usage` returns empty; `!` echo capture returns `None`.

**Fix:** patch the offending constant / regex; parser tests cover most
of these.

**Drift quick-fix (no backend release):** for `STATUS_SPINNERS`,
`_SKIPPABLE_OVERLAY_PATTERNS`, and `STATUS_SKIP_GLYPHS`, append the
new glyph / overlay regex / checklist glyph to `status_spinners`,
`skippable_overlays`, or `status_skip_glyphs` in
`$CCMUX_DIR/parser_config.json` and restart the frontend.
Chrome-separator and `parse_usage_output` / `extract_bash_output`
anchors are not yet externalised — they still need a backend patch.

### 🟡 Moderate — JSONL and hook contracts

**Module:**
[`src/ccmux/claude_transcript_parser.py`](../src/ccmux/claude_transcript_parser.py)
and [`src/ccmux/hook.py`](../src/ccmux/hook.py).

The JSONL transcript schema (`type: user|assistant|summary|…`,
`message.content` is a list, `tool_use.id`,
`tool_result.tool_use_id`, `thinking.thinking`) is part of the
Anthropic tool-use protocol and evolves slowly. What does churn:

- `_SIMPLE_SUMMARY_FIELDS` — a per-tool map of `tool_name → input key`
  to surface as a summary (e.g. `Read → file_path`, `Bash → command`).
  When Claude Code renames a tool's input field, the summary silently
  falls back to `**ToolName**` with no argument. Visual regression
  only; no crash.
- New tools — `Skill` was added in 2.1.x. If a new tool lands with
  display semantics (takes a specific field), add it to
  `_SIMPLE_SUMMARY_FIELDS` or `_BARE_SUMMARY_TOOLS`.

**Drift quick-fix (no backend release):** add tool renames or new
tools to `simple_summary_fields` / `bare_summary_tools` in
`$CCMUX_DIR/parser_config.json` and restart the frontend.

The hook CLI depends on Claude Code's `SessionStart` payload fields
`session_id`, `cwd`, `hook_event_name`. If the hook API changes:

**Symptom:** new Claude sessions never appear in
`~/.ccmux/window_bindings.json`, so the backend never discovers them.
Telegram bot loses new windows entirely.

**How to notice:** Claude Code itself keeps running; the failure is
silent. Check `~/.ccmux/ccmux.log` and Claude Code's own hook stderr.

**Fix:** update `hook_main()` in `hook.py` to match the new payload.

### 🟢 Low-risk — assumptions that rarely break

- **`src/ccmux/liveness.py`** — default set
  `_DEFAULT_CLAUDE_PROC_NAMES = {"claude", "node"}`. If Claude Code
  ever switches runtimes (Bun, Deno, a compiled binary with a
  different process name), every live pane would be misclassified as
  dead and the slow loop would spam auto-resume. Easy to spot — log
  fills with "Attempting to resume".
  **Hot-fix without a release:** set
  `CCMUX_CLAUDE_PROC_NAMES=claude,node,<new-runtime>` in
  `~/.ccmux/.env` and restart the frontend. The env var is read every
  verify-all tick, so no backend code change is needed.
- **Claude Code `!` bash mode timing** — `tmux.py` inserts a 1s delay
  after sending `!` so the TUI has time to switch modes. If the
  keybinding or mode-switch latency changes, the tail of `! long cmd`
  arrives before the mode is ready and gets eaten.
- **`window.set_option("allow-rename", "off")`** — ccmux tells tmux not
  to let Claude Code relabel the window. If libtmux ever renames this
  option again, fix in `tmux.py`.

## Recommended upgrade ritual

When a Claude Code release lands:

1. **Before upgrading:** `uv run pytest` (baseline — should already be
   green).
2. **Upgrade Claude Code.** Keep using it for a day.
3. **Check `~/.ccmux/drift.log`** — a new fingerprint line means an
   interactive UI is no longer recognised. Capture the pane that
   triggered it (`tmux capture-pane -t <pane> -p`) and paste a sample
   into a new or existing `UIPattern` as a regex target.
4. **Watch `~/.ccmux/ccmux.log`** for:
   - Repeated "Attempting to resume" → probably the runtime process
     name changed; check `pane_current_command` of a known-live
     Claude pane and set `CCMUX_CLAUDE_PROC_NAMES` to include the new
     name (no code change needed).
   - "Failed to parse session_name:window_id from tmux" → hook payload
     or tmux `display-message` format drift.
5. **Re-run parser tests:**
   `uv run pytest tests/test_tmux_pane_parser.py tests/test_claude_transcript_parser.py`.

## Where to grep first

```text
UI prompt drift         → tmux_pane_parser.UI_PATTERNS
status line frozen      → tmux_pane_parser.STATUS_SPINNERS / _find_chrome_separator
/usage broken           → tmux_pane_parser.parse_usage_output
new tool not labelled   → claude_transcript_parser._SIMPLE_SUMMARY_FIELDS
hook not firing         → hook._install_hook / hook_main (payload fields)
auto-resume loop        → liveness._DEFAULT_CLAUDE_PROC_NAMES (or set CCMUX_CLAUDE_PROC_NAMES)
drift quick-fix         → $CCMUX_DIR/parser_config.json (see v1.2.0 design)
```

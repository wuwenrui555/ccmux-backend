# Design: externalize Claude-Code-coupled parser constants

Status: approved, ready for implementation plan
Target release: ccmux-backend v1.2.0
Date: 2026-04-19

## Problem

`ccmux-backend` owns five constants that track Claude Code's pane UI,
status chrome, and tool rendering. Every Claude Code release can
invalidate the exact wording these constants match, at which point
users have to wait for a backend patch to see prompts on Telegram
again. The built-in safety net (`~/.ccmux/drift.log`) surfaces the
break but does not unblock anyone — the fix still requires a ship.

The five constants:

| Constant | Module | Purpose |
|---|---|---|
| `UI_PATTERNS` | `tmux_pane_parser` | list of `UIPattern` (name + top/bottom regex anchors) driving Telegram keyboard detection |
| `_SKIPPABLE_OVERLAY_PATTERNS` | `tmux_pane_parser` | regex tuple; overlay lines that must not short-circuit the spinner scan |
| `STATUS_SPINNERS` | `tmux_pane_parser` | frozenset of spinner glyphs indicating "Claude is working" |
| `_SIMPLE_SUMMARY_FIELDS` | `claude_transcript_parser` | dict `tool_name -> input_field_key` for one-line tool summaries |
| `_BARE_SUMMARY_TOOLS` | `claude_transcript_parser` | frozenset of tool names rendered without arg |

Today each is a Python literal edited via PR + release.

## Goal

Let ops recover from Claude-Code-induced drift by editing a local JSON
file and restarting the frontend — no backend release required.
Built-in defaults stay shipped in the package and still define the
baseline; the override augments them.

## Non-goals

- Externalizing algorithmic anchors embedded in functions (`_find_chrome_separator`, `parse_usage_output` literals, `extract_bash_output` markers). These are not pure lookup tables.
- Hot-reload of the override file (restart is accepted).
- Removing or disabling entries from the built-in set (see "Limitations").
- Changing `CCMUX_CLAUDE_PROC_NAMES` — it already has an env-driven override that fits its shape.

## User-visible contract

### File location

`$CCMUX_DIR/parser_config.json` (default `~/.ccmux/parser_config.json`).
Optional. When absent, the backend behaves exactly as today.

### Schema (JSON)

```json
{
  "$schema_version": 1,

  "ui_patterns": [
    {
      "name": "ExitPlanMode",
      "top": ["Would you like to proceed", "Claude has written up a plan"],
      "bottom": ["ctrl-g to edit", "Esc to (cancel|exit)"],
      "min_gap": 2
    }
  ],

  "skippable_overlays": [
    "^\\s*●\\s*How is Claude doing this session\\?"
  ],

  "status_spinners": ["·", "✻", "✽"],

  "simple_summary_fields": {
    "Read": "file_path",
    "NewToolName": "input_key"
  },

  "bare_summary_tools": ["TodoRead", "ExitPlanMode"]
}
```

All sections are optional; a minimal override touches a single section.

### Merge semantics (per constant)

| Constant | Shape | Merge rule |
|---|---|---|
| `UI_PATTERNS` | list, scan-in-order, first match wins | user entries **prepended** so they match first |
| `_SKIPPABLE_OVERLAY_PATTERNS` | regex tuple, any match triggers skip | **union** (order irrelevant) |
| `STATUS_SPINNERS` | frozenset | **union** |
| `_SIMPLE_SUMMARY_FIELDS` | `dict[str, str]` | `{**built_in, **user}` — user wins per-key |
| `_BARE_SUMMARY_TOOLS` | frozenset | **union** |

User cannot disable a built-in entry. Fixing a broken built-in still
requires a backend release; the common drift case — new wording to
cover — is handled additively.

### Shadow detection

At load time, emit INFO logs for the two constants whose semantics
support shadowing:

- `ui_patterns` entry whose `name` already exists in built-in
- `simple_summary_fields` entry whose key already exists in built-in

Set-union constants have no shadow concept (the union absorbs
duplicates silently).

### Error handling

The bot must never fail to start because of a malformed override. Each
failure mode degrades the smallest unit it can:

| Failure | Effect | Log |
|---|---|---|
| File absent | Silent, use built-ins | none |
| Unreadable (permissions / IO) | Empty overrides, use built-ins | WARNING |
| JSON syntax error | Empty overrides, use built-ins | WARNING + exception string |
| `$schema_version` != `1` | Empty overrides, use built-ins | WARNING |
| Section has wrong top-level type | That section skipped, others still load | WARNING |
| Entry malformed (bad regex, missing field, wrong value type) | That entry skipped, rest of section loaded | WARNING with entry index |

### Success log

On successful load, emit one INFO summary line with per-section counts,
followed by one additional INFO line per detected shadow:

```
ccmux.parser_overrides: loaded parser_config.json:
  ui_patterns=3, skippable_overlays=1, status_spinners=0,
  simple_summary_fields=2, bare_summary_tools=0
ccmux.parser_overrides: shadowing built-in ui_pattern 'ExitPlanMode'
ccmux.parser_overrides: shadowing built-in simple_summary_field 'Read' (file_path -> new_field)
```

## Architecture

One new module: `src/ccmux/parser_overrides.py`.

```python
@dataclass(frozen=True)
class ParserOverrides:
    ui_patterns: list[UIPattern]
    skippable_overlays: tuple[re.Pattern[str], ...]
    status_spinners: frozenset[str]
    simple_summary_fields: dict[str, str]
    bare_summary_tools: frozenset[str]

OVERRIDES: ParserOverrides  # loaded once at package import
```

Consumers merge at **module top level** so the merged value stays a
plain module attribute — no runtime function dispatch, no behavioural
change for existing call sites:

```python
# tmux_pane_parser.py
from .parser_overrides import OVERRIDES

_BUILTIN_UI_PATTERNS: list[UIPattern] = [...]  # today's hard-coded list

UI_PATTERNS: list[UIPattern] = OVERRIDES.ui_patterns + _BUILTIN_UI_PATTERNS
_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    OVERRIDES.skippable_overlays + _BUILTIN_SKIPPABLE_OVERLAYS
)
STATUS_SPINNERS: frozenset[str] = _BUILTIN_STATUS_SPINNERS | OVERRIDES.status_spinners
```

```python
# claude_transcript_parser.py
from .parser_overrides import OVERRIDES

class TranscriptParser:
    _BUILTIN_SIMPLE_SUMMARY_FIELDS = {...}
    _SIMPLE_SUMMARY_FIELDS: dict[str, str] = {
        **_BUILTIN_SIMPLE_SUMMARY_FIELDS,
        **OVERRIDES.simple_summary_fields,
    }
    _BUILTIN_BARE_SUMMARY_TOOLS = frozenset({...})
    _BARE_SUMMARY_TOOLS: frozenset[str] = (
        _BUILTIN_BARE_SUMMARY_TOOLS | OVERRIDES.bare_summary_tools
    )
```

### Loading sequence

```
parser_overrides.load()           # called once when the module first imports
  ├─ path = Path($CCMUX_DIR) / "parser_config.json"
  ├─ if not path.exists(): return ParserOverrides(empty)          # silent
  ├─ raw = read_json_or_warn(path)                                # WARNING on failure
  ├─ if raw is None: return ParserOverrides(empty)
  ├─ if raw.get("$schema_version") != 1:
  │     warn("unsupported schema_version"); return empty
  ├─ sections = {
  │     "ui_patterns": parse_ui_patterns(raw.get("ui_patterns")),
  │     "skippable_overlays": parse_regex_list(raw.get("skippable_overlays")),
  │     "status_spinners": parse_chars(raw.get("status_spinners")),
  │     "simple_summary_fields": parse_str_dict(raw.get("simple_summary_fields")),
  │     "bare_summary_tools": parse_str_set(raw.get("bare_summary_tools")),
  │ }
  ├─ detect_and_log_shadows(sections)
  └─ return ParserOverrides(**sections)
```

Each `parse_*` helper is isolated: its own try/except so one bad entry
or one bad section cannot poison the others.

### Import graph

- `parser_overrides` imports only from stdlib (`re`, `json`,
  `dataclasses`, `logging`, `pathlib`) plus `ccmux.util.ccmux_dir`.
- `tmux_pane_parser` and `claude_transcript_parser` import
  `OVERRIDES` (and the `UIPattern` dataclass) from `parser_overrides`.
- No reverse edges — `parser_overrides` does not import any of the
  parser modules.

`parser_overrides` must construct `UIPattern` instances from the JSON,
which would create a circular import if `UIPattern` lived in
`tmux_pane_parser`. Resolution: the `UIPattern` dataclass is defined
in `parser_overrides`, and `tmux_pane_parser` re-exports it so
existing imports (`from .tmux_pane_parser import UIPattern`, used by
tests and external consumers) continue to resolve.

## Testing

New file: `tests/test_parser_overrides.py`.

### Loader

- `test_no_file_returns_empty` — `OVERRIDES` empty, no log
- `test_malformed_json_falls_back` — WARNING + empty
- `test_unknown_schema_version_falls_back` — WARNING + empty
- `test_permission_error_falls_back` — WARNING + empty

### Per-section tolerance

- `test_invalid_regex_in_ui_pattern_skipped`
- `test_missing_required_field_skipped`
- `test_non_single_char_spinner_rejected`
- `test_wrong_section_type_scoped_to_that_section`

### Merge semantics

- `test_ui_patterns_user_first` — user `ExitPlanMode` wins on match
- `test_skippable_overlays_union`
- `test_status_spinners_union`
- `test_simple_summary_fields_user_overrides`
- `test_bare_summary_tools_union`

### Shadow detection

- `test_shadow_ui_pattern_logs_info`
- `test_shadow_simple_summary_field_logs_info_with_values`
- `test_no_shadow_no_log`

### Integration smoke

Added to `test_tmux_pane_parser.py` and
`test_claude_transcript_parser.py` respectively:

- `test_override_ui_pattern_recognised_by_extract_interactive_content`
- `test_override_summary_field_used_by_format_tool_use_summary`

Pattern for each:

```python
def test_...(monkeypatch, tmp_path):
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    (tmp_path / "parser_config.json").write_text(json.dumps({...}))
    import ccmux.parser_overrides as po
    import ccmux.tmux_pane_parser as tpp
    importlib.reload(po)
    importlib.reload(tpp)
    # assert behaviour reflects override
```

### Regression

`uv run pytest` stays green. Existing parser tests do not need
changes — with no override file they see `UI_PATTERNS` etc. identical
to today's literal.

### Coverage

`parser_overrides.py` ≥ 90% (pure data plumbing). Existing modules'
coverage does not drop.

## Release plan

ccmux-backend **v1.2.0** (minor bump — purely additive to the
implementation; `ccmux.api` unaffected).

Per `managing-git-branches`:

1. `feature/parser-overrides` from `dev`
2. Implement + tests, merge back to `dev` with `--no-ff`
3. `release/v1.2.0` from `dev`, bump `pyproject.toml`, add CHANGELOG
   `[1.2.0]` section, update docs (below)
4. Merge `release/v1.2.0` into `main` (`--no-ff`), tag `v1.2.0`
5. Back-merge `release/v1.2.0` into `dev` (`--no-ff`)
6. Push `main dev --tags`

`ccmux-telegram`'s `ccmux>=1.0.0,<2.0.0` pin already covers v1.2.0.

### Docs to update

- `docs/claude-code-compat.md` — under each 🔴/🟠/🟡 section add: "drift quick-fix: add an entry to the relevant section of `$CCMUX_DIR/parser_config.json` and restart the frontend — no release needed." Extend `Where to grep first` with `parser_config.json` pointer.
- `README.md` State files section — add:
  > `parser_config.json` — optional; overrides brittle Claude Code parser constants without a backend release. See [compat guide](docs/claude-code-compat.md).
- `CHANGELOG.md` — draft entry:
  > Added: `$CCMUX_DIR/parser_config.json` override file for Claude-Code-coupled parser constants … [full text in §6 of the design discussion]

### Post-release validation

1. Write a throwaway override that shadows `ExitPlanMode` with a
   nonsense top pattern. Restart bot. Confirm INFO shadow log shows up
   in `ccmux.log`.
2. Remove the override, restart, confirm logs are quiet.
3. `grep Traceback ~/.ccmux/hook.log` is empty (v1.1.0 coverage holds).

## Limitations and deferred work

- No way to **remove** a built-in entry via override. Rare. If it
  comes up, revisit with a `disabled: ["name"]` section in a future
  minor bump.
- Changes to `parser_config.json` require a frontend restart. A file
  watcher is deferred; restarts are quick and infrequent.
- Algorithmic anchors (`_find_chrome_separator`, `parse_usage_output`,
  `extract_bash_output`, hook payload fields) are still in code. If
  they start drifting, a follow-up spec can pull specific strings into
  `parser_config.json` or add a parallel override for them.

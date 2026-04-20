# Design: centralize parser configuration

Status: approved, ready for implementation plan
Target release: ccmux-backend v1.2.1 (hotfix)
Date: 2026-04-19

## Problem

v1.2.0 introduced `src/ccmux/parser_overrides.py` to let ops patch
Claude-Code-coupled parser constants without a backend release. The
module does too much: it performs shadow detection against a **local
duplicate** of two built-in datasets (`_BUILTIN_UI_PATTERN_NAMES`,
`_BUILTIN_SIMPLE_SUMMARY_FIELDS`) that actually live in
`tmux_pane_parser` and `claude_transcript_parser`. Anyone adding a
new built-in UI pattern or summary field in those modules has to
remember to update the names clone in `parser_overrides` as well,
or shadow detection silently misses the new entry. That is exactly
the kind of hidden maintenance contract the backend otherwise
avoids.

The root cause is abstraction misplacement: `parser_overrides`
emits logs that require knowing both the user overrides and the
built-ins, yet it does not own the built-ins. Shadow detection
belongs at the point where both are in scope, which is the point
where they are **merged**.

## Goal

Move built-in parser data and merge composition into a single
module that also hosts the loader and shadow detection. After the
refactor:

- There is exactly one place in the codebase where a
  Claude-Code-coupled default is declared.
- Adding a new built-in requires editing one location; shadow
  detection picks it up automatically.
- Parser modules hold only parsing *logic* — no constants, no
  re-exports, no composition glue.
- `ccmux.api` is unaffected.

Breaking changes to backend-internal import paths are explicitly in
scope.

## Non-goals

- No change to what `parser_config.json` accepts. Schema stays at
  `$schema_version: 1`; all five sections, merge rules, and error
  behaviour are preserved.
- No change to `ccmux.api` public surface. External library users
  see nothing.
- No functional change to pane parsing, transcript rendering, or
  status detection. The refactor is invisible at the behavioural
  level.
- No file watcher or reload-on-change. Overrides still require a
  frontend restart (unchanged from v1.2.0).

## Breaking changes (internal only)

| Item removed | Replacement |
|---|---|
| Module `ccmux.parser_overrides` | Module `ccmux.parser_config` |
| Logger name `ccmux.parser_overrides` | Logger name `ccmux.parser_config` |
| `ccmux.tmux_pane_parser.UIPattern` re-export | `ccmux.parser_config.UIPattern` (single source) |
| `ccmux.tmux_pane_parser.UI_PATTERNS` module attr | `ccmux.parser_config.UI_PATTERNS` |
| `ccmux.tmux_pane_parser.STATUS_SPINNERS` module attr | `ccmux.parser_config.STATUS_SPINNERS` |
| `ccmux.tmux_pane_parser._SKIPPABLE_OVERLAY_PATTERNS` module attr | `ccmux.parser_config.SKIPPABLE_OVERLAY_PATTERNS` |
| `TranscriptParser._SIMPLE_SUMMARY_FIELDS` class attr | `ccmux.parser_config.SIMPLE_SUMMARY_FIELDS` |
| `TranscriptParser._BARE_SUMMARY_TOOLS` class attr | `ccmux.parser_config.BARE_SUMMARY_TOOLS` |

`ccmux.api` continues to export the same names (`TranscriptParser`,
`extract_interactive_content`, `parse_status_line`, `UIPattern` via
internal import, etc.) — the promises there are unchanged.

Semver note: the affected names are **not** in `ccmux.api`. The
`ccmux.api frozen at v1.0` promise is preserved. Per that promise,
internal reorganisation is a patch. v1.2.1 (hotfix) is the correct
bump.

## Architecture

One module, `src/ccmux/parser_config.py`, owns:

1. The type definition (`UIPattern`).
2. The built-in defaults for all five sections.
3. The override loader (unchanged from v1.2.0 modulo renames).
4. Merge composition into final module-level names.
5. Shadow detection, called at the merge site (no duplicated
   names, no maintenance contract).

```python
# src/ccmux/parser_config.py (outline)

@dataclass(frozen=True)
class UIPattern: ...
@dataclass(frozen=True)
class ParserOverrides: ...

# Built-in defaults — the *only* place these live.
_BUILTIN_UI_PATTERNS: list[UIPattern] = [...]
_BUILTIN_STATUS_SPINNERS: frozenset[str] = frozenset({...})
_BUILTIN_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (...)
_BUILTIN_SIMPLE_SUMMARY_FIELDS: dict[str, str] = {...}
_BUILTIN_BARE_SUMMARY_TOOLS: frozenset[str] = frozenset({...})

# Loader + section parsers (same behaviour as v1.2.0).
_CONFIG_FILENAME = "parser_config.json"
_SUPPORTED_SCHEMA_VERSION = 1
def _config_path() -> Path: ...
def _parse_ui_patterns(raw) -> tuple[UIPattern, ...]: ...
def _parse_regex_list(raw) -> tuple[re.Pattern[str], ...]: ...
def _parse_chars(raw) -> frozenset[str]: ...
def _parse_str_dict(raw) -> dict[str, str]: ...
def _parse_str_set(raw) -> frozenset[str]: ...
def load() -> ParserOverrides: ...

# Shadow helpers — pure functions, no built-in knowledge baked in.
def _log_ui_pattern_shadows(
    user: Iterable[UIPattern], builtin: Iterable[UIPattern]
) -> None:
    builtin_names = {p.name for p in builtin}
    for p in user:
        if p.name in builtin_names:
            logger.info("shadowing built-in ui_pattern '%s'", p.name)

def _log_summary_field_shadows(
    user: Mapping[str, str], builtin: Mapping[str, str]
) -> None:
    for key, value in user.items():
        if key in builtin:
            logger.info(
                "shadowing built-in simple_summary_field '%s' (%s -> %s)",
                key,
                builtin[key],
                value,
            )

# One-time composition at module import. This is the entire "plumbing".
_OVERRIDES: ParserOverrides = load()

UI_PATTERNS: list[UIPattern] = list(_OVERRIDES.ui_patterns) + _BUILTIN_UI_PATTERNS
STATUS_SPINNERS: frozenset[str] = (
    _BUILTIN_STATUS_SPINNERS | _OVERRIDES.status_spinners
)
SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    _OVERRIDES.skippable_overlays + _BUILTIN_SKIPPABLE_OVERLAY_PATTERNS
)
SIMPLE_SUMMARY_FIELDS: dict[str, str] = {
    **_BUILTIN_SIMPLE_SUMMARY_FIELDS,
    **_OVERRIDES.simple_summary_fields,
}
BARE_SUMMARY_TOOLS: frozenset[str] = (
    _BUILTIN_BARE_SUMMARY_TOOLS | _OVERRIDES.bare_summary_tools
)

_log_ui_pattern_shadows(_OVERRIDES.ui_patterns, _BUILTIN_UI_PATTERNS)
_log_summary_field_shadows(
    _OVERRIDES.simple_summary_fields, _BUILTIN_SIMPLE_SUMMARY_FIELDS
)
```

Parser modules become pure consumers. They import `parser_config`
as a namespace and read the constants via qualified access so that
reloading `parser_config` (tests use `importlib.reload`) naturally
propagates to the next function call:

```python
# src/ccmux/tmux_pane_parser.py (relevant excerpts)
from . import parser_config as _pc

def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    ...
    for ui_pattern in _pc.UI_PATTERNS:
        ...

def parse_status_line(pane_text: str) -> str | None:
    ...
    for i in range(...):
        ...
        if any(p.search(line) for p in _pc.SKIPPABLE_OVERLAY_PATTERNS):
            continue
        if stripped[0] in _pc.STATUS_SPINNERS:
            ...
```

```python
# src/ccmux/claude_transcript_parser.py (relevant excerpts)
from . import parser_config as _pc

class TranscriptParser:
    @classmethod
    def format_tool_use_summary(cls, name: str, input_data: dict | Any) -> str:
        ...
        if name in _pc.SIMPLE_SUMMARY_FIELDS:
            summary = input_data.get(_pc.SIMPLE_SUMMARY_FIELDS[name], "")
            ...
        elif name in _pc.BARE_SUMMARY_TOOLS:
            ...
```

There is no `UIPattern` re-export on `tmux_pane_parser`, no
`_BUILTIN_*` class attrs on `TranscriptParser`, no module-level
`UI_PATTERNS` / `STATUS_SPINNERS` / `_SKIPPABLE_OVERLAY_PATTERNS`
on `tmux_pane_parser`. Backend-internal consumers that want these
names import them from `parser_config`.

### Import graph

```
parser_config.py        (stdlib + ccmux.util.ccmux_dir only)
        ↑ imports
tmux_pane_parser.py
claude_transcript_parser.py
```

Unchanged at a topological level from v1.2.0. The difference is
that `parser_config` is now self-contained — it holds the full
data needed for shadow detection — so the reverse edge
(`parser_config` needing parser modules) is eliminated as a latent
maintenance pressure.

## Behavioural contract

Every user-facing behaviour is preserved from v1.2.0:

| Event | Behaviour | Log |
|---|---|---|
| No `parser_config.json` | UI_PATTERNS etc. = built-ins | none |
| File exists, valid | UI_PATTERNS = user + built-in; others merged per spec | INFO summary with counts |
| File exists, malformed / unversioned / unreadable | UI_PATTERNS etc. = built-ins (empty `_OVERRIDES`) | WARNING |
| User entry malformed (bad regex, missing field) | that entry skipped | WARNING with index |
| User `ui_patterns[i].name` matches built-in | user prepended, matches first | INFO shadowing |
| User `simple_summary_fields[k]` exists in built-in | user value wins per-key | INFO shadowing (with old → new) |
| User entry that is new | added to merged collection | no shadow log |

Log ordering differs in one observable way: the shadow INFOs now
appear **after** the summary INFO (both still during
`parser_config` import). This was already the ordering in v1.2.0
so ops see the same sequence.

## Testing

File move: `tests/test_parser_overrides.py` →
`tests/test_parser_config.py`. All existing tests carry over; some
are reframed.

### Retained (rename-only) tests

The loader contract is unchanged. The following v1.2.0 tests move
verbatim aside from `po` → `pc` alias and logger name:

- `test_overrides_singleton_is_parser_overrides_instance`
  (keep asserting `isinstance(pc._OVERRIDES, ParserOverrides)`
  against the now-private singleton)
- `test_ui_pattern_is_defined_in_parser_overrides` (rename to
  `test_ui_pattern_is_defined_in_parser_config`; assert
  `UIPattern.__module__ == "ccmux.parser_config"`)
- `test_load_returns_empty_when_file_missing`
- `test_load_parses_all_sections`
- `test_invalid_regex_in_ui_pattern_skips_entry`
- `test_missing_required_field_skipped`
- `test_non_single_char_spinner_rejected`
- `test_wrong_section_type_scoped_to_that_section`
- `test_malformed_json_falls_back_with_warning`
- `test_unknown_schema_version_falls_back_with_warning`
- `test_permission_error_falls_back_with_warning`
- `test_successful_load_emits_summary_info`
- `test_missing_file_emits_no_summary`

All of these exercise `pc.load()` or the underlying parsers. They
do not need to be rewritten beyond renames.

### Reframed shadow tests

The v1.2.0 shadow tests called `po.load()` and asserted the
INFO log appeared. After the refactor, shadow detection runs at
module import, not inside `load()`. The tests split:

**Unit tests on the helpers:**

- `test_log_ui_pattern_shadows_emits_info_for_name_collision`
- `test_log_ui_pattern_shadows_silent_when_no_collision`
- `test_log_summary_field_shadows_includes_old_and_new_values`
- `test_log_summary_field_shadows_silent_when_no_collision`

Each passes synthetic `user` and `builtin` iterables to the
helper and asserts log output.

**End-to-end tests on import:**

- `test_import_emits_shadow_ui_pattern_for_builtin_name`
- `test_import_emits_shadow_summary_field_for_builtin_key`
- `test_import_emits_no_shadow_when_names_are_fresh`

Each writes `parser_config.json` under a `CCMUX_DIR` tmp path,
reloads `ccmux.parser_config`, and inspects caplog.

### Integration tests in parser module test files

`test_tmux_pane_parser.py` and `test_claude_transcript_parser.py`
each keep their one v1.2.0 integration test, adjusted for the new
import surface:

```python
# test_tmux_pane_parser.py
def test_user_ui_pattern_is_prepended_and_matches_first(monkeypatch, tmp_path):
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    (tmp_path / "parser_config.json").write_text(json.dumps({...}))

    from ccmux import parser_config
    importlib.reload(parser_config)

    assert parser_config.UI_PATTERNS[0].name == "ExitPlanMode"
    ...
```

```python
# test_claude_transcript_parser.py
def test_user_simple_summary_field_overrides_builtin(monkeypatch, tmp_path):
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    (tmp_path / "parser_config.json").write_text(json.dumps({...}))

    from ccmux import parser_config
    importlib.reload(parser_config)

    assert parser_config.SIMPLE_SUMMARY_FIELDS["Read"] == "new_field"
    assert "BrandNewTool" in parser_config.BARE_SUMMARY_TOOLS
```

Note the assertion targets are now `parser_config.*`, not
`TranscriptParser._*` or `tmux_pane_parser.UI_PATTERNS`.

### Test file not modified

All other parser-module tests exercise pure parsing functions and
are unaffected by the refactor. They should stay green with zero
edits.

## Release plan

Hotfix flow per `managing-git-branches`:

1. `hotfix/v1.2.1` from `main`
2. Execute the refactor + test updates
3. Bump `pyproject.toml` version 1.2.0 → 1.2.1
4. `uv sync --all-extras` to refresh `uv.lock`
5. Update `CHANGELOG.md` with a `[1.2.1]` section
6. Update `docs/claude-code-compat.md` pointers that reference
   `parser_overrides` to `parser_config`
7. Merge `hotfix/v1.2.1` into `main` with `--no-ff`
8. Tag `v1.2.1`
9. Back-merge `hotfix/v1.2.1` into `dev` with `--no-ff`
10. Push `main dev --tags`

### CHANGELOG draft

```markdown
## 1.2.1 — 2026-04-19

### Changed (internal only — no ccmux.api impact)

- Renamed `ccmux.parser_overrides` → `ccmux.parser_config`. Logger
  name follows (`ccmux.parser_overrides` → `ccmux.parser_config`).
- `parser_config` is now the single source of truth for
  Claude-Code-coupled parser constants. Built-in defaults, user
  override loading, merge composition, and shadow detection all
  live here. Parser modules
  (`tmux_pane_parser`, `claude_transcript_parser`) are pure
  consumers of `parser_config.UI_PATTERNS`,
  `parser_config.STATUS_SPINNERS`, etc.
- Removed `UIPattern` re-export from `tmux_pane_parser`. Import
  from `ccmux.parser_config` instead.
- Removed `_SIMPLE_SUMMARY_FIELDS` / `_BARE_SUMMARY_TOOLS` class
  attributes from `TranscriptParser`. Use
  `ccmux.parser_config.SIMPLE_SUMMARY_FIELDS` /
  `BARE_SUMMARY_TOOLS` directly.

### Fixed

- Shadow detection no longer relies on a local duplicate of built-in
  names inside the override module. Adding a new built-in UI pattern
  or summary field automatically participates in shadow detection
  without a second manual edit.

### Not affected

- `ccmux.api` surface is unchanged.
- `$CCMUX_DIR/parser_config.json` schema is unchanged.
- All user-observable behaviour (merge semantics, error handling,
  log output) is preserved.
```

### Doc updates

- `docs/claude-code-compat.md` references to `parser_overrides` →
  `parser_config`. No new sections.
- `docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md`
  stays as-is (historical record of v1.2.0's design). Add a
  one-line "superseded in part by v1.2.1 spec" note at the top.
- `README.md` needs no changes — it already refers to the file
  name (`parser_config.json`) and to the compat doc.

### Post-release validation

Same smoke test as v1.2.0, re-confirming that the observable
behaviour is identical:

1. Write a throwaway override that shadows `ExitPlanMode`.
2. Restart the bot.
3. Confirm two INFO lines in `ccmux.log`: the summary and the
   shadow message. Both now tagged with logger name
   `ccmux.parser_config`.
4. Remove the override, restart, confirm silence.
5. Grep `hook.log` for new tracebacks — expect zero.

## Limitations deferred to future work

- A file watcher for hot-reload of `parser_config.json` is still
  out of scope.
- Algorithmic Claude Code anchors (`_find_chrome_separator`,
  `parse_usage_output`, `extract_bash_output`, hook payload
  fields) remain hard-coded. Extending `parser_config.json` to
  cover these would be a minor bump (v1.3.0) with a new section
  schema.

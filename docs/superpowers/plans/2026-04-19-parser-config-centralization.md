# Parser Config Centralization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ccmux-backend v1.2.1 so `ccmux.parser_config` owns every Claude-Code-coupled parser default, override-loading, merge composition, and shadow detection in one module; parser modules become pure consumers of qualified `parser_config.UI_PATTERNS`, `STATUS_SPINNERS`, etc.

**Architecture:** The new `parser_config` module is populated by promoting built-in datasets from `tmux_pane_parser` and `claude_transcript_parser` up into it, alongside the loader previously in `parser_overrides`. Shadow detection runs at the merge site, where both built-ins and user overrides are natively in scope — no duplicated name lists. Parser modules use `from . import parser_config as _pc` and reference the composed constants via qualified attribute access so `importlib.reload(parser_config)` in tests propagates without extra plumbing.

**Tech Stack:** Python 3.12, dataclasses, `re`, stdlib `json`, pytest (with caplog, monkeypatch, tmp_path), ruff, pyright, uv.

**Spec:** [`docs/superpowers/specs/2026-04-19-parser-config-centralization-design.md`](../specs/2026-04-19-parser-config-centralization-design.md)

---

## File Structure

Files created:
- `src/ccmux/parser_config.py` — the new single-source module: `UIPattern` dataclass, `ParserOverrides` dataclass, five `_BUILTIN_*` datasets, loader + parsers, two `_log_*_shadows` helpers, module-level composition, `_OVERRIDES` private singleton.
- `tests/test_parser_config.py` — new test module; covers everything `test_parser_overrides.py` covered plus the reframed shadow tests.

Files deleted:
- `src/ccmux/parser_overrides.py`
- `tests/test_parser_overrides.py`

Files modified:
- `src/ccmux/tmux_pane_parser.py` — drop `UIPattern` re-export; drop `_BUILTIN_UI_PATTERNS`, `_BUILTIN_STATUS_SPINNERS`, `_BUILTIN_SKIPPABLE_OVERLAY_PATTERNS`; drop `UI_PATTERNS`, `STATUS_SPINNERS`, `_SKIPPABLE_OVERLAY_PATTERNS` module attrs; change function bodies to reference `_pc.UI_PATTERNS` etc.
- `src/ccmux/claude_transcript_parser.py` — drop `TranscriptParser._BUILTIN_SIMPLE_SUMMARY_FIELDS`, `_SIMPLE_SUMMARY_FIELDS`, `_BUILTIN_BARE_SUMMARY_TOOLS`, `_BARE_SUMMARY_TOOLS`; method bodies read `_pc.SIMPLE_SUMMARY_FIELDS` / `_pc.BARE_SUMMARY_TOOLS`.
- `tests/test_tmux_pane_parser.py` — update the integration test to assert against `parser_config.UI_PATTERNS`.
- `tests/test_claude_transcript_parser.py` — update the integration test to assert against `parser_config.SIMPLE_SUMMARY_FIELDS` / `BARE_SUMMARY_TOOLS`.
- `pyproject.toml` — bump `1.2.0` → `1.2.1`.
- `uv.lock` — regenerated.
- `CHANGELOG.md` — append `## 1.2.1 — 2026-04-19` section.
- `docs/claude-code-compat.md` — `parser_overrides` → `parser_config` references.
- `docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md` — one-line "superseded in part by v1.2.1" note at top.

`README.md` is not modified — it already refers to the config filename (`parser_config.json`) and to the compat guide, neither of which changes.

`ccmux.api` is unchanged at both source and behaviour level.

---

## Task 1: Create hotfix branch and confirm baseline

**Files:** none modified directly; verification only.

- [ ] **Step 1: Confirm clean starting state**

Run:
```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-backend
git status --short
git branch --show-current
```

Expected: empty status output; branch is `dev`. If the working tree is dirty, STOP and resolve.

- [ ] **Step 2: Switch to main and verify it's at v1.2.0**

Run:
```bash
git checkout main
git describe --tags --exact-match HEAD
```

Expected: `v1.2.0`. If this differs, STOP — the hotfix flow branches from the released tip and not from a later state.

- [ ] **Step 3: Create hotfix branch**

Run:
```bash
git checkout -b hotfix/v1.2.1
```

Expected: `Switched to a new branch 'hotfix/v1.2.1'`.

- [ ] **Step 4: Baseline green gate**

Run:
```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pyright src/ && uv run pytest
```

Expected: all green, `203 passed`. This is the reference we defend through the refactor.

---

## Task 2: Create parser_config.py and test_parser_config.py alongside existing modules

This task creates the new module and its tests **without disturbing parser_overrides.py or the parser modules yet**. After this task, `parser_config` is fully implemented and tested, but nothing imports it — so `OVERRIDES` in the old `parser_overrides` module still drives runtime behaviour. Task 3 flips the switch.

**Files:**
- Create: `src/ccmux/parser_config.py`
- Create: `tests/test_parser_config.py`

- [ ] **Step 1: Create the new parser_config module**

Write the following to `src/ccmux/parser_config.py`:

```python
"""Single source of truth for Claude-Code-coupled parser constants.

Owns:
- The `UIPattern` type.
- Built-in defaults for all five Claude-Code-coupled constants
  (`UI_PATTERNS`, `STATUS_SPINNERS`, `SKIPPABLE_OVERLAY_PATTERNS`,
  `SIMPLE_SUMMARY_FIELDS`, `BARE_SUMMARY_TOOLS`).
- The JSON override loader that reads `$CCMUX_DIR/parser_config.json`.
- Merge composition producing the final runtime values.
- Shadow detection emitted at the merge site.

Parser modules (`tmux_pane_parser`, `claude_transcript_parser`)
import the composed constants from here as their only source.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .util import ccmux_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans patterns top-down; the first matching top anchor
    starts a region that closes at the first matching bottom anchor.
    Both boundary lines are included in the extracted content.
    """

    name: str
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2


@dataclass(frozen=True)
class ParserOverrides:
    """User-supplied overrides produced by `load()`."""

    ui_patterns: tuple[UIPattern, ...] = ()
    skippable_overlays: tuple[re.Pattern[str], ...] = ()
    status_spinners: frozenset[str] = frozenset()
    simple_summary_fields: dict[str, str] = field(default_factory=dict)
    bare_summary_tools: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Built-in defaults — the ONLY place these live in the codebase.
# ---------------------------------------------------------------------------


_BUILTIN_UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"Would you like to proceed"),
            re.compile(r"Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"ctrl-g to edit in"),
            re.compile(r"Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(
            re.compile(r"[☐☒✔]"),
        ),
        bottom=(
            re.compile(r"Enter to select"),
        ),
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"Do you want to proceed"),
            re.compile(r"Do you want to make this edit"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
        ),
    ),
    UIPattern(
        name="BashApproval",
        top=(
            re.compile(r"Bash command"),
            re.compile(r"This command requires approval"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
        ),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(
            re.compile(r"Restore the code"),
        ),
        bottom=(
            re.compile(r"Enter to continue"),
        ),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"Status\s+Config\s+Usage\s+Stats"),
            re.compile(r"Select model"),
        ),
        bottom=(
            re.compile(r"Esc to (cancel|exit|close)"),
        ),
    ),
]

_BUILTIN_STATUS_SPINNERS: frozenset[str] = frozenset({"·", "✻", "✽", "✶", "✳", "✢"})

_BUILTIN_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*●\s*How is Claude doing this session\?"),
    re.compile(r"^\s*1:\s*Bad\b"),
)

_BUILTIN_SIMPLE_SUMMARY_FIELDS: dict[str, str] = {
    "Read": "file_path",
    "Write": "file_path",
    "Bash": "command",
    "Grep": "pattern",
    "Task": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "Skill": "skill",
}

_BUILTIN_BARE_SUMMARY_TOOLS: frozenset[str] = frozenset({"TodoRead", "ExitPlanMode"})


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CONFIG_FILENAME = "parser_config.json"
_SUPPORTED_SCHEMA_VERSION = 1


def _config_path() -> Path:
    return ccmux_dir() / _CONFIG_FILENAME


def _parse_ui_patterns(raw: object) -> tuple[UIPattern, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[UIPattern] = []
    for index, entry in enumerate(raw):
        try:
            if not isinstance(entry, dict):
                raise TypeError("entry is not a JSON object")
            name = entry.get("name")
            top_src = entry.get("top")
            bottom_src = entry.get("bottom")
            if not isinstance(name, str):
                raise KeyError("name")
            if not isinstance(top_src, list):
                raise KeyError("top")
            if not isinstance(bottom_src, list):
                raise KeyError("bottom")
            top = tuple(re.compile(p) for p in top_src if isinstance(p, str))
            bottom = tuple(re.compile(p) for p in bottom_src if isinstance(p, str))
            min_gap_raw = entry.get("min_gap", 2)
            min_gap = min_gap_raw if isinstance(min_gap_raw, int) else 2
            out.append(UIPattern(name=name, top=top, bottom=bottom, min_gap=min_gap))
        except (KeyError, TypeError, re.error) as e:
            logger.warning("ui_patterns[%d] skipped: %s", index, e)
    return tuple(out)


def _parse_regex_list(raw: object) -> tuple[re.Pattern[str], ...]:
    if not isinstance(raw, list):
        return ()
    compiled: list[re.Pattern[str]] = []
    for src in raw:
        if isinstance(src, str):
            compiled.append(re.compile(src))
    return tuple(compiled)


def _parse_chars(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(s for s in raw if isinstance(s, str) and len(s) == 1)


def _parse_str_dict(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _parse_str_set(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(s for s in raw if isinstance(s, str))


def load() -> ParserOverrides:
    """Load overrides from `$CCMUX_DIR/parser_config.json`.

    Returns an empty `ParserOverrides` on any failure path (missing file,
    unreadable file, malformed JSON, unknown schema version). Bot startup
    is never blocked by a bad override file.
    """
    path = _config_path()
    if not path.exists():
        return ParserOverrides()
    try:
        text = path.read_text()
    except OSError as e:
        logger.warning("could not read parser_config.json: %s", e)
        return ParserOverrides()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("invalid JSON in parser_config.json: %s", e)
        return ParserOverrides()
    if not isinstance(raw, dict):
        logger.warning("parser_config.json top-level must be an object")
        return ParserOverrides()
    version = raw.get("$schema_version")
    if version != _SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "parser_config.json $schema_version=%r unsupported "
            "(expected %d); ignoring file",
            version,
            _SUPPORTED_SCHEMA_VERSION,
        )
        return ParserOverrides()
    overrides = ParserOverrides(
        ui_patterns=_parse_ui_patterns(raw.get("ui_patterns")),
        skippable_overlays=_parse_regex_list(raw.get("skippable_overlays")),
        status_spinners=_parse_chars(raw.get("status_spinners")),
        simple_summary_fields=_parse_str_dict(raw.get("simple_summary_fields")),
        bare_summary_tools=_parse_str_set(raw.get("bare_summary_tools")),
    )
    logger.info(
        "loaded parser_config.json: "
        "ui_patterns=%d, skippable_overlays=%d, status_spinners=%d, "
        "simple_summary_fields=%d, bare_summary_tools=%d",
        len(overrides.ui_patterns),
        len(overrides.skippable_overlays),
        len(overrides.status_spinners),
        len(overrides.simple_summary_fields),
        len(overrides.bare_summary_tools),
    )
    return overrides


# ---------------------------------------------------------------------------
# Shadow detection — pure helpers, no hardcoded name lists.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Module-level composition — runs once at import.
# ---------------------------------------------------------------------------

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

**IMPORTANT**: the `_BUILTIN_UI_PATTERNS` content above is a schematic — verify by copying the **actual** built-in `UIPattern` instances from the current `src/ccmux/tmux_pane_parser.py` (the `_BUILTIN_UI_PATTERNS` list, introduced in v1.2.0). Use exactly the same regex sources so behaviour is preserved bit-for-bit. The six pattern names must remain: `ExitPlanMode`, `AskUserQuestion`, `PermissionPrompt`, `BashApproval`, `RestoreCheckpoint`, `Settings`.

Likewise copy `_BUILTIN_STATUS_SPINNERS` and `_BUILTIN_SKIPPABLE_OVERLAY_PATTERNS` literal values from the current `tmux_pane_parser.py`, and `_BUILTIN_SIMPLE_SUMMARY_FIELDS` + `_BUILTIN_BARE_SUMMARY_TOOLS` from `claude_transcript_parser.py`.

- [ ] **Step 2: Sanity-check the new module imports in isolation**

Run:
```bash
uv run python -c "from ccmux import parser_config as pc; print(len(pc.UI_PATTERNS), len(pc.STATUS_SPINNERS), len(pc.SIMPLE_SUMMARY_FIELDS))"
```

Expected output: `6 6 8` (six ui_patterns, six spinners, eight summary fields) — no crash, no warning. If warnings appear, something in `_BUILTIN_*` didn't copy correctly; fix before moving on.

- [ ] **Step 3: Copy the v1.2.0 loader test file and adapt**

Create `tests/test_parser_config.py` starting from the current `tests/test_parser_overrides.py` content, with these find-and-replaces:

- `from ccmux import parser_overrides as po` → `from ccmux import parser_config as pc`
- `parser_overrides` anywhere in a test body → `parser_config`
- `po.` → `pc.`
- `po.load()` → `pc.load()`
- `po.ParserOverrides` → `pc.ParserOverrides`
- `caplog.set_level(logging.WARNING, logger="ccmux.parser_overrides")` → `caplog.set_level(logging.WARNING, logger="ccmux.parser_config")`
- Same for `logging.INFO` level
- Rename the singleton test: keep the name `test_overrides_singleton_is_parser_overrides_instance` (the class is still `ParserOverrides`, only the variable is now private). Update its body to:
  ```python
  def test_private_overrides_singleton_is_parser_overrides_instance() -> None:
      assert isinstance(pc._OVERRIDES, pc.ParserOverrides)
  ```
  Rename the function to `test_private_overrides_singleton_is_parser_overrides_instance`.
- Rename `test_ui_pattern_is_defined_in_parser_overrides` → `test_ui_pattern_is_defined_in_parser_config` and update its assertion to `UIPattern.__module__ == "ccmux.parser_config"`.
- Update `assert result == po.ParserOverrides()` style comparisons to `assert result == pc.ParserOverrides()` — these still work because `ParserOverrides()` compares equal via frozen-dataclass semantics.

- [ ] **Step 4: Delete the obsolete shadow-log tests from the new test file**

In `tests/test_parser_config.py`, DELETE these three tests (they were built on the v1.2.0 shadow-via-`load()` design):

- `test_shadow_ui_pattern_logs_info`
- `test_shadow_simple_summary_field_logs_info_with_values`
- `test_no_shadow_no_info_log`

Also DELETE the local sets that were only used by those tests:

```python
_BUILTIN_UI_PATTERN_NAMES = {...}
_BUILTIN_SIMPLE_SUMMARY_KEYS = {...}
```

- [ ] **Step 5: Add unit tests for the shadow helpers**

Append to `tests/test_parser_config.py`:

```python
def test_log_ui_pattern_shadows_emits_info_for_name_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    import re as _re

    empty = (_re.compile(r""),)
    user = [pc.UIPattern(name="ExitPlanMode", top=empty, bottom=empty)]
    builtin = [pc.UIPattern(name="ExitPlanMode", top=empty, bottom=empty)]
    pc._log_ui_pattern_shadows(user, builtin)

    assert any(
        "shadowing built-in ui_pattern 'ExitPlanMode'" in r.message
        for r in caplog.records
    )


def test_log_ui_pattern_shadows_silent_when_no_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    import re as _re

    empty = (_re.compile(r""),)
    user = [pc.UIPattern(name="BrandNewUI", top=empty, bottom=empty)]
    builtin = [pc.UIPattern(name="ExitPlanMode", top=empty, bottom=empty)]
    pc._log_ui_pattern_shadows(user, builtin)

    assert not any("shadowing" in r.message for r in caplog.records)


def test_log_summary_field_shadows_includes_old_and_new_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    pc._log_summary_field_shadows(
        {"Read": "new_field"}, {"Read": "file_path"}
    )
    assert any(
        "Read" in r.message and "file_path" in r.message and "new_field" in r.message
        for r in caplog.records
    )


def test_log_summary_field_shadows_silent_when_no_collision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    pc._log_summary_field_shadows({"BrandNewTool": "arg"}, {"Read": "file_path"})
    assert not any("shadowing" in r.message for r in caplog.records)
```

- [ ] **Step 6: Add end-to-end shadow tests that reload parser_config**

Append to `tests/test_parser_config.py`:

```python
def test_import_emits_shadow_ui_pattern_for_builtin_name(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib

    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "ExitPlanMode", "top": ["^x$"], "bottom": ["^y$"]}
            ],
        },
    )

    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    importlib.reload(pc)

    assert any(
        "shadowing built-in ui_pattern 'ExitPlanMode'" in r.message
        for r in caplog.records
    )


def test_import_emits_shadow_summary_field_for_builtin_key(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib

    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "simple_summary_fields": {"Read": "new_field"},
        },
    )

    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    importlib.reload(pc)

    assert any(
        "Read" in r.message
        and "file_path" in r.message
        and "new_field" in r.message
        for r in caplog.records
    )


def test_import_emits_no_shadow_when_names_are_fresh(
    isolated_ccmux_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib

    _write_config(
        isolated_ccmux_dir,
        {
            "$schema_version": 1,
            "ui_patterns": [
                {"name": "BrandNewUI", "top": ["^x$"], "bottom": ["^y$"]}
            ],
            "simple_summary_fields": {"BrandNewTool": "arg"},
        },
    )

    caplog.set_level(logging.INFO, logger="ccmux.parser_config")
    importlib.reload(pc)

    assert not any("shadowing" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 7: Run the new test file to verify it's green on its own**

Run:
```bash
uv run pytest tests/test_parser_config.py -v
```

Expected: all tests pass. The exact count depends on how many loader tests carried over; target ~20 tests. No failures.

- [ ] **Step 8: Run the full suite to confirm nothing else breaks**

Run:
```bash
uv run pytest
```

Expected: the pre-existing 203 tests still pass (they use `parser_overrides`, which is unchanged), plus the new `test_parser_config.py` tests. Note: during the full run, both modules execute their `load()` at import time because both appear in the import graph via their respective test files. This is transitional; Task 3 cleans it up.

- [ ] **Step 9: Ruff format the new files**

Run:
```bash
uv run ruff format src/ccmux/parser_config.py tests/test_parser_config.py
uv run ruff check src/ tests/
```

Expected: formatter applies idiomatic style; ruff check green.

- [ ] **Step 10: Commit**

```bash
git add src/ccmux/parser_config.py tests/test_parser_config.py
git commit -m "refactor(parser-config): introduce parser_config module alongside parser_overrides

Creates src/ccmux/parser_config.py as the future single source of
truth for Claude-Code-coupled parser constants. Built-in datasets
are promoted up from tmux_pane_parser and claude_transcript_parser;
merge composition and shadow detection live at the merge site so no
duplicated name list is needed.

parser_overrides.py and the parser modules are untouched in this
commit; Task 3 flips the import switch.

New tests in tests/test_parser_config.py cover the loader contract
(rename of the v1.2.0 test suite), plus unit tests on the shadow
helpers and end-to-end tests that reload parser_config to observe
shadow INFO logs."
```

No `Co-Authored-By`.

---

## Task 3: Switch consumers to parser_config and delete parser_overrides

**Files:**
- Modify: `src/ccmux/tmux_pane_parser.py`
- Modify: `src/ccmux/claude_transcript_parser.py`
- Modify: `tests/test_tmux_pane_parser.py`
- Modify: `tests/test_claude_transcript_parser.py`
- Delete: `src/ccmux/parser_overrides.py`
- Delete: `tests/test_parser_overrides.py`

- [ ] **Step 1: Refactor `src/ccmux/tmux_pane_parser.py`**

Edit the file so that:

1. The import block at the top REMOVES any line importing from `parser_overrides`. The current line is:
   ```python
   from .parser_overrides import (
       OVERRIDES,
       UIPattern,
   )  # re-exported for back-compat
   ```
   Replace with:
   ```python
   from . import parser_config as _pc
   ```

2. DELETE the following module-level blocks (they exist in the v1.2.0 file):
   - The `_BUILTIN_UI_PATTERNS: list[UIPattern] = [...]` literal (after the `# UI pattern definitions` comment).
   - The `UI_PATTERNS: list[UIPattern] = list(OVERRIDES.ui_patterns) + _BUILTIN_UI_PATTERNS` line.
   - The `_BUILTIN_STATUS_SPINNERS: frozenset[str] = frozenset(...)` literal.
   - The `STATUS_SPINNERS: frozenset[str] = _BUILTIN_STATUS_SPINNERS | OVERRIDES.status_spinners` line.
   - The `_BUILTIN_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (...)` literal.
   - The `_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (...)` line.

3. In every function body, update references:
   - `for pattern in UI_PATTERNS:` → `for pattern in _pc.UI_PATTERNS:`
   - `stripped[0] in STATUS_SPINNERS` → `stripped[0] in _pc.STATUS_SPINNERS`
   - `any(p.search(line) for p in _SKIPPABLE_OVERLAY_PATTERNS)` → `any(p.search(line) for p in _pc.SKIPPABLE_OVERLAY_PATTERNS)`

4. `UIPattern` is no longer used as a type annotation inside `tmux_pane_parser.py` except perhaps in function return types — check by searching. If present, either qualify as `_pc.UIPattern` or leave the annotation if the code still compiles without the import (most existing usages reference the class only inside `_BUILTIN_UI_PATTERNS`, which is now gone, so likely zero references remain).

5. Remove the `from dataclasses import dataclass` import ONLY IF `InteractiveUIContent` / `UsageInfo` no longer exist in this file. They DO exist, so keep the import.

- [ ] **Step 2: Refactor `src/ccmux/claude_transcript_parser.py`**

Edit the file so that:

1. Remove the v1.2.0 line:
   ```python
   from .parser_overrides import OVERRIDES
   ```
   Replace with:
   ```python
   from . import parser_config as _pc
   ```

2. Inside `class TranscriptParser:`, DELETE the following class attributes (added in v1.2.0):
   - `_BUILTIN_SIMPLE_SUMMARY_FIELDS: dict[str, str] = {...}`
   - `_SIMPLE_SUMMARY_FIELDS: dict[str, str] = {...}`
   - `_BUILTIN_BARE_SUMMARY_TOOLS: frozenset[str] = frozenset({...})`
   - `_BARE_SUMMARY_TOOLS: frozenset[str] = ...`

3. In `format_tool_use_summary` and any other method that referenced the above:
   - `cls._SIMPLE_SUMMARY_FIELDS` → `_pc.SIMPLE_SUMMARY_FIELDS`
   - `cls._BARE_SUMMARY_TOOLS` → `_pc.BARE_SUMMARY_TOOLS`
   - `name in cls._SIMPLE_SUMMARY_FIELDS` → `name in _pc.SIMPLE_SUMMARY_FIELDS`
   - `name in cls._BARE_SUMMARY_TOOLS` → `name in _pc.BARE_SUMMARY_TOOLS`

- [ ] **Step 3: Update `tests/test_tmux_pane_parser.py` integration test**

Find the v1.2.0 test `test_user_ui_pattern_is_prepended_and_matches_first`. Replace its body with:

```python
def test_user_ui_pattern_is_prepended_and_matches_first(
    monkeypatch, tmp_path
) -> None:
    import importlib
    import json

    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    (tmp_path / "parser_config.json").write_text(
        json.dumps(
            {
                "$schema_version": 1,
                "ui_patterns": [
                    {
                        "name": "ExitPlanMode",
                        "top": ["^CUSTOM TOP$"],
                        "bottom": ["^CUSTOM BOTTOM$"],
                    }
                ],
            }
        )
    )

    from ccmux import parser_config

    importlib.reload(parser_config)

    names = [p.name for p in parser_config.UI_PATTERNS]
    assert names[0] == "ExitPlanMode"
    assert names.count("ExitPlanMode") == 2  # user + built-in
    assert parser_config.UI_PATTERNS[0].top[0].pattern == "^CUSTOM TOP$"
```

Note: the imports `from ccmux import parser_overrides, tmux_pane_parser` and the `importlib.reload(tmux_pane_parser)` call from the v1.2.0 version are gone. `parser_config` is where the value lives now.

- [ ] **Step 4: Update `tests/test_claude_transcript_parser.py` integration test**

Find the v1.2.0 test `test_user_simple_summary_field_overrides_builtin`. Replace its body with:

```python
def test_user_simple_summary_field_overrides_builtin(monkeypatch, tmp_path) -> None:
    import importlib
    import json

    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
    (tmp_path / "parser_config.json").write_text(
        json.dumps(
            {
                "$schema_version": 1,
                "simple_summary_fields": {"Read": "new_field"},
                "bare_summary_tools": ["BrandNewTool"],
            }
        )
    )

    from ccmux import parser_config

    importlib.reload(parser_config)

    assert parser_config.SIMPLE_SUMMARY_FIELDS["Read"] == "new_field"
    assert parser_config.SIMPLE_SUMMARY_FIELDS["Bash"] == "command"  # built-in preserved
    assert "BrandNewTool" in parser_config.BARE_SUMMARY_TOOLS
    assert "TodoRead" in parser_config.BARE_SUMMARY_TOOLS  # built-in preserved
```

- [ ] **Step 5: Delete parser_overrides.py and its test file**

Run:
```bash
git rm src/ccmux/parser_overrides.py
git rm tests/test_parser_overrides.py
```

- [ ] **Step 6: Verify pyright finds no dangling references**

Run:
```bash
uv run pyright src/
```

Expected: 0 errors. If errors surface (e.g., a reference to `OVERRIDES` that was overlooked), fix them in the offending file and re-run.

- [ ] **Step 7: Run the full test suite**

Run:
```bash
uv run pytest
```

Expected: all tests pass. The test count is now slightly different:
- `test_parser_overrides.py` (16 tests) has been removed.
- `test_parser_config.py` (~20 tests) has been added.
- Two integration tests were modified in place, not added.
- Net: 207 passed, give or take.

If anything fails, stop and investigate. Common cause: a leftover reference to the old name; grep for `parser_overrides` under `src/` and `tests/` and fix.

- [ ] **Step 8: Ruff and formatter sweep**

Run:
```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run ruff format --check src/ tests/
```

Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(parser-config): switch consumers to parser_config; drop parser_overrides

tmux_pane_parser and claude_transcript_parser now read all composed
constants from ccmux.parser_config via qualified access
(from . import parser_config as _pc). Removed: UIPattern re-export,
_BUILTIN_* constants, module-level UI_PATTERNS / STATUS_SPINNERS /
_SKIPPABLE_OVERLAY_PATTERNS on tmux_pane_parser;
TranscriptParser._BUILTIN_SIMPLE_SUMMARY_FIELDS /
_SIMPLE_SUMMARY_FIELDS / _BUILTIN_BARE_SUMMARY_TOOLS /
_BARE_SUMMARY_TOOLS class attributes.

src/ccmux/parser_overrides.py and tests/test_parser_overrides.py
are deleted. Integration tests in test_tmux_pane_parser.py and
test_claude_transcript_parser.py now target parser_config.
ccmux.api surface unaffected."
```

No `Co-Authored-By`.

---

## Task 4: Green gate on the hotfix branch

**Files:** no edits expected; this task verifies.

- [ ] **Step 1: Ruff check**

```bash
uv run ruff check src/ tests/
```

Expected: `All checks passed!`.

- [ ] **Step 2: Ruff format check**

```bash
uv run ruff format --check src/ tests/
```

Expected: no files would be reformatted.

- [ ] **Step 3: Pyright**

```bash
uv run pyright src/
```

Expected: `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 4: Full pytest**

```bash
uv run pytest
```

Expected: all tests green. Exact count will be ~207 depending on how many loader tests were dropped or added.

- [ ] **Step 5: Visual review of the hotfix history**

```bash
git log --oneline main..hotfix/v1.2.1
```

Expected: two commits (`refactor(parser-config): introduce parser_config module alongside parser_overrides` and `refactor(parser-config): switch consumers to parser_config; drop parser_overrides`), in that order. No stray commits.

If any step fails, fix in a new commit on the hotfix branch (do NOT amend). Return to Step 1.

---

## Task 5: Release prep on the hotfix branch

**Files:**
- Modify: `pyproject.toml`
- Regenerated: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `docs/claude-code-compat.md`
- Modify: `docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md`

- [ ] **Step 1: Bump version in `pyproject.toml`**

Change the line `version = "1.2.0"` to `version = "1.2.1"`.

- [ ] **Step 2: Regenerate uv.lock**

```bash
uv sync --all-extras
```

Expected: `ccmux==1.2.1` installed. Confirm with `grep -A1 'name = "ccmux"' uv.lock | head -3`.

- [ ] **Step 3: Append CHANGELOG entry**

Edit `CHANGELOG.md`. Insert the following section IMMEDIATELY BEFORE the existing `## 1.2.0 — 2026-04-19` section:

```markdown
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
```

- [ ] **Step 4: Update compat doc references**

In `docs/claude-code-compat.md`, replace every occurrence of `parser_overrides` with `parser_config`. These are the references added in v1.2.0 (in the quick-fix paragraphs under each severity section, and in the `## Where to grep first` block). Run:

```bash
grep -n "parser_overrides" docs/claude-code-compat.md
```

Expected before edit: several hits. Edit each inline, then re-run the grep:

```bash
grep -n "parser_overrides" docs/claude-code-compat.md
```

Expected after edit: zero hits.

- [ ] **Step 5: Add superseded note to v1.2.0 spec**

Edit `docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md`. At the very top, after the `# Design: ...` heading, insert this line:

```markdown
> **Note:** The module named `parser_overrides` here was renamed to
> `parser_config` in v1.2.1; shadow detection and built-in datasets
> moved into the same module. See
> [2026-04-19-parser-config-centralization-design.md](2026-04-19-parser-config-centralization-design.md).
```

- [ ] **Step 6: Run the full green gate**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pyright src/ && uv run pytest
```

Expected: all green.

- [ ] **Step 7: Commit release prep**

```bash
git add pyproject.toml uv.lock CHANGELOG.md docs/claude-code-compat.md docs/superpowers/specs/2026-04-19-externalize-cc-constants-design.md
git commit -m "chore: bump version to 1.2.1 and update CHANGELOG"
```

No `Co-Authored-By`.

---

## Task 6: Hotfix merge, tag, back-merge, push

**Files:** none modified directly.

- [ ] **Step 1: Merge hotfix into main with --no-ff**

```bash
git checkout main
git merge hotfix/v1.2.1 --no-ff
```

Expected: merge commit `Merge branch 'hotfix/v1.2.1'` (git default message, no `-m`).

- [ ] **Step 2: Tag v1.2.1 on main's merge commit**

```bash
git tag v1.2.1 -m "v1.2.1: centralize parser_config; eliminate shadow-detection duplication

Internal refactor. ccmux.api unchanged. Renamed
ccmux.parser_overrides -> ccmux.parser_config; parser modules now
pure consumers of parser_config.UI_PATTERNS etc. See CHANGELOG.md."
```

- [ ] **Step 3: Back-merge hotfix into dev**

```bash
git checkout dev
git merge hotfix/v1.2.1 --no-ff
```

Expected: merge commit `Merge branch 'hotfix/v1.2.1' into dev`. No conflicts — dev had no independent commits since the hotfix branched.

- [ ] **Step 4: Delete hotfix branch locally**

```bash
git branch -d hotfix/v1.2.1
```

- [ ] **Step 5: Push main, dev, and the tag**

```bash
git push origin main dev --tags
```

Expected: three lines confirming updates. No force-push needed; both branches moved forward only.

- [ ] **Step 6: Verify CI**

```bash
until [ "$(/mnt/md0/home/wenruiwu/.local/bin/gh run list --repo wuwenrui555/ccmux-backend --limit 1 2>&1 | awk '{print $1}')" = "completed" ]; do sleep 5; done
/mnt/md0/home/wenruiwu/.local/bin/gh run list --repo wuwenrui555/ccmux-backend --limit 2
```

Expected: the latest run (for `Merge branch 'hotfix/v1.2.1'` on main) is `completed success`.

---

## Task 7: Live smoke test in `__ccmux__`

**Files:** temporary — `~/.ccmux/parser_config.json` and `~/.ccmux/ccmux.log` (read-only).

- [ ] **Step 1: Write a throwaway override that shadows ExitPlanMode**

Run:
```bash
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.ccmux/parser_config.json'
p.write_text(json.dumps({
    '\$schema_version': 1,
    'ui_patterns': [{
        'name': 'ExitPlanMode',
        'top': ['ZZZ-will-never-match-in-real-panes'],
        'bottom': ['ZZZ-also-no-match'],
    }],
}))
"
```

Verify: `cat ~/.ccmux/parser_config.json`.

- [ ] **Step 2: Restart ccmux-telegram**

```bash
tmux send-keys -t %6 C-c
sleep 3
tmux send-keys -t %6 "uv run ccmux-telegram" Enter
sleep 8
```

Verify the bot is running: `ps auxww | grep ccmux-telegram | grep -v grep`.

- [ ] **Step 3: Confirm INFO summary and shadow log with the new logger name**

```bash
grep -E "ccmux.parser_config.*(loaded parser_config|shadowing built-in)" ~/.ccmux/ccmux.log | tail -5
```

Expected two lines tagged with logger `ccmux.parser_config` (NOT `ccmux.parser_overrides`):

```
... ccmux.parser_config - INFO - loaded parser_config.json: ui_patterns=1, skippable_overlays=0, status_spinners=0, simple_summary_fields=0, bare_summary_tools=0
... ccmux.parser_config - INFO - shadowing built-in ui_pattern 'ExitPlanMode'
```

If the logger name still says `ccmux.parser_overrides`, something in Task 2 did not rename the logger. Investigate before continuing.

- [ ] **Step 4: Remove override, restart, confirm silence**

```bash
trash-put ~/.ccmux/parser_config.json
tmux send-keys -t %6 C-c
sleep 3
tmux send-keys -t %6 "uv run ccmux-telegram" Enter
sleep 8
grep -E "ccmux.parser_config.*(loaded parser_config|shadowing built-in)" ~/.ccmux/ccmux.log | tail -1
```

Expected: the last matching line is still the one from Step 3 — no new `loaded parser_config` or `shadowing` lines after restart with no override file.

- [ ] **Step 5: Regression check on v1.1.0 hook.log**

```bash
grep -c "Traceback" ~/.ccmux/hook.log
```

Expected: same count as before Task 1 began (likely 0). Any increase is a regression — investigate.

---

## Self-Review Checklist

Run this after completing the plan draft.

- [ ] **Spec coverage.** Every section of
      `docs/superpowers/specs/2026-04-19-parser-config-centralization-design.md`
      maps to a task:
  - Architecture (parser_config as single source, parser modules as
    consumers) → Tasks 2 & 3.
  - Breaking-changes table → Task 3 (removals) + Task 5 (CHANGELOG
    documenting them).
  - Behavioural contract (merge rules, error paths, log ordering) →
    covered by the loader tests carried over from v1.2.0 plus the
    new shadow tests, all in Task 2.
  - Testing plan (unit tests on helpers, e2e reload tests,
    integration tests) → Tasks 2 & 3.
  - Release plan (hotfix/v1.2.1 flow) → Tasks 1, 5, 6.
  - Post-release validation → Task 7.
- [ ] **Placeholder scan.** No "TBD", "TODO", "appropriate", or
      "similar to Task N" in any step. Code steps all show complete
      code.
- [ ] **Type consistency.** The module attribute names used
      throughout are consistent: `UI_PATTERNS`, `STATUS_SPINNERS`,
      `SKIPPABLE_OVERLAY_PATTERNS` (no underscore in the new module),
      `SIMPLE_SUMMARY_FIELDS`, `BARE_SUMMARY_TOOLS`. The private
      singleton is `_OVERRIDES`. Helpers are `_log_ui_pattern_shadows`
      and `_log_summary_field_shadows`. Parser-module alias is `_pc`.
      All tasks reference these names the same way.
- [ ] **One atomic commit per task.** Tasks 2, 3, 5, 6 each produce
      exactly one commit. Tasks 1, 4, 7 are verification-only and
      make no commits.

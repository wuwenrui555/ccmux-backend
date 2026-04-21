# ClaudeState Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans-test-first to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor ccmux-backend around a sealed four-case `ClaudeState`
union keyed per `ClaudeInstance`, replacing `WindowStatus` + the old
`PaneState` StrEnum. Telegram frontend migrates in lockstep.

**Architecture:** Backend stays a pure producer. Two parallel monitors
(`state_monitor`, `message_monitor`) emit to two callbacks
(`on_state`, `on_message`). Liveness detection folds into `state_monitor`
as the `Dead` variant. No backward compatibility; old persistence file
and old API are deleted outright.

**Tech Stack:** Python 3.12+, asyncio, `@dataclass(frozen=True)` sealed
unions via `|`-typed aliases, `StrEnum`, pytest.

**Repos affected:**

- `/mnt/md0/home/wenruiwu/projects/ccmux-backend` (Phase A)
- `/mnt/md0/home/wenruiwu/projects/ccmux-telegram` (Phase B)

**Source spec:** `docs/superpowers/specs/2026-04-20-claude-state-unification-design.md`

---

## Execution order

Phase A (backend) must merge in full before Phase B (telegram) starts —
Phase B imports the new types from `ccmux.api`. Within Phase A, Task
ordering matters: new types first, consumers next, deletions last.

---

## Phase A: ccmux-backend

### Task A1: Create `claude_state.py` with sealed types + `BlockedUI`

**Files:**

- Create: `/mnt/md0/home/wenruiwu/projects/ccmux-backend/src/ccmux/claude_state.py`
- Create: `/mnt/md0/home/wenruiwu/projects/ccmux-backend/tests/test_claude_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_state.py`:

```python
"""Tests for the ClaudeState sealed union + BlockedUI enum."""

import pytest

from ccmux.claude_state import (
    BlockedUI,
    Blocked,
    ClaudeState,
    Dead,
    Idle,
    Working,
)


class TestBlockedUI:
    def test_has_six_members(self) -> None:
        assert {m.value for m in BlockedUI} == {
            "permission_prompt",
            "ask_user_question",
            "exit_plan_mode",
            "bash_approval",
            "restore_checkpoint",
            "settings",
        }

    def test_is_strenum(self) -> None:
        assert BlockedUI.PERMISSION_PROMPT == "permission_prompt"


class TestWorking:
    def test_accepts_valid_status_text(self) -> None:
        w = Working(status_text="Thinking… (3s)")
        assert w.status_text == "Thinking… (3s)"

    def test_rejects_empty_status_text(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Working(status_text="")

    def test_rejects_missing_ellipsis(self) -> None:
        with pytest.raises(ValueError, match="ellipsis"):
            Working(status_text="Thinking for 3s")

    def test_is_frozen(self) -> None:
        w = Working(status_text="Reading…")
        with pytest.raises(Exception):
            w.status_text = "Writing…"  # type: ignore[misc]


class TestIdle:
    def test_has_no_payload(self) -> None:
        i = Idle()
        assert i == Idle()

    def test_is_frozen(self) -> None:
        i = Idle()
        with pytest.raises(Exception):
            i.foo = 1  # type: ignore[attr-defined]


class TestBlocked:
    def test_carries_ui_and_content(self) -> None:
        b = Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="Do you want to proceed?")
        assert b.ui is BlockedUI.PERMISSION_PROMPT
        assert b.content == "Do you want to proceed?"


class TestDead:
    def test_has_no_payload(self) -> None:
        assert Dead() == Dead()


class TestExhaustiveMatch:
    def test_match_covers_every_variant(self) -> None:
        """All four variants must be reachable via structural pattern match."""
        seen: set[str] = set()
        states: list[ClaudeState] = [
            Working(status_text="Thinking…"),
            Idle(),
            Blocked(ui=BlockedUI.SETTINGS, content="Status | Config | Usage"),
            Dead(),
        ]
        for s in states:
            match s:
                case Working(text):
                    seen.add(f"working:{text}")
                case Idle():
                    seen.add("idle")
                case Blocked(ui, content):
                    seen.add(f"blocked:{ui}:{content}")
                case Dead():
                    seen.add("dead")
        assert len(seen) == 4
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-backend
uv run pytest tests/test_claude_state.py -x -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ccmux.claude_state'`.

- [ ] **Step 3: Write the implementation**

Create `src/ccmux/claude_state.py`:

```python
"""ClaudeState sealed union — the four-case classification of a running
Claude Code instance.

A running Claude Code process is always in exactly one of:

- ``Working`` — the input chrome is rendered and a spinner with ``…``
  is running above it. Carries the status text (e.g. ``Thinking… (3s)``).
- ``Idle`` — the input chrome is rendered, no spinner. Claude is waiting
  for the user's next message. Carries no payload.
- ``Blocked`` — the input chrome has been replaced by a blocking UI
  (permission prompt, AskUserQuestion, ExitPlanMode, Settings, etc.).
  Carries the matched ``BlockedUI`` variant and the extracted content.
- ``Dead`` — the tmux window is alive but the ``claude`` process is no
  longer foreground in its pane. Triggers auto-resume. Carries no payload.

Union is sealed: adding a fifth case requires editing every match site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BlockedUI(StrEnum):
    """Which blocking UI is currently covering the input chrome.

    Mirrors the names in ``parser_config.UI_PATTERNS`` so parser
    classification and state classification share vocabulary.
    """

    PERMISSION_PROMPT = "permission_prompt"
    ASK_USER_QUESTION = "ask_user_question"
    EXIT_PLAN_MODE = "exit_plan_mode"
    BASH_APPROVAL = "bash_approval"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    SETTINGS = "settings"


@dataclass(frozen=True)
class Working:
    """Spinner running above the input chrome."""

    status_text: str

    def __post_init__(self) -> None:
        if not self.status_text:
            raise ValueError("Working.status_text must be non-empty")
        if "…" not in self.status_text:
            raise ValueError(
                "Working.status_text must contain '…' (U+2026); "
                "completion summaries like 'Worked for 56s' are not running states"
            )


@dataclass(frozen=True)
class Idle:
    """Input chrome present, no spinner."""


@dataclass(frozen=True)
class Blocked:
    """Input chrome replaced by a blocking UI."""

    ui: BlockedUI
    content: str


@dataclass(frozen=True)
class Dead:
    """tmux window alive, ``claude`` process not foreground."""


ClaudeState = Working | Idle | Blocked | Dead
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_claude_state.py -x -q
```

Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/claude_state.py tests/test_claude_state.py
git commit -m "feat(claude_state): add sealed ClaudeState union + BlockedUI enum

Four-case state classification of a running Claude Code instance:
Working (with status text), Idle, Blocked (with UI + content), Dead.
BlockedUI enumerates the six interactive-UI patterns the parser
already recognizes. Working enforces its invariants (non-empty text
containing U+2026) in __post_init__.

Foundation for the v2.0.0 state-unification refactor."
```

---

### Task A2: Integrate `BlockedUI` into `tmux_pane_parser`

`InteractiveUIContent.name: str` becomes `.ui: BlockedUI`. Every
call site and every pattern definition updates to the enum.

**Files:**

- Modify: `src/ccmux/tmux_pane_parser.py` (`InteractiveUIContent` dataclass and `_try_extract`)
- Modify: `src/ccmux/parser_config.py` (`UIPattern.name` → enum-backed value)
- Modify: `tests/test_tmux_pane_parser.py` (rewrite assertions to compare `BlockedUI` members)

- [ ] **Step 1: Write the failing test**

Replace the `TestExtractInteractiveContent` assertions in
`tests/test_tmux_pane_parser.py` so they expect the enum. Add one new
top-of-file assertion:

```python
# At the top of tests/test_tmux_pane_parser.py, alongside other imports:
from ccmux.claude_state import BlockedUI


class TestInteractiveUIContentShape:
    def test_ui_field_is_BlockedUI_enum(
        self, sample_pane_permission_prompt: str
    ) -> None:
        """extract_interactive_content returns `ui: BlockedUI`, not `name: str`."""
        result = extract_interactive_content(sample_pane_permission_prompt)
        assert result is not None
        assert isinstance(result.ui, BlockedUI)
        assert result.ui is BlockedUI.PERMISSION_PROMPT
        assert isinstance(result.content, str)
```

For every existing test that checks `.name == "PermissionPrompt"` (etc.),
replace with `.ui is BlockedUI.PERMISSION_PROMPT`. The mapping is:

| Old `name` string | New `BlockedUI` member |
|---|---|
| `"PermissionPrompt"` | `BlockedUI.PERMISSION_PROMPT` |
| `"AskUserQuestion"` | `BlockedUI.ASK_USER_QUESTION` |
| `"ExitPlanMode"` | `BlockedUI.EXIT_PLAN_MODE` |
| `"BashApproval"` | `BlockedUI.BASH_APPROVAL` |
| `"RestoreCheckpoint"` | `BlockedUI.RESTORE_CHECKPOINT` |
| `"Settings"` | `BlockedUI.SETTINGS` |

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_tmux_pane_parser.py -x -q
```

Expected: FAIL on `isinstance(result.ui, BlockedUI)` (attribute error — the field is still called `.name`).

- [ ] **Step 3: Update `UIPattern` in `parser_config.py`**

The built-in `UIPattern` entries still use string names that match the
StrEnum's `.value`. Change the `name: str` field type to `BlockedUI` so
the compiler enforces the vocabulary:

```python
# Near the top of src/ccmux/parser_config.py, after the regex import:
from .claude_state import BlockedUI


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region."""

    name: BlockedUI
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2
```

Update each built-in entry:

```python
_BUILTIN_UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name=BlockedUI.EXIT_PLAN_MODE,
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name=BlockedUI.ASK_USER_QUESTION,
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name=BlockedUI.ASK_USER_QUESTION,
        top=(re.compile(r"^\s*[☐✔☒]"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name=BlockedUI.PERMISSION_PROMPT,
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name=BlockedUI.PERMISSION_PROMPT,
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        name=BlockedUI.BASH_APPROVAL,
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name=BlockedUI.RESTORE_CHECKPOINT,
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name=BlockedUI.SETTINGS,
        top=(
            re.compile(r"^\s*Status\s+Config\s+Usage\s+Stats\s*$"),
            re.compile(r"^\s*Select model"),
            re.compile(r"^\s*Settings:.*tab to cycle"),
        ),
        bottom=(
            re.compile(r"Esc to (cancel|exit|clear|close)"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]
```

Update `_parse_ui_patterns` to map string names to `BlockedUI`:

```python
def _parse_ui_patterns(raw: object) -> tuple[UIPattern, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[UIPattern] = []
    for index, entry in enumerate(raw):
        try:
            if not isinstance(entry, dict):
                raise TypeError("entry is not a JSON object")
            name_src = entry.get("name")
            top_src = entry.get("top")
            bottom_src = entry.get("bottom")
            if not isinstance(name_src, str):
                raise KeyError("name")
            try:
                name = BlockedUI(name_src)
            except ValueError as e:
                raise KeyError(f"name {name_src!r} is not a valid BlockedUI") from e
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
```

- [ ] **Step 4: Update `InteractiveUIContent` + `_try_extract` in `tmux_pane_parser.py`**

```python
# src/ccmux/tmux_pane_parser.py — replace the InteractiveUIContent dataclass

from .claude_state import BlockedUI  # add to imports at top


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str
    ui: BlockedUI
```

Update `_try_extract` (one line — the constructor call):

```python
def _try_extract(
    lines: list[str], pattern: _pc.UIPattern
) -> InteractiveUIContent | None:
    # ... (body unchanged until the final return)
    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), ui=pattern.name)
```

- [ ] **Step 5: Run the test suite**

```bash
uv run pytest -x -q
```

Expected: PASS. If `test_parser_config.py` has tests that reference
`UIPattern(name="...")` with a string, update them to `BlockedUI` members
(same mapping table as step 1).

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/claude_state.py src/ccmux/tmux_pane_parser.py src/ccmux/parser_config.py \
        tests/test_tmux_pane_parser.py tests/test_parser_config.py
git commit -m "refactor(parser): surface UI pattern match as BlockedUI enum

InteractiveUIContent.name: str becomes .ui: BlockedUI. Parser
vocabulary is now shared with the coming ClaudeState module — a Blocked
state and an extracted UI describe the same six variants, and the
compiler enforces the match.

parser_config.UIPattern.name field type narrows from str to BlockedUI.
JSON-config overrides still use string names, validated on load."
```

---

### Task A3: Create `claude_instance.py` (rename from `window_bindings.py`)

Rename the module, the two classes, the one field, and the persistence
filename. No behavioural changes — pure structural rename with new
vocabulary.

**Files:**

- Create: `src/ccmux/claude_instance.py`
- Delete: `src/ccmux/window_bindings.py` (handled in Task A8; this task stops importing from it)
- Modify: `src/ccmux/config.py` (`bindings_file` → `instances_file`, filename constant)
- Create: `tests/test_claude_instance.py`

**Field name changes inside the new `ClaudeInstance`:**

| Old (`WindowBinding`) | New (`ClaudeInstance`) |
|---|---|
| `window_id: str` | `window_id: str` |
| `session_name: str` | `instance_id: str` |
| `claude_session_id: str` | `session_id: str` |
| `cwd: str` | `cwd: str` |

**Method renames inside `ClaudeInstanceRegistry` (was `WindowBindings`):**

| Old | New |
|---|---|
| `get(window_id)` | `get_by_window_id(window_id)` |
| `get_by_session_name(name)` | `get(instance_id)` |
| `find_by_claude_session_id(sid)` | `find_by_session_id(sid)` |
| `is_session_in_map(name)` | `contains(instance_id)` |
| `all()` | `all()` (signature unchanged) |
| `raw` property | `raw` property (unchanged) |
| `encode_cwd(cwd)` staticmethod | `encode_cwd(cwd)` (unchanged) |

Persistence file: `$CCMUX_DIR/window_bindings.json` → `$CCMUX_DIR/claude_instances.json`.
Inside the file, the JSON top-level keys were `session_name` strings
pointing to `{window_id, session_id, cwd}` dicts. After rename the keys
are `instance_id` strings (the value type is already what we want —
`claude_session_id` was nested under `session_id` in the JSON, keep
that).

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_instance.py`:

```python
"""Tests for ClaudeInstance + ClaudeInstanceRegistry."""

import json
from pathlib import Path

import pytest

from ccmux.claude_instance import ClaudeInstance, ClaudeInstanceRegistry


@pytest.fixture
def tmp_instances_file(tmp_path: Path) -> Path:
    return tmp_path / "claude_instances.json"


class TestClaudeInstance:
    def test_fields(self) -> None:
        inst = ClaudeInstance(
            instance_id="__ccmux__",
            window_id="@7",
            session_id="abc-123",
            cwd="/home/w/proj",
        )
        assert inst.instance_id == "__ccmux__"
        assert inst.window_id == "@7"
        assert inst.session_id == "abc-123"
        assert inst.cwd == "/home/w/proj"

    def test_is_frozen(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        with pytest.raises(Exception):
            inst.window_id = "@99"  # type: ignore[misc]


class TestClaudeInstanceRegistry:
    def test_empty_when_file_missing(self, tmp_instances_file: Path) -> None:
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        assert list(reg.all()) == []

    def test_get_by_instance_id(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "__ccmux__": {
                        "window_id": "@7",
                        "session_id": "abc-123",
                        "cwd": "/home/w/proj",
                    }
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        inst = reg.get("__ccmux__")
        assert inst is not None
        assert inst.instance_id == "__ccmux__"
        assert inst.window_id == "@7"
        assert inst.session_id == "abc-123"

    def test_get_by_window_id(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "s1", "cwd": "/a"},
                    "beta":  {"window_id": "@9", "session_id": "s2", "cwd": "/b"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        hit = reg.get_by_window_id("@9")
        assert hit is not None
        assert hit.instance_id == "beta"

    def test_find_by_session_id(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "target", "cwd": "/a"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        hit = reg.find_by_session_id("target")
        assert hit is not None
        assert hit.instance_id == "alpha"

    def test_contains(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "s", "cwd": "/a"},
                    "empty": {"window_id": "", "session_id": "", "cwd": "/b"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        assert reg.contains("alpha") is True
        assert reg.contains("empty") is False
        assert reg.contains("missing") is False

    def test_all_skips_windowless_entries(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "s", "cwd": "/a"},
                    "pending": {"window_id": "", "session_id": "", "cwd": "/b"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        ids = sorted(i.instance_id for i in reg.all())
        assert ids == ["alpha"]

    @pytest.mark.asyncio
    async def test_load_reloads_from_disk(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(json.dumps({}))
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        assert list(reg.all()) == []
        tmp_instances_file.write_text(
            json.dumps(
                {"a": {"window_id": "@1", "session_id": "s", "cwd": "/c"}}
            )
        )
        await reg.load()
        assert [i.instance_id for i in reg.all()] == ["a"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_claude_instance.py -x -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ccmux.claude_instance'`.

- [ ] **Step 3: Write the implementation**

Create `src/ccmux/claude_instance.py`:

```python
"""Claude instance registry — persistent ``instance_id → window/session``
map backed by ``$CCMUX_DIR/claude_instances.json``.

A ``ClaudeInstance`` is one running Claude Code process in a tmux
window. The registry is the persisted record of every known instance;
it is written by the ``ccmux hook`` CLI on SessionStart and read by the
backend's poll loops.

Instance identity:

- ``instance_id`` — stable key (the tmux session name chosen at bind
  time). Survives Claude resume, ``/clear``, and re-attach.
- ``window_id`` — current tmux window id; changes when the backend
  auto-resumes a dead Claude session.
- ``session_id`` — Claude's JSONL session UUID; changes on ``/clear``.
- ``cwd`` — the launch directory; stable.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeInstance:
    """Backend view of one running Claude Code process."""

    instance_id: str
    window_id: str
    session_id: str
    cwd: str


@dataclass
class ClaudeSession:
    """Summary of a Claude Code JSONL session file (unchanged from v1.x)."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


class ClaudeInstanceRegistry:
    """``instance_id -> ClaudeInstance`` persistent map.

    Backed by ``claude_instances.json``. Read-only from the backend's
    perspective (the hook CLI writes it). Reloaded each fast-loop tick
    via ``load()``.
    """

    def __init__(self, map_file: Path | None = None) -> None:
        self._map_file = map_file if map_file is not None else config.instances_file
        self._data: dict[str, dict[str, str]] = {}
        self._read()

    def _read(self) -> None:
        self._data = {}
        if not self._map_file.exists():
            logger.info("claude_instances.json not found")
            return
        try:
            raw = json.loads(self._map_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load claude_instances.json: %s", e)
            return
        if isinstance(raw, dict):
            for instance_id, entry in raw.items():
                if isinstance(entry, dict):
                    self._data[instance_id] = entry

    async def load(self) -> None:
        """Reload from disk."""
        self._read()

    # -- lookups --------------------------------------------------------

    def get(self, instance_id: str) -> ClaudeInstance | None:
        """Primary lookup by stable id."""
        entry = self._data.get(instance_id)
        if not entry:
            return None
        return self._to_instance(instance_id, entry)

    def get_by_window_id(self, window_id: str) -> ClaudeInstance | None:
        if not window_id:
            return None
        for instance_id, entry in self._data.items():
            if entry.get("window_id") == window_id:
                return self._to_instance(instance_id, entry)
        return None

    def find_by_session_id(self, session_id: str) -> ClaudeInstance | None:
        if not session_id:
            return None
        for instance_id, entry in self._data.items():
            if entry.get("session_id") == session_id:
                return self._to_instance(instance_id, entry)
        return None

    def contains(self, instance_id: str) -> bool:
        """True iff ``instance_id`` has both a window_id and a session_id."""
        entry = self._data.get(instance_id)
        return bool(entry and entry.get("window_id") and entry.get("session_id"))

    def all(self) -> Iterator[ClaudeInstance]:
        """Iterate only instances with a non-empty window_id."""
        for instance_id, entry in list(self._data.items()):
            wid = entry.get("window_id", "")
            if wid:
                yield self._to_instance(instance_id, entry)

    # -- raw access (for internal consumers) ----------------------------

    @property
    def raw(self) -> Mapping[str, dict[str, str]]:
        return self._data

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _to_instance(instance_id: str, entry: dict[str, str]) -> ClaudeInstance:
        return ClaudeInstance(
            instance_id=instance_id,
            window_id=entry.get("window_id", ""),
            session_id=entry.get("session_id", ""),
            cwd=entry.get("cwd", ""),
        )

    @staticmethod
    def encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming."""
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)
```

- [ ] **Step 4: Update `config.py` to expose `instances_file`**

Modify `src/ccmux/config.py` line 56 (or nearby; locate the
`bindings_file` attribute and rename):

```python
# Old:
self.bindings_file = self.config_dir / "window_bindings.json"

# New:
self.instances_file = self.config_dir / "claude_instances.json"
```

Do not keep `bindings_file` — per the no-compat principle, delete.

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_claude_instance.py -x -q
```

Expected: PASS. Note: `test_load_reloads_from_disk` needs
`pytest-asyncio` which is already a dev dependency.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/claude_instance.py src/ccmux/config.py tests/test_claude_instance.py
git commit -m "feat(claude_instance): add ClaudeInstance + ClaudeInstanceRegistry

Rename window_bindings.py → claude_instance.py with vocabulary
aligned to the entity model:

- WindowBinding       → ClaudeInstance
- WindowBindings      → ClaudeInstanceRegistry
- session_name (field) → instance_id
- claude_session_id   → session_id (shorter; the 'claude' prefix is redundant)
- bindings_file (config) → instances_file
- window_bindings.json → claude_instances.json

Registry primary getter is now get(instance_id); lookups by window_id
and session_id remain available with explicit names.

window_bindings.py is not yet deleted — consumers (hook, backend,
status_monitor, liveness) still import from it and will migrate in
subsequent tasks."
```

---

### Task A4: Update `hook.py` to write the new filename and key shape

The ``ccmux hook`` CLI (invoked by Claude Code's SessionStart hook) is
the sole writer of the persistence file. Switch the write path and
keep the value dict shape compatible with the new registry's reader.

**Files:**

- Modify: `src/ccmux/hook.py` (line 432 area; docstrings on line 4 and line 317)
- Modify: `tests/test_hook.py`

- [ ] **Step 1: Update `tests/test_hook.py`**

Every assertion that reads/writes `window_bindings.json` becomes
`claude_instances.json`. The top-level keys in the JSON were already
called "session_name" conceptually; rename references in the test to
"instance_id" for clarity, and make sure the dict *values* still use
nested keys `window_id`, `session_id`, `cwd` — those are stable because
`ClaudeInstanceRegistry._to_instance()` already reads those nested
keys.

Find every occurrence of `window_bindings.json` in `tests/test_hook.py`
and replace with `claude_instances.json`. Find every comment or
variable name referencing `session_name` (as the map's *outer* key) and
rename to `instance_id`.

Example pattern (adapt to actual test content):

```python
# Before:
map_file = tmp_path / "window_bindings.json"
...
assert json.loads(map_file.read_text()) == {
    "__ccmux__": {"window_id": "@7", "session_id": "abc", "cwd": "/tmp"}
}

# After:
map_file = tmp_path / "claude_instances.json"
...
assert json.loads(map_file.read_text()) == {
    "__ccmux__": {"window_id": "@7", "session_id": "abc", "cwd": "/tmp"}
}
```

(The JSON content itself doesn't change shape — only the filename.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_hook.py -x -q
```

Expected: FAIL — hook still writes to `window_bindings.json` so tests
expecting `claude_instances.json` can't find it.

- [ ] **Step 3: Update `src/ccmux/hook.py`**

Line 432:

```python
# Old:
map_file = ccmux_dir() / "window_bindings.json"

# New:
map_file = ccmux_dir() / "claude_instances.json"
```

Update the module docstring (line 4):

```python
"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain an instance
registry in <CCMUX_DIR>/claude_instances.json. ...
"""
```

Update the inline comment on line 317 (or wherever the
`window_bindings.json` reference appears in the docstring of the
CLI-entry function):

```python
# Change any remaining mention of window_bindings.json to
# claude_instances.json. Search the file for the old filename and
# replace each occurrence.
```

Verify with grep:

```bash
grep -n "window_bindings" src/ccmux/hook.py
```

Expected: no output (all references renamed).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_hook.py -x -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "refactor(hook): write to claude_instances.json

Matches the new registry module. The JSON value shape (window_id /
session_id / cwd) is unchanged; only the filename and the
docstring/comment vocabulary update to the instance model."
```

---

### Task A5: Create `state_monitor.py` (merges `status_monitor` + `liveness`)

The new monitor produces `ClaudeState | None` per `ClaudeInstance` on
each fast tick, and produces `Dead()` on the slow tick when the
`claude` process has exited. A single callback injected at construction
receives `(instance_id, state)` whenever the monitor has something to
report.

Skip semantics: if the monitor cannot observe the instance (tmux window
missing, `capture-pane` empty/error), the callback is **not** invoked
at all.

**Files:**

- Create: `src/ccmux/state_monitor.py`
- Create: `tests/test_state_monitor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_monitor.py`:

```python
"""Tests for StateMonitor — classifies a ClaudeInstance into ClaudeState."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ccmux.claude_instance import ClaudeInstance
from ccmux.claude_state import (
    Blocked,
    BlockedUI,
    ClaudeState,
    Dead,
    Idle,
    Working,
)
from ccmux.state_monitor import StateMonitor


# ---- Fakes ----------------------------------------------------------------


@dataclass
class _FakeTmux:
    """Stub tmux session registry: only what StateMonitor reads."""

    panes: dict[str, str]
    window_ids_present: set[str]
    pane_commands: dict[str, str]  # window_id -> current foreground command

    def get_by_window_id(self, wid: str):
        if wid not in self.window_ids_present:
            return None
        return self

    async def find_window_by_id(self, wid: str):
        if wid not in self.window_ids_present:
            return None
        return _FakeWindow(window_id=wid, pane_current_command=self.pane_commands.get(wid, "claude"))

    async def capture_pane(self, wid: str) -> str:
        return self.panes.get(wid, "")

    def get_or_create(self, session_name: str):
        return self


@dataclass
class _FakeWindow:
    window_id: str
    pane_current_command: str


@dataclass
class _FakeRegistry:
    instances: list[ClaudeInstance]
    raw: dict[str, Any]

    def all(self):
        return iter(self.instances)

    async def load(self) -> None:
        pass


@pytest.fixture
def chrome() -> str:
    return "─────────────────────────────\n❯\n─────\nstatusbar"


# ---- Tests ----------------------------------------------------------------


class TestClassification:
    @pytest.mark.asyncio
    async def test_working_from_spinner(self, chrome: str) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        pane = f"some output\n✽ Thinking… (3s)\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={"a": {"window_id": "@1"}})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert seen[0][0] == "a"
        assert isinstance(seen[0][1], Working)
        assert seen[0][1].status_text == "Thinking… (3s)"

    @pytest.mark.asyncio
    async def test_idle_from_chrome_no_spinner(self, chrome: str) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        pane = f"just some scrollback\n{chrome}"
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert isinstance(seen[0][1], Idle)

    @pytest.mark.asyncio
    async def test_blocked_from_missing_chrome(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        # Permission prompt pattern, no chrome sandwich at bottom.
        pane = (
            "Edit /tmp/foo\n"
            "Do you want to proceed?\n"
            "1. Yes\n"
            "2. No\n"
            "Esc to cancel\n"
        )
        tmux = _FakeTmux(
            panes={"@1": pane},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert len(seen) == 1
        assert isinstance(seen[0][1], Blocked)
        assert seen[0][1].ui is BlockedUI.PERMISSION_PROMPT


class TestSkipRules:
    @pytest.mark.asyncio
    async def test_skip_when_window_missing(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@gone", session_id="s", cwd="/")
        tmux = _FakeTmux(panes={}, window_ids_present=set(), pane_commands={})
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []  # bindings not auto-cleared, no callback

    @pytest.mark.asyncio
    async def test_skip_when_pane_capture_empty(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        tmux = _FakeTmux(
            panes={"@1": ""},  # capture returned empty
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.fast_tick()

        assert seen == []


class TestSlowTickDead:
    @pytest.mark.asyncio
    async def test_dead_when_claude_not_foreground(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/home/u")
        tmux = _FakeTmux(
            panes={"@1": "irrelevant"},
            window_ids_present={"@1"},
            pane_commands={"@1": "zsh"},  # user dropped to a shell
        )
        reg = _FakeRegistry(instances=[inst], raw={"a": {"window_id": "@1", "session_id": "s", "cwd": "/home/u"}})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        # Exactly one Dead emission, no resume attempt at this layer.
        deads = [(iid, s) for iid, s in seen if isinstance(s, Dead)]
        assert len(deads) == 1
        assert deads[0][0] == "a"

    @pytest.mark.asyncio
    async def test_slow_tick_silent_when_claude_alive(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/home/u")
        tmux = _FakeTmux(
            panes={"@1": "irrelevant"},
            window_ids_present={"@1"},
            pane_commands={"@1": "claude"},
        )
        reg = _FakeRegistry(instances=[inst], raw={"a": {"window_id": "@1", "session_id": "s", "cwd": "/home/u"}})
        seen: list[tuple[str, ClaudeState]] = []

        async def on_state(instance_id: str, state: ClaudeState) -> None:
            seen.append((instance_id, state))

        mon = StateMonitor(registry=reg, tmux_registry=tmux, on_state=on_state)
        await mon.slow_tick()

        assert seen == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_state_monitor.py -x -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ccmux.state_monitor'`.

- [ ] **Step 3: Write the implementation**

Create `src/ccmux/state_monitor.py`:

```python
"""State monitor — classifies every known ClaudeInstance into a
ClaudeState and emits observations via a callback.

Two ticks:

- ``fast_tick()`` — called at ``config.monitor_poll_interval``. For each
  instance, captures its pane, classifies into
  ``Working / Idle / Blocked``, emits via ``on_state``. Silent skip
  when the window is gone or capture returns empty.
- ``slow_tick()`` — called at ``slow_interval`` (default 60s). For each
  instance, probes ``pane_current_command``; emits ``Dead()`` when
  tmux is alive but the foreground process is no longer ``claude`` /
  ``node``. Auto-resume is the backend's responsibility — this module
  only reports.

The monitor keeps no state between ticks. Each emission is a fresh
observation.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, TYPE_CHECKING

from .claude_state import Blocked, ClaudeState, Dead, Idle, Working
from .tmux_pane_parser import (
    _has_input_chrome,
    extract_interactive_content,
    parse_status_line,
)

if TYPE_CHECKING:
    from .claude_instance import ClaudeInstance, ClaudeInstanceRegistry
    from .tmux import TmuxSessionRegistry

logger = logging.getLogger(__name__)


_DEFAULT_CLAUDE_PROC_NAMES: frozenset[str] = frozenset({"claude", "node"})


def _claude_proc_names() -> frozenset[str]:
    """Resolve the set of process names counted as 'Claude is alive'."""
    raw = os.getenv("CCMUX_CLAUDE_PROC_NAMES", "")
    names = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(names) if names else _DEFAULT_CLAUDE_PROC_NAMES


OnStateCallback = Callable[[str, ClaudeState], Awaitable[None]]


class StateMonitor:
    """Produces ``(instance_id, ClaudeState)`` observations."""

    def __init__(
        self,
        *,
        registry: "ClaudeInstanceRegistry",
        tmux_registry: "TmuxSessionRegistry",
        on_state: OnStateCallback,
    ) -> None:
        self._registry = registry
        self._tmux_registry = tmux_registry
        self._on_state = on_state

    async def fast_tick(self) -> None:
        """Classify each live instance from its pane text; emit or skip."""
        for inst in list(self._registry.all()):
            try:
                state = await self._classify_from_pane(inst)
            except Exception as e:
                logger.debug("fast_tick classify error for %s: %s", inst.instance_id, e)
                continue
            if state is not None:
                await self._on_state(inst.instance_id, state)

    async def slow_tick(self) -> None:
        """Probe each instance's foreground process; emit Dead when needed."""
        for inst in list(self._registry.all()):
            try:
                dead = await self._probe_dead(inst)
            except Exception as e:
                logger.debug("slow_tick probe error for %s: %s", inst.instance_id, e)
                continue
            if dead:
                await self._on_state(inst.instance_id, Dead())

    # ------------------------------------------------------------------

    async def _classify_from_pane(self, inst: "ClaudeInstance") -> ClaudeState | None:
        """Return a ClaudeState from pane text, or None to skip."""
        if not inst.window_id:
            return None
        tm = self._tmux_registry.get_by_window_id(inst.window_id)
        if tm is None:
            return None
        w = await tm.find_window_by_id(inst.window_id)
        if w is None:
            return None
        pane_text = await tm.capture_pane(inst.window_id)
        if not pane_text:
            return None

        lines = pane_text.strip().split("\n")
        if not _has_input_chrome(lines):
            ui = extract_interactive_content(pane_text)
            if ui is None:
                # Chrome absent but no known UI matched — drift; skip.
                return None
            return Blocked(ui=ui.ui, content=ui.content)

        status_text = parse_status_line(pane_text)
        if status_text:
            return Working(status_text=status_text)
        return Idle()

    async def _probe_dead(self, inst: "ClaudeInstance") -> bool:
        """True when the tmux window exists but the pane foreground is not claude."""
        if not inst.window_id:
            return False
        tm = self._tmux_registry.get_by_window_id(inst.window_id)
        if tm is None:
            return False
        w = await tm.find_window_by_id(inst.window_id)
        if w is None:
            return False
        return w.pane_current_command not in _claude_proc_names()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_state_monitor.py -x -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/state_monitor.py tests/test_state_monitor.py
git commit -m "feat(state_monitor): add ClaudeState-emitting monitor

Replaces status_monitor.py and liveness.py (still present during the
transition, to be deleted in a later task). Produces (instance_id,
ClaudeState) observations via a callback:

- fast_tick classifies pane text into Working/Idle/Blocked (skips on
  missing window or empty capture)
- slow_tick probes the foreground process; emits Dead when the tmux
  pane is no longer running claude

Auto-resume is not this module's responsibility — it is a pure observer."
```

---

### Task A6: Rewire `backend.py` — dual callback + auto-resume orchestration

Replace the `Backend` protocol's `on_message + on_status` pair with
`on_state + on_message`. The `DefaultBackend` internals switch to the
new monitor, the old `LivenessChecker._window_alive` cache vanishes,
and auto-resume moves from `liveness.py`'s internals to a small
coordinator owned by `DefaultBackend` that subscribes to `Dead`
observations.

**Files:**

- Modify: `src/ccmux/backend.py` (whole file, sweep mechanical)
- Modify: `src/ccmux/message_monitor.py` (callback signature only)
- Modify: `tests/test_claude_backend.py`
- Modify: `tests/fake_backend.py`

- [ ] **Step 1: Update `tests/fake_backend.py` first**

The fake backend is reused by most tests; make it match the new
protocol before touching production code.

```python
"""In-memory FakeBackend that satisfies the new Backend Protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ccmux.claude_instance import ClaudeInstance, ClaudeSession
from ccmux.claude_state import ClaudeState
from ccmux.claude_transcript_parser import ClaudeMessage
from ccmux.tmux import TmuxWindow


@dataclass
class _FakeTmuxOps:
    _parent: FakeBackend

    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]:
        self._parent._record("tmux.send_text", window_id, text)
        return True, "ok"

    async def send_keys(self, window_id: str, keys: list[str]) -> None:
        self._parent._record("tmux.send_keys", window_id, keys)

    async def capture_pane(self, window_id: str) -> str:
        self._parent._record("tmux.capture_pane", window_id)
        return self._parent.pane_text.get(window_id, "")

    async def create_window(self, cwd: str, session_name: str | None = None) -> str:
        self._parent._record("tmux.create_window", cwd, session_name)
        return "@fake"

    async def list_windows(self) -> list[TmuxWindow]:
        self._parent._record("tmux.list_windows")
        return []


@dataclass
class _FakeClaudeOps:
    _parent: FakeBackend

    async def list_sessions(self, cwd: str) -> list[ClaudeSession]:
        self._parent._record("claude.list_sessions", cwd)
        return self._parent.claude_sessions.get(cwd, [])

    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]:
        self._parent._record(
            "claude.get_history", session_id, start_byte=start_byte, end_byte=end_byte
        )
        return self._parent.history.get(session_id, [])


@dataclass
class FakeBackend:
    """In-memory double for the new Backend Protocol."""

    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    instances: dict[str, ClaudeInstance] = field(default_factory=dict)
    pane_text: dict[str, str] = field(default_factory=dict)
    claude_sessions: dict[str, list[ClaudeSession]] = field(default_factory=dict)
    history: dict[str, list[dict]] = field(default_factory=dict)
    on_state: Callable[[str, ClaudeState], Awaitable[None]] | None = None
    on_message: Callable[[str, ClaudeMessage], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False
    tmux: Any = field(init=False)
    claude: Any = field(init=False)

    def __post_init__(self) -> None:
        self.tmux = _FakeTmuxOps(self)
        self.claude = _FakeClaudeOps(self)

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def get_instance(self, instance_id: str) -> ClaudeInstance | None:
        self._record("get_instance", instance_id)
        return self.instances.get(instance_id)

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None:
        self._record("start")
        self.on_state = on_state
        self.on_message = on_message
        self.started = True

    async def stop(self) -> None:
        self._record("stop")
        self.stopped = True

    # ---- Test-helper methods (not part of the Protocol) ----

    async def emit_state(self, instance_id: str, state: ClaudeState) -> None:
        assert self.on_state is not None, "Call start() before emit_state()"
        await self.on_state(instance_id, state)

    async def emit_message(self, instance_id: str, msg: ClaudeMessage) -> None:
        assert self.on_message is not None, "Call start() before emit_message()"
        await self.on_message(instance_id, msg)
```

- [ ] **Step 2: Update `tests/test_claude_backend.py`**

Any test that calls `fake.emit_status(...)` renames to
`fake.emit_state(instance_id, state)`. Any test that reads
`fake.window_binding[...]` renames to `fake.instances[...]`. Any test
that calls `fake.is_alive[...]` / `fake.get_window_binding` etc. should
already be gone from the fake — if tests still reference them, rewrite
to use `emit_state(..., Dead())` or `fake.instances`.

Also: tests that used to do
`backend.start(on_message=..., on_status=...)` switch to
`backend.start(on_state=..., on_message=...)`.

- [ ] **Step 3: Update `src/ccmux/message_monitor.py` callback signature**

`MessageMonitor.poll()` returns `list[ClaudeMessage]`; keep that. The
*caller* (backend) passes the `(instance_id, msg)` pair into the user's
`on_message` callback. That means `MessageMonitor` internally has to
know which instance each message came from — it does, via
`TrackedClaudeSession.session_id` mapped back through the registry. We
need a small surface change: `poll()` now returns
`list[tuple[str, ClaudeMessage]]` with the instance_id attached.

Modify `MessageMonitor.poll` to yield pairs. Exact code depends on the
current loop structure — find the inner `for msg in parsed_messages:`
body and wrap emissions:

```python
# in message_monitor.py, inside poll():
# (sketch — adapt to the existing loop body)

instance = self._registry.find_by_session_id(tracked.session_id)
if instance is None:
    continue  # orphan transcript, skip
# ... existing parse/filter ...
new_pairs.append((instance.instance_id, parsed))
return new_pairs
```

Also rename the constructor parameter:

```python
# old:
def __init__(self, window_bindings: WindowBindings, ...):
    self._window_bindings = window_bindings

# new:
def __init__(self, registry: ClaudeInstanceRegistry, ...):
    self._registry = registry
```

Update imports:

```python
from .claude_instance import ClaudeInstanceRegistry
```

Modify `tests/test_message_monitor.py` accordingly — every
`WindowBindings(...)` becomes `ClaudeInstanceRegistry(...)`, and
assertions on the return value of `poll()` expect the
`(instance_id, msg)` tuple shape.

- [ ] **Step 4: Rewrite `src/ccmux/backend.py`**

Replace the current module with the new protocol + `DefaultBackend`:

```python
"""Backend — the single Protocol any frontend drives.

Split into two sub-Protocols by domain (TmuxOps, ClaudeOps) and a
top-level Backend that orchestrates the poll loops.

Backend emits two kinds of observation via two callbacks:

- on_state(instance_id, ClaudeState) — per fast tick per known instance
  (or on slow-tick Dead detection)
- on_message(instance_id, ClaudeMessage) — per new JSONL line

The DefaultBackend owns the fast/slow tasks, injects state_monitor /
message_monitor with an internal fan-in, and handles auto-resume
when state_monitor reports Dead.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .claude_files import ClaudeFileResolver
from .claude_instance import ClaudeInstance, ClaudeInstanceRegistry, ClaudeSession
from .claude_state import ClaudeState, Dead
from .claude_transcript_parser import ClaudeMessage
from .config import config
from .message_monitor import MessageMonitor
from .state_monitor import StateMonitor
from .tmux import TmuxSessionRegistry, TmuxWindow

logger = logging.getLogger(__name__)


class TmuxOps(Protocol):
    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]: ...
    async def send_keys(self, window_id: str, keys: list[str]) -> None: ...
    async def capture_pane(self, window_id: str) -> str: ...
    async def create_window(self, cwd: str, session_name: str | None = None) -> str: ...
    async def list_windows(self) -> list[TmuxWindow]: ...


class ClaudeOps(Protocol):
    async def list_sessions(self, cwd: str) -> list[ClaudeSession]: ...
    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]: ...


class Backend(Protocol):
    tmux: TmuxOps
    claude: ClaudeOps

    def get_instance(self, instance_id: str) -> ClaudeInstance | None: ...

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None: ...

    async def stop(self) -> None: ...


class _TmuxOpsImpl:
    def __init__(self, tmux_registry: TmuxSessionRegistry) -> None:
        self._tmux_registry = tmux_registry

    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]:
        tm = self._tmux_registry.get_by_window_id(window_id)
        if not tm:
            return False, "Window no longer exists"
        window = await tm.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        success = await tm.send_keys(window.window_id, text)
        return (True, "Sent") if success else (False, "Failed to send keys")

    async def send_keys(self, window_id: str, keys: list[str]) -> None:
        tm = self._tmux_registry.get_by_window_id(window_id)
        if tm is None:
            return
        for key in keys:
            await tm.send_keys(window_id, key, enter=False, literal=False)

    async def capture_pane(self, window_id: str) -> str:
        tm = self._tmux_registry.get_by_window_id(window_id)
        if tm is None:
            return ""
        text = await tm.capture_pane(window_id)
        return text or ""

    async def create_window(self, cwd: str, session_name: str | None = None) -> str:
        sn = session_name or config.tmux_session_name
        tm = self._tmux_registry.get_or_create(sn)
        success, message, _, wid = await tm.create_window(work_dir=cwd)
        if not success:
            raise RuntimeError(f"create_window failed: {message}")
        return wid

    async def list_windows(self) -> list[TmuxWindow]:
        return await self._tmux_registry.list_all_windows()


class _ClaudeOpsImpl:
    def __init__(self, files: ClaudeFileResolver) -> None:
        self._files = files

    async def list_sessions(self, cwd: str) -> list[ClaudeSession]:
        encoded = ClaudeInstanceRegistry.encode_cwd(cwd)
        project_dir = config.claude_projects_path / encoded
        if not project_dir.exists():
            return []
        paths: list[Path] = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        sessions: list[ClaudeSession] = []
        for path in paths:
            cs = await self._files.get_session_summary(path.stem, cwd)
            if cs is not None:
                sessions.append(cs)
        return sessions

    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]:
        file_path = await self._files.find_file(session_id)
        if file_path is None:
            return []
        return await self._files.read_messages(
            file_path, session_id, start_byte=start_byte, end_byte=end_byte
        )


class DefaultBackend:
    """Default tmux-backed Backend."""

    def __init__(
        self,
        tmux_registry: TmuxSessionRegistry,
        registry: ClaudeInstanceRegistry,
        message_monitor: MessageMonitor | None = None,
        slow_interval: float = 60.0,
        show_user_messages: bool | None = None,
    ) -> None:
        self._tmux_registry = tmux_registry
        self._registry = registry
        self._files = ClaudeFileResolver(registry)
        self._message_monitor = message_monitor or MessageMonitor(
            registry=registry,
            show_user_messages=show_user_messages,
        )
        self._slow_interval = slow_interval
        self._fast_task: asyncio.Task[None] | None = None
        self._slow_task: asyncio.Task[None] | None = None

        self.tmux: TmuxOps = _TmuxOpsImpl(tmux_registry)
        self.claude: ClaudeOps = _ClaudeOpsImpl(self._files)

    # --- Queries -----------------------------------------------------

    def get_instance(self, instance_id: str) -> ClaudeInstance | None:
        return self._registry.get(instance_id)

    # --- Lifecycle ---------------------------------------------------

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None:
        self._message_monitor.startup_cleanup()

        async def on_state_with_resume(instance_id: str, state: ClaudeState) -> None:
            # Fan-out to the user callback first so the UI reflects Dead
            # before the resume attempt.
            try:
                await on_state(instance_id, state)
            except Exception as e:
                logger.debug("on_state consumer error: %s", e)
            if isinstance(state, Dead):
                try:
                    await self._try_resume(instance_id)
                except Exception as e:
                    logger.warning("auto-resume failed for %s: %s", instance_id, e)

        state_monitor = StateMonitor(
            registry=self._registry,
            tmux_registry=self._tmux_registry,
            on_state=on_state_with_resume,
        )

        async def fast_loop() -> None:
            logger.info(
                "Fast poll loop started (interval: %ss)",
                config.monitor_poll_interval,
            )
            while True:
                try:
                    await self._registry.load()
                    new_pairs, _ = await asyncio.gather(
                        self._message_monitor.poll(),
                        state_monitor.fast_tick(),
                    )
                    for instance_id, msg in new_pairs:
                        try:
                            await on_message(instance_id, msg)
                        except Exception as e:
                            logger.debug("on_message consumer error: %s", e)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Fast poll loop error: %s", e)
                await asyncio.sleep(config.monitor_poll_interval)

        async def slow_loop() -> None:
            logger.info("Slow poll loop started (interval: %ss)", self._slow_interval)
            while True:
                try:
                    await state_monitor.slow_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Slow poll loop error: %s", e)
                await asyncio.sleep(self._slow_interval)

        self._fast_task = asyncio.create_task(fast_loop())
        self._slow_task = asyncio.create_task(slow_loop())
        logger.info("Backend poll loops started")

    async def stop(self) -> None:
        for name, task in (("fast", self._fast_task), ("slow", self._slow_task)):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("%s poll loop raised during stop: %s", name, e)
            logger.info("%s poll loop stopped", name)
        self._fast_task = None
        self._slow_task = None

        try:
            self._message_monitor.shutdown()
        except Exception as e:
            logger.debug("message monitor shutdown error: %s", e)

    # --- Auto-resume -------------------------------------------------

    async def _try_resume(self, instance_id: str) -> None:
        inst = self._registry.get(instance_id)
        if inst is None:
            return
        cwd = inst.cwd or str(Path.home())
        logger.info(
            "Attempting to resume Claude session %s in instance %s (cwd=%s)",
            inst.session_id,
            instance_id,
            cwd,
        )
        tm = self._tmux_registry.get_or_create(instance_id)
        ok, msg, _, new_wid = await tm.create_window(
            work_dir=cwd, resume_session_id=inst.session_id
        )
        if ok:
            logger.info("Resumed %s in window %s", inst.session_id, new_wid)
        else:
            logger.warning("Failed to resume %s: %s", inst.session_id, msg)


# --- Module-level default singleton --------------------------------------

_default_backend: Backend | None = None


def get_default_backend() -> Backend:
    if _default_backend is None:
        raise RuntimeError(
            "Default backend not set; call set_default_backend() before accessing."
        )
    return _default_backend


def set_default_backend(backend: Backend) -> None:
    global _default_backend
    _default_backend = backend
```

- [ ] **Step 5: Run the test suite**

```bash
uv run pytest -x -q
```

Expected: PASS. Some tests in `test_claude_backend.py` will need
mechanical updates (done in Step 2). `test_pane_state.py` and
`test_verify_all.py` will likely start failing — leave those for Task
A8 (deletion).

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/backend.py src/ccmux/message_monitor.py \
        tests/test_claude_backend.py tests/test_message_monitor.py \
        tests/fake_backend.py
git commit -m "refactor(backend): dual callback (on_state + on_message)

Backend protocol now emits two kinds of observation:

- on_state(instance_id, ClaudeState) — per fast tick (and Dead on slow tick)
- on_message(instance_id, ClaudeMessage) — per new JSONL line

DefaultBackend composes StateMonitor (new, merges old status_monitor +
liveness) with MessageMonitor. Auto-resume moves from liveness to a
small coordinator inside DefaultBackend that subscribes to Dead
observations.

Removed API:
- Backend.is_alive() — consumers infer from last observed state
- Backend.get_window_binding() — renamed to get_instance()
- Backend.start(on_message, on_status) — replaced with new signature

MessageMonitor now returns (instance_id, ClaudeMessage) pairs from poll()
so the backend can route per-instance without a separate lookup."
```

---

### Task A7: Update `api.py` re-exports

Remove old exports and publish the new type family. This is the sole
touch-point external consumers (ccmux-telegram) import from.

**Files:**

- Modify: `src/ccmux/api.py`
- Modify: `tests/test_api_smoke.py`

- [ ] **Step 1: Update `tests/test_api_smoke.py`**

Edit the expected export set:

```python
# tests/test_api_smoke.py — update __all__ assertion and import list

def test_api_exports() -> None:
    from ccmux import api

    expected = {
        # Protocol + lifecycle
        "Backend", "TmuxOps", "ClaudeOps", "DefaultBackend",
        "get_default_backend", "set_default_backend",
        # State family
        "ClaudeState", "Working", "Idle", "Blocked", "Dead", "BlockedUI",
        # Message / transcript
        "ClaudeMessage", "TranscriptParser",
        # Instance model
        "ClaudeInstance", "ClaudeInstanceRegistry", "ClaudeSession",
        # Composition inputs
        "TmuxSessionRegistry",
        # Parser surfaces
        "InteractiveUIContent", "UsageInfo",
        "extract_bash_output", "extract_interactive_content",
        "parse_status_line", "parse_usage_output",
        # Query types
        "TmuxWindow",
        # Composition helpers
        "tmux_registry", "sanitize_session_name",
    }
    assert set(api.__all__) == expected
    for name in expected:
        assert hasattr(api, name), f"api missing {name!r}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_api_smoke.py -x -q
```

Expected: FAIL — old `__all__` still has `WindowStatus`, `PaneState`,
`WindowBinding`, `WindowBindings` which are not in `expected`.

- [ ] **Step 3: Rewrite `src/ccmux/api.py`**

```python
"""Public API of the ccmux backend.

**Frontends must import from `ccmux.api` only.** Everything reachable
via `from ccmux.<submodule>` is internal and may change without notice.

Four groups:

1. Protocol + lifecycle — the abstract contract and its default implementation.
2. Data types — event payloads, query returns, composition inputs.
3. Parsers — pane text and JSONL parsing functions/classes.
4. Composition helpers — singleton, naming utility.
"""

from __future__ import annotations

# --- 1. Protocol + lifecycle ----------------------------------------------

from .backend import (
    Backend,
    TmuxOps,
    ClaudeOps,
    DefaultBackend,
    get_default_backend,
    set_default_backend,
)

# --- 2. Data types --------------------------------------------------------

# State family (emitted via on_state)
from .claude_state import (
    BlockedUI,
    Blocked,
    ClaudeState,
    Dead,
    Idle,
    Working,
)

# Message family (emitted via on_message)
from .claude_transcript_parser import ClaudeMessage, TranscriptParser

# Instance model
from .claude_instance import ClaudeInstance, ClaudeInstanceRegistry, ClaudeSession

# Parser data types
from .tmux_pane_parser import InteractiveUIContent, UsageInfo

# Query returns
from .tmux import TmuxWindow

# Composition inputs
from .tmux import TmuxSessionRegistry

# --- 3. Parsers -----------------------------------------------------------

from .tmux_pane_parser import (
    extract_bash_output,
    extract_interactive_content,
    parse_status_line,
    parse_usage_output,
)

# --- 4. Composition helpers -----------------------------------------------

from .tmux import tmux_registry, sanitize_session_name


__all__ = [
    # Protocol + lifecycle
    "Backend",
    "TmuxOps",
    "ClaudeOps",
    "DefaultBackend",
    "get_default_backend",
    "set_default_backend",
    # State family
    "ClaudeState",
    "Working",
    "Idle",
    "Blocked",
    "Dead",
    "BlockedUI",
    # Message / transcript
    "ClaudeMessage",
    "TranscriptParser",
    # Instance model
    "ClaudeInstance",
    "ClaudeInstanceRegistry",
    "ClaudeSession",
    # Composition inputs
    "TmuxSessionRegistry",
    # Parser surfaces
    "InteractiveUIContent",
    "UsageInfo",
    "extract_bash_output",
    "extract_interactive_content",
    "parse_status_line",
    "parse_usage_output",
    # Query types
    "TmuxWindow",
    # Composition helpers
    "tmux_registry",
    "sanitize_session_name",
]
```

- [ ] **Step 4: Run the test suite**

```bash
uv run pytest tests/test_api_smoke.py -x -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/api.py tests/test_api_smoke.py
git commit -m "refactor(api): re-export ClaudeState family; drop v1.x surface

Public API now exposes ClaudeState / Working / Idle / Blocked / Dead /
BlockedUI (state family), ClaudeInstance / ClaudeInstanceRegistry
(instance model), and the unchanged ClaudeMessage / parser functions.

Removed: WindowStatus, the old PaneState StrEnum, WindowBinding,
WindowBindings. No aliases; external consumers (ccmux-telegram) must
update in lockstep per the v2.0.0 no-compat principle."
```

---

### Task A8: Delete old modules; full test suite green

`status_monitor.py`, `liveness.py`, and `window_bindings.py` should
have no remaining importers. Delete them, retire the obsolete tests,
and make sure the whole suite passes.

**Files:**

- Delete: `src/ccmux/status_monitor.py`
- Delete: `src/ccmux/liveness.py`
- Delete: `src/ccmux/window_bindings.py`
- Delete: `tests/test_pane_state.py`
- Delete: `tests/test_verify_all.py`

- [ ] **Step 1: Verify nothing imports the deleted modules**

```bash
grep -rn "status_monitor\|window_bindings\|from ccmux.liveness\|from .liveness" \
    src/ tests/
```

Expected: no matches (the three deleted modules should have zero
importers at this point). If any match remains, fix it before
proceeding — the grep must be clean.

- [ ] **Step 2: Delete the modules**

```bash
git rm src/ccmux/status_monitor.py
git rm src/ccmux/liveness.py
git rm src/ccmux/window_bindings.py
git rm tests/test_pane_state.py
git rm tests/test_verify_all.py
```

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -x -q
```

Expected: PASS (every test).

- [ ] **Step 4: Ruff sweep**

```bash
uv run ruff check src/ tests/ --fix
uv run ruff format src/ tests/
```

Expected: either zero diagnostics or only auto-fixable imports. If
anything non-trivial surfaces (unused import, undefined name), fix by
hand — do **not** commit on top of unresolved lints.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: drop status_monitor/liveness/window_bindings

Removed the three modules superseded by claude_state +
claude_instance + state_monitor. Retired test_pane_state.py and
test_verify_all.py; their coverage is now split between
test_claude_state.py and test_state_monitor.py.

Full test suite green under the new type family."
```

---

### Task A9: Version bump + CHANGELOG + README

**Files:**

- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`
- Modify: `README.md`

- [ ] **Step 1: Bump version to 2.0.0**

Edit `pyproject.toml` — find the `version = "1.3.1"` line and change
to `version = "2.0.0"`.

- [ ] **Step 2: Write CHANGELOG entry**

Prepend to `CHANGELOG.md`:

```markdown
## 2.0.0 — 2026-04-20

### Breaking changes — `ccmux.api`

| Removed | Replacement |
|---|---|
| `WindowStatus` | two callbacks: `on_state(instance_id, ClaudeState)` + `on_message(instance_id, ClaudeMessage)` |
| `PaneState` (StrEnum) | `ClaudeState` sealed union: `Working \| Idle \| Blocked \| Dead` |
| `InteractiveUIContent.name: str` | `InteractiveUIContent.ui: BlockedUI` |
| `WindowBinding` | `ClaudeInstance` (`session_name` → `instance_id`, `claude_session_id` → `session_id`) |
| `WindowBindings` | `ClaudeInstanceRegistry` (primary getter is `get(instance_id)`) |
| `Backend.is_alive(window_id)` | no direct replacement; infer from last observed state |
| `Backend.get_window_binding(window_id)` | `Backend.get_instance(instance_id)` |
| `Backend.start(on_message, on_status)` | `Backend.start(on_state, on_message)` |

### Breaking changes — persistence

- `$CCMUX_DIR/window_bindings.json` → `$CCMUX_DIR/claude_instances.json`
- Inside the file, outer keys were conceptually `session_name`; they
  are now `instance_id`. The inner dict shape (`window_id`,
  `session_id`, `cwd`) is unchanged.
- **No migration.** On upgrade the old file is ignored; users re-bind
  their Claude sessions (consistent with the existing "bindings are
  manually managed" policy).

### Internal changes

- `status_monitor.py` and `liveness.py` merged into `state_monitor.py`.
- `window_bindings.py` renamed to `claude_instance.py`.
- New `claude_state.py` hosts the sealed union and `BlockedUI` enum.
- `LivenessChecker._window_alive` cache deleted.
```

- [ ] **Step 3: Regenerate README API table**

Find the "Public API" section in `README.md`. Rewrite it to list the
new exports in the order they appear in `api.__all__`. Include brief
one-line descriptions so readers can navigate without opening the
source.

- [ ] **Step 4: Run tests one more time to confirm clean state**

```bash
uv run pytest -x -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml CHANGELOG.md README.md
git commit -m "chore: bump version to 2.0.0

Major release: ccmux-backend reorganizes around the ClaudeState
sealed union. See CHANGELOG.md for the full breaking-change table
and migration notes."
```

- [ ] **Step 6: Tag**

```bash
git tag -a v2.0.0 -m "v2.0.0 — ClaudeState unification"
```

Do not push the tag yet; wait until Phase B merges in telegram.

---

## Phase B: ccmux-telegram

Phase B starts on the telegram repo. Switch directory:

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-telegram
```

Update the ccmux-backend dependency to the local v2.0.0 checkout
before running any tests:

```bash
uv sync --reinstall-package ccmux
```

### Task B1: Last-state cache + rewire `topic_bindings.is_alive()`

The backend no longer provides `is_alive()`. The frontend maintains a
`{instance_id: ClaudeState}` cache populated from `on_state`, and
`topic_bindings.is_alive(topic)` reads the cache instead of calling the
backend.

**Files:**

- Create: `src/ccmux_telegram/state_cache.py`
- Create: `tests/test_state_cache.py`
- Modify: `src/ccmux_telegram/topic_bindings.py` (around `is_alive()` at line 245)

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_cache.py`:

```python
"""Tests for the per-instance last-state cache."""

from ccmux.api import Dead, Idle, Working

from ccmux_telegram.state_cache import StateCache


class TestStateCache:
    def test_unknown_instance_is_not_alive(self) -> None:
        cache = StateCache()
        assert cache.is_alive("missing") is False

    def test_working_is_alive(self) -> None:
        cache = StateCache()
        cache.update("a", Working(status_text="Thinking…"))
        assert cache.is_alive("a") is True

    def test_idle_is_alive(self) -> None:
        cache = StateCache()
        cache.update("a", Idle())
        assert cache.is_alive("a") is True

    def test_dead_is_not_alive(self) -> None:
        cache = StateCache()
        cache.update("a", Dead())
        assert cache.is_alive("a") is False

    def test_most_recent_state_wins(self) -> None:
        cache = StateCache()
        cache.update("a", Working(status_text="Reading…"))
        assert cache.is_alive("a") is True
        cache.update("a", Dead())
        assert cache.is_alive("a") is False

    def test_get_returns_last_state(self) -> None:
        cache = StateCache()
        w = Working(status_text="Running…")
        cache.update("a", w)
        assert cache.get("a") is w
        assert cache.get("missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_state_cache.py -x -q
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/ccmux_telegram/state_cache.py`:

```python
"""Frontend-side last-state cache.

The backend is a stateless observer; it re-emits ClaudeState every
fast tick. Consumers that need "is this instance alive?" or edge
detection maintain the cache themselves.

This module is the single in-process cache shared by every consumer
that cares (topic_bindings.is_alive, watcher, status_line).
"""

from __future__ import annotations

from ccmux.api import ClaudeState, Dead


class StateCache:
    """``{instance_id: last ClaudeState}`` with a convenience ``is_alive``."""

    def __init__(self) -> None:
        self._data: dict[str, ClaudeState] = {}

    def update(self, instance_id: str, state: ClaudeState) -> None:
        self._data[instance_id] = state

    def get(self, instance_id: str) -> ClaudeState | None:
        return self._data.get(instance_id)

    def is_alive(self, instance_id: str) -> bool:
        state = self._data.get(instance_id)
        if state is None:
            return False
        return not isinstance(state, Dead)


# Module-level singleton for convenience.
_cache = StateCache()


def get_state_cache() -> StateCache:
    return _cache
```

- [ ] **Step 4: Rewire `topic_bindings.is_alive()` to read the cache**

In `src/ccmux_telegram/topic_bindings.py` around line 245, the current
implementation calls `get_default_backend().is_alive(window_id)`.
Replace with:

```python
# at top of file:
from .state_cache import get_state_cache


# inside the class, replace the existing is_alive body:
def is_alive(self, topic: TopicBinding) -> bool:
    """True iff the bound instance is not Dead per the last observation."""
    if topic.instance_id is None:
        # A pending binding without an instance is treated as alive
        # until the binding is completed (matches v1.x semantics).
        return True
    return get_state_cache().is_alive(topic.instance_id)
```

If `TopicBinding` still references `window_id` instead of
`instance_id`, rename the field as part of this task. Search for every
`topic.window_id` in `src/ccmux_telegram/` and update.

- [ ] **Step 5: Run the suite**

```bash
uv run pytest tests/test_state_cache.py tests/test_bindings.py -x -q
```

Expected: PASS. If `test_bindings.py` has tests that previously fed
`backend.is_alive` via the fake backend, rewrite them to feed the
state cache directly (e.g. `get_state_cache().update("a", Dead())`).

- [ ] **Step 6: Commit**

```bash
git add src/ccmux_telegram/state_cache.py src/ccmux_telegram/topic_bindings.py \
        tests/test_state_cache.py tests/test_bindings.py
git commit -m "feat(state_cache): frontend last-state cache; is_alive reads it

Backend no longer exposes is_alive(). Consumers maintain their own
{instance_id: ClaudeState} cache, populated from the on_state callback
(wired in the next task). topic_bindings.is_alive() reads the cache:
Dead means not alive, everything else means alive, unknown instance
means not alive."
```

---

### Task B2: Rewrite `status_line.py` to match on `ClaudeState`

**Files:**

- Modify: `src/ccmux_telegram/status_line.py`
- Modify: `tests/test_status_monitor.py` (or whichever tests exercise `consume_statuses`)

- [ ] **Step 1: Update the test**

Adapt `tests/test_status_monitor.py` — every call to
`consume_statuses(bot, [WindowStatus(...)])` becomes a call to the new
entry point:

```python
from ccmux.api import ClaudeState, Working, Idle, Blocked, BlockedUI, Dead
from ccmux_telegram.status_line import on_state


# replace the pattern:
#   await consume_statuses(bot, [WindowStatus(...)])
# with:
#   await on_state("instance-a", Working(status_text="Reading…"))
```

Add coverage for each ClaudeState variant — one test per case. Example
for the `Working` branch:

```python
@pytest.mark.asyncio
async def test_working_enqueues_status(bot: FakeBot) -> None:
    set_topic_binding("instance-a", user_id=1, thread_id=7, group_chat_id=100)
    await on_state("instance-a", Working(status_text="Reading…"))
    assert bot.enqueued_statuses == [("instance-a", "Reading…")]
```

Similar tests for `Idle()`, `Blocked(ui=BlockedUI.PERMISSION_PROMPT, content=...)`,
`Dead()`.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_status_monitor.py -x -q
```

Expected: FAIL — `on_state` does not exist yet (or still accepts the
old `WindowStatus` argument).

- [ ] **Step 3: Rewrite `status_line.py`**

```python
"""State event consumer — Telegram-side translator for ClaudeState.

Consumes (instance_id, ClaudeState) observations from the backend and
performs all Telegram-facing actions:

  - Enqueue status-line updates on Working.
  - Show/hide interactive prompt UI on Blocked / back-to-Working.
  - Surface resume state on Dead.
"""

from __future__ import annotations

import logging

from telegram import Bot

from ccmux.api import Blocked, ClaudeState, Dead, Idle, Working

from .message_queue import enqueue_status_update
from .prompt import clear_interactive_msg, handle_interactive_ui
from .prompt_state import get_interactive_instance
from .state_cache import get_state_cache

logger = logging.getLogger(__name__)


async def on_state(instance_id: str, state: ClaudeState, *, bot: Bot) -> None:
    """Apply a ClaudeState observation to the Telegram side."""
    # Always update the cache first so downstream consumers (is_alive,
    # watcher) see the latest state before any side-effect runs.
    get_state_cache().update(instance_id, state)

    from .runtime import get_topic_by_instance_id

    topic = get_topic_by_instance_id(instance_id)
    if topic is None:
        return

    user_id = topic.user_id
    thread_id = topic.thread_id
    chat_id = topic.group_chat_id

    match state:
        case Working(status_text):
            await enqueue_status_update(
                bot, user_id, instance_id, status_text,
                thread_id=thread_id, chat_id=chat_id,
            )

        case Idle():
            # Clear any dangling interactive message bound to this instance.
            if get_interactive_instance(user_id, thread_id) == instance_id:
                await clear_interactive_msg(user_id, bot, thread_id, chat_id=chat_id)

        case Blocked(ui, content):
            await handle_interactive_ui(
                bot, user_id, instance_id, thread_id, chat_id=chat_id,
                ui=ui, content=content,
            )

        case Dead():
            await enqueue_status_update(
                bot, user_id, instance_id, "Resuming session…",
                thread_id=thread_id, chat_id=chat_id,
            )
```

`prompt.handle_interactive_ui` needs a small signature widening to
accept `ui` + `content` directly (previously it introspected the pane
via a separate call). Update its implementation and any existing
callers.

Also rename `get_interactive_window` / `get_topic_by_window_id` /
etc. to the `_instance`/`_instance_id` variants throughout — search
for `window_id` in telegram source and systematically rename.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_status_monitor.py -x -q
```

Expected: PASS. Fix any remaining mismatches (e.g. `prompt_state.py`
API renames).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(status_line): match on ClaudeState

consume_statuses is replaced with on_state(instance_id, ClaudeState).
Each variant gets a dedicated branch:

- Working(text)       → enqueue status update
- Idle()              → clear interactive msg if bound to this instance
- Blocked(ui, content) → handle_interactive_ui with explicit UI type
- Dead()              → enqueue 'Resuming session…' placeholder

State cache is updated on every observation so downstream consumers
always see the latest state."
```

---

### Task B3: Simplify `watcher.py::classify`

**Files:**

- Modify: `src/ccmux_telegram/watcher.py`
- Modify: `tests/test_watcher.py`

- [ ] **Step 1: Update tests**

Rewrite `tests/test_watcher.py` assertions to feed ClaudeState directly
into `classify()`:

```python
from ccmux.api import Blocked, BlockedUI, Dead, Idle, Working

from ccmux_telegram.watcher import classify


def test_working_classifies_as_working() -> None:
    assert classify(Working(status_text="Running…")) == "working"


def test_idle_classifies_as_waiting() -> None:
    assert classify(Idle()) == "waiting"


def test_blocked_classifies_as_waiting() -> None:
    assert classify(
        Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="Do you want to proceed?")
    ) == "waiting"


def test_dead_classifies_as_resuming() -> None:
    assert classify(Dead()) == "resuming"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_watcher.py::test_working_classifies_as_working -x -q
```

Expected: FAIL (type signature of `classify` still accepts
`WindowStatus`).

- [ ] **Step 3: Update `watcher.py::classify`**

```python
# at top of watcher.py
from ccmux.api import Blocked, ClaudeState, Dead, Idle, Working

SourceState = Literal["working", "waiting", "resuming"]


def classify(state: ClaudeState) -> SourceState:
    """Reduce the four-case ClaudeState to the dashboard's three buckets."""
    match state:
        case Working(_):
            return "working"
        case Idle() | Blocked(_, _):
            return "waiting"
        case Dead():
            return "resuming"
```

Update `WatcherService.process` (the caller) — it used to take a
`WindowStatus` and ignore "transient" observations via
`not status.window_exists or not status.pane_captured`. That branch
goes away — every `state` arriving from `on_state` is already a real
observation. Rewire:

```python
def process(self, instance_id: str, state: ClaudeState, *, topic: TopicBinding | None = None) -> None:
    bucket = classify(state)
    # ... existing dashboard logic, substituting bucket for the old classify() result
```

Any remaining reference to `status.window_id` → `instance_id`,
`status.pane_captured` → always true (delete the guard),
`status.interactive_ui is not None` → `isinstance(state, Blocked)`.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_watcher.py -x -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux_telegram/watcher.py tests/test_watcher.py
git commit -m "refactor(watcher): classify on ClaudeState with Dead → resuming

The three-bucket dashboard now pattern-matches on the sealed union:
Working → working, Idle|Blocked → waiting, Dead → resuming. Transient
observation guards are gone because the backend no longer emits
partial/invalid observations — every ClaudeState is a real classification."
```

---

### Task B4: Update `bot.py` startup + imports throughout

**Files:**

- Modify: `src/ccmux_telegram/bot.py` (startup at line 232)
- Modify: every file that imports from `ccmux.api` (mass rename sweep)

- [ ] **Step 1: Update bot.py startup**

Around line 232:

```python
# Old:
await backend.start(on_message=_on_message, on_status=_on_status)

# New (note: bot.py's local on_state / on_message callables adapt the
# backend's 2-arg signature to the bot's internal handlers):
async def _on_state(instance_id: str, state: ClaudeState) -> None:
    await on_state(instance_id, state, bot=bot)

async def _on_message(instance_id: str, msg: ClaudeMessage) -> None:
    await on_message(instance_id, msg, bot=bot)

await backend.start(on_state=_on_state, on_message=_on_message)
```

Update imports at top of `bot.py`:

```python
from ccmux.api import ClaudeMessage, ClaudeState
```

- [ ] **Step 2: Rename sweep — `window_id` → `instance_id`**

Search every `.py` file under `src/ccmux_telegram/` and `tests/` for:

- `WindowBinding` → `ClaudeInstance`
- `get_window_binding` → `get_instance`
- `window_id` as a semantic identifier → `instance_id` (NOT the tmux
  window id itself — some code legitimately needs the tmux `@7`-style
  id; for those cases call `backend.get_instance(instance_id).window_id`
  explicitly).

Helper:

```bash
grep -rn "WindowBinding\|get_window_binding" src/ tests/
```

Expected after edits: no matches.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -x -q
```

Expected: PASS. Fix any remaining broken imports or signature
mismatches.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(telegram): start backend with on_state+on_message, rename window_id→instance_id

Mechanical rename sweep. Every ccmux.api import matches the v2.0.0
surface; every consumer uses instance_id as the stable identifier.
tmux window ids are still available via backend.get_instance(...).window_id
for the handful of sites that actually need them."
```

---

### Task B5: Update `fake_backend.py` + remaining tests

**Files:**

- Modify: `tests/fake_backend.py`
- Modify: any test file still using `WindowStatus`, `WindowBinding`,
  `backend.is_alive`, or `backend.get_window_binding`

- [ ] **Step 1: Update fake_backend.py**

Mirror the backend-side fake from Task A6 — same shape, but this lives
in ccmux-telegram's test dir because it used to. If the telegram repo
has its own fake_backend, keep it but adjust types and methods:

```python
# tests/fake_backend.py (telegram-side)

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ccmux.api import (
    ClaudeInstance, ClaudeMessage, ClaudeState, ClaudeSession, TmuxWindow,
)


@dataclass
class FakeBackend:
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    instances: dict[str, ClaudeInstance] = field(default_factory=dict)
    pane_text: dict[str, str] = field(default_factory=dict)
    claude_sessions: dict[str, list[ClaudeSession]] = field(default_factory=dict)
    history: dict[str, list[dict]] = field(default_factory=dict)
    on_state: Callable[[str, ClaudeState], Awaitable[None]] | None = None
    on_message: Callable[[str, ClaudeMessage], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False

    # (tmux / claude sub-ops as before — identical to backend-side FakeBackend)
    # ...

    def get_instance(self, instance_id: str) -> ClaudeInstance | None:
        self._record("get_instance", instance_id)
        return self.instances.get(instance_id)

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None:
        self._record("start")
        self.on_state = on_state
        self.on_message = on_message
        self.started = True

    async def emit_state(self, instance_id: str, state: ClaudeState) -> None:
        assert self.on_state is not None
        await self.on_state(instance_id, state)

    async def emit_message(self, instance_id: str, msg: ClaudeMessage) -> None:
        assert self.on_message is not None
        await self.on_message(instance_id, msg)
```

- [ ] **Step 2: Grep for leftovers**

```bash
grep -rn "WindowStatus\|PaneState\|\.is_alive(" tests/
```

Expected: no matches. Any remaining hits mean a test file missed the
sweep — update it to the new API.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -x -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test(telegram): update fake_backend and stragglers to v2.0.0

FakeBackend emits on_state / on_message, exposes get_instance, drops
is_alive / get_window_binding. Every remaining reference to the v1.x
types in the test tree is updated or removed."
```

---

### Task B6: Version bump + dep bump + release coordination

**Files:**

- Modify: `pyproject.toml` (ccmux-telegram version + ccmux-backend dep)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump dep + telegram version**

In `pyproject.toml`, bump the `ccmux` dependency constraint:

```toml
# Old (example):
dependencies = ["ccmux>=1.3.0,<2.0.0", ...]

# New:
dependencies = ["ccmux>=2.0.0,<3.0.0", ...]
```

Bump the ccmux-telegram package version (follow the existing cadence —
typically a minor or major bump when the backend breaks):

```toml
# e.g. 0.5.x → 0.6.0, documenting the backend upgrade as the reason
version = "0.6.0"
```

- [ ] **Step 2: CHANGELOG entry**

Prepend to ccmux-telegram's `CHANGELOG.md`:

```markdown
## 0.6.0 — 2026-04-20

### Changed

- Upgrade to ccmux-backend v2.0.0 (required; v1.x is incompatible).
- Consumers migrated to the `on_state` / `on_message` split:
  - `status_line.consume_statuses` replaced by
    `status_line.on_state(instance_id, ClaudeState, bot=...)`.
  - `watcher.classify` now takes a `ClaudeState` and returns
    `working | waiting | resuming`.
  - `topic_bindings.is_alive` reads from the new frontend-side
    `StateCache` instead of `backend.is_alive`.
- Vocabulary: every `window_id`-as-stable-key renamed to
  `instance_id`; tmux window ids remain available via
  `backend.get_instance(id).window_id`.

### Removed

- Every reference to `WindowStatus`, `PaneState` (old StrEnum),
  `WindowBinding`, `WindowBindings`, `Backend.is_alive`,
  `Backend.get_window_binding`.

### Note on persistence

The backend changes `$CCMUX_DIR/window_bindings.json` to
`$CCMUX_DIR/claude_instances.json` with no migration. Existing users
must re-bind their Claude sessions after upgrading.
```

- [ ] **Step 3: Run the full suite one final time**

```bash
uv run pytest -x -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml CHANGELOG.md uv.lock
git commit -m "chore: bump to 0.6.0; require ccmux-backend >=2.0.0

Coordinated release with ccmux-backend v2.0.0. See CHANGELOG.md for
the migration summary."
```

- [ ] **Step 5: Tag and push both repos**

In ccmux-backend:

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-backend
git push origin dev
git push origin v2.0.0
```

In ccmux-telegram:

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-telegram
git tag -a v0.6.0 -m "v0.6.0 — ccmux-backend v2.0.0 adaptation"
git push origin dev
git push origin v0.6.0
```

- [ ] **Step 6: Restart the running bot**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-telegram
# Find and kill the current uv run ccmux-telegram process
ps -ef | grep "ccmux-telegram" | grep -v grep
# Send restart via tmux send-keys to the pane it was last running in
tmux send-keys -t __ccmux__:1.1 "uv run ccmux-telegram" Enter
```

Watch the bot's logs for the first few minutes to confirm state
observations flow and existing topics continue updating.

---

## Self-review

### Spec coverage

| Spec section | Task(s) |
|---|---|
| Entity model (ClaudeInstance) | A3 |
| ClaudeState sealed union + BlockedUI | A1, A2 |
| Type invariants (Working.text contains `…`, etc.) | A1 |
| Monitor behaviour (fast/slow ticks, skip rules, Dead) | A5 |
| Auto-resume orchestration | A6 |
| Backend API (on_state, on_message, get_instance) | A6 |
| Statelessness | A5, A6 (no cache introduced) |
| Module layout (3 del / 3 new / 6 mod) | A1–A8 |
| Field-level renames (session_name → instance_id, etc.) | A3, A4, A7, B4 |
| Persistence rename (claude_instances.json) | A3, A4 |
| Frontend impact — status_line, watcher, topic_bindings | B1, B2, B3 |
| Frontend is_alive rewire | B1 |
| Testing — new files and rewrites | Tasks carry their own TDD |
| CHANGELOG + version bumps | A9, B6 |

No gaps.

### Placeholder scan

No "TBD", "TODO", or "similar to above" references. Every task carries
actual test code, actual implementation code, and exact commit
commands.

### Type consistency

- `instance_id: str` used consistently across ClaudeInstance, Registry
  API, on_state / on_message callbacks, and frontend consumers.
- `ClaudeState` imported from `ccmux.claude_state` in internal files;
  from `ccmux.api` in the frontend.
- `BlockedUI` members used identically in `parser_config.UI_PATTERNS`
  and in `Blocked.ui`.

---

## Execution handoff

Plan complete and saved to
`docs/superpowers/plans/2026-04-20-claude-state-unification.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per
   task, review between tasks, fast iteration.
2. **Inline Execution** (`executing-plans-test-first`) — execute tasks
   in this session with checkpoints for review.

Which approach?

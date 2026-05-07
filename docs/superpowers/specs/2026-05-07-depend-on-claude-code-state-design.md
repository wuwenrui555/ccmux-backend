<!-- markdownlint-disable MD024 -->

# Depend on `claude-code-state` Design (v5.1.0)

- **Date**: 2026-05-07
- **Repos affected**: `ccmux-backend` (minor, v5.0.0 → v5.1.0)
- **Status**: design accepted; implementation pending

## Problem

`ccmux-backend` v5.0.0 still embeds the four files that were extracted into the
standalone [`claude-code-state`](https://github.com/wuwenrui555/claude-code-state)
package back in April:

- `src/ccmux/claude_state.py` (74 lines) — the `ClaudeState` sealed union and
  `BlockedUI` enum.
- `src/ccmux/parser_config.py` (430 lines) — built-in pattern data and the
  user-override loader.
- `src/ccmux/tmux_pane_parser.py` (515 lines) — parser primitives plus two
  unrelated tmux-text utilities.
- (parts of `src/ccmux/state_monitor.py` (167 lines) — the only file that
  combines parser primitives into a `ClaudeState` decision.)

`claude-code-state` published v0.3.0 on 2026-05-06 with stable surface
(`parse_pane`, the state types, the parser primitives, an optional
`capture.tmux` helper, and `CLAUDE_CODE_STATE_DIR` for user overrides).
Backend has been carrying its own copy ever since the split. The two have not
diverged in *behaviour*, but every fix to either side has to be manually
mirrored.

This design replaces the embedded copies with a real dependency on
`claude-code-state @ v0.3.0`.

## Goals

1. Add `claude-code-state` as a Git-tag-pinned runtime dependency
   (`@ v0.3.0`).
2. Delete the embedded `claude_state.py` and `parser_config.py`.
3. Reduce `tmux_pane_parser.py` to the two utilities that are *not* part of
   the upstream package's responsibility (`extract_bash_output`,
   `parse_usage_output`, `UsageInfo`); rename to `pane_extras.py`.
4. Rewire `state_monitor.py` to call `claude_code_state.parse_pane` instead of
   stitching the parser primitives by hand.
5. Keep `ccmux.api`'s public surface byte-identical so `ccmux-telegram` does
   not need to change.
6. Ensure `claude-code-state` continues to write `drift.log` and read
   `parser_config.json` from `~/.ccmux/` (or whatever `$CCMUX_DIR` points at),
   matching v5.0.0 behaviour for the user.

## Non-goals

- **Not publishing `claude-code-state` to PyPI.** Git+tag is sufficient for
  this iteration. PyPI publication is a separate decision.
- **Not pushing the bash/usage utilities upstream.** They are tmux-pane
  scraping helpers used only by `ccmux-telegram`, not state classification.
  They live closer to ccmux than to `claude-code-state` and stay in backend.
- **Not changing `ccmux.api`'s public surface.** No additions
  (`parse_pane`, `has_input_chrome` stay internal); no removals; no renames.
- **Not touching `ccmux-telegram` in this iteration.** It consumes everything
  through `ccmux.api`, which stays compatible.
- **Not migrating to a separate, pinned `claude-code-state` per-environment**
  (e.g. via `[tool.uv.sources]`). The Git URL form in
  `[project].dependencies` is the simplest thing that resolves correctly under
  `uv sync` for both dev and CI.

## Architecture

### Dependency declaration

`pyproject.toml`:

```toml
[project]
dependencies = [
    "aiofiles>=24.0.0",
    "claude-code-state @ git+https://github.com/wuwenrui555/claude-code-state.git@v0.3.0",
    "libtmux>=0.37.0",
    "python-dotenv>=1.0.0",
]
```

### Configuration directory bridge

`claude-code-state` reads `$CLAUDE_CODE_STATE_DIR` at import time:

- when set, it loads `parser_config.json` from that directory and writes
  `drift.log` to it;
- when unset, it runs on built-ins only, and drift warnings propagate through
  the standard `claude_code_state.drift` logger.

Backend wants `claude-code-state`'s config to live alongside the rest of
ccmux's state (`~/.ccmux/`), so `src/ccmux/__init__.py` (which is currently a
docstring-only file) is taught one job: bridge `$CCMUX_DIR` →
`$CLAUDE_CODE_STATE_DIR`.

```python
"""ccmux — Claude-tmux backend library.

**Import from `ccmux.api` for the public surface.** The package root is
deliberately bare of re-exports — `from ccmux import X` fails loudly
rather than silently routing through a back door.

The only side effect at import time is pointing ``claude_code_state`` at
the same configuration directory as ccmux: when ``$CCMUX_DIR`` is set,
``$CLAUDE_CODE_STATE_DIR`` inherits it; otherwise both default to
``~/.ccmux``. ``setdefault`` is used so a caller (e.g. a test) can pin
``$CLAUDE_CODE_STATE_DIR`` explicitly.
"""

from __future__ import annotations

import os

from .util import ccmux_dir

os.environ.setdefault("CLAUDE_CODE_STATE_DIR", str(ccmux_dir()))
```

This must run *before* any submodule does `from claude_code_state import …`.
Python guarantees this: importing `ccmux.<sub>` runs `ccmux/__init__.py` first.

### File-level changes

| Operation | File | Notes |
| ---       | ---  | ---   |
| Delete    | `src/ccmux/claude_state.py`                | Now provided by `claude_code_state`. |
| Delete    | `src/ccmux/parser_config.py`               | Now provided by `claude_code_state.config`. |
| Rename + shrink | `src/ccmux/tmux_pane_parser.py` → `src/ccmux/pane_extras.py` | Keep `extract_bash_output`, `parse_usage_output`, `UsageInfo` (and any private helpers they need, e.g. `_strip_pane_chrome`). Drop everything else. |
| Edit      | `src/ccmux/state_monitor.py`               | Imports + `_classify_from_pane` body. |
| Edit      | `src/ccmux/backend.py`                     | Single import line: `from .claude_state import ClaudeState, Dead` → `from claude_code_state import ClaudeState, Dead`. |
| Edit      | `src/ccmux/api.py`                         | Re-export from `claude_code_state` and from `pane_extras`. |
| Edit      | `src/ccmux/__init__.py`                    | Add the env-var bridge above. |

### `state_monitor.py`: surface unchanged, internals delegated

Imports change:

```python
# was
from .claude_state import Blocked, ClaudeState, Dead, Idle, Working
from .tmux_pane_parser import (
    extract_interactive_content,
    has_input_chrome,
    parse_status_line,
)

# becomes
from claude_code_state import ClaudeState, Dead, parse_pane
```

`_classify_from_pane` collapses to:

```python
async def _classify_from_pane(self, b: "CurrentClaudeBinding") -> ClaudeState | None:
    if not b.window_id:
        return None
    tm = self._tmux_registry.get_by_window_id(b.window_id)
    if tm is None:
        return None
    w = await tm.find_window_by_id(b.window_id)
    if w is None:
        return None
    pane_text = await tm.capture_pane(b.window_id)
    return parse_pane(pane_text)
```

`parse_pane` already short-circuits to `None` on empty input, matching the
local fast skip. Behaviour is identical to v5.0.0.

`fast_tick`, `slow_tick`, `_probe_dead`, the `_claude_proc_names()` env-var
logic, and the `OnStateCallback` type alias all remain unchanged.

### `ccmux.api`: same names, new sources

```python
# State family + parser primitives — from external package
from claude_code_state import (
    Blocked,
    BlockedUI,
    ClaudeState,
    Dead,
    Idle,
    InteractiveUIContent,
    Working,
    extract_interactive_content,
    parse_status_line,
)

# Bash / usage scrapers — backend-local
from .pane_extras import UsageInfo, extract_bash_output, parse_usage_output
```

`__all__` is byte-identical to v5.0.0. `parse_pane` and `has_input_chrome`
remain internal — they are implementation details of `state_monitor`, not part
of backend's public surface.

### `pane_extras.py`: what stays, what goes

Stays (the only callers of these are `ccmux-telegram`):

- `extract_bash_output(pane_text: str, command: str) -> str | None`
- `parse_usage_output(pane_text: str) -> UsageInfo | None`
- `@dataclass class UsageInfo`
- whatever private helpers (e.g. `_strip_pane_chrome`) those two need

Goes (now in `claude_code_state.parser`):

- `has_input_chrome`, `parse_status_line`, `extract_interactive_content`,
  `InteractiveUIContent`, `parse_pane`
- the `drift_logger` setup (upstream owns this; the env-var bridge keeps
  drift writes in `~/.ccmux/`)

The split is clean: `claude_code_state` owns "did Claude print one of its
known UIs?", `pane_extras` owns "scrape arbitrary echoed text out of a tmux
pane". The shared bit is the dependency on stable Claude-Code formatting,
which both inherit from `claude_code_state.config.UI_PATTERNS` etc.

### Tests

| File | Operation |
| ---  | ---       |
| `tests/test_state_monitor.py` | Imports switch to `claude_code_state`. Behaviour assertions are unchanged. |
| `tests/test_api_smoke.py`     | The body imports `BlockedUI` directly from `ccmux.claude_state` once for an isinstance check — switch that line to `claude_code_state`. The re-export assertions stay (the file's job is to verify `ccmux.api` exposes the expected names). |
| `tests/test_claude_backend.py` | Single import line for `Idle` switches from `ccmux.claude_state` to `claude_code_state`. |
| `tests/fake_backend.py`       | `from ccmux.claude_state import ClaudeState` → `from claude_code_state import ClaudeState`. |
| `tests/test_claude_state.py`  | **Delete.** Upstream `claude_code_state` has the corresponding tests for state types. |
| `tests/test_parser_config.py` | **Delete.** Upstream owns the merge logic and pattern data. |
| `tests/test_tmux_pane_parser.py` → `tests/test_pane_extras.py` | Keep only the `extract_bash_output` and `parse_usage_output` blocks. The `parse_status_line`, `extract_interactive_content`, `has_input_chrome`, and pattern-drift tests are deleted; they are upstream's responsibility now. Imports update to `ccmux.pane_extras`. |

### CHANGELOG

```text
## [5.1.0]

### Changed
- Depend on the external `claude-code-state` package
  (`@ git+https://github.com/wuwenrui555/claude-code-state.git@v0.3.0`)
  for pane → ClaudeState classification instead of embedding a local
  copy. Public `ccmux.api` surface is unchanged.
- `src/ccmux/__init__.py` now bridges `$CCMUX_DIR` →
  `$CLAUDE_CODE_STATE_DIR` so `parser_config.json` and `drift.log`
  continue to live in `~/.ccmux/`.
- Renamed internal `tmux_pane_parser` → `pane_extras`, retaining only
  `extract_bash_output`, `parse_usage_output`, and `UsageInfo`.

### Removed
- Internal modules `claude_state` and `parser_config`. These were never
  part of the public API; consumers always went through `ccmux.api`,
  which keeps the same names.
```

`pyproject.toml` version bumps `5.0.0 → 5.1.0`.

## Risks

| Risk | Mitigation |
| ---  | ---        |
| `claude_code_state` reads `CLAUDE_CODE_STATE_DIR` at import time and a submodule imports it before `ccmux/__init__.py` runs. | Python imports `ccmux/__init__.py` before any `ccmux.<sub>`. Verified by reading `claude_code_state/__init__.py` (no eager import side effects beyond the env-var read). |
| Behaviour drift between local `parse_*` helpers and upstream's. | Cross-checked file diffs on 2026-05-07: `claude_state.py` differs only in docstrings (zero code drift); `parser_config.py` differs only in env var name and docstring (zero data drift); `tmux_pane_parser.py` upstream is the local file minus `extract_bash_output` / `parse_usage_output` minus the drift logger setup — see "What stays, what goes". |
| `ccmux-telegram` consumes `extract_bash_output` / `parse_usage_output` via `ccmux.api`. | Those names stay in `ccmux.api`, sourced from the local `pane_extras` module. `ccmux-telegram` is unchanged. |
| Some user has a private import of `ccmux.claude_state` or `ccmux.tmux_pane_parser`. | These were never in `ccmux.api`, so they were never public. `__init__.py`'s docstring already says "Import from `ccmux.api` for the public surface."; this is an acceptable minor-version break. CHANGELOG calls it out. |

## Verification

After implementation, on the feature branch:

1. `uv sync` succeeds; `uv.lock` contains `claude-code-state @ git+…@v0.3.0`.
2. `uv run pytest` is green.
3. `uv run pyright src` reports no new errors vs. `dev`.
4. Smoke run against a real tmux + Claude-Code session: states cycle
   `Idle → Working → Idle` and a `Ctrl-C` from the host produces `Dead`.
5. Sibling check: `cd ~/ccmux/ccmux-telegram && uv run pytest` is still green
   without touching its lockfile.
6. Manual drift check: temporarily rename a status spinner in
   `~/.ccmux/parser_config.json`, run a fast tick, verify a line lands in
   `~/.ccmux/drift.log`.

## Release flow (gitflow)

1. Branch `feature/depend-on-claude-code-state` is cut from `dev` and
   implementation lands there.
2. PR merges into `dev`.
3. `release/v5.1.0` is cut from `dev`, version bumped, CHANGELOG date filled
   in, merged into `main` and back into `dev`, tagged `v5.1.0`.

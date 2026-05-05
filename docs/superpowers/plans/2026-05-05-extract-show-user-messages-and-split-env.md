<!-- markdownlint-disable MD024 MD031 MD032 MD040 -->

# Extract show_user_messages and Split Env Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans-test-first to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `CCMUX_SHOW_USER_MESSAGES` from backend; add it as a frontend filter; split `~/.ccmux/.env` into secrets-only `.env` plus settings-only `settings.env`.

**Architecture:** Backend always emits user-message events (no presentation filter). Frontend gains a `show_user_messages` config that filters in `handle_new_message`. Each package's `Config` loads only the env files it needs: backend reads `settings.env` only, frontend reads both `settings.env` and `.env`.

**Tech Stack:** Python 3.12, python-telegram-bot, python-dotenv, pytest, ruff, pyright. Two repos: `ccmux-backend` (pip name `ccmux`) and `ccmux-telegram`. uv-managed editable installs.

**Spec:** `docs/superpowers/specs/2026-05-05-extract-show-user-messages-and-split-env-design.md` (in ccmux-backend).

---

## Phase 1: Backend feature branch (`feature/extract-show-user-messages`)

Cut from `dev`. All changes land here, then merge to `dev`.

### Task 1: Cut feature branch

**Files:** none (git plumbing).

- [ ] **Step 1: Verify on backend dev**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-backend
git checkout dev
git log --oneline -1
```

Expected: HEAD is `18ebf11 docs: spec for v5.0.0 / v5.1.0 ...` (or newer if more docs commits).

- [ ] **Step 2: Cut feature branch**

```bash
git checkout -b feature/extract-show-user-messages
```

Expected: `Switched to a new branch 'feature/extract-show-user-messages'`.

### Task 2: Update MessageMonitor tests to assert user messages always emit

**Files:**
- Test: `tests/test_message_monitor.py` (or whichever file currently asserts the filter; see grep below)

- [ ] **Step 1: Locate existing tests that depend on `show_user_messages`**

```bash
grep -rn "show_user_messages" tests/
```

Expected: any matches indicate places that currently exercise the filter. These need updating.

- [ ] **Step 2: Replace any "user message is filtered when off" assertion with "user message is always emitted"**

Pattern to find and remove:

```python
# Old (delete):
monitor = MessageMonitor(..., show_user_messages=False)
# ... feed JSONL with role=user line ...
assert no_message_emitted

# Replacement (keep / new):
monitor = MessageMonitor(...)  # no show_user_messages kwarg
# ... feed JSONL with role=user line ...
assert one_message_emitted_with_role_user
```

If a test only existed to verify the `False` path, delete it. If a test parameterized over both values, collapse to the always-emit path.

- [ ] **Step 3: Run the updated test file to verify it currently FAILS**

```bash
uv run pytest tests/test_message_monitor.py -v
```

Expected: at least one assertion failure caused by the still-present filter (line 493) skipping the `role=user` entry.

### Task 3: Remove filter from `MessageMonitor`

**Files:**
- Modify: `src/ccmux/message_monitor.py`

- [ ] **Step 1: Remove the filter guard**

Delete these two lines around line 492-494:

```python
                    # Skip user messages unless show_user_messages is enabled
                    if entry.role == "user" and not self._show_user_messages:
                        continue
```

- [ ] **Step 2: Drop the constructor param**

In `MessageMonitor.__init__` (around line 244-269), remove:

- The `show_user_messages: bool | None = None,` parameter from the signature.
- The whole `# Controls whether user-typed messages are emitted ...` comment block plus the `self._show_user_messages = (...)` assignment.

After the edit, `__init__` reads only `projects_path`, `state_file`, `event_reader`.

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_message_monitor.py -v
```

Expected: PASS (the assertions from Task 2 now hold because there is no filter).

- [ ] **Step 4: Commit**

```bash
git add src/ccmux/message_monitor.py tests/test_message_monitor.py
git commit -m "refactor!: drop show_user_messages from MessageMonitor

User messages are always emitted now. Filtering is presentation
policy and moves to the frontend in ccmux-telegram v5.1.0.

BREAKING: MessageMonitor.__init__ no longer accepts
show_user_messages."
```

### Task 4: Remove `show_user_messages` from `DefaultBackend`

**Files:**
- Modify: `src/ccmux/backend.py`

- [ ] **Step 1: Locate signature**

```bash
grep -n "show_user_messages" src/ccmux/backend.py
```

Expected: one or two lines around 176 / 191 referencing the parameter.

- [ ] **Step 2: Drop the parameter**

In `DefaultBackend.__init__` (around line 170-200):

- Remove `show_user_messages: bool | None = None,` from the signature.
- Remove `show_user_messages=show_user_messages,` from the `MessageMonitor(...)` constructor call.

After: the `MessageMonitor(...)` call passes only `event_reader=self.event_reader`.

- [ ] **Step 3: Update any backend test that constructs `DefaultBackend(show_user_messages=...)` or `MessageMonitor(show_user_messages=...)`**

```bash
grep -rn "show_user_messages" tests/
```

Expected: empty. If any matches remain, drop the kwarg from the call.

- [ ] **Step 4: Run full test suite**

```bash
uv run pytest -q
```

Expected: ALL PASS. No `unexpected keyword argument` errors.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/backend.py tests/
git commit -m "refactor!: drop show_user_messages from DefaultBackend

Forwarded the kwarg to MessageMonitor; that constructor no longer
accepts it as of the previous commit. Drop here too.

BREAKING: DefaultBackend.__init__ no longer accepts
show_user_messages."
```

### Task 5: Remove `show_user_messages` from `Config` and switch loader to `settings.env`

**Files:**
- Modify: `src/ccmux/config.py`

- [ ] **Step 1: Replace the `Config.__init__` body**

Replace the file's `Config` class (and the docstring header at the top of the module) with the following:

```python
"""Backend configuration — reads env vars and exposes a `config` singleton.

Only the Claude-tmux backend's own settings live here. Frontend packages
ship their own `config.py` for bot tokens, allow-lists, etc.

Loads `settings.env` only — backend has no secrets, so it never reads
`.env` (which is reserved for sensitive values consumed by frontends).
Loading priority: cwd `settings.env` > `$CCMUX_DIR/settings.env`
(default `~/.ccmux/settings.env`). Reads:

- `CCMUX_TMUX_SESSION_NAME` (default `__ccmux__`) — reserved session
  that holds the bot process itself; never listed as a binding target.
- `CCMUX_CLAUDE_COMMAND` (default `claude`) — command to launch Claude Code.
- `CCMUX_CLAUDE_PROJECTS_PATH` / `CLAUDE_CONFIG_DIR` — where Claude
  Code writes its JSONL transcripts.
- `CCMUX_MONITOR_POLL_INTERVAL` (default `0.5` seconds) — fast-loop tick.
- `CCMUX_DIR` (default `~/.ccmux`) — state-file root. Read from the
  process environment; not loaded from settings.env (chicken-and-egg).
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .util import ccmux_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux).
# Backend has none; frontends maintain their own list.
SENSITIVE_ENV_VARS: set[str] = set()


class Config:
    """Backend configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccmux_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        local_settings = Path("settings.env")
        global_settings = self.config_dir / "settings.env"
        if local_settings.is_file():
            load_dotenv(local_settings)
            logger.debug("Loaded settings from %s", local_settings.resolve())
        if global_settings.is_file():
            load_dotenv(global_settings)
            logger.debug("Loaded settings from %s", global_settings)

        # Reserved tmux session name — holds the bot process itself.
        self.tmux_session_name = os.getenv("CCMUX_TMUX_SESSION_NAME", "__ccmux__")

        # Claude command to run in new windows
        self.claude_command = os.getenv("CCMUX_CLAUDE_COMMAND", "claude")

        self.instances_file = self.config_dir / "claude_instances.json"
        self.monitor_state_file = self.config_dir / "claude_monitor.json"

        custom_projects_path = os.getenv("CCMUX_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(
            os.getenv("CCMUX_MONITOR_POLL_INTERVAL", "0.5")
        )

        logger.debug(
            "Config initialized: dir=%s, tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.tmux_session_name,
            self.claude_projects_path,
        )


config = Config()
```

Diff vs old: removes `show_user_messages` field entirely; switches both `load_dotenv` calls from `.env` to `settings.env`; updates module docstring.

- [ ] **Step 2: Run tests**

```bash
uv run pytest -q
```

Expected: PASS. If any test referenced `config.show_user_messages`, drop the reference (it would have failed compile-time as `AttributeError`).

- [ ] **Step 3: Pyright**

```bash
uv run pyright src/
```

Expected: 0 errors.

- [ ] **Step 4: Commit**

```bash
git add src/ccmux/config.py
git commit -m "refactor!: drop show_user_messages, switch loader to settings.env

Backend config no longer exposes show_user_messages — that toggle
moves to the frontend. Backend has no secrets, so the loader now
reads settings.env exclusively (never .env).

BREAKING: config.show_user_messages is removed.
BREAKING: env loader no longer reads ~/.ccmux/.env. Existing
deployments must move CCMUX_* lines into ~/.ccmux/settings.env.
See spec for migration commands."
```

### Task 6: Merge feature → dev

**Files:** none.

- [ ] **Step 1: Run pre-push checks**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest -q
```

Expected: all PASS / "All checks passed!" / "0 errors".

- [ ] **Step 2: Merge to dev**

```bash
git checkout dev
git merge --no-ff feature/extract-show-user-messages
```

Expected: a merge commit with default message `Merge branch 'feature/extract-show-user-messages' into dev`.

- [ ] **Step 3: Delete the feature branch**

```bash
git branch -d feature/extract-show-user-messages
```

---

## Phase 2: Backend release v5.0.0 (`release/v5.0.0`)

Cut from `dev` after Phase 1.

### Task 7: Cut release branch + bump version + CHANGELOG

**Files:**
- Modify: `pyproject.toml` (line: `version = "4.0.1"`)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Cut release branch**

```bash
git checkout -b release/v5.0.0 dev
```

- [ ] **Step 2: Bump version in pyproject.toml**

Edit `pyproject.toml`: change `version = "4.0.1"` to `version = "5.0.0"`.

- [ ] **Step 3: Add CHANGELOG entry**

Insert immediately under the `## [Unreleased]` line:

```markdown
## [Unreleased]

## 5.0.0 — 2026-05-05

Major version bump. Two BREAKING changes shipped together for a
single migration cost.

### Removed

- `Config.show_user_messages` and `CCMUX_SHOW_USER_MESSAGES` env var.
- `MessageMonitor.__init__` no longer accepts `show_user_messages`.
- `DefaultBackend.__init__` no longer accepts `show_user_messages`.
- Loader no longer reads `~/.ccmux/.env`. Backend reads only
  `settings.env` (cwd, then `$CCMUX_DIR/settings.env`).

### Changed

- User-typed messages (JSONL `role=="user"`) are always emitted as
  `ClaudeMessage` events. Frontends decide whether to display.

### Migration

```bash
# Move CCMUX_* lines from .env into a new settings.env, then drop them
# from .env. Run before upgrading to v5.0.0.
grep '^CCMUX_' ~/.ccmux/.env > ~/.ccmux/settings.env
sed -i '/^CCMUX_/d' ~/.ccmux/.env
```

Frontend package `ccmux-telegram` v5.1.0+ depends on this version.
```

(Keep the existing `## 4.0.1` entry below.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to 5.0.0 and update CHANGELOG"
```

### Task 8: Pre-push checks

**Files:** none.

- [ ] **Step 1: Run all checks**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest -q
```

Expected: ALL PASS.

### Task 9: Merge release → main + tag + back-merge dev + push

**Files:** none.

- [ ] **Step 1: Merge release → main**

```bash
git checkout main
git merge --no-ff release/v5.0.0
```

Expected: merge commit with default message `Merge branch 'release/v5.0.0'`.

- [ ] **Step 2: Tag**

```bash
git tag v5.0.0 -m "v5.0.0: drop show_user_messages, split env to settings.env"
```

- [ ] **Step 3: Back-merge release → dev**

```bash
git checkout dev
git merge --no-ff release/v5.0.0
```

- [ ] **Step 4: Delete release branch**

```bash
git branch -d release/v5.0.0
```

- [ ] **Step 5: Push**

```bash
git push origin main dev --tags
```

Expected: `main`, `dev`, `v5.0.0` all pushed. No force needed (forward progress only).

---

## Phase 3: Frontend feature branch (`feature/filter-user-messages-and-split-env`)

Switch to telegram repo. Cut from `dev`.

### Task 10: Cut feature branch

**Files:** none.

- [ ] **Step 1: Switch to telegram repo + dev**

```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux/ccmux-telegram
git checkout dev
git log --oneline -1
```

Expected: HEAD is `2f9f20f chore: bump version to 5.0.0 and update CHANGELOG` (the v5.0.0 commit on telegram, already shipped).

- [ ] **Step 2: Cut feature branch**

```bash
git checkout -b feature/filter-user-messages-and-split-env
```

### Task 11: Add `show_user_messages` to `Config` and split env loader

**Files:**
- Modify: `src/ccmux_telegram/config.py`

- [ ] **Step 1: Replace the loader block + add the new field**

Replace the existing loader block in `Config.__init__` (the block that loads `local_env` / `global_env`) with the following four-file load:

```python
        # Settings (non-sensitive) — cwd then global
        local_settings = Path("settings.env")
        global_settings = self.config_dir / "settings.env"
        if local_settings.is_file():
            load_dotenv(local_settings)
            logger.debug("Loaded settings from %s", local_settings.resolve())
        if global_settings.is_file():
            load_dotenv(global_settings)
            logger.debug("Loaded settings from %s", global_settings)

        # Secrets — cwd then global
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded secrets from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded secrets from %s", global_env)
```

(Replaces the existing two-file loader; `python-dotenv`'s default `override=False` means earlier-loaded values win, so cwd > global is preserved within each kind.)

- [ ] **Step 2: Add `show_user_messages` field**

Insert near the other display-toggle fields (after `self.show_skill_bodies = ...`):

```python
        # Whether user-typed messages emitted by the backend should be
        # rendered to Telegram (echoed with 👤 prefix). Default true:
        # the user types in CC, the bot relays the prompt to Telegram so
        # mobile-side users see what was sent. Set false to suppress.
        self.show_user_messages = (
            os.getenv("CCMUX_SHOW_USER_MESSAGES", "true").lower() != "false"
        )
```

- [ ] **Step 3: Update module docstring** (top of file, the `.env loading priority: ...` line)

Change:

```python
.env loading priority: local `.env` (cwd) > `$CCMUX_DIR/.env`.
```

to:

```python
Loads `settings.env` (operational toggles) and `.env` (secrets) as
two separate files. Within each kind: cwd > `$CCMUX_DIR/`. The two
kinds are loaded in order settings.env → .env so secrets and
settings cannot accidentally cross-contaminate environment lookup.
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest -q
```

Expected: PASS (no test depends yet on the new field).

- [ ] **Step 5: Commit**

```bash
git add src/ccmux_telegram/config.py
git commit -m "feat(config): split loader into settings.env + .env, add show_user_messages

settings.env (operational, non-sensitive CCMUX_*) and .env (secrets:
TELEGRAM_BOT_TOKEN, ALLOWED_USERS, OPENAI_*) are now separate files.
Both are loaded; cwd local file shadows ~/.ccmux/ global within
each kind.

Adds show_user_messages config field (default true), in preparation
for filtering user-message events that the backend now always emits."
```

### Task 12: Filter user messages in `handle_new_message` (TDD)

**Files:**
- Create: `tests/test_message_in_user_filter.py`
- Modify: `src/ccmux_telegram/message_in.py` around line 147 (top of `handle_new_message`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_message_in_user_filter.py
"""User-message filter in handle_new_message (CCMUX_SHOW_USER_MESSAGES)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import ClaudeMessage


def _make_topic(user_id: int = 1, window_id: str = "@5", thread_id: int = 42):
    topic = MagicMock()
    topic.user_id = user_id
    topic.window_id = window_id
    topic.thread_id = thread_id
    topic.group_chat_id = 100
    return topic


def _make_user_msg() -> ClaudeMessage:
    return ClaudeMessage(
        session_id="sess-1",
        role="user",
        text="hello from cc",
        is_complete=True,
    )


def _make_assistant_msg() -> ClaudeMessage:
    return ClaudeMessage(
        session_id="sess-1",
        role="assistant",
        text="hi back",
        is_complete=True,
    )


@pytest.fixture
def cfg(monkeypatch):
    from ccmux_telegram import message_in

    config_mock = MagicMock()
    config_mock.show_tool_calls = True
    config_mock.show_thinking = True
    config_mock.show_skill_bodies = False
    config_mock.tool_calls_allowlist = frozenset({"Skill"})
    config_mock.show_user_messages = True
    monkeypatch.setattr(message_in, "config", config_mock)
    return config_mock


class TestUserMessageFilter:
    @pytest.mark.asyncio
    async def test_user_message_dropped_when_show_user_messages_false(self, cfg):
        cfg.show_user_messages = False

        from ccmux_telegram import message_in

        topic = _make_topic()
        with patch.object(message_in, "get_topic_for_claude_session", return_value=topic):
            with patch.object(
                message_in, "enqueue_content_message", new=AsyncMock()
            ) as eq:
                await message_in.handle_new_message("sess-1", _make_user_msg(), MagicMock())
        assert eq.await_count == 0

    @pytest.mark.asyncio
    async def test_user_message_emitted_when_show_user_messages_true(self, cfg):
        cfg.show_user_messages = True

        from ccmux_telegram import message_in

        topic = _make_topic()
        with patch.object(message_in, "get_topic_for_claude_session", return_value=topic):
            with patch.object(
                message_in, "enqueue_content_message", new=AsyncMock()
            ) as eq:
                await message_in.handle_new_message("sess-1", _make_user_msg(), MagicMock())
        assert eq.await_count >= 1

    @pytest.mark.asyncio
    async def test_assistant_message_unaffected_by_toggle(self, cfg):
        cfg.show_user_messages = False  # toggle off should not affect assistant

        from ccmux_telegram import message_in

        topic = _make_topic()
        with patch.object(message_in, "get_topic_for_claude_session", return_value=topic):
            with patch.object(
                message_in, "enqueue_content_message", new=AsyncMock()
            ) as eq:
                await message_in.handle_new_message(
                    "sess-1", _make_assistant_msg(), MagicMock()
                )
        assert eq.await_count >= 1
```

- [ ] **Step 2: Run test, verify it fails on the "filter off" case**

```bash
uv run pytest tests/test_message_in_user_filter.py -v
```

Expected: `test_user_message_dropped_when_show_user_messages_false` FAILS (the filter does not exist yet, so the user message gets enqueued).

- [ ] **Step 3: Add the filter to `handle_new_message`**

In `src/ccmux_telegram/message_in.py`, locate the body of `handle_new_message`. Right after the `status = "complete" if msg.is_complete else "streaming"` log line and before `topic = get_topic_for_claude_session(...)`, insert:

```python
    # Frontend-side filter: backend always emits user messages; we drop
    # them here when the user has opted out (CCMUX_SHOW_USER_MESSAGES=false).
    if msg.role == "user" and not config.show_user_messages:
        return
```

The exact insertion point is just before `# Find the bound topic for this Claude session`.

- [ ] **Step 4: Run tests, verify all pass**

```bash
uv run pytest tests/test_message_in_user_filter.py -v
```

Expected: 3/3 PASS.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_message_in_user_filter.py src/ccmux_telegram/message_in.py
git commit -m "feat(message_in): filter user messages when show_user_messages is false

Backend in v5.0.0 stops filtering user-typed messages. The frontend
takes ownership of the toggle: at the top of handle_new_message,
drop role=user messages when config.show_user_messages is false.
Default true preserves the current echo-to-Telegram behavior."
```

### Task 13: Bump backend pin

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Update the pin**

Change `"ccmux>=4.0.0,<5.0.0"` to `"ccmux>=5.0.0,<6.0.0"`.

- [ ] **Step 2: Run tests**

```bash
uv run pytest -q
```

Expected: PASS. The editable backend install at `../ccmux-backend` is already at v5.0.0 from Phase 2.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(deps): require ccmux>=5.0.0,<6.0.0"
```

### Task 14: Merge feature → dev

**Files:** none.

- [ ] **Step 1: Pre-push checks**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest -q
```

Expected: ALL PASS.

- [ ] **Step 2: Merge feature → dev**

```bash
git checkout dev
git merge --no-ff feature/filter-user-messages-and-split-env
```

- [ ] **Step 3: Delete feature branch**

```bash
git branch -d feature/filter-user-messages-and-split-env
```

---

## Phase 4: Frontend release v5.1.0 (`release/v5.1.0`)

Cut from `dev` after Phase 3.

### Task 15: Cut release + bump version + CHANGELOG

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Cut release branch**

```bash
git checkout -b release/v5.1.0 dev
```

- [ ] **Step 2: Bump version in pyproject.toml**

Edit `pyproject.toml`: change `version = "5.0.0"` to `version = "5.1.0"`.

- [ ] **Step 3: Add CHANGELOG entry**

Insert immediately under the `## [Unreleased]` line:

```markdown
## [Unreleased]

## 5.1.0 — 2026-05-05

### Added

- `CCMUX_SHOW_USER_MESSAGES` env var (default `true`). When false,
  `handle_new_message` drops `role==user` messages so they are not
  echoed to Telegram. Replaces the same-named backend toggle that
  was removed in `ccmux` v5.0.0.

### Changed

- Env loader now reads two files by purpose:
  - `~/.ccmux/.env` for secrets (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`,
    `OPENAI_API_KEY`, `OPENAI_BASE_URL`)
  - `~/.ccmux/settings.env` for operational settings (all `CCMUX_*`)
  
  cwd-local files (`./.env`, `./settings.env`) shadow the global
  ones within each kind. Migration command for existing deployments:

  ```bash
  grep '^CCMUX_' ~/.ccmux/.env > ~/.ccmux/settings.env
  sed -i '/^CCMUX_/d' ~/.ccmux/.env
  ```

- Backend dependency pin: `ccmux>=4.0.0,<5.0.0` → `ccmux>=5.0.0,<6.0.0`.
```

(Keep the `## 5.0.0` entry below intact.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to 5.1.0 and update CHANGELOG"
```

### Task 16: Pre-push checks + merge release → main + tag + back-merge dev + push

**Files:** none.

- [ ] **Step 1: Pre-push checks**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest -q
```

Expected: ALL PASS.

- [ ] **Step 2: Merge release → main**

```bash
git checkout main
git merge --no-ff release/v5.1.0
```

- [ ] **Step 3: Tag**

```bash
git tag v5.1.0 -m "v5.1.0: filter user messages on frontend, split env into secrets+settings"
```

- [ ] **Step 4: Back-merge release → dev**

```bash
git checkout dev
git merge --no-ff release/v5.1.0
```

- [ ] **Step 5: Delete release branch**

```bash
git branch -d release/v5.1.0
```

- [ ] **Step 6: Push**

```bash
git push origin main dev --tags
```

---

## Phase 5: User migration + bot restart

### Task 17: Migrate `~/.ccmux/.env`

**Files:**
- Modify: `~/.ccmux/.env`
- Create: `~/.ccmux/settings.env`

- [ ] **Step 1: Backup**

```bash
cp ~/.ccmux/.env ~/.ccmux/.env.pre-v5-split-bak
```

- [ ] **Step 2: Move CCMUX_* lines into settings.env**

```bash
grep '^CCMUX_' ~/.ccmux/.env > ~/.ccmux/settings.env
sed -i '/^CCMUX_/d' ~/.ccmux/.env
```

- [ ] **Step 3: Verify**

```bash
echo "--- ~/.ccmux/.env (secrets only) ---"
grep -v '^#' ~/.ccmux/.env | grep -v '^$' | sed 's/=.*/=***/'
echo "--- ~/.ccmux/settings.env (settings only) ---"
cat ~/.ccmux/settings.env
```

Expected: `.env` shows only `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, optionally `OPENAI_*`. `settings.env` shows all `CCMUX_*` lines that used to be in `.env`.

### Task 18: Restart bot + verify

**Files:** none.

- [ ] **Step 1: Stop the running bot**

```bash
pgrep -f "ccmux-telegram$" | xargs -r kill -KILL
sleep 3
pgrep -f "ccmux-telegram$" | grep -v grep || echo "bot stopped"
```

- [ ] **Step 2: Start in the existing tmux pane**

```bash
tmux send-keys -t __ccmux__:1.1 'ccmux-telegram' Enter
sleep 8
```

- [ ] **Step 3: Verify startup logs**

```bash
pgrep -af "ccmux-telegram$" | grep -v grep
tail -20 ~/.ccmux/ccmux.log | grep -E "Backend started|Fast poll loop started|TimedOut|Error"
```

Expected: a fresh PID running. Logs show `Backend started` and `Fast poll loop started`. No `TimedOut` / `Error` from startup.

- [ ] **Step 4: Verify a Telegram inbound + outbound round-trip**

Send a message from Telegram (or have the user send "ping"). Check the bot relays it to tmux. Check that a CC reply gets echoed back.

```bash
# After the user sends a message
tail -30 ~/.ccmux/ccmux.log | grep -E "handle_new_message|Enqueue content"
```

Expected: `handle_new_message [complete]: ...` for the inbound, and `Enqueue content` for the outbound echo.

If `show_user_messages=true` (default), the user-typed echo should still appear in Telegram with `👤` prefix.

---

## Self-Review (post-write)

- **Spec coverage:** every spec section has tasks. Migration section covered by Task 17. Versioning covered by Tasks 7, 15. Test sections covered by Tasks 2, 11. Loader sections covered by Tasks 5, 10. Filter sections covered by Tasks 3, 11.
- **Placeholder scan:** no "TBD"/"TODO"/"similar to". Code blocks present where steps mutate code.
- **Type consistency:** `MessageMonitor.__init__` signature change is consistent across Tasks 3 and 4. `DefaultBackend.__init__` consistent with backend.py grep in Task 4. `config.show_user_messages` removed in Task 5 and added in Task 11 (different repos, intentionally).
- **Cross-references:** Task 13 (frontend pin bump) requires Task 9 (backend release+push) to have completed first — ordering is enforced by phase numbering.

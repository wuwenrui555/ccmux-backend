<!-- markdownlint-disable MD024 -->

# Extract `show_user_messages` and Split Env File Design (v5.0.0 / v5.1.0)

- **Date**: 2026-05-05
- **Repos affected**: `ccmux-backend` (major, v5.0.0), `ccmux-telegram` (minor, v5.1.0)
- **Status**: design accepted; implementation pending

## Problem

Two unrelated env-config issues, batched into one design because they share migration cost (single `~/.ccmux/.env` rewrite, single bot restart).

### Issue 1: `CCMUX_SHOW_USER_MESSAGES` is misplaced

`ccmux.config.Config.show_user_messages` lives in the backend and gates whether `MessageMonitor` emits an `on_message` event when a JSONL transcript line has `role=="user"`. Backend's own comment on the field reads "Frontends often prefer to drop these (they echoed them already)". The decision "should this event be displayed at all?" is presentation policy, not a backend concern; backend should always emit and let each frontend decide.

This is the only such layering leak in the project. All other rendering toggles (`CCMUX_SHOW_TOOL_CALLS`, `CCMUX_SHOW_THINKING`, `CCMUX_SHOW_SKILL_BODIES`, etc.) already live on the frontend. Closing this one fully separates the two layers.

### Issue 2: secrets and operational settings live in the same file

`~/.ccmux/.env` mixes two kinds of values:

- **Secrets**: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`. Must not leave the host. Cannot be committed to a dotfiles repo, synced via mackup, or shared.
- **Operational settings**: `CCMUX_SHOW_*`, `CCMUX_TMUX_SESSION_NAME`, `CCMUX_MONITOR_POLL_INTERVAL`, `CCMUX_CLAUDE_COMMAND`, `CCMUX_CLAUDE_PROJECTS_PATH`, `CCMUX_TOOL_CALLS_ALLOWLIST`, etc. Non-sensitive; could be committed / synced freely if separable.

A single file means the user cannot syncthing/mackup the settings without also syncing the bot token.

## Goals

1. Move `show_user_messages` from backend (`ccmux.config`, `MessageMonitor`, `DefaultBackend`) to frontend (`ccmux_telegram.config`, `handle_new_message`).
2. Backend always emits user-message events; frontend filters in `handle_new_message` based on its own config.
3. Split `~/.ccmux/.env` into two files by purpose:
   - `~/.ccmux/.env` → secrets only.
   - `~/.ccmux/settings.env` → all `CCMUX_*` operational settings.
4. Each loader reads only the files it needs:
   - Backend: `settings.env` only (no secrets to read).
   - Frontend: both files.
5. Strict file-purpose convention. No fallback or backward-compat code; one-shot migration before upgrade.

## Non-goals

- **Not adding validation** that prevents misplaced variables (e.g., a `TELEGRAM_BOT_TOKEN` line accidentally placed in `settings.env`). Convention-only; YAGNI.
- **Not changing default behavior**. `show_user_messages` still defaults to `true` so the user-visible echo behavior remains identical after upgrade.
- **Not touching any other env var.** Inventory was reviewed; `show_user_messages` is the only layering leak. The other backend env vars (`CCMUX_TMUX_SESSION_NAME`, `CCMUX_MONITOR_POLL_INTERVAL`, etc.) are correctly placed and stay put.
- **Not introducing a new file format** (no TOML, no YAML). Stays `.env` so existing `python-dotenv` tooling keeps working.

## Architecture

### Data flow change

Before:

```text
JSONL → MessageMonitor [filter role=="user" if not show_user_messages]
      → on_message event
      → frontend handle_new_message
      → render
```

After:

```text
JSONL → MessageMonitor [no filter]
      → on_message event (always emitted, including role=="user")
      → frontend handle_new_message [drop if role=="user" and not show_user_messages]
      → render
```

Backend becomes a pure event source. Frontend owns presentation policy end-to-end.

### File layout

```text
~/.ccmux/
├── .env              ← secrets only (TELEGRAM_BOT_TOKEN, ALLOWED_USERS, OPENAI_*)
└── settings.env      ← settings only (all CCMUX_*)
```

### Loader behavior

| Package | Loads `.env`? | Loads `settings.env`? |
|---|---|---|
| `ccmux` (backend) | no | yes |
| `ccmux-telegram` (frontend) | yes | yes |

Each `Config.__init__` calls `load_dotenv` on the files it needs. cwd-local files (`./.env`, `./settings.env`) are loaded before `~/.ccmux/` globals to preserve the existing dev-override pattern. Backend never touches `.env`, so secrets cannot leak into Claude child processes via backend code paths.

## Components

### `ccmux-backend` (v5.0.0, BREAKING)

| File | Change |
|---|---|
| `src/ccmux/config.py` | Remove `show_user_messages` field. Replace `.env` loader with `settings.env` loader (cwd `settings.env` then `~/.ccmux/settings.env`). |
| `src/ccmux/message_monitor.py` | Remove `show_user_messages` constructor param, `_show_user_messages` field, and the `if entry.role == "user" and not self._show_user_messages: continue` guard. User messages are always emitted. |
| `src/ccmux/backend.py` | Remove `show_user_messages` from `DefaultBackend.__init__` signature; stop forwarding to `MessageMonitor`. |
| `tests/test_message_monitor.py` (or wherever) | Delete tests that exercised the filter; verify user messages now flow through unconditionally. |
| `CHANGELOG.md` | v5.0.0 entry: BREAKING (constructor and config-field removal); migration note. |
| `pyproject.toml` | Bump version to `5.0.0`. |

### `ccmux-telegram` (v5.1.0)

| File | Change |
|---|---|
| `src/ccmux_telegram/config.py` | Add `show_user_messages = (os.getenv("CCMUX_SHOW_USER_MESSAGES", "true").lower() != "false")`. Loader now reads four files in order: cwd `settings.env`, `~/.ccmux/settings.env`, cwd `.env`, `~/.ccmux/.env`. |
| `src/ccmux_telegram/message_in.py` | At the top of `handle_new_message`, drop the message if `msg.role == "user" and not config.show_user_messages`. |
| `tests/test_message_in_user_filter.py` (new) | Two cases: filter off + role=user → not enqueued; filter on (default) → enqueued. |
| `pyproject.toml` | Bump backend pin from `ccmux>=4.0.0,<5.0.0` to `ccmux>=5.0.0,<6.0.0`. Bump frontend version to `5.1.0`. |
| `CHANGELOG.md` | v5.1.0 entry: feature add (frontend filter) + dep bump + env-file-split note. |

## Migration

User runs once before upgrade (or as part of upgrade):

```bash
# Move CCMUX_* lines out of .env into a new settings.env, then delete them from .env
grep '^CCMUX_' ~/.ccmux/.env > ~/.ccmux/settings.env
sed -i '/^CCMUX_/d' ~/.ccmux/.env
```

After migration:

- `~/.ccmux/.env` contains `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, optionally `OPENAI_*`.
- `~/.ccmux/settings.env` contains every `CCMUX_*` line that used to be in `.env`.

If user upgrades without migrating: `settings.env` does not exist, `python-dotenv` silently skips, and `CCMUX_*` vars stay in `.env`. Backend will not find them (does not load `.env`), so backend defaults take effect for `CCMUX_TMUX_SESSION_NAME`, `CCMUX_MONITOR_POLL_INTERVAL`, etc. Frontend still picks them up because frontend loads both files. **This silent half-broken state is the cost of "no backward compat";** it is expected, documented, and recoverable by running the migration commands.

CHANGELOG records the migration step at the top of both v5.0.0 (backend) and v5.1.0 (frontend) entries.

## Error handling

- Missing `settings.env`: `python-dotenv` silently skips; no error raised. Backend uses defaults; frontend uses defaults for `CCMUX_*` it cares about.
- Missing `.env` in frontend: existing behavior — `Config.__init__` raises `ValueError` for missing `TELEGRAM_BOT_TOKEN` / `ALLOWED_USERS`. No change.
- Misplaced variable (e.g., `CCMUX_SHOW_TOOL_CALLS` left in `.env`): both files are still loaded by frontend, so the var is still in `os.environ`; frontend reads it normally. Cost is only the convention violation. Not policed.

## Testing

### Backend

- Update or remove `test_message_monitor.py` cases that asserted `show_user_messages=False` skips entries. New invariant: every JSONL `role=="user"` line emits an event.
- Update tests that constructed `MessageMonitor` or `DefaultBackend` with `show_user_messages=` keyword (the kwarg is gone).
- `Config` test (if any) for `settings.env` loading: verify `CCMUX_*` from `~/.ccmux/settings.env` are picked up; `.env` is **not** read by backend.

### Frontend

- New `tests/test_message_in_user_filter.py`:
  - role=user + `show_user_messages=False` → `handle_new_message` does not call `enqueue_content_message`.
  - role=user + `show_user_messages=True` → does call.
  - role=assistant: unaffected by the toggle.
- `Config` test: split-file loading. Verify `TELEGRAM_BOT_TOKEN` from `.env`, `CCMUX_SHOW_TOOL_CALLS` from `settings.env`, both picked up.

### Pre-push (per `managing-git-branches` skill)

For each repo before pushing:

1. `uv run ruff check src/ tests/`
2. `uv run ruff format --check src/ tests/`
3. `uv run pyright src/`
4. `uv run pytest`

## Versioning

- **Backend** is BREAKING (constructor signature + config field removal): `4.0.1 → 5.0.0`.
- **Frontend** adds a feature and bumps its backend pin (`<5.0.0` → `<6.0.0`); user-visible behavior unchanged at default settings: `5.0.0 → 5.1.0`.

Order of release matters: backend v5.0.0 first (frontend cannot pin it otherwise). Frontend v5.1.0 immediately after.

## Git-flow

Per the `managing-git-branches` skill.

### Backend

```text
feature/extract-show-user-messages  (from dev)
  ├── implement removal + settings.env loader
  ├── update tests
  └── merge --no-ff → dev

release/v5.0.0  (from dev)
  ├── bump pyproject + CHANGELOG
  ├── pre-push checks
  ├── merge --no-ff → main
  ├── git tag v5.0.0 -m "..."
  ├── merge --no-ff → dev
  └── push origin main dev --tags
```

### Frontend

```text
feature/filter-user-messages-and-split-env  (from dev)
  ├── add filter, split loader, bump backend pin
  ├── update tests
  └── merge --no-ff → dev

release/v5.1.0  (from dev)
  ├── bump pyproject + CHANGELOG
  ├── pre-push checks
  ├── merge --no-ff → main
  ├── git tag v5.1.0 -m "..."
  ├── merge --no-ff → dev
  └── push origin main dev --tags
```

### After release

- Run migration commands on user's `~/.ccmux/`.
- Restart bot via tmux pane `__ccmux__:1.1`.
- Verify `handle_new_message` still echoes user-typed CC messages with `👤` prefix in Telegram (current behavior at `show_user_messages=true` default).

## Open questions

None. Spec accepted as written.

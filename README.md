# ccmux

The Claude–tmux bridge: a backend library that mirrors Claude Code sessions running inside `tmux` windows into a small, stable Python API.

`ccmux` does not talk to any chat platform. It monitors tmux panes, parses Claude Code's JSONL transcripts, tracks tool_use / tool_result pairing, and exposes a single `Backend` Protocol that any frontend (Telegram bot, CLI, web UI) can drive.

## Components

Everything below is exported from `ccmux.api`. Anything imported from submodules (`ccmux.tmux`, `ccmux.backend`, …) is internal and may change without notice.

### Lifecycle

- `Backend` — Protocol with `tmux: TmuxOps` and `claude: ClaudeOps` sub-protocols.
- `TmuxOps` — Tmux-side operations sub-protocol.
- `ClaudeOps` — Claude-side operations sub-protocol.
- `DefaultBackend` — Default `Backend` implementation; compose with `tmux_registry` and a `ClaudeInstanceRegistry`.
- `get_default_backend` / `set_default_backend` — Process-wide singleton accessors.

### State

- `ClaudeState` — Sealed union pushed per instance via the `on_state` callback.
- `Working` / `Idle` / `Blocked` / `Dead` — `ClaudeState` variants.
- `BlockedUI` — StrEnum identifying which Blocked UI is on screen (`Blocked.ui`).

### Messages

- `ClaudeMessage` — One parsed message; `text` is standard CommonMark.
- `TranscriptParser` — JSONL → `ClaudeMessage` stream; thinking, tool_result, and long output go inside `>` blockquotes for collapsible-UI rendering.

### Instances

- `ClaudeInstance` — Backend view of one running Claude Code process (`instance_id`, `window_id`, `session_id`, `cwd`).
- `ClaudeInstanceRegistry` — Persistent `instance_id → ClaudeInstance` map at `~/.ccmux/claude_instances.json`; populated by the `ccmux hook` CLI on Claude Code `SessionStart`.
- `ClaudeSession` — Summary of a Claude Code JSONL session file (`session_id`, `summary`, `message_count`, `file_path`).

### Tmux

- `TmuxSessionRegistry` / `tmux_registry` — Multi-session tmux orchestration (the singleton you compose into `DefaultBackend`).
- `TmuxWindow` — Window query return type, identified by `window_id` (e.g. `@5`).
- `sanitize_session_name` — Helper that produces a tmux-safe session name.

### Parser surfaces

Lower-level helpers for frontends that capture panes themselves rather than going through the backend's emit loop:

- `InteractiveUIContent` — Parsed Blocked-UI payload.
- `UsageInfo` — Parsed `/usage` modal contents.
- `extract_bash_output` — Pull `! cmd` output out of a captured pane.
- `extract_interactive_content` — Parse a Blocked overlay.
- `parse_status_line` — Parse the spinner / working status line.
- `parse_usage_output` — Parse the `/usage` modal capture.

## Usage

### 1. Install the hook

Required for either frontend. Auto-install with:

```bash
ccmux hook --install
```

This registers `ccmux hook` as Claude Code's `SessionStart` callback so the instance map gets populated.

### 2. Choose a frontend

This library does not run on its own. It needs a frontend that consumes the `Backend` Protocol.

#### 2.1 Reference frontend

> [!NOTE]
> Want a ready-made Telegram bot? See [GitHub - wuwenrui555/ccmux-telegram](https://github.com/wuwenrui555/ccmux-telegram).

#### 2.2 Custom frontend

Depend on ccmux as a git URL:

```toml
# pyproject.toml
dependencies = [
    "ccmux @ git+https://github.com/wuwenrui555/ccmux-backend.git@main",
]
```

For reproducible builds, pin to a release tag (e.g. `@v2.5.1`) instead of `@main`. See the [Releases page](https://github.com/wuwenrui555/ccmux-backend/releases).

A minimum frontend looks like:

```python
import asyncio
from ccmux.api import (
    DefaultBackend, ClaudeInstanceRegistry, tmux_registry,
    ClaudeMessage, ClaudeState,
)

async def on_message(instance_id: str, msg: ClaudeMessage) -> None:
    print(f"[{instance_id}] [{msg.role}] {msg.text}")

async def on_state(instance_id: str, state: ClaudeState) -> None:
    print(f"[{instance_id}] state -> {state}")

async def main() -> None:
    backend = DefaultBackend(
        tmux_registry=tmux_registry,
        claude_instances=ClaudeInstanceRegistry(),
    )
    await backend.start(on_state=on_state, on_message=on_message)
    try:
        await asyncio.Event().wait()
    finally:
        await backend.stop()

asyncio.run(main())
```

## Environment variables

Set in `$CCMUX_DIR/.env` (default `~/.ccmux/.env`) or your shell. A local `.env` in the cwd takes precedence.

- `CCMUX_DIR` (default `~/.ccmux`) — state-file root
- `CCMUX_TMUX_SESSION_NAME` (default `__ccmux__`) — tmux session your frontend runs in; backend skips it when listing windows so it's never treated as a Claude session
- `CCMUX_CLAUDE_COMMAND` (default `claude`) — command to launch Claude Code
- `CCMUX_CLAUDE_PROJECTS_PATH` — where Claude Code writes its JSONL transcripts. Falls back to `$CLAUDE_CONFIG_DIR/projects` (Claude Code's own var, useful for Claude variants like cc-mirror), then to `~/.claude/projects`.
- `CCMUX_SHOW_USER_MESSAGES` (default `true`) — emit user-typed messages as events
- `CCMUX_MONITOR_POLL_INTERVAL` (default `0.5`) — fast-loop tick in seconds
- `CCMUX_CLAUDE_PROC_NAMES` (default `claude,node`) — comma-separated pane foreground process names counted as "Claude is alive". Override if a Claude Code release switches runtimes (e.g. to Bun) and the liveness checker starts flagging every window as dead. See [Claude Code compatibility](docs/claude-code-compat.md).

`DefaultBackend(show_user_messages=…)` takes precedence over the env var.

## State files (under `$CCMUX_DIR`, default `~/.ccmux/`)

### Backend

- `claude_instances.json` / `claude_instances.lock` — instance registry; written by the `ccmux hook` CLI on Claude Code `SessionStart`
- `claude_monitor.json` — per-session JSONL byte offsets, written by `MessageMonitor`
- `drift.log` — created on first pane-parser drift warning (Claude Code UI change alert)
- `hook.log` — appended by the `ccmux hook` CLI on every invocation; captures unhandled tracebacks for postmortems after Claude Code's inline error banner scrolls away
- `parser_config.json` — optional; overrides brittle Claude Code parser constants without a backend release. See [Claude Code compatibility](docs/claude-code-compat.md).

### Frontends

See e.g. [GitHub - wuwenrui555/ccmux-telegram](https://github.com/wuwenrui555/ccmux-telegram).

- `ccmux.log` — runtime log
- `topic_bindings.json` — topic ↔ session bindings
- `images/` — downloaded photos

## Development policy

The `ccmux.api` surface is **frozen at v2.0**. Day-to-day feature work
— new Telegram commands, new inbound flows, richer rendering, rate
limiting, retries — should happen in the **frontend** (e.g.
`ccmux-telegram`) rather than here.

The backend only changes for one of these reasons:

- A Claude Code release broke a parser or changed the JSONL / hook
  contract (see [Claude Code compatibility](docs/claude-code-compat.md)).
- A confirmed backend bug (race, leak, logical error in the Protocol
  implementation).
- A deliberate major bump — any signature or semantic change to
  anything re-exported from `ccmux.api` requires a **new major
  version**.

If you find yourself wanting to add a frontend-facing feature to the
backend, that's a signal to add it to the frontend instead.

## Claude Code compatibility

Claude Code evolves its pane UI, JSONL schema, and hook API between
releases. The modules most likely to break — and the safety net
(`~/.ccmux/drift.log`) that surfaces those breaks — are documented in
[`docs/claude-code-compat.md`](docs/claude-code-compat.md). Start there
whenever a Claude Code upgrade causes the bot to misbehave.

## License

MIT (see `LICENSE`).

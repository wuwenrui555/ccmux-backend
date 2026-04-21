# ccmux

The Claude–tmux bridge: a backend library that mirrors Claude Code sessions running inside `tmux` windows into a small, stable Python API.

`ccmux` does not talk to any chat platform. It monitors tmux panes, parses Claude Code's JSONL transcripts, tracks tool_use / tool_result pairing, and exposes a single `Backend` Protocol that any frontend (Telegram bot, CLI, web UI) can drive.

## What's in the box

- `Backend` Protocol with `tmux: TmuxOps` and `claude: ClaudeOps` sub-protocols, plus `DefaultBackend` implementation
- `TmuxSessionRegistry` — multi-session tmux orchestration (`tmux_registry` singleton)
- `ClaudeInstanceRegistry` — `instance_id → (session_id, cwd)` map, backed by `~/.ccmux/claude_instances.json`
- `ClaudeState` — sealed union (`Working | Idle | Blocked | Dead`) pushed per instance via `on_state` callback
- `MessageMonitor` — byte-offset incremental JSONL reader with tool_use / tool_result pairing
- `StateMonitor` — tmux pane capture + status line / interactive UI parsing, emits `ClaudeState` observations
- `TranscriptParser` — JSONL-to-`ClaudeMessage` parser; emits standard Markdown (including `>` blockquotes for collapsible regions)
- `ccmux hook` CLI — Claude Code `SessionStart` hook that populates the instance map

## Public API

Everything a frontend needs lives at `ccmux.api`:

```text
ccmux.api exports (after v2.0.0)
─────────────────────────────────────────
Protocol + lifecycle
  Backend, TmuxOps, ClaudeOps
  DefaultBackend, get_default_backend, set_default_backend
State family
  ClaudeState (sealed union of Working | Idle | Blocked | Dead)
  Working, Idle, Blocked, Dead  — variants
  BlockedUI  — StrEnum for Blocked.ui
Message / transcript
  ClaudeMessage, TranscriptParser
Instance model
  ClaudeInstance, ClaudeInstanceRegistry, ClaudeSession
Composition inputs
  TmuxSessionRegistry
Parser surfaces
  InteractiveUIContent, UsageInfo
  extract_bash_output, extract_interactive_content
  parse_status_line, parse_usage_output
Query types
  TmuxWindow
Composition helpers
  tmux_registry, sanitize_session_name
```

Anything imported from submodules (`ccmux.tmux`, `ccmux.backend`, …) is internal and may change without notice. Consumers outside the library should pin to `ccmux.api` only.

## Install

```bash
uv add ccmux  # or: pip install ccmux
```

Configure once:

```bash
# ~/.claude/settings.json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "ccmux hook", "timeout": 5 }] }
    ]
  }
}
```

Or auto-install: `ccmux hook --install`.

## Minimal frontend shape

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

## Message rendering

`ClaudeMessage.text` is **standard CommonMark Markdown**. Tool results, thinking blocks, and long command outputs use `>` blockquotes for regions that a rich frontend may want to render as collapsible UI. Plain-text frontends display them as readable quoted lines.

## State files (under `~/.ccmux/`, overridable with `CCMUX_DIR`)

- `claude_instances.json` — written by the `ccmux hook` CLI on Claude Code `SessionStart`
- `claude_monitor.json` — per-session JSONL byte offsets, written by `MessageMonitor`
- `drift.log` — created on first pane-parser drift warning (Claude Code UI change alert)
- `hook.log` — appended by the `ccmux hook` CLI on every invocation; captures unhandled tracebacks for postmortems after Claude Code's inline error banner scrolls away
- `parser_config.json` — optional; overrides brittle Claude Code parser constants without a backend release. See [Claude Code compatibility](docs/claude-code-compat.md).

## Environment variables

- `TMUX_SESSION_NAME` (default `__ccmux__`) — reserved session holding the frontend process itself
- `CLAUDE_COMMAND` (default `claude`) — command to launch Claude Code
- `CCMUX_CLAUDE_PROJECTS_PATH` / `CLAUDE_CONFIG_DIR` — where Claude Code writes its JSONL transcripts
- `MONITOR_POLL_INTERVAL` (default `0.5`) — fast-loop tick in seconds
- `CCMUX_DIR` (default `~/.ccmux`) — state-file root
- `CCMUX_SHOW_USER_MESSAGES` (default `true`) — emit user-typed messages as events
- `CCMUX_CLAUDE_PROC_NAMES` (default `claude,node`) — comma-separated pane foreground process names counted as "Claude is alive". Override if a Claude Code release switches runtimes (e.g. to Bun) and the liveness checker starts flagging every window as dead. See [Claude Code compatibility](docs/claude-code-compat.md).

`DefaultBackend(show_user_messages=…)` takes precedence over the env var.

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

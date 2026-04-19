# ccmux

The Claude-tmux bridge: a backend library that mirrors Claude Code sessions running inside `tmux` windows into a small, stable Python API.

`ccmux` does not talk to any chat platform. It monitors tmux panes, parses Claude Code's JSONL transcripts, tracks tool_use / tool_result pairing, and exposes a single `ClaudeBackend` Protocol that any frontend (Telegram bot, CLI, web UI) can drive.

## What's in the box

- `ClaudeBackend` Protocol + `DefaultClaudeBackend` implementation
- `TmuxManagerRegistry` — multi-session tmux orchestration
- `WindowRegistry` — `window_id → (session_id, cwd)` map, backed by `~/.ccmux/tmux_claude_map.json`
- `MessageMonitor` — byte-offset incremental JSONL reader with tool-use / tool-result pairing
- `StatusMonitor` — tmux pane capture + status line / interactive UI parsing
- `TranscriptParser` — JSONL-to-ClaudeMessage parser
- `ccmux hook` CLI — Claude Code `SessionStart` hook that populates the window map

## Public API

Everything a frontend needs lives at `ccmux.api`:

```python
from ccmux.api import (
    ClaudeBackend,          # Protocol
    DefaultClaudeBackend,   # default implementation
    ClaudeMessage,          # event payload
    WindowStatus,           # event payload
    WindowInfo,             # window map entry
    TranscriptParser,       # JSONL parser
    parse_status_line,      # pane text → status spinner line
    extract_interactive_content,  # pane text → interactive UI
    registry,               # TmuxManagerRegistry singleton
    # ...
)
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

## State files (under `~/.ccmux/`, overridable with `CCMUX_DIR`)

- `tmux_claude_map.json` — written by the `ccmux hook` CLI on Claude Code `SessionStart`
- `claude_monitor.json` — per-session JSONL byte offsets, written by `MessageMonitor`

## License

MIT (see `LICENSE`).

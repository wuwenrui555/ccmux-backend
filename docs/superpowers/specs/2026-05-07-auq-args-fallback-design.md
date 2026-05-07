<!-- markdownlint-disable MD024 -->

# AskUserQuestion / ExitPlanMode args fallback design

- **Date**: 2026-05-07
- **Repos affected**: `ccmux-backend` (minor), `ccmux-telegram` (minor)
- **Status**: design accepted; implementation pending

## Problem

When the assistant calls a "blocking prompt" tool (`AskUserQuestion`,
`ExitPlanMode`), Claude Code renders an interactive UI in its tmux pane and
suspends until the user answers. Telegram should mirror that UI as inline
buttons so the user can answer from the chat. Today the path is:

1. `MessageMonitor` reads the JSONL `tool_use` entry → emits `ClaudeMessage`.
2. `ccmux_telegram.message_in` recognises `tool_name in PROMPT_TOOL_NAMES` and
   calls `handle_interactive_ui`.
3. `handle_interactive_ui` captures the tmux pane via libtmux,
   parses the captured text with `claude_code_state.extract_interactive_content`,
   and renders the resulting `BlockedUI` + content as inline buttons.

Step 3 fails for several different reasons that all look the same to the
parser ("no interactive UI in pane text"):

- **Pane scroll race.** The user is scrolled up in their tmux session
  (copy-mode) when the bot ticks. `tmux capture-pane` defaults to the visible
  region, so the bot captures the scrolled view, which doesn't include the
  live input chrome.
- **Fast-answer race.** The user answers the prompt directly in the TUI
  faster than the bot's tick (fast_tick = 0.5 s, but the args path has its own
  300 ms sleep). By the time `handle_interactive_ui` reads the pane, CC has
  already accepted the answer and dropped the AUQ frame.
- **Capture timing within the prompt's lifecycle.** Claude Code may write the
  `tool_use` JSONL line a few hundred ms before the AUQ frame fully renders;
  in some pane states the in-between view does not match any UI pattern.

When step 3 fails, Telegram silently drops the prompt: the user never sees a
button, and there is no fallback. They have to answer in the TUI directly.

This is observable in the field; e.g. the bash-utils-related `AskUserQuestion`
on 2026-05-07 at 01:09:21 (and again at 01:38:43 after the v5.1.0 restart) had
a `tool_use` line in JSONL but produced no `Dispatch pending: state=Blocked`
log entry on the Telegram side and no inline-button message in the chat.

## Goals

1. When `handle_interactive_ui` cannot extract a UI from the pane, fall back
   to rendering the prompt directly from the JSONL `tool_use` input args.
2. Cover both `AskUserQuestion` (questions + options + multi-select) and
   `ExitPlanMode` (plan markdown).
3. Keep the pane-capture path as the **primary** route — its output is what
   the parser of `claude_code_state` produces, includes any tool-preview
   header that CC renders above the prompt, and stays consistent with how
   permission prompts (which have no corresponding `tool_use`) are rendered
   today.
4. No change to backend `Blocked` state observation logic. No synthetic
   `on_state` emissions; no race with `state_monitor`'s fast tick.
5. Public `ccmux.api` surface stays compatible (`ClaudeMessage` adds an
   optional field; existing `__all__` is unchanged).

## Non-goals

- **Not** fixing the underlying `tmux capture-pane` scroll race
  (a separate followup; see "Related followups").
- **Not** handling `PERMISSION_PROMPT`, `BashApproval`, `RestoreCheckpoint`,
  or `Settings` BlockedUIs in this fallback. Those are not driven by an
  assistant-side `tool_use`, so there is no JSONL args payload to fall back
  to.
- **Not** managing the late-click footgun (user answers via the TUI
  directly, then later taps the Telegram button; bot's `tmux send-keys`
  delivers the answer text into a now-idle prompt). See "Known footgun".
- **Not** unifying `PROMPT_TOOL_NAMES` into a shared module across the two
  repos. A two-line frozenset is repeated in each side; that is cheaper than
  a new shared dependency.
- **Not** carrying tool args for non-prompt tools (Edit / Bash / Read /
  Write / etc.). Those payloads can be large and sometimes sensitive
  (file contents, raw shell commands); backend whitelists the prompt tools
  only.

## Architecture

### Data flow

```text
JSONL tool_use ─► MessageMonitor ─► ClaudeMessage(tool_name=AUQ,
                                                  input={questions: [...]})
                                                              │
                                                              ▼
                          telegram.message_in: recognises PROMPT_TOOL_NAMES
                                                              │
                                                              ▼
                          handle_interactive_ui(...,
                                                tool_name=msg.tool_name,
                                                tool_use_args=msg.input)
                                                              │
                  ┌───────────────────────────────────────────┤
                  ▼                                           ▼
          capture pane ─ extract_interactive_content        (else)
                  │                                           │
        ┌─────────┴─────────┐                                 ▼
        ▼                   ▼                  args fallback (NEW)
     extracted          extracted=None         _render_from_tool_args
        │                   │                                 │
        └────────►   render with `ui_name`, `text`   ◄─────────┘
                                  │
                                  ▼
                     existing inline-keyboard build
```

The new branch attaches at the existing `extracted is None → return False`
exit. Pane-capture remains the primary path; args fallback only fires when
the pane path produces nothing.

### Backend (`ccmux-backend`)

**`ClaudeMessage` adds an optional field:**

```python
@dataclass
class ClaudeMessage:
    session_id: str
    role: Literal["user", "assistant"]
    content_type: Literal["text", "thinking", "tool_use", "tool_result", "local_command"]
    text: str
    tool_use_id: str | None = None
    tool_name: str | None = None
    input: dict | None = None       # NEW — raw tool args, populated only for prompt tools
    image_data: list[tuple[str, bytes]] | None = None
    timestamp: str | None = None
    is_complete: bool = False
```

**`TranscriptParser` populates `input` only for whitelisted tools:**

```python
# Module-level constant in claude_transcript_parser.py
_PROMPT_TOOL_INPUT_PASSTHROUGH = frozenset({"AskUserQuestion", "ExitPlanMode"})

# In the existing tool_use construction (around line 593-616):
input_passthrough = (
    inp if isinstance(inp, dict) and name in _PROMPT_TOOL_INPUT_PASSTHROUGH
    else None
)
result.append(
    ClaudeMessage(
        session_id=session_id,
        role="assistant",
        text=summary,
        content_type="tool_use",
        tool_use_id=tool_id or None,
        tool_name=name,
        input=input_passthrough,
        timestamp=entry_timestamp,
    )
)
```

The whitelist mirrors `ccmux_telegram.prompt_state.PROMPT_TOOL_NAMES`. They
are intentionally duplicated; they only need to stay in sync when CC
introduces a new prompt tool, which is a once-a-year event.

`ccmux.api` re-exports `ClaudeMessage` unchanged in `__all__`.
The new field is part of the dataclass and therefore visible to consumers,
but adding an optional field is a non-breaking change for `frozen=False`
dataclasses (which `ClaudeMessage` is).

### Telegram (`ccmux-telegram`)

**`handle_interactive_ui` (in `prompt.py`) gains two kwargs and a fallback
branch:**

```python
async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    chat_id: int | None = None,
    *,
    ui: BlockedUI | None = None,
    content: str | None = None,
    tool_name: str | None = None,        # NEW
    tool_use_args: dict | None = None,   # NEW
) -> bool:
    if chat_id is None:
        return False

    if ui is not None and content is not None:
        ui_name, text = ui.value, content
    else:
        ui_name = text = None
        # Primary: capture pane and extract.
        tm = tmux_registry.get_by_window_id(window_id)
        if tm:
            w = await tm.find_window_by_id(window_id)
            if w:
                pane_text = await tm.capture_pane(w.window_id)
                if pane_text:
                    extracted = extract_interactive_content(pane_text)
                    if extracted:
                        ui_name = extracted.ui.value
                        text = extracted.content

        # Fallback: render from tool_use args.
        if ui_name is None and tool_name in PROMPT_TOOL_NAMES and tool_use_args:
            ui_name, text = _render_from_tool_args(tool_name, tool_use_args)

        if ui_name is None or text is None:
            return False

    # ... existing keyboard / send-or-edit code unchanged ...
```

**`_render_from_tool_args(tool_name, args) -> tuple[str, str]`:**

| `tool_name`        | `args` shape                                                                                              | Output `(ui_name, text)`                                                                          |
| ------------------ | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `AskUserQuestion`  | `{"questions": [{"question": str, "header": str?, "options": [{"label": str, "description": str}], "multiSelect": bool?}, ...]}` | `("ask_user_question", "<questions joined>\n\n<options enumerated 1.…N. with em-dashed descriptions>")` |
| `ExitPlanMode`     | `{"plan": str}`                                                                                           | `("exit_plan_mode", args["plan"])`                                                                |

The text shape mimics what `claude_code_state.extract_interactive_content`
emits today (verified against the AUQ snapshot test in section "Verification")
so downstream `_format_blocked_content` / `_render_mdv2` paths see the same
structure either way. Helper lives in `prompt.py` next to the existing render
helpers.

**`message_in.py` passes the two new kwargs when invoking the prompt path:**

```python
# Around line 184 in handle_message — current call:
handled = await handle_interactive_ui(
    bot, user_id, wid, thread_id, chat_id=topic.group_chat_id,
    tool_name=msg.tool_name,           # NEW
    tool_use_args=msg.input,           # NEW
)
```

`prompt.py` `set_interactive_mode` and other callbacks that re-enter
`handle_interactive_ui` (refresh callback at line 307/314/345) keep passing
`None` for the new kwargs — they go through the pane-capture path as today.

### Inline keyboard

`_build_interactive_keyboard(window_id, ui_name=ui_name)` is unchanged.
`ui_name` is a string (`"ask_user_question"` / `"exit_plan_mode"`) and the
keyboard builder already maps these to button templates. Whether the value
came from `extracted.ui.value` or `_render_from_tool_args` is invisible to
the keyboard layer.

## Known footgun (deliberately not handled)

If the user answers an AUQ in the TUI (typing the option number directly)
**before** they tap the Telegram button, the bot's `tmux send-keys` callback
will deliver "1\r" (or similar) into a now-idle Claude Code prompt — the user
ends up with a stray `1` typed into chat input.

Mitigation would require tracking AUQ lifecycle via `tool_result` events and
deleting / disabling the Telegram interactive message once the corresponding
`tool_result` arrives. That is a separate followup; this design only fixes
the "prompt never appears in Telegram" half of the problem.

## Verification

1. `uv sync` succeeds in both repos.
2. `uv run pytest` is green in both repos.
3. `uv run pyright src` is clean in both repos.
4. Live smoke: assistant calls `AskUserQuestion`; user scrolls up in tmux
   (forces `extract_interactive_content` to fail on the captured pane);
   Telegram still receives an inline-button message with the question and
   options; tapping a button delivers the answer back to the assistant.
5. Live smoke: assistant calls `ExitPlanMode`; same scrolled-pane scenario;
   Telegram receives the plan body with Approve / Cancel inline buttons.
6. Negative: assistant calls a non-prompt tool (Edit, Bash); `ClaudeMessage`
   for that `tool_use` has `input is None`. Confirms the whitelist works.

## Tests

| File | What it covers |
| ---  | ---            |
| `ccmux-backend/tests/test_claude_transcript_parser.py` | New cases: parsing a JSONL line with an `AskUserQuestion` tool_use produces `ClaudeMessage.input == <expected dict>`; same for `ExitPlanMode`; parsing a `Read` / `Bash` / `Edit` tool_use produces `ClaudeMessage.input is None`. |
| `ccmux-telegram/tests/test_prompt.py` (or new `test_prompt_args_fallback.py`) | (a) pane capture returns no UI + valid `tool_name` + `tool_use_args` → returns True, calls `_render_from_tool_args`; (b) pane capture returns no UI + missing `tool_use_args` → returns False; (c) `_render_from_tool_args("AskUserQuestion", <fixture>)` returns expected `(ui_name, text)`; (d) same for `ExitPlanMode`. |

## Release flow

Two coordinated minor bumps:

- `ccmux-backend`: 5.1.0 → 5.1.1 (or 5.2.0 if we treat the `ClaudeMessage`
  field addition as worth signposting; recommend **5.1.1**, since it is a
  pure additive change with no consumer impact).
- `ccmux-telegram`: 5.1.0 → 5.2.0 (user-visible behaviour change: prompts
  that previously dropped now render).

Both go through gitflow as separate `feature/...` → `dev` → `release/...` →
`main` cycles. Backend ships first (its change is independent and unblocks
telegram); telegram lifts its `ccmux>=5.1.1` floor in `pyproject.toml` and
then ships.

## Related followups (out of scope here, recorded for visibility)

- **`tmux capture-pane` scroll robustness.** `ccmux.tmux.capture_pane` should
  pass `-S - -E -` (or at least `-E -`) so a user in copy-mode scroll does
  not feed parsers a stale visible frame. This affects every Blocked-UI
  detection, not just AUQ; fixing it would also retire the
  `Implementing via sub-agent…` drift entries we saw on 2026-05-07.
- **Sub-agent / TodoWrite UI parser coverage.** When CC renders a long-running
  Task with the `* spinner` + `⎿ TodoWrite` block plus a horizontal-rule
  task header, the bottom of the pane shows only `❯ ` (chrome compressed).
  `claude_code_state.has_input_chrome` returns False; the parser then hunts
  for a BlockedUI and drifts. Either teach `has_input_chrome` to recognise
  the compressed form, or add a non-blocking pattern that classifies this
  pane as Working. Lives upstream in `claude_code_state`.
- **AUQ tool_result lifecycle handling.** Track `tool_result` for a
  previously-rendered AUQ; delete or disable the Telegram interactive
  message when the answer arrives, so a late tap can no longer leak a
  stray digit into the chat input.

# Integration prompts

Hand-curated prompts that drive a live Claude Code session into
specific UI states so the full stack (CC → tmux → `ccmux-backend` →
frontend) can be exercised against real pane output — not hand-forged
strings.

## Why this file exists

Unit tests in [`tests/test_tmux_pane_parser.py`](../tests/test_tmux_pane_parser.py)
guard against regressions on *known* pane shapes, but they have a
blind spot: if Claude Code reworks its rendering and the real output
no longer matches the hard-coded fixture, the unit test keeps
passing — against a snapshot of yesterday's CC. These prompts let
Claude re-run the full stack at any time to check the assumption is
still true.

Runbook per entry (Claude executes; user only reads the summary):

1. `/clear` the designated test CC session so prior state doesn't
   contaminate the capture.
2. Send the prompt to the test pane and press Enter.
3. Wait for CC to render the target state.
4. Run the single **Verify** shell block; it fails fast on the first
   layer that doesn't match.
5. Report per-layer `PASS` / `FAIL` plus actual output on failure.

## Conventions

- Placeholder names (`foo`, `bar`) over real repo names — these
  prompts should work on any machine.
- Each entry is self-contained; don't assume state from a previous
  prompt.
- Prompts are English, single-message, self-contained. Keep-alive
  instructions belong inside the prompt, not as external tooling.
- Cite the unit fixture derived from the capture so the unit test
  and the integration prompt stay linked.

---

## Working + TodoWrite overflow tail

**Target state:** spinner line above a checklist whose length exceeds
Claude Code's render window, producing a trailing (indented)
`… +N pending[, M completed]` overflow row.

**Exercises:**
[`tmux_pane_parser.parse_status_line`](../src/ccmux/tmux_pane_parser.py)
skip coverage for checkbox rows, elbow connector, and the overflow
tail. Without the tail-skip rule, `parse_status_line` bails on the
`…` row and returns `None`, so the frontend sees IDLE while CC is
actively working.

**Prep:** send `/clear` to the test pane and wait for the prompt to
clear before sending the main prompt.

**Prompt** (send as a single message):

```text
Create exactly 12 TodoWrite tasks for planning a refactor of a Python
package `foo`. Cover: types layer, data layer, parser, state machine,
monitor, backend, public API, legacy-module cleanup, version bump,
CHANGELOG, README, tests. Do not merge, shorten, or drop any.

Then work through the plan one task at a time. For each: mark it
`in_progress`, analyze what the refactor of that layer would involve
in at least three paragraphs of detailed reasoning (interleaved
thinking is fine), mark it `completed`, pick the next. Use no tools
other than TodoWrite.

Do not look for the `foo` package on disk — it is a hypothetical
planning exercise, not a real codebase.
```

**Prompt design notes:**

- No `Bash(sleep N)` loop: Claude Code's harness blocks standalone
  long sleeps (`Blocked: standalone sleep 60. Use Monitor ...`), so
  the keep-alive has to come from CC's own thinking time. Three
  paragraphs of analysis per task sustains the `✻ Thinking…` spinner
  long enough to capture multiple times across 12 iterations.
- No meta-language (`load-test`, `I'm testing`, `treat this as an
  instruction`): CC sessions sharing `MEMORY.md` with a meta-testing
  session can pick up that context and ask for confirmation instead
  of executing. The prompt reads as a normal planning request.

**Verify** (Claude runs; user reads summary):

```bash
set -e
TEST_PANE="${TEST_PANE:-test}"
BOT_PANE="${BOT_PANE:-__ccmux__:1.1}"
BACKEND_DIR="${BACKEND_DIR:-$HOME/projects/ccmux-backend}"

# Layer 1 — current visible pane shows spinner + overflow tail.
# Do NOT include scrollback: `-S -N` would match historical spinners
# from earlier turns and false-positive when CC is actually idle.
pane="$(tmux capture-pane -p -t "$TEST_PANE")"
echo "$pane" | grep -qE '(✶|✽|✻|✢|✳|·).+…'  || { echo "Layer 1 FAIL: no spinner"; exit 1; }
echo "$pane" | grep -qE '… \+[0-9]+ (pending|completed)' || { echo "Layer 1 FAIL: no overflow tail"; exit 1; }
echo "Layer 1 PASS"

# Layer 2 — backend parser returns the spinner text
parsed="$(echo "$pane" | uv run --project "$BACKEND_DIR" python -c \
    'import sys; from ccmux.tmux_pane_parser import parse_status_line; r = parse_status_line(sys.stdin.read()); print(r if r else "__NONE__")')"
case "$parsed" in
    __NONE__|"") echo "Layer 2 FAIL: parse_status_line returned None"; exit 1 ;;
    *…*)         echo "Layer 2 PASS: $parsed" ;;
    *)           echo "Layer 2 FAIL: missing ellipsis: $parsed"; exit 1 ;;
esac

# Layer 3 — ccmux-telegram enqueued the status update
tmux capture-pane -p -t "$BOT_PANE" -S -500 \
  | grep "Enqueue status_update" | tail -3 \
  | grep -q "text='" || { echo "Layer 3 FAIL: no recent Enqueue status_update"; exit 1; }
echo "Layer 3 PASS"
```

**Unit fixture derived from this prompt:**
`tests/test_tmux_pane_parser.py::TestParseStatusLine::test_realistic_long_todowrite_pane`

**Category:** manual integration (CC → tmux → parser → frontend).

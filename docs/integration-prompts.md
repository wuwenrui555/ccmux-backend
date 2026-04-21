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
Use your task tracking tool (TodoWrite or TaskCreate, whatever is
available) to create exactly 12 tasks for planning a refactor of a
Python package `foo`. Cover: types, data, parser, state machine,
monitor, backend, public API, legacy cleanup, version bump,
CHANGELOG, README, tests. Do not merge, shorten, or drop any.

Then cycle: mark task 1 in_progress, do extensive interleaved
thinking (at least 2000 tokens) about that layer, mark it completed,
move to next. No visible response text between task-state changes.
Use only the task tool. Do not touch the filesystem, `foo` does not
exist.
```

**Prompt design notes:**

- **Tool-agnostic phrasing.** Different Claude Code harnesses expose
  the task tracker under different names (`TodoWrite` in some, the
  deferred `TaskCreate` / `TaskUpdate` in others). Naming the canonical
  one and allowing either keeps the prompt working across harness
  setups.
- **No visible text between tool calls.** Any response text Claude
  produces pushes the checklist out of the `parse_status_line` scan
  window (it only looks at lines above the chrome separator). Pure
  interleaved thinking keeps the checklist visible next to chrome
  while the spinner is still active.
- **≥2000 tokens of thinking per task** forces each window to last
  ~30–60s instead of the 2–3s Claude would otherwise spend. Across
  12 tasks this gives ~8 minutes of accumulated in-state dwell time,
  plenty of chances for the sampling verify to hit.
- **No `Bash(sleep N)` loop.** Claude Code's harness blocks standalone
  long sleeps (`Blocked: standalone sleep 60. Use Monitor ...`). Even
  the allowed `until`-loop form streams output that would push the
  checklist up and out of scan range.
- **No meta-language** (`load-test`, `I'm testing`, `treat this as a
  load-test instruction`): Claude Code sessions that share
  `MEMORY.md` with a meta-testing session can pick up that context
  and stop to ask for confirmation. The prompt reads as an ordinary
  planning request.

**Verify** (Claude runs; user reads summary):

The target state — spinner + checklist + overflow tail rendered
together — is transient. It exists only between two task-tracker
calls while Claude is thinking. Layers 1 and 2 therefore sample the
pane up to 15 times at 1-second intervals; the first sample that
matches wins. Layer 3 is a one-shot log grep since events stay in
scrollback.

Expected Layer 2 output shape (post-v2.4.0):

```text
Spinner text… (…)
  [>] Task in progress
  [ ] Task pending
  ~~[x] Task completed~~
  … +N pending, M completed
```

Rows start with two spaces and an ASCII bracket. Completed rows are
wrapped in GitHub-flavored double tildes so Telegram's MarkdownV2
pipeline renders them with native strikethrough. The `[>]` arrow
marks the in-progress row.

```bash
set -e
TEST_PANE="${TEST_PANE:-test}"
BOT_PANE="${BOT_PANE:-__ccmux__:1.1}"
BACKEND_DIR="${BACKEND_DIR:-$HOME/projects/ccmux-backend}"
MAX_SAMPLES="${MAX_SAMPLES:-15}"

# Always stop the test CC's cycle on exit so it does not keep
# consuming tokens after the report. Fires on pass, fail, or
# interrupt — use trap rather than a trailing command.
cleanup() {
    tmux send-keys -t "$TEST_PANE" Escape 2>/dev/null || true
    sleep 1
    tmux send-keys -t "$TEST_PANE" "/clear" Enter 2>/dev/null || true
    echo "Test session cleared."
}
trap cleanup EXIT

# Layer 1 + 2 — sample the live pane until the target state is
# captured (or the attempt budget runs out). Layer 1 gates on
# spinner + overflow tail both being visible; Layer 2 then feeds
# that same pane snapshot into parse_status_line and checks the
# returned string carries both the spinner ellipsis AND at least one
# ASCII-bracket task row (distinguishes normalized v2.4.0 output
# from legacy single-line spinner).
#
# The parser runs from a temporary file to dodge uv run's own
# stderr noise (`Uninstalled N packages...`) leaking into `$parsed`.
l12_pass=0
for attempt in $(seq 1 "$MAX_SAMPLES"); do
    pane="$(tmux capture-pane -p -t "$TEST_PANE")"
    if echo "$pane" | grep -qE '(✶|✽|✻|✢|✳|·).+…' \
       && echo "$pane" | grep -qE '… \+[0-9]+ (pending|completed)'; then
        pane_file="$(mktemp)"
        printf '%s' "$pane" > "$pane_file"
        parsed="$(uv run --project "$BACKEND_DIR" --quiet python -c \
            "import sys; from ccmux.tmux_pane_parser import parse_status_line; r = parse_status_line(open(sys.argv[1]).read()); print(r if r else '__NONE__')" \
            "$pane_file")"
        rm -f "$pane_file"
        case "$parsed" in
            *…*\[[\ \>x]\]*)
                echo "Layer 1 PASS (sample $attempt/$MAX_SAMPLES)"
                echo "Layer 2 PASS:"
                printf '%s\n' "$parsed" | sed 's/^/  | /'
                l12_pass=1
                break
                ;;
        esac
    fi
    sleep 1
done
[ "$l12_pass" = 1 ] || { echo "Layer 1/2 FAIL after $MAX_SAMPLES samples"; exit 1; }

# Layer 3 — ccmux-telegram enqueued the status update. Scrollback is
# fine here: the log is append-only, we just want to see a recent entry.
tmux capture-pane -p -t "$BOT_PANE" -S -500 \
  | grep "Enqueue status_update" | tail -3 \
  | grep -q "text='" || { echo "Layer 3 FAIL: no recent Enqueue status_update"; exit 1; }
echo "Layer 3 PASS"
```

**Unit fixture derived from this prompt:**
`tests/test_tmux_pane_parser.py::TestParseStatusLine::test_realistic_long_todowrite_pane`

**Category:** manual integration (CC → tmux → parser → frontend).

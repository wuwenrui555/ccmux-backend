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
passing — against a snapshot of yesterday's CC. These prompts let a
human re-run the full stack at any time to check the assumption is
still true.

Runbook per entry:

1. Start a fresh CC session in a known tmux pane.
2. Send the prompts in order; wait for CC to respond between each.
3. Once CC renders the target state, run every command under
   **Verify**.
4. If a check fails on current CC but unit tests still pass, the
   unit fixture has drifted — refresh it from a new capture.

## Conventions

- Placeholder names (`foo`, `bar`) over real repo names — these
  prompts should work on any machine.
- Each entry is self-contained; don't assume state from a previous
  prompt.
- "Keep CC in state" instructions go inside the prompt sequence, not
  as external keep-alive — the sequence is the reproducible unit.
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

**Prompt sequence:**

1. Queue up 12 tasks, stop before executing:

   ```text
   我要你重构一个叫 foo 的 Python 包，具体步骤很多，每一步都必须独立做完
   再进下一步。请先用 TodoWrite 列出至少 12 个明确的子任务（不要合并不要
   省略），覆盖：类型层、数据层、解析层、状态机、监听、后端、公开 API、
   删除遗留模块、版本号、CHANGELOG、README、测试。列完之后不要急着执行，
   先卡住等我确认。
   ```

2. Hold CC in Working without touching the filesystem:

   ```text
   不用真改 foo 包（它不存在），但请把任务列表保持为运行状态：每推进一条
   任务前 sleep 60 秒再继续，只做 TodoWrite 状态切换，不碰文件系统。
   ```

**Verify — check all three layers:**

1. **Pane capture** — spinner line and the overflow tail are both
   present in the live pane:

   ```bash
   tmux capture-pane -p -t <your-test-pane> \
     | grep -E '(✶|✽|✻|✢|✳|·).+…|… \+\d+ pending'
   ```

   Two matching lines = CC is in the target state.

2. **Backend parser** — confirms the v2.2.1 fix still applies (old
   versions returned `None` here):

   ```bash
   tmux capture-pane -p -t <your-test-pane> \
     | uv run --project ~/projects/ccmux-backend python -c \
       'import sys; from ccmux.tmux_pane_parser import parse_status_line; print(parse_status_line(sys.stdin.read()))'
   ```

   Expect a non-`None` string ending in `…` — e.g. `Nesting… (12s · thinking)`.

3. **Frontend log** — the status reached `ccmux-telegram` and was
   enqueued for delivery:

   ```bash
   tmux capture-pane -p -t __ccmux__:1.1 -S -200 \
     | grep "Enqueue status_update.*text='"
   ```

   Expect a recent entry (timestamp newer than when you sent prompt 2)
   with `text='<spinner text>'` matching what the parser returned.

**Unit fixture derived from this prompt:**
`tests/test_tmux_pane_parser.py::TestParseStatusLine::test_realistic_long_todowrite_pane`

**Category:** manual integration (CC → tmux → parser → frontend).

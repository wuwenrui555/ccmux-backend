# Hook PID-Based Session Resolution Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Claude Code hands the ccmux SessionStart hook empty/invalid stdin (notably on `/clear` in v2.1.x), recover `session_id` and `cwd` via a PID-based fallback so `window_bindings.json` stays current.

**Architecture:** Add a fallback helper chain inside `src/ccmux/hook.py`. When stdin is empty or missing `session_id`/`cwd`, the hook: (1) reads the pane's shell PID from `tmux display-message -p '#{pane_pid}'`, (2) finds the child `claude` process via `pgrep -P` + `/proc/<pid>/cmdline`, (3) reads the launch `cwd` from `~/.claude/sessions/<claude_pid>.json` (never updated on `/clear`, so stable), (4) encodes that `cwd` to Claude Code's project-dir naming by replacing `/`, `_`, `.` with `-`, and (5) picks the newest `*.jsonl` in `~/.claude/projects/<encoded>/` — its basename is the current `session_id`. The fallback fails silently (debug log only) if any step can't resolve, so the hook never crashes or prints to stderr on the happy path.

**Tech Stack:** Python 3.12, stdlib `json`/`re`/`subprocess`/`pathlib`, pytest with `monkeypatch` + `tmp_path`, existing `_UUID_RE`/`_PANE_RE` patterns in `hook.py`.

**Background:** Earlier in the conversation we verified on the live system:
- Empty stdin produces log line `Failed to parse stdin JSON: Expecting value: line 1 column 1 (char 0)` in `hook.log` — exit code 0 but Claude Code surfaces it as a banner, and worse, `window_bindings.json` stops tracking the new session after `/clear`.
- `~/.claude/sessions/<claude_pid>.json` is **not** updated on `/clear` — `sessionId` stays stale but `cwd` remains correct.
- The project-dir encoding `re.sub(r"[/_.]", "-", cwd)` was verified against every directory under `~/.claude/projects/` on this machine (plain path, `.claude` dot, `obsidian_notes` and `aclf_review` underscores, nested paths).
- The newest `*.jsonl` in the project dir is the post-`/clear` session: birth time precedes hook-fire time, so there is no creation race.

---

## File Structure

Files modified:
- `src/ccmux/hook.py` — add three module-private helpers (`_encode_project_dir`, `_find_claude_pid`, `_resolve_session_via_pid`), one new regex constant (`_SESSION_FILE_RE`), and rewire `_hook_main_impl` to call the fallback when stdin doesn't provide a usable session.
- `tests/test_hook.py` — add unit tests for each helper plus integration tests that exercise the empty-stdin path of `hook_main`.

No new files. No deletions. `pyproject.toml` / version / `CHANGELOG.md` are out of scope — batch with the next release when the user chooses to cut one.

---

## Task 1: Confirm baseline

**Files:** none modified; verification only.

- [ ] **Step 1: Confirm clean working tree on dev**

Run:
```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-backend
git status --short
git branch --show-current
```

Expected: empty status output; branch is `dev`. If dirty, STOP and resolve.

- [ ] **Step 2: Run the current hook test suite to establish a green baseline**

Run:
```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-backend
uv run pytest tests/test_hook.py -v
```

Expected: all tests pass (no failures). Note the test count so the new tests are obviously additive.

---

## Task 2: Add `_encode_project_dir` helper (TDD)

**Files:**
- Modify: `src/ccmux/hook.py` (add helper near `_UUID_RE`)
- Modify: `tests/test_hook.py` (new `TestEncodeProjectDir` class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_hook.py`:

```python
from ccmux.hook import _encode_project_dir


class TestEncodeProjectDir:
    @pytest.mark.parametrize(
        "cwd, expected",
        [
            ("/mnt/md0/home/wenruiwu", "-mnt-md0-home-wenruiwu"),
            ("/mnt/md0/home/wenruiwu/.claude", "-mnt-md0-home-wenruiwu--claude"),
            (
                "/mnt/md0/home/wenruiwu/obsidian_notes",
                "-mnt-md0-home-wenruiwu-obsidian-notes",
            ),
            (
                "/mnt/md0/home/wenruiwu/projects/aclf_review",
                "-mnt-md0-home-wenruiwu-projects-aclf-review",
            ),
            ("/tmp", "-tmp"),
        ],
        ids=["plain", "dotfile", "underscore", "nested-underscore", "short"],
    )
    def test_matches_claude_project_dir_naming(
        self, cwd: str, expected: str
    ) -> None:
        assert _encode_project_dir(cwd) == expected
```

Also add `_encode_project_dir` to the import at the top of the file:
```python
from ccmux.hook import _UUID_RE, _encode_project_dir, _is_hook_installed, hook_main
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_hook.py::TestEncodeProjectDir -v
```

Expected: collection failure — `ImportError: cannot import name '_encode_project_dir'`.

- [ ] **Step 3: Implement `_encode_project_dir`**

In `src/ccmux/hook.py`, directly below the existing `_PANE_RE` line (~line 38), add:

```python
# Claude Code derives its per-project transcript directory from the launch
# cwd by replacing every `/`, `_`, and `.` with `-`. Verified against the
# full listing of `~/.claude/projects/` on a live system.
def _encode_project_dir(cwd: str) -> str:
    """Return the `~/.claude/projects/<encoded>` basename for a launch cwd."""
    return re.sub(r"[/_.]", "-", cwd)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_hook.py::TestEncodeProjectDir -v
```

Expected: 5/5 pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "feat(hook): add _encode_project_dir helper for PID fallback"
```

---

## Task 3: Add `_find_claude_pid` helper (TDD)

**Files:**
- Modify: `src/ccmux/hook.py` (new helper below `_encode_project_dir`)
- Modify: `tests/test_hook.py` (new `TestFindClaudePid` class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_hook.py`:

```python
from ccmux.hook import _find_claude_pid


class TestFindClaudePid:
    def _mock_pgrep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        children: list[int],
        returncode: int = 0,
    ) -> None:
        """Make subprocess.run return the given children for any pgrep call."""
        result = MagicMock()
        result.stdout = "\n".join(str(c) for c in children) + (
            "\n" if children else ""
        )
        result.returncode = returncode
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

    def _mock_cmdline(
        self, monkeypatch: pytest.MonkeyPatch, mapping: dict[int, str]
    ) -> None:
        """Make `/proc/<pid>/cmdline` reads return mapping[pid] as argv0."""
        from ccmux import hook as hook_mod

        def fake_read_bytes(self: "Path") -> bytes:  # type: ignore[name-defined]
            parts = self.parts
            assert parts[1] == "proc" and parts[-1] == "cmdline", self
            pid = int(parts[2])
            if pid not in mapping:
                raise FileNotFoundError(str(self))
            return mapping[pid].encode() + b"\0--dangerously-skip-permissions\0"

        monkeypatch.setattr(hook_mod.Path, "read_bytes", fake_read_bytes)

    def test_returns_claude_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [12345])
        self._mock_cmdline(monkeypatch, {12345: "claude"})
        assert _find_claude_pid(shell_pid=999) == 12345

    def test_returns_claude_when_argv0_is_absolute_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [12345])
        self._mock_cmdline(monkeypatch, {12345: "/usr/local/bin/claude"})
        assert _find_claude_pid(shell_pid=999) == 12345

    def test_skips_non_claude_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [100, 200, 300])
        self._mock_cmdline(
            monkeypatch,
            {100: "vim", 200: "node", 300: "claude"},
        )
        assert _find_claude_pid(shell_pid=999) == 300

    def test_returns_none_when_pgrep_has_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [], returncode=1)
        assert _find_claude_pid(shell_pid=999) is None

    def test_returns_none_when_no_child_is_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pgrep(monkeypatch, [100, 200])
        self._mock_cmdline(monkeypatch, {100: "vim", 200: "node"})
        assert _find_claude_pid(shell_pid=999) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_hook.py::TestFindClaudePid -v
```

Expected: `ImportError: cannot import name '_find_claude_pid'`.

- [ ] **Step 3: Implement `_find_claude_pid`**

In `src/ccmux/hook.py`, add `from pathlib import Path` is already imported at the top. Below `_encode_project_dir`, add:

```python
def _find_claude_pid(shell_pid: int) -> int | None:
    """Return the direct `claude` child PID of `shell_pid`, or None.

    Uses `pgrep -P` to enumerate direct children, then matches argv0 of
    `/proc/<pid>/cmdline`. We match by basename so either a bare `claude`
    on $PATH or an absolute path like `/usr/local/bin/claude` is accepted.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(shell_pid)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            continue
        argv0 = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        if Path(argv0).name == "claude":
            return pid
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_hook.py::TestFindClaudePid -v
```

Expected: 5/5 pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "feat(hook): add _find_claude_pid helper for PID fallback"
```

---

## Task 4: Add `_resolve_session_via_pid` helper (TDD)

**Files:**
- Modify: `src/ccmux/hook.py` (new helper + `_SESSION_FILE_RE` constant)
- Modify: `tests/test_hook.py` (new `TestResolveSessionViaPid` class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_hook.py`:

```python
import time

from ccmux.hook import _resolve_session_via_pid


class TestResolveSessionViaPid:
    """Reconstruct (session_id, cwd) from tmux pane → claude PID → filesystem."""

    def _setup_claude_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        *,
        claude_pid: int,
        launch_cwd: str,
        session_jsonls: list[str],
        newest: str | None,
    ) -> None:
        """Lay out a fake `~/.claude/` tree and route Path.home() to it."""
        claude_dir = tmp_path / ".claude"
        (claude_dir / "sessions").mkdir(parents=True)
        (claude_dir / "sessions" / f"{claude_pid}.json").write_text(
            json.dumps(
                {
                    "pid": claude_pid,
                    "sessionId": "stale-00000000-0000-0000-0000-000000000000",
                    "cwd": launch_cwd,
                }
            )
        )
        project_dir = claude_dir / "projects" / _encode_project_dir(launch_cwd)
        project_dir.mkdir(parents=True)
        for name in session_jsonls:
            (project_dir / name).write_text("{}\n")
            if name == newest:
                time.sleep(0.01)
                (project_dir / name).write_text('{"touched": true}\n')
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _mock_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shell_pid: int,
        children: list[int],
    ) -> None:
        """Dispatch subprocess.run based on argv[0] to handle tmux + pgrep."""

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "tmux":
                result.stdout = f"{shell_pid}\n"
            elif cmd[0] == "pgrep":
                result.stdout = (
                    "\n".join(str(c) for c in children) + "\n"
                    if children
                    else ""
                )
                result.returncode = 0 if children else 1
            else:
                raise AssertionError(f"unexpected subprocess: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)

    def _mock_cmdline(
        self, monkeypatch: pytest.MonkeyPatch, mapping: dict[int, str]
    ) -> None:
        from ccmux import hook as hook_mod

        original_read_bytes = hook_mod.Path.read_bytes

        def fake_read_bytes(self: "Path") -> bytes:  # type: ignore[name-defined]
            parts = self.parts
            if len(parts) >= 3 and parts[1] == "proc" and parts[-1] == "cmdline":
                pid = int(parts[2])
                if pid not in mapping:
                    raise FileNotFoundError(str(self))
                return mapping[pid].encode() + b"\0"
            return original_read_bytes(self)

        monkeypatch.setattr(hook_mod.Path, "read_bytes", fake_read_bytes)

    def test_returns_newest_session_id_and_launch_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        launch_cwd = "/mnt/data/project"
        self._setup_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd=launch_cwd,
            session_jsonls=[
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl",
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl",
            ],
            newest="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl",
        )
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        result = _resolve_session_via_pid("%17")

        assert result == (
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            launch_cwd,
        )

    def test_returns_none_when_no_claude_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        assert _resolve_session_via_pid("%17") is None

    def test_returns_none_when_session_file_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        assert _resolve_session_via_pid("%17") is None

    def test_returns_none_when_project_dir_has_no_jsonl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._setup_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd="/mnt/data/project",
            session_jsonls=[],
            newest=None,
        )
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        assert _resolve_session_via_pid("%17") is None

    def test_ignores_non_uuid_jsonl_filenames(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._setup_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd="/mnt/data/project",
            session_jsonls=["scratchpad.jsonl"],
            newest="scratchpad.jsonl",
        )
        self._mock_subprocess(monkeypatch, shell_pid=9999, children=[4242])
        self._mock_cmdline(monkeypatch, {4242: "claude"})

        assert _resolve_session_via_pid("%17") is None

    def test_rejects_invalid_pane_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Note: no subprocess mock — the function should short-circuit before it.
        assert _resolve_session_via_pid("not-a-pane") is None
```

Also update the import at the top of `tests/test_hook.py` to include the new symbol and `Path`:

```python
from pathlib import Path

from ccmux.hook import (
    _UUID_RE,
    _encode_project_dir,
    _find_claude_pid,
    _is_hook_installed,
    _resolve_session_via_pid,
    hook_main,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_hook.py::TestResolveSessionViaPid -v
```

Expected: `ImportError: cannot import name '_resolve_session_via_pid'`.

- [ ] **Step 3: Implement `_SESSION_FILE_RE` and `_resolve_session_via_pid`**

In `src/ccmux/hook.py`, below `_UUID_RE` add the session-file regex:

```python
# Session transcripts are named `<uuid>.jsonl` under each project directory.
_SESSION_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
)
```

Below `_find_claude_pid`, add:

```python
def _resolve_session_via_pid(pane_id: str) -> tuple[str, str] | None:
    """Recover (session_id, launch_cwd) when stdin is empty.

    Claude Code v2.1.x hands SessionStart hooks an empty stdin on `/clear`
    (and sometimes other sources). We reconstruct the identity by walking:
        tmux pane -> shell pid -> claude pid -> launch cwd (stable across
        /clear) -> newest transcript jsonl in the project dir.

    Returns None if any step fails; callers should treat that as "skip".
    """
    if not _PANE_RE.match(pane_id):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    try:
        shell_pid = int(result.stdout.strip())
    except ValueError:
        return None

    claude_pid = _find_claude_pid(shell_pid)
    if claude_pid is None:
        return None

    sessions_file = Path.home() / ".claude" / "sessions" / f"{claude_pid}.json"
    try:
        launch_info = json.loads(sessions_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    launch_cwd = launch_info.get("cwd", "")
    if not launch_cwd or not os.path.isabs(launch_cwd):
        return None

    project_dir = (
        Path.home() / ".claude" / "projects" / _encode_project_dir(launch_cwd)
    )
    try:
        candidates = [
            p
            for p in project_dir.iterdir()
            if p.is_file() and _SESSION_FILE_RE.match(p.name)
        ]
    except OSError:
        return None
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.stem, launch_cwd
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_hook.py::TestResolveSessionViaPid -v
```

Expected: 6/6 pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "feat(hook): add _resolve_session_via_pid fallback helper"
```

---

## Task 5: Wire fallback into `_hook_main_impl` (TDD)

**Files:**
- Modify: `src/ccmux/hook.py` (`_hook_main_impl` body, lines ~246-283)
- Modify: `tests/test_hook.py` (new `TestHookMainEmptyStdinFallback` class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_hook.py`:

```python
class TestHookMainEmptyStdinFallback:
    """When Claude Code sends empty stdin, hook_main must reconstruct
    session_id + cwd via the PID fallback and still write window_bindings.json.
    """

    def _lay_out_claude_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        *,
        claude_pid: int,
        launch_cwd: str,
        new_session_id: str,
    ) -> None:
        claude_dir = tmp_path / ".claude"
        (claude_dir / "sessions").mkdir(parents=True)
        (claude_dir / "sessions" / f"{claude_pid}.json").write_text(
            json.dumps({"pid": claude_pid, "cwd": launch_cwd})
        )
        project_dir = claude_dir / "projects" / _encode_project_dir(launch_cwd)
        project_dir.mkdir(parents=True)
        (project_dir / f"{new_session_id}.jsonl").write_text("{}\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _dispatch_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shell_pid: int,
        claude_pid: int,
        tmux_window: str,
    ) -> None:
        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "tmux" and "#{pane_pid}" in cmd:
                result.stdout = f"{shell_pid}\n"
            elif cmd[0] == "tmux":
                result.stdout = tmux_window
            elif cmd[0] == "pgrep":
                result.stdout = f"{claude_pid}\n"
            else:
                raise AssertionError(f"unexpected: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)

    def _stub_cmdline(
        self, monkeypatch: pytest.MonkeyPatch, claude_pid: int
    ) -> None:
        from ccmux import hook as hook_mod

        original_read_bytes = hook_mod.Path.read_bytes

        def fake_read_bytes(self: "Path") -> bytes:  # type: ignore[name-defined]
            parts = self.parts
            if len(parts) >= 3 and parts[1] == "proc" and parts[-1] == "cmdline":
                if int(parts[2]) == claude_pid:
                    return b"claude\0"
                raise FileNotFoundError(str(self))
            return original_read_bytes(self)

        monkeypatch.setattr(hook_mod.Path, "read_bytes", fake_read_bytes)

    def test_empty_stdin_triggers_fallback_and_writes_bindings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Isolate both CCMUX_DIR and ~/.claude from the real host.
        ccmux_dir = tmp_path / "ccmux"
        ccmux_dir.mkdir()
        monkeypatch.setenv("CCMUX_DIR", str(ccmux_dir))

        new_session_id = "99999999-9999-9999-9999-999999999999"
        self._lay_out_claude_home(
            monkeypatch,
            tmp_path,
            claude_pid=4242,
            launch_cwd="/mnt/data/project",
            new_session_id=new_session_id,
        )
        self._dispatch_subprocess(
            monkeypatch,
            shell_pid=9999,
            claude_pid=4242,
            tmux_window="ccmux:@16\n",
        )
        self._stub_cmdline(monkeypatch, claude_pid=4242)

        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # empty stdin
        monkeypatch.setenv("TMUX_PANE", "%17")

        hook_main()

        data = json.loads((ccmux_dir / "window_bindings.json").read_text())
        assert data["ccmux"]["window_id"] == "@16"
        assert data["ccmux"]["session_id"] == new_session_id
        assert data["ccmux"]["cwd"] == "/mnt/data/project"

    def test_empty_stdin_with_failed_fallback_skips_silently(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """No claude child → no write, but also no exception."""
        ccmux_dir = tmp_path / "ccmux"
        ccmux_dir.mkdir()
        monkeypatch.setenv("CCMUX_DIR", str(ccmux_dir))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            if cmd[0] == "tmux":
                result.stdout = "1234\n"
                result.returncode = 0
            elif cmd[0] == "pgrep":
                result.stdout = ""
                result.returncode = 1
            else:
                raise AssertionError(f"unexpected: {cmd}")
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["ccmux", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setenv("TMUX_PANE", "%17")

        hook_main()  # must not raise

        assert not (ccmux_dir / "window_bindings.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_hook.py::TestHookMainEmptyStdinFallback -v
```

Expected: both tests fail — the first fails because `hook_main` with empty stdin currently just warns and returns without writing; the second may spuriously pass today (no write is correct) but will validate the no-exception guarantee once the rewrite lands.

- [ ] **Step 3: Rewrite `_hook_main_impl` body**

In `src/ccmux/hook.py`, replace the block from `# Check tmux environment first — not in tmux means nothing to do` (line ~246) through `if event != "SessionStart":` / `return` (line ~283) with:

```python
    # Check tmux environment first — not in tmux means nothing to do.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.debug("TMUX_PANE not set, not running in tmux — skipping")
        return
    if not _PANE_RE.match(pane_id):
        logger.warning("Invalid TMUX_PANE format: %r — skipping", pane_id)
        return

    # Read hook payload from stdin. Claude Code v2.1.x sometimes hands us
    # empty/invalid stdin (notably on /clear), so we treat stdin as best-
    # effort and fall back to PID-based resolution below.
    logger.debug("Processing hook event from stdin")
    session_id = ""
    cwd = ""
    event = "SessionStart"
    try:
        payload = json.load(sys.stdin)
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        event = payload.get("hook_event_name", "SessionStart") or "SessionStart"
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug("stdin not usable JSON (%s); will try PID fallback", e)

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # cwd must be absolute to be trustworthy.
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        cwd = ""

    # Fall back to PID-based resolution when stdin didn't deliver both a
    # valid session_id and cwd. This recovers from the v2.1.x empty-stdin
    # bug on /clear.
    if not session_id or not _UUID_RE.match(session_id) or not cwd:
        resolved = _resolve_session_via_pid(pane_id)
        if resolved is None:
            logger.debug(
                "Could not resolve session via PID fallback; skipping"
            )
            return
        session_id, cwd = resolved
        logger.info(
            "Resolved session via PID fallback: session_id=%s cwd=%s",
            session_id,
            cwd,
        )
```

The rest of the function (tmux `display-message` for `session:window`, lock + `atomic_write_json`) is unchanged.

- [ ] **Step 4: Run the new tests**

Run:
```bash
uv run pytest tests/test_hook.py::TestHookMainEmptyStdinFallback -v
```

Expected: 2/2 pass.

- [ ] **Step 5: Run the full hook test module to ensure no regressions**

Run:
```bash
uv run pytest tests/test_hook.py -v
```

Expected: all prior tests still pass (in particular `TestHookMainValidation` — those tests don't set `TMUX_PANE`, so the early return still fires before any fallback).

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/hook.py tests/test_hook.py
git commit -m "feat(hook): fall back to PID-based session_id when stdin is empty

Claude Code v2.1.x hands SessionStart hooks an empty stdin on /clear
(and sometimes other sources), which previously left window_bindings.json
stuck on the pre-/clear session_id. The hook now reconstructs
(session_id, cwd) from the Claude process PID found via the tmux pane,
reading launch cwd from ~/.claude/sessions/<pid>.json and picking the
newest transcript jsonl in ~/.claude/projects/<encoded-cwd>/.

The fallback is silent on failure so a missing /proc, non-Claude pane,
or absent project dir never turns into a scary banner."
```

---

## Task 6: Run the full test suite

**Files:** none modified; verification only.

- [ ] **Step 1: Run full `pytest`**

Run:
```bash
cd /mnt/md0/home/wenruiwu/projects/ccmux-backend
uv run pytest -v
```

Expected: all tests pass. No tests outside `test_hook.py` should have broken (the hook isn't imported from other modules' test paths). If anything fails, STOP and diagnose before the smoke test.

- [ ] **Step 2: Run linters/type checks the repo uses**

Run:
```bash
uv run ruff check src/ccmux/hook.py tests/test_hook.py
uv run pyright src/ccmux/hook.py tests/test_hook.py
```

Expected: clean. If pyright complains about `tuple[str, str] | None`, confirm `from __future__ import annotations` isn't already in `hook.py`; if needed, bare the hint to `Optional[Tuple[str, str]]`.

---

## Task 7: Live smoke test on this machine

**Files:** none modified directly; clean-up of `~/.ccmux/window_bindings.json` is manual.

- [ ] **Step 1: Clean stale test entries from the live bindings file**

Earlier investigation left two bogus entries in `~/.ccmux/window_bindings.json`: the `""` key (empty tmux session name) and a `ccmux` entry whose `session_id` is the dummy `abcdefab-…` from a manual test. Remove them manually:

Read: `~/.ccmux/window_bindings.json`
Remove: the `""` key entirely; leave `ccmux` alone — it'll be overwritten in step 3.

- [ ] **Step 2: Confirm ccmux hook executable points at editable install**

Run:
```bash
\ls -la /mnt/md0/home/wenruiwu/projects/ccmux-backend/.venv/bin/ccmux
cat /mnt/md0/home/wenruiwu/projects/ccmux-backend/.venv/lib/python3.12/site-packages/ccmux-*.dist-info/direct_url.json
```

Expected: `"editable": true` pointing at the repo root — no reinstall needed.

- [ ] **Step 3: Manually reproduce the /clear payload with empty stdin**

From the pane currently running Claude Code:
```bash
TMUX_PANE="$TMUX_PANE" /mnt/md0/home/wenruiwu/projects/ccmux-backend/.venv/bin/ccmux hook </dev/null
```

Expected: exit 0 and `~/.ccmux/hook.log` tail now contains an INFO line like `Resolved session via PID fallback: session_id=<uuid> cwd=/mnt/md0/home/wenruiwu`, followed by `Updated session_map: ccmux -> window_id=@16, session_id=<same uuid>, cwd=/mnt/md0/home/wenruiwu`.

Verify with:
```bash
tail -5 /mnt/md0/home/wenruiwu/.ccmux/hook.log
python3 -c "import json; d=json.load(open('/mnt/md0/home/wenruiwu/.ccmux/window_bindings.json')); print(d.get('ccmux'))"
```

The printed `session_id` must match the latest `*.jsonl` basename in `~/.claude/projects/-mnt-md0-home-wenruiwu/`.

- [ ] **Step 4: If the smoke test succeeded, nothing to commit**

If step 3 produced the expected log and binding, the fix is live. The user can observe behavior across real `/clear` invocations over the next session.

If smoke step 3 fails, STOP and diagnose — typical causes: `pgrep` not on PATH, `/proc` not mounted (containers), or `Path.home()` not resolving to `/mnt/md0/home/wenruiwu` in the ccmux venv.

---

## Out of scope (noted for future work)

- **Version bump / CHANGELOG entry.** This is a bugfix worth shipping, but batching with the next parser_config follow-up keeps release noise down. When cutting a release, add a `## 1.2.2` (or whatever version is next) block mentioning "Hook recovers session_id via PID fallback when Claude Code sends empty stdin on /clear".
- **Upstream report.** The empty-stdin behavior in Claude Code v2.1.x SessionStart hooks is almost certainly a bug in Claude Code itself. Worth filing at `anthropics/claude-code` once we have a minimal reproducer that doesn't require ccmux.
- **Shrinking hook-log noise.** `_configure_hook_logging` currently streams DEBUG to stderr, which Claude Code surfaces in its hook-error banner. Raising the stderr handler to WARNING (keeping file logging at DEBUG) would silence the banner in normal operation. Tracked separately because it's a logging-policy decision, not a correctness fix.

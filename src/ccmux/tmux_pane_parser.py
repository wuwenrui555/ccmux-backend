"""Tmux pane parser — detects Claude Code UI elements in captured pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs via regex-based UIPattern matching with top/bottom
    delimiters. Covered types: ExitPlanMode, AskUserQuestion,
    PermissionPrompt, BashApproval, RestoreCheckpoint, Settings.
  - Status line (spinner characters + working text) by scanning upward
    from the chrome separator.
  - Bash command output (for `! cmd` echoes).
  - `/usage` modal content.

All Claude Code text patterns live in parser_config. To support a new UI
type or a changed Claude Code version, edit parser_config.UI_PATTERNS /
parser_config.STATUS_SPINNERS.

Key functions:
  - extract_interactive_content()
  - extract_bash_output()
  - parse_status_line()
  - parse_usage_output()
"""

import hashlib
import logging
import re
from dataclasses import dataclass

from . import parser_config as _pc
from .claude_state import BlockedUI
from .util import ccmux_dir

logger = logging.getLogger(__name__)

# Dedicated logger for pattern-drift warnings. Writes to its own file
# (~/.ccmux/drift.log) so new-UI samples form a clean review queue
# instead of getting drowned in the main log.
drift_logger = logging.getLogger("ccmux.drift")
_drift_handler = logging.FileHandler(
    ccmux_dir() / "drift.log",
    encoding="utf-8",
    delay=True,  # don't create the file until a drift actually fires
)
_drift_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
drift_logger.addHandler(_drift_handler)
drift_logger.setLevel(logging.WARNING)
drift_logger.propagate = False  # keep drift out of the main ccmux.log


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str
    ui: BlockedUI


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


_WALKBACK_WINDOW = 20


def _walkback_to_separator(lines: list[str], top_idx: int) -> int:
    """Expand `top_idx` upward to the line after the nearest ──── separator.

    Scans up to `_WALKBACK_WINDOW` lines above `top_idx`. Returns the
    original `top_idx` when no separator is found in range — the
    extracted region stays unchanged and we fall back to the pre-walkback
    behavior.
    """
    search_end = max(top_idx - _WALKBACK_WINDOW, -1)
    for i in range(top_idx - 1, search_end, -1):
        stripped = lines[i].strip()
        if len(stripped) >= _CHROME_MIN_LEN and all(c == "─" for c in stripped):
            return i + 1
    return top_idx


def _try_extract(
    lines: list[str], pattern: _pc.UIPattern
) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When `pattern.bottom` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).

    When `pattern.walkback` is True, the top anchor is expanded upward
    to the line after the nearest ``────`` separator so the tool-preview
    block that sits above permission questions (``Read file`` /
    ``Read(/etc/passwd)`` / ``Enable auto mode?`` / etc.) is included in
    the extracted content.
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary, but cap
    # the search at the chrome separator so we don't swallow the tmux
    # chrome + status bar below the UI.
    if not pattern.bottom:
        chrome_idx = _find_chrome_separator(lines)
        search_end = chrome_idx if chrome_idx is not None else len(lines)
        for i in range(search_end - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    effective_top = (
        _walkback_to_separator(lines, top_idx) if pattern.walkback else top_idx
    )
    content = "\n".join(lines[effective_top : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), ui=pattern.name)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


# Substrings that strongly suggest the pane shows an interactive prompt.
# Used only for the "pattern drift" warning; order / exactness doesn't matter.
_PROMPT_SIGNAL_MARKERS: tuple[str, ...] = (
    "Esc to ",
    "Enter to ",
    "❯ 1.",
    "Would you like to",
    "Do you want to",
    "Type to filter",
)

# Rough size of the "prompt region" at the bottom of a pane — covers
# chrome + status + a few lines of UI footer. Used for both drift
# detection and the fingerprint window so they agree on what "bottom" means.
_PROMPT_REGION_LINES = 12

# Bounded dedup set so the same unmatched pane doesn't spam the log.
# Cleared wholesale when it grows past the cap — acceptable since pane
# fingerprints rarely cluster that tightly in practice.
_UNMATCHED_LOG_CAP = 32
_unmatched_prompt_fingerprints: set[str] = set()


def _looks_like_prompt(lines: list[str]) -> bool:
    """True when the prompt region contains a known prompt-footer marker."""
    tail = lines[-_PROMPT_REGION_LINES:]
    return any(
        any(marker in line for marker in _PROMPT_SIGNAL_MARKERS) for line in tail
    )


def _log_pattern_drift(lines: list[str]) -> None:
    """Warn once per unique pane fingerprint when prompt signals don't match any pattern.

    Fires when `_looks_like_prompt` is True but `_pc.UI_PATTERNS` produced no
    match — typically means a Claude Code upgrade reworded a prompt and
    the regex needs an update. Dedup is per-process; resets across restarts.
    """
    # Strip chrome before fingerprinting so per-tick volatile lines
    # (claude-hud progress bars, context %, spinner) don't defeat dedup.
    ui_lines = _strip_pane_chrome(lines)
    tail = ui_lines[-_PROMPT_REGION_LINES:]
    fingerprint = hashlib.md5("\n".join(tail).encode("utf-8")).hexdigest()[:12]

    if fingerprint in _unmatched_prompt_fingerprints:
        return

    if len(_unmatched_prompt_fingerprints) >= _UNMATCHED_LOG_CAP:
        _unmatched_prompt_fingerprints.clear()
    _unmatched_prompt_fingerprints.add(fingerprint)

    drift_logger.warning(
        "Interactive UI signals detected but no UI_PATTERNS matched "
        "(fingerprint=%s). Claude Code upgrade may have changed prompt "
        "wording — edit parser_config.UI_PATTERNS. Last lines:\n%s",
        fingerprint,
        "\n".join(tail),
    )


# ---------------------------------------------------------------------------
# Interactive UI entry point
# ---------------------------------------------------------------------------


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    A live blocking UI (permission prompt, AskUserQuestion, ExitPlanMode,
    Settings panel) always replaces Claude's input chrome — the `────\\n❯\\n
    ────\\nstatusbar` sandwich at the pane bottom — so we short-circuit
    when that chrome is still present. Any UI-looking text in that case
    is scrollback (e.g., a pasted transcript) and must not trigger UI
    detection. This also stops the status-line poller from spamming the
    bound topic every tick.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.

    If prompt-like signals are present but no pattern matches, emits a
    one-shot warning so a Claude Code version drift is surfaced rather
    than silently breaking interactive UI detection.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    if _has_input_chrome(lines):
        return None

    for pattern in _pc.UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result

    if _looks_like_prompt(lines):
        _log_pattern_drift(lines)

    return None


# ---------------------------------------------------------------------------
# Input chrome detection
# ---------------------------------------------------------------------------

# How far from the bottom to look for Claude's input chrome sandwich. The
# real chrome always sits within the last few lines (separator, prompt,
# separator, 1-4 status lines); 20 lines is a generous upper bound that
# accommodates extra status widgets without catching scrollback.
_CHROME_SEARCH_WINDOW = 20


def _has_input_chrome(lines: list[str]) -> bool:
    """True when Claude's input box is rendered at the pane bottom.

    Pattern: a full-width ``────`` separator whose very next line starts
    with ``❯`` (possibly with user-typed text after). Presence of this
    sandwich means Claude is in WORKING or IDLE state — no blocking UI.
    Absence means a UI has taken over the input region.
    """
    if not lines:
        return False
    search_start = max(0, len(lines) - _CHROME_SEARCH_WINDOW)
    for i in range(search_start, len(lines) - 1):
        stripped = lines[i].strip()
        if len(stripped) < _CHROME_MIN_LEN or not all(c == "─" for c in stripped):
            continue
        if lines[i + 1].lstrip().startswith("❯"):
            return True
    return False


# Public alias — callers outside this module should use this.
has_input_chrome = _has_input_chrome


# ---------------------------------------------------------------------------
# Chrome detection
# ---------------------------------------------------------------------------

# Minimum length for a line of `─` to count as the chrome separator.
# Claude Code always renders the chrome separator at full terminal width
# (≫ 20 chars); short decorative `─────` dividers inside UI bodies are
# well under 20 and therefore excluded.
_CHROME_MIN_LEN = 20


def _find_chrome_separator(lines: list[str], search_window: int = 10) -> int | None:
    """Return the index of the topmost `────` line in the last *search_window* lines."""
    search_start = max(0, len(lines) - search_window)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= _CHROME_MIN_LEN and all(c == "─" for c in stripped):
            return i
    return None


def _strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    Finds the topmost `────` separator in the last 10 lines and strips
    everything from there down.
    """
    idx = _find_chrome_separator(lines)
    return lines[:idx] if idx is not None else lines


# ---------------------------------------------------------------------------
# Bash output extraction
# ---------------------------------------------------------------------------


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract `!` command output from a captured tmux pane.

    Searches from the bottom for the `! <command>` echo line, then
    returns that line and everything below it (including the `⎿` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = _strip_pane_chrome(pane_text.splitlines())

    cmd_idx: int | None = None
    # Match on a 10-char prefix rather than the full command: tmux may
    # truncate long echoes with `…` (e.g. `! long_comma…`), and partial
    # matches on a stable prefix are robust to that truncation.
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    raw_output = lines[cmd_idx:]
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ---------------------------------------------------------------------------
# Status line parsing
# ---------------------------------------------------------------------------

# Absolute safety cap on how far above the chrome separator we'll scan.
# Every iteration (blank, overlay, checklist, or unknown) consumes one
# slot. Empirically the real layout is spinner + ≤20 TodoWrite rows +
# blank + ≤2 overlay lines ≈ 24; 30 leaves headroom for subagent
# stacking without risking runaway scans on corrupted panes.
_STATUS_SCAN_WINDOW = 30


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code RUNNING status line from terminal output.

    The status line (spinner + working text) lives above the chrome
    separator (a full line of `─` characters). We locate the separator
    first, then scan upward, skipping blanks and lines matching any
    pattern in ``_pc.SKIPPABLE_PATTERNS`` (overlay modals, TodoWrite
    checkbox rows, overflow tail). The scan returns the spinner when
    found and bails on the first truly unknown line, which keeps stray
    `·` bullets in regular output from producing false positives.

    Only **running** status lines are returned. Completion summaries
    (``✻ Worked for 56s``, ``· Cogitated for 1m 25s``) share the spinner
    prefix but represent a finished turn and lack the ``…`` ellipsis
    that Claude Code uses to signal "still in progress". Treating them
    as running status leaks throwaway `Worked for 56s` bubbles into the
    Telegram frontend, which then get eaten by the next user message
    via status→content conversion. Returning None for the completion
    form keeps the displayed timeline clean; the frontend transitions
    straight to IDLE once the running status disappears.

    Returns the text after the spinner, or None if no running status
    line is found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    chrome_idx = _find_chrome_separator(lines)
    if chrome_idx is None:
        return None

    for i in range(chrome_idx - 1, max(chrome_idx - 1 - _STATUS_SCAN_WINDOW, -1), -1):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue
        if any(p.search(line) for p in _pc.SKIPPABLE_PATTERNS):
            continue
        if stripped[0] in _pc.STATUS_SPINNERS:
            text = stripped[1:].strip()
            if "…" not in text:
                return None
            return text
        return None
    return None


# ---------------------------------------------------------------------------
# Usage modal parsing (Claude Code 2.1.114+)
# ---------------------------------------------------------------------------


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage tab.

    CC 2.1.x+ puts /usage inside the /config modal as one of four tabs
    (Status / Config / Usage / Stats); older CC wrapped it in a
    `Settings: ... Usage` header. Rather than chasing header wording,
    anchor on content markers specific to the Usage tab body
    (`Current session` / `Current week`), then collect lines until the
    footer `Esc to ...`. Progress-bar block characters get stripped.

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Anchor on Usage-tab content markers, not the UI header (which has
    # already drifted once and could again).
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            if stripped.startswith("Current session") or stripped.startswith(
                "Current week"
            ):
                start_idx = i
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress-bar block characters and
    # whitespace. Blank lines are preserved as section breaks (collapsed to
    # one and never at the edges).
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        stripped = line.strip()
        if not stripped:
            # Collapse consecutive blanks; drop leading blanks
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        # Progress bars look like `█████▋   38% used`; strip leading block
        # drawing chars (U+2580..U+259F) but keep the rest of the line.
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    # Drop trailing blank introduced when the last content line is followed
    # by whitespace before `Esc to ...`.
    while cleaned and cleaned[-1] == "":
        cleaned.pop()

    if cleaned:
        return UsageInfo(parsed_lines=cleaned)

    return None

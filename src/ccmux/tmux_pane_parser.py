"""Tmux pane parser — detects Claude Code UI elements in captured pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs via regex-based UIPattern matching with top/bottom
    delimiters. Covered types: ExitPlanMode, AskUserQuestion,
    PermissionPrompt, BashApproval, RestoreCheckpoint, Settings.
  - Status line (spinner characters + working text) by scanning upward
    from the chrome separator.
  - Bash command output (for `! cmd` echoes).
  - `/usage` modal content.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

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

from .parser_overrides import UIPattern  # re-exported for back-compat
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

    content: str  # The extracted display content
    name: str  # Pattern name that matched (e.g. "AskUserQuestion")


# ---------------------------------------------------------------------------
# UI pattern definitions (order matters — first match wins)
# ---------------------------------------------------------------------------

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            # CC 2.1.x+ /config UI: tab bar replaces the old "Settings:" header.
            # Active tab highlighting is invisible in plain pane capture — we
            # just anchor on the fixed word order.
            re.compile(r"^\s*Status\s+Config\s+Usage\s+Stats\s*$"),
            # /model picker (both pre- and post-2.1)
            re.compile(r"^\s*Select model"),
            # Legacy (pre-2.1.x) — kept for older CC installs
            re.compile(r"^\s*Settings:.*tab to cycle"),
        ),
        bottom=(
            # cancel/exit/clear/close span tab variants across CC versions
            re.compile(r"Esc to (cancel|exit|clear|close)"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


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


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When `pattern.bottom` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
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

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


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

    Fires when `_looks_like_prompt` is True but `UI_PATTERNS` produced no
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
        "wording — edit tmux_pane_parser.UI_PATTERNS. Last lines:\n%s",
        fingerprint,
        "\n".join(tail),
    )


# ---------------------------------------------------------------------------
# Interactive UI entry point
# ---------------------------------------------------------------------------


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.

    If prompt-like signals are present but no pattern matches, emits a
    one-shot warning so a Claude Code version drift is surfaced rather
    than silently breaking interactive UI detection.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result

    if _looks_like_prompt(lines):
        _log_pattern_drift(lines)

    return None


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

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])

# Overlay lines that may sit between the real spinner and the chrome.
# These are transient Claude Code modals — e.g. the "How is Claude doing
# this session?" rating prompt — that must not short-circuit spinner
# detection. Scan-upward skips any line matching one of these patterns
# and continues looking for the spinner above.
_SKIPPABLE_OVERLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Session-rating modal (CC 2.1.x+).
    re.compile(r"^\s*●\s*How is Claude doing this session\?"),
    re.compile(r"^\s*1:\s*Bad\b"),
)

# Upper bound on how far above the chrome separator the real spinner can
# sit once overlays are in the way. Generous enough to tolerate 2–3
# lines of modal plus blank gaps.
_STATUS_SCAN_WINDOW = 10


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) lives above the chrome
    separator (a full line of `─` characters). We locate the separator
    first, then scan upward — skipping blank lines and recognised
    overlay modals (e.g. the session-rating prompt) — until we either
    find a spinner or exhaust the scan window. Bailing only on a
    non-spinner, non-overlay line keeps `·` bullets in regular output
    from producing false positives.

    Returns the text after the spinner, or None if no status line found.
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
        if any(p.search(line) for p in _SKIPPABLE_OVERLAY_PATTERNS):
            continue
        if stripped[0] in STATUS_SPINNERS:
            return stripped[1:].strip()
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

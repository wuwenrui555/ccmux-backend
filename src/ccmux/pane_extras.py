"""Tmux-pane scrapers for `! cmd` echoes and the `/usage` modal.

These helpers are intentionally separate from `claude_code_state`: they
scrape arbitrary echoed text out of a tmux pane (a bash-output echo or
the `/usage` content tab), they do not classify Claude's runtime state.

Key functions:
  - extract_bash_output()
  - parse_usage_output()
"""

import re
from dataclasses import dataclass


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

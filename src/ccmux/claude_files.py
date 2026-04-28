"""Claude JSONL file resolution and reading.

Locates Claude Code transcript files under `~/.claude/projects/`,
reads session summaries, and provides byte-sliced message reads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles

from .config import config
from .claude_transcript_parser import TranscriptParser

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeSession:
    """Summary of a Claude Code JSONL session file (frontend-facing)."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


def _encode_cwd(cwd: str) -> str:
    """Convert a cwd to Claude Code's project-directory naming.

    Claude Code stores transcripts under
    ``~/.claude/projects/<encoded>/<session-uuid>.jsonl`` where
    ``<encoded>`` replaces every ``/`` and ``_`` in ``cwd`` with ``-``.
    """
    return cwd.replace("/", "-").replace("_", "-")


class ClaudeFileResolver:
    """Find and read Claude Code JSONL session files."""

    def __init__(self) -> None:
        pass

    def build_path(self, claude_session_id: str, cwd: str) -> Path | None:
        if not claude_session_id or not cwd:
            return None
        return (
            config.claude_projects_path
            / _encode_cwd(cwd)
            / f"{claude_session_id}.jsonl"
        )

    async def find_file(self, claude_session_id: str, cwd: str = "") -> Path | None:
        if cwd:
            direct = self.build_path(claude_session_id, cwd)
            if direct and direct.exists():
                return direct
        pattern = f"*/{claude_session_id}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if matches:
            # Sort by mtime (newest first) so behavior is deterministic
            # when the same session id appears under multiple project dirs
            # (e.g. after a cwd rename or symlink).
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]
        logger.debug("JSONL file not found for session %s", claude_session_id)
        return None

    async def get_session_summary(
        self, claude_session_id: str, cwd: str = ""
    ) -> ClaudeSession | None:
        """Read summary and message count from a Claude session's JSONL file."""
        file_path = await self.find_file(claude_session_id, cwd)
        if not file_path:
            return None

        summary = ""
        last_user_msg = ""
        message_count = 0

        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        elif TranscriptParser.is_user_message(data):
                            parsed = TranscriptParser.parse_message(data)
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return ClaudeSession(
            session_id=claude_session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    async def read_messages(
        self,
        file_path: Path,
        session_id: str,
        *,
        start_byte: int,
        end_byte: int | None,
    ) -> list[dict]:
        """Read JSONL entries from `start_byte` to `end_byte`, parse, return."""
        if not file_path.exists():
            return []

        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)
                while True:
                    if end_byte is not None and await f.tell() >= end_byte:
                        break
                    line = await f.readline()
                    if not line:
                        break
                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return []

        parsed, _, _ = TranscriptParser.parse_entries(entries, session_id=session_id)
        return [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed
        ]

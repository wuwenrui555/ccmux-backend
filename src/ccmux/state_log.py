"""State log: append-only JSONL recorder for (pane_text, state) observations.

Opt-in via the ``CCMUX_STATE_LOG_PATH`` env var. When the path is set,
``DefaultBackend`` constructs a ``StateLog`` and injects it into
``StateMonitor``; ``fast_tick`` calls ``record(...)`` after every
``parse_pane`` classification.

Adjacent ticks with identical pane text for the same instance are
collapsed into a single record with ``first_seen``, ``last_seen``, and
``tick_count``. State only flushes to disk when the pane text changes
for that instance, or when ``close()`` is called at shutdown.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from claude_code_state import ClaudeState


def _serialize_state(state: ClaudeState) -> dict[str, Any]:
    """Serialize a ``ClaudeState`` to a JSON-ready dict.

    All variants are frozen dataclasses; ``dataclasses.asdict`` flattens
    them and ``BlockedUI`` (a ``StrEnum``) serializes as its string value.
    A ``type`` field with the variant class name is injected at the top
    level so log readers can branch on variant without duck typing.
    """
    payload: dict[str, Any] = {"type": type(state).__name__}
    payload.update(asdict(state))
    return payload


import asyncio
import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StagedRecord:
    instance_id: str
    window_id: str
    pane_text: str
    state: dict[str, Any]
    first_seen: datetime
    last_seen: datetime
    tick_count: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class StateLog:
    """Append-only JSONL writer for (pane_text, state) observations.

    Adjacent ticks with identical pane text for the same instance are
    collapsed; only when the pane text changes does the previous record
    get written to disk. ``close()`` flushes all in-memory staged
    records.

    Concurrency: ``record()`` may be invoked from multiple coroutines
    (``state_monitor.fast_tick`` fans out via ``asyncio.gather``). A
    single ``asyncio.Lock`` protects the staged-record dict and the
    file write. The critical section is short and contention is bounded
    by the number of instances, so a single lock is sufficient.

    The file handle is opened in ``__init__`` and held for the
    lifetime of the object. The parent directory must already exist;
    we do not silently ``mkdir`` because the caller may have typed the
    path wrong.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        if not p.parent.exists():
            raise FileNotFoundError(
                f"State log parent directory does not exist: {p.parent}"
            )
        self._path = p
        self._fh: IO[str] = open(p, "a", encoding="utf-8")
        self._staged: dict[str, _StagedRecord] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def record(
        self,
        *,
        instance_id: str,
        window_id: str,
        pane_text: str,
        state: ClaudeState,
    ) -> None:
        if self._closed:
            return
        now = _utcnow()
        async with self._lock:
            prev = self._staged.get(instance_id)
            if prev is not None and prev.pane_text == pane_text:
                self._staged[instance_id] = replace(
                    prev,
                    last_seen=now,
                    tick_count=prev.tick_count + 1,
                )
                return
            if prev is not None:
                self._write(prev)
            self._staged[instance_id] = _StagedRecord(
                instance_id=instance_id,
                window_id=window_id,
                pane_text=pane_text,
                state=_serialize_state(state),
                first_seen=now,
                last_seen=now,
                tick_count=1,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            for rec in self._staged.values():
                self._write(rec)
            self._staged.clear()
            try:
                self._fh.close()
            except Exception as e:
                logger.debug("state_log file close error: %s", e)
            self._closed = True

    def _write(self, rec: _StagedRecord) -> None:
        line = json.dumps(
            {
                "first_seen": _iso(rec.first_seen),
                "last_seen": _iso(rec.last_seen),
                "tick_count": rec.tick_count,
                "instance_id": rec.instance_id,
                "window_id": rec.window_id,
                "state": rec.state,
                "pane_text": rec.pane_text,
            },
            ensure_ascii=False,
        )
        self._fh.write(line + "\n")
        self._fh.flush()

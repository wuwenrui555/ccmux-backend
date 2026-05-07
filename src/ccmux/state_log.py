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

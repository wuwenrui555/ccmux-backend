"""Backward-compatibility shim — superseded by ``claude_code_state.parser``.

The pane parser was extracted into the standalone ``claude-code-state``
package in v5.1.0 (see CHANGELOG). External callers should migrate to
``claude_code_state`` directly.

This shim only re-exports the names that were observed in the wild as
private imports against ``ccmux.tmux_pane_parser`` (most notably the
drift logger that ``ccmux-telegram``'s autouse test fixture clears).
It will be deleted in the next major version.
"""

from __future__ import annotations

from claude_code_state.parser import drift_logger as drift_logger

__all__ = ["drift_logger"]

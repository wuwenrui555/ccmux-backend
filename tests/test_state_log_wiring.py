"""Wiring tests for CCMUX_STATE_LOG_PATH env-var driven state log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ccmux.state_log import StateLog


class TestEnvVarConstruction:
    def test_unset_env_var_yields_no_state_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CCMUX_STATE_LOG_PATH", raising=False)
        from ccmux.backend import _build_state_log

        assert _build_state_log() is None

    def test_empty_env_var_yields_no_state_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCMUX_STATE_LOG_PATH", "   ")
        from ccmux.backend import _build_state_log

        assert _build_state_log() is None

    def test_env_var_set_yields_state_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "state.jsonl"
        monkeypatch.setenv("CCMUX_STATE_LOG_PATH", str(log_path))
        from ccmux.backend import _build_state_log

        log = _build_state_log()
        assert isinstance(log, StateLog)

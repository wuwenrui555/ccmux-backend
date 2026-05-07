"""Wiring tests for CCMUX_STATE_LOG and CCMUX_STATE_SNAPSHOT env-var toggles."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ccmux.state_log import StateLog, StateSnapshot


class TestEnvVarToggles:
    def test_both_unset_yields_empty_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CCMUX_STATE_LOG", raising=False)
        monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert observers == ()
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_only_log_yields_one_state_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.setenv("CCMUX_STATE_LOG", "1")
        monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert len(observers) == 1
            assert isinstance(observers[0], StateLog)
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_only_snapshot_yields_one_state_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.delenv("CCMUX_STATE_LOG", raising=False)
        monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", "1")
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert len(observers) == 1
            assert isinstance(observers[0], StateSnapshot)
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_both_set_yields_log_then_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        monkeypatch.setenv("CCMUX_STATE_LOG", "1")
        monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", "1")
        from ccmux.backend import _build_state_observers

        observers = _build_state_observers()
        try:
            assert len(observers) == 2
            assert isinstance(observers[0], StateLog)
            assert isinstance(observers[1], StateSnapshot)
        finally:
            for obs in observers:
                asyncio.run(obs.close())

    def test_falsy_values_keep_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("", "   ", "0", "false", "no", "off", "garbage"):
            monkeypatch.setenv("CCMUX_STATE_LOG", value)
            monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", value)
            from ccmux.backend import _build_state_observers

            observers = _build_state_observers()
            try:
                assert observers == (), f"value {value!r} should disable both"
            finally:
                for obs in observers:
                    asyncio.run(obs.close())

    def test_truthy_variants(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        for value in ("1", "true", "yes", "on", "TRUE", "On"):
            monkeypatch.setenv("CCMUX_STATE_LOG", value)
            monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)
            from ccmux.backend import _build_state_observers

            observers = _build_state_observers()
            try:
                assert len(observers) == 1
                assert isinstance(observers[0], StateLog)
            finally:
                for obs in observers:
                    asyncio.run(obs.close())

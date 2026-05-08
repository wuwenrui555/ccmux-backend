"""Wiring tests for CCMUX_STATE_LOG and CCMUX_STATE_SNAPSHOT env-var toggles."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ccmux.backend import _build_state_observers
from ccmux.state_log import StateLog, StateSnapshot


@pytest.fixture(autouse=True)
def _clean_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip CCMUX_STATE_LOG / CCMUX_STATE_SNAPSHOT before every test.

    Importing ``ccmux.backend`` triggers ``Config()`` at module load,
    which calls ``load_dotenv`` on ``$CCMUX_DIR/settings.env`` and
    injects whatever the user has there into ``os.environ``. This
    fixture removes both toggles so each test starts from a known-off
    baseline; tests that need a value set then call ``monkeypatch.setenv``
    explicitly. Without this fixture, isolation runs of this file are
    order-dependent on whatever the developer happened to put in their
    settings.env.
    """
    monkeypatch.delenv("CCMUX_STATE_LOG", raising=False)
    monkeypatch.delenv("CCMUX_STATE_SNAPSHOT", raising=False)


class TestEnvVarToggles:
    def test_both_unset_yields_empty_tuple(self) -> None:
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
        monkeypatch.setenv("CCMUX_STATE_SNAPSHOT", "1")

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

            observers = _build_state_observers()
            try:
                assert len(observers) == 1
                assert isinstance(observers[0], StateLog)
            finally:
                for obs in observers:
                    asyncio.run(obs.close())

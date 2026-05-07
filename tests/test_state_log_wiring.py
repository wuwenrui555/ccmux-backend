"""Wiring tests for CCMUX_STATE_LOG env-var toggle."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccmux.state_log import StateLog


class TestEnvVarToggle:
    def test_unset_env_var_yields_no_state_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CCMUX_STATE_LOG", raising=False)
        from ccmux.backend import _build_state_log

        assert _build_state_log() is None

    def test_falsy_env_var_yields_no_state_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for value in ("", "   ", "0", "false", "no", "off", "garbage"):
            monkeypatch.setenv("CCMUX_STATE_LOG", value)
            from ccmux.backend import _build_state_log

            assert _build_state_log() is None, f"value {value!r} should disable"

    def test_truthy_env_var_yields_state_log_under_ccmux_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CCMUX_DIR", str(tmp_path))
        for value in ("1", "true", "yes", "on", "TRUE", "On"):
            monkeypatch.setenv("CCMUX_STATE_LOG", value)
            from ccmux.backend import _build_state_log

            log = _build_state_log()
            assert isinstance(log, StateLog), f"value {value!r} should enable"
            assert log._path == tmp_path / "state.jsonl"
            # Close so we can re-open in the next iteration without leaking fds.
            import asyncio

            asyncio.run(log.close())

    def test_parent_dir_created_if_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ccmux_dir = tmp_path / "fresh-dir-that-does-not-exist"
        monkeypatch.setenv("CCMUX_DIR", str(ccmux_dir))
        monkeypatch.setenv("CCMUX_STATE_LOG", "1")
        from ccmux.backend import _build_state_log

        log = _build_state_log()
        assert isinstance(log, StateLog)
        assert ccmux_dir.is_dir()
        import asyncio

        asyncio.run(log.close())

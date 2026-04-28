"""Override-layer behavior of ClaudeInstanceRegistry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccmux.claude_instance import ClaudeInstance, ClaudeInstanceRegistry


@pytest.fixture
def map_file(tmp_path: Path) -> Path:
    p = tmp_path / "claude_instances.json"
    p.write_text(
        json.dumps(
            {
                "outlook": {
                    "window_id": "@35",
                    "session_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "cwd": "/Users/wenruiwu",
                },
            }
        )
    )
    return p


def test_get_returns_override_over_file(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    assert reg.get("outlook") == override


def test_clear_override_reverts_to_file(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    reg.clear_override("outlook")
    inst = reg.get("outlook")
    assert inst is not None
    assert inst.window_id == "@35"


def test_set_override_for_unmapped_instance(tmp_path: Path) -> None:
    map_file = tmp_path / "empty.json"
    map_file.write_text("{}")
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="ghost",
        window_id="@99",
        session_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        cwd="/tmp",
    )
    reg.set_override("ghost", override)
    assert reg.get("ghost") == override


def test_get_by_window_id_consults_overrides(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    found = reg.get_by_window_id("@22")
    assert found is not None and found.instance_id == "outlook"


def test_find_by_session_id_consults_overrides(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id=sid,
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    found = reg.find_by_session_id(sid)
    assert found is not None and found.window_id == "@22"


def test_clear_override_is_noop_when_absent(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    reg.clear_override("not-there")  # must not raise

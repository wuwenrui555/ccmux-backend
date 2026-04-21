"""Tests for ClaudeInstance + ClaudeInstanceRegistry."""

import json
from pathlib import Path

import pytest

from ccmux.claude_instance import ClaudeInstance, ClaudeInstanceRegistry


@pytest.fixture
def tmp_instances_file(tmp_path: Path) -> Path:
    return tmp_path / "claude_instances.json"


class TestClaudeInstance:
    def test_fields(self) -> None:
        inst = ClaudeInstance(
            instance_id="__ccmux__",
            window_id="@7",
            session_id="abc-123",
            cwd="/home/w/proj",
        )
        assert inst.instance_id == "__ccmux__"
        assert inst.window_id == "@7"
        assert inst.session_id == "abc-123"
        assert inst.cwd == "/home/w/proj"

    def test_is_frozen(self) -> None:
        inst = ClaudeInstance(instance_id="a", window_id="@1", session_id="s", cwd="/")
        with pytest.raises(Exception):
            inst.window_id = "@99"  # type: ignore[misc]


class TestClaudeInstanceRegistry:
    def test_empty_when_file_missing(self, tmp_instances_file: Path) -> None:
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        assert list(reg.all()) == []

    def test_get_by_instance_id(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "__ccmux__": {
                        "window_id": "@7",
                        "session_id": "abc-123",
                        "cwd": "/home/w/proj",
                    }
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        inst = reg.get("__ccmux__")
        assert inst is not None
        assert inst.instance_id == "__ccmux__"
        assert inst.window_id == "@7"
        assert inst.session_id == "abc-123"

    def test_get_by_window_id(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "s1", "cwd": "/a"},
                    "beta": {"window_id": "@9", "session_id": "s2", "cwd": "/b"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        hit = reg.get_by_window_id("@9")
        assert hit is not None
        assert hit.instance_id == "beta"

    def test_find_by_session_id(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "target", "cwd": "/a"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        hit = reg.find_by_session_id("target")
        assert hit is not None
        assert hit.instance_id == "alpha"

    def test_contains(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "s", "cwd": "/a"},
                    "empty": {"window_id": "", "session_id": "", "cwd": "/b"},
                    "window_only": {"window_id": "@6", "session_id": "", "cwd": "/c"},
                    "session_only": {"window_id": "", "session_id": "s2", "cwd": "/d"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        assert reg.contains("alpha") is True
        assert reg.contains("empty") is False
        assert reg.contains("window_only") is False
        assert reg.contains("session_only") is False
        assert reg.contains("missing") is False

    def test_all_skips_windowless_entries(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(
            json.dumps(
                {
                    "alpha": {"window_id": "@5", "session_id": "s", "cwd": "/a"},
                    "pending": {"window_id": "", "session_id": "", "cwd": "/b"},
                }
            )
        )
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        ids = sorted(i.instance_id for i in reg.all())
        assert ids == ["alpha"]

    @pytest.mark.asyncio
    async def test_load_reloads_from_disk(self, tmp_instances_file: Path) -> None:
        tmp_instances_file.write_text(json.dumps({}))
        reg = ClaudeInstanceRegistry(map_file=tmp_instances_file)
        assert list(reg.all()) == []
        tmp_instances_file.write_text(
            json.dumps({"a": {"window_id": "@1", "session_id": "s", "cwd": "/c"}})
        )
        await reg.load()
        assert [i.instance_id for i in reg.all()] == ["a"]

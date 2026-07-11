"""Tests for the Codex ``hooks.json`` merger."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from agam import codex_hooks_merger as merger


def _commands(config: dict, event: str) -> list[str]:
    return [
        handler["command"]
        for group in config["hooks"][event]
        for handler in group["hooks"]
    ]


def test_creates_official_nested_shape(tmp_path: Path):
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_dir = tmp_path / ".codex" / "hooks"

    result = merger.merge_hooks_into_file(hooks_path, hooks_dir)

    assert json.loads(hooks_path.read_text()) == result
    assert "matcher" not in result["hooks"]["UserPromptSubmit"][0]
    assert "matcher" not in result["hooks"]["Stop"][0]
    assert {group["matcher"] for group in result["hooks"]["PreToolUse"]} == {
        "Bash",
        "Edit|Write",
    }
    for groups in result["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                assert handler["type"] == "command"
                assert handler["timeout"] == 30


def test_registers_the_canonical_agam_scripts(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    result = merger.merge_hooks_into_file(tmp_path / "hooks.json", hooks_dir)

    assert any("graph_recall.py" in command for command in _commands(result, "UserPromptSubmit"))
    assert any("codex_stop.py" in command for command in _commands(result, "Stop"))
    assert all(
        "lesson_activate.py" in command
        for command in _commands(result, "PreToolUse")
    )
    assert any(
        "lesson_activate_post.py" in command
        for command in _commands(result, "PostToolUse")
    )


def test_commands_are_guarded_absolute_paths(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    result = merger.merge_hooks_into_file(tmp_path / "hooks.json", hooks_dir)

    for event in result["hooks"]:
        for command in _commands(result, event):
            assert command.startswith("[ -x ")
            assert "exec " in command
            assert command.endswith("|| true")
            assert str(hooks_dir) in command


def test_commands_use_canonical_namespaced_paths_and_tools_dir(
    tmp_path: Path,
):
    hooks_dir = tmp_path / ".codex" / "hooks" / "agam"
    tools_dir = tmp_path / ".codex" / "tools" / "agam"
    result = merger.merge_hooks_into_file(
        tmp_path / ".codex" / "hooks.json",
        hooks_dir / ".." / "agam",
        tools_dir=tools_dir / ".." / "agam",
    )

    for event in result["hooks"]:
        for command in _commands(result, event):
            assert str(hooks_dir) in command
            assert "/../" not in command
            assert f"AGAM_TOOLS_DIR={tools_dir}" in command


def test_preserves_user_config_and_hooks(tmp_path: Path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "custom": {"enabled": True},
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {"type": "command", "command": "/user/start.py"}
                            ],
                        }
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/user/stop.py"}]}
                    ],
                },
            }
        )
    )

    result = merger.merge_hooks_into_file(hooks_path, tmp_path / "hooks")

    assert result["custom"] == {"enabled": True}
    assert result["hooks"]["SessionStart"][0]["hooks"][0]["command"] == (
        "/user/start.py"
    )
    assert "/user/stop.py" in _commands(result, "Stop")
    assert any("codex_stop.py" in command for command in _commands(result, "Stop"))


def test_idempotent(tmp_path: Path):
    hooks_path = tmp_path / "hooks.json"
    hooks_dir = tmp_path / "hooks"

    first = merger.merge_hooks_into_file(hooks_path, hooks_dir)
    second = merger.merge_hooks_into_file(hooks_path, hooks_dir)
    third = merger.merge_hooks_into_file(hooks_path, hooks_dir)

    assert first == second == third
    assert len(third["hooks"]["UserPromptSubmit"]) == 1
    assert len(third["hooks"]["Stop"]) == 1
    assert len(third["hooks"]["PreToolUse"]) == 2
    assert len(third["hooks"]["PostToolUse"]) == 1


def test_dedup_is_matcher_aware():
    existing = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/hook.py"}],
                }
            ]
        }
    }
    new_hooks = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "/hook.py"}],
            },
            {
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": "/hook.py"}],
            },
        ]
    }

    result = merger.merge_hooks(existing, new_hooks)

    assert len(result["hooks"]["PreToolUse"]) == 2
    assert existing == {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/hook.py"}],
                }
            ]
        }
    }


def test_atomic_failure_preserves_original(tmp_path: Path):
    hooks_path = tmp_path / "hooks.json"
    original = json.dumps({"hooks": {}, "user": "kept"}, indent=2) + "\n"
    hooks_path.write_text(original)

    with mock.patch(
        "agam.codex_hooks_merger.os.replace",
        side_effect=OSError("simulated failure"),
    ):
        with pytest.raises(OSError, match="simulated failure"):
            merger.merge_hooks_into_file(hooks_path, tmp_path / "hooks")

    assert hooks_path.read_text() == original
    assert list(tmp_path.glob(".hooks-*.json.tmp")) == []

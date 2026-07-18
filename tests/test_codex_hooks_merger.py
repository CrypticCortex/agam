"""Tests for the Codex ``hooks.json`` merger."""

from __future__ import annotations

import json
import os
import subprocess
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
    scope_root = tmp_path / ".agam" / "knowledge" / "scopes"

    result = merger.merge_hooks_into_file(
        hooks_path, hooks_dir, scope_root=scope_root
    )

    assert json.loads(hooks_path.read_text()) == result
    assert "matcher" not in result["hooks"]["UserPromptSubmit"][0]
    assert {group["matcher"] for group in result["hooks"]["PreToolUse"]} == {
        "*"
    }
    for groups in result["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                assert handler["type"] == "command"
                assert handler["timeout"] == 30


def test_registers_the_canonical_agam_scripts(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    result = merger.merge_hooks_into_file(
        tmp_path / "hooks.json",
        hooks_dir,
        scope_root=tmp_path / "scopes",
    )

    assert any("graph_recall.py" in command for command in _commands(result, "UserPromptSubmit"))
    assert any("scope_guard.py" in command for command in _commands(result, "PreToolUse"))
    serialized = json.dumps(result)
    assert "codex_stop.py" not in serialized
    assert "lesson_activate.py" not in serialized
    assert "lesson_activate_post.py" not in serialized
    assert set(result["hooks"]) == {"UserPromptSubmit", "PreToolUse"}


def test_commands_are_guarded_absolute_paths(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    result = merger.merge_hooks_into_file(
        tmp_path / "hooks.json",
        hooks_dir,
        scope_root=tmp_path / "scopes",
    )

    recall = _commands(result, "UserPromptSubmit")[0]
    guard = _commands(result, "PreToolUse")[0]
    for command in (recall, guard):
        assert command.startswith("[ -x ")
        assert str(hooks_dir) in command
    assert "exec " in recall
    assert "exec " not in guard
    assert recall.endswith("|| true")
    assert "agam_scope_guard_unavailable" in guard
    assert "exit 2" in guard


def test_missing_guard_script_blocks_without_echoing_a_path(tmp_path: Path):
    result = merger.merge_hooks_into_file(
        tmp_path / "hooks.json",
        tmp_path / "missing-hooks",
        scope_root=tmp_path / "scopes",
    )
    guard = _commands(result, "PreToolUse")[0]

    completed = subprocess.run(
        ["/bin/sh", "-c", guard],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == "agam_scope_guard_unavailable"
    assert str(tmp_path) not in completed.stderr


def test_refuses_symlinked_hooks_config_without_touching_target(tmp_path: Path):
    target = tmp_path / "synthetic-user-config.json"
    original = json.dumps({"synthetic": "untouched"})
    target.write_text(original)
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.symlink_to(target)

    with pytest.raises(OSError):
        merger.merge_hooks_into_file(
            hooks_path,
            tmp_path / "hooks",
            scope_root=tmp_path / "scopes",
        )

    assert target.read_text() == original
    assert hooks_path.is_symlink()


def test_refuses_fifo_hooks_config_without_blocking(tmp_path: Path):
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir()
    os.mkfifo(hooks_path)

    with pytest.raises(OSError):
        merger.merge_hooks_into_file(
            hooks_path,
            tmp_path / "hooks",
            scope_root=tmp_path / "scopes",
        )


def test_crashing_guard_script_blocks_instead_of_bypassing(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    guard_script = hooks_dir / "scope_guard.py"
    guard_script.write_text("#!/bin/sh\nexit 7\n")
    guard_script.chmod(0o700)
    result = merger.merge_hooks_into_file(
        tmp_path / "hooks.json",
        hooks_dir,
        scope_root=tmp_path / "scopes",
    )
    guard = _commands(result, "PreToolUse")[0]

    completed = subprocess.run(
        ["/bin/sh", "-c", guard],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == "agam_scope_guard_unavailable"
    assert str(tmp_path) not in completed.stderr


def test_commands_use_canonical_namespaced_paths_and_tools_dir(
    tmp_path: Path,
):
    hooks_dir = tmp_path / ".codex" / "hooks" / "agam"
    tools_dir = tmp_path / ".codex" / "tools" / "agam"
    result = merger.merge_hooks_into_file(
        tmp_path / ".codex" / "hooks.json",
        hooks_dir / ".." / "agam",
        tools_dir=tools_dir / ".." / "agam",
        scope_root=tmp_path / ".agam" / "knowledge" / "scopes",
    )

    for event in result["hooks"]:
        for command in _commands(result, event):
            assert str(hooks_dir) in command
            assert "/../" not in command
            assert f"AGAM_TOOLS_DIR={tools_dir}" in command


def test_commands_pin_only_safe_policy_metadata(tmp_path: Path):
    hooks_dir = tmp_path / ".codex" / "hooks" / "agam"
    scope_root = tmp_path / ".agam" / "knowledge" / "scopes"

    result = merger.merge_hooks_into_file(
        tmp_path / ".codex" / "hooks.json",
        hooks_dir,
        scope_root=scope_root,
    )

    serialized = json.dumps(result)
    assert f"AGAM_KNOWLEDGE_SCOPE_ROOT={scope_root}" in serialized
    assert f"AGAM_KNOWLEDGE_CONFIG={scope_root / 'config.json'}" in serialized
    assert f"AGAM_ACTIVE_MANIFEST={scope_root / 'active.json'}" in serialized
    assert "AGAM_KNOWLEDGE_PROFILE" not in serialized
    assert "AGAM_KG_PATH" not in serialized
    assert "/sealed/" not in serialized
    assert "/vault_" not in serialized
    assert "graph.db" not in serialized


def test_codex_refuses_removed_profile_selectors(tmp_path: Path):
    with pytest.raises(ValueError, match="profile_selector_removed"):
        merger.agam_hook_entries(
            tmp_path / "hooks",
            scope_root=tmp_path / "scopes",
            profile="expanded",
        )


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

    result = merger.merge_hooks_into_file(
        hooks_path, tmp_path / "hooks", scope_root=tmp_path / "scopes"
    )

    assert result["custom"] == {"enabled": True}
    assert result["hooks"]["SessionStart"][0]["hooks"][0]["command"] == (
        "/user/start.py"
    )
    assert "/user/stop.py" in _commands(result, "Stop")
    assert all("codex_stop.py" not in command for command in _commands(result, "Stop"))


def test_update_removes_only_obsolete_agam_owned_capture_entries(tmp_path: Path):
    hooks_dir = (tmp_path / ".codex" / "hooks" / "agam").resolve()
    hooks_path = tmp_path / ".codex" / "hooks.json"
    user_stop = "/user/hooks/codex_stop.py"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": user_stop}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(hooks_dir / "codex_stop.py"),
                                }
                            ]
                        },
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(
                                        hooks_dir / "lesson_activate_post.py"
                                    ),
                                }
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    result = merger.merge_hooks_into_file(
        hooks_path, hooks_dir, scope_root=tmp_path / "scopes"
    )

    serialized = json.dumps(result)
    assert user_stop in serialized
    assert str(hooks_dir / "codex_stop.py") not in serialized
    assert str(hooks_dir / "lesson_activate_post.py") not in serialized


def test_idempotent(tmp_path: Path):
    hooks_path = tmp_path / "hooks.json"
    hooks_dir = tmp_path / "hooks"

    kwargs = {"scope_root": tmp_path / "scopes"}
    first = merger.merge_hooks_into_file(hooks_path, hooks_dir, **kwargs)
    second = merger.merge_hooks_into_file(hooks_path, hooks_dir, **kwargs)
    third = merger.merge_hooks_into_file(hooks_path, hooks_dir, **kwargs)

    assert first == second == third
    assert len(third["hooks"]["UserPromptSubmit"]) == 1
    assert len(third["hooks"]["PreToolUse"]) == 1
    assert set(third["hooks"]) == {"UserPromptSubmit", "PreToolUse"}


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
            merger.merge_hooks_into_file(
                hooks_path,
                tmp_path / "hooks",
                scope_root=tmp_path / "scopes",
            )

    assert hooks_path.read_text() == original
    assert list(tmp_path.glob(".hooks-*.json.tmp")) == []

"""Tests for ``agam.settings_merger``.

All tests operate against a ``tmp_path`` tempdir. No test reads or writes
any ``~/.claude/settings.json``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from agam.settings_merger import merge_hooks, merge_hooks_into_settings


# ---------------------------------------------------------------------------
# The four tests required by the plan
# ---------------------------------------------------------------------------


def test_merges_into_empty_settings():
    existing: dict = {}
    result = merge_hooks(
        existing,
        {"Stop": [{"command": "~/.claude/hooks/session_close.py"}]},
    )
    # Event key exists and carries one block whose inner command matches.
    stop_blocks = result["hooks"]["Stop"]
    assert len(stop_blocks) == 1
    assert stop_blocks[0]["hooks"][0]["command"] == (
        "~/.claude/hooks/session_close.py"
    )


def test_skips_duplicate_hook():
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "~/.claude/hooks/session_close.py",
                        }
                    ],
                }
            ]
        }
    }
    result = merge_hooks(
        existing,
        {"Stop": [{"command": "~/.claude/hooks/session_close.py"}]},
    )
    # Still one block -- duplicate command was skipped.
    assert len(result["hooks"]["Stop"]) == 1


def test_conflicting_hook_appends_rather_than_raises():
    # Name from the plan is slightly misleading: Claude Code supports
    # multiple hooks per event, so a different command for the same
    # event should coexist, not raise.
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "~/.claude/hooks/other.py",
                        }
                    ],
                }
            ]
        }
    }
    result = merge_hooks(
        existing,
        {"Stop": [{"command": "~/.claude/hooks/session_close.py"}]},
    )
    assert len(result["hooks"]["Stop"]) == 2


def test_preserves_non_hook_keys():
    existing = {"otherKey": "preserved", "hooks": {}}
    result = merge_hooks(existing, {"Stop": [{"command": "x"}]})
    assert result["otherKey"] == "preserved"


# ---------------------------------------------------------------------------
# Additional tests: realistic Claude Code schema edge cases
# ---------------------------------------------------------------------------


def test_dedup_on_nested_inner_command_shape():
    # Existing block uses the real Claude Code shape with an inner
    # hooks[].command. New hook arrives in simplified shape. Both should
    # be recognized as identical and the new one dropped.
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/x/graph_recall.py",
                            "timeout": 5,
                        }
                    ],
                }
            ]
        }
    }
    result = merge_hooks(
        existing,
        {"UserPromptSubmit": [{"command": "/x/graph_recall.py"}]},
    )
    assert len(result["hooks"]["UserPromptSubmit"]) == 1


def test_matcher_aware_dedup_different_matcher_is_different_hook():
    # Same command, different matcher -> treat as distinct. Claude Code
    # runs a matcher="Bash" hook only on Bash tools and a matcher="" hook
    # on every tool, so merging them would change behavior.
    existing = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/x/check.py",
                        }
                    ],
                }
            ]
        }
    }
    result = merge_hooks(
        existing,
        {
            "PreToolUse": [
                {"command": "/x/check.py", "matcher": "Write|Edit"},
            ]
        },
    )
    assert len(result["hooks"]["PreToolUse"]) == 2


def test_merge_does_not_mutate_inputs():
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "/x/a.py"}
                    ],
                }
            ]
        }
    }
    new = {"Stop": [{"command": "/x/b.py"}]}
    snapshot_existing = json.dumps(existing, sort_keys=True)
    snapshot_new = json.dumps(new, sort_keys=True)
    merge_hooks(existing, new)
    assert json.dumps(existing, sort_keys=True) == snapshot_existing
    assert json.dumps(new, sort_keys=True) == snapshot_new


def test_merge_hooks_into_settings_creates_file_when_missing(tmp_path: Path):
    settings_path = tmp_path / ".claude" / "settings.json"
    hooks_dir = tmp_path / ".claude" / "hooks"
    assert not settings_path.exists()

    result = merge_hooks_into_settings(settings_path, hooks_dir)

    assert settings_path.exists()
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk == result
    # Every Agam event is registered.
    assert set(on_disk["hooks"].keys()) == {
        "UserPromptSubmit",
        "Stop",
        "PreToolUse",
        "PostToolUse",
    }
    # Stop has two Agam hooks: graph_update + session_close.
    stop_cmds = [
        inner["command"]
        for block in on_disk["hooks"]["Stop"]
        for inner in block["hooks"]
    ]
    assert any("graph_update.py" in c for c in stop_cmds)
    assert any("session_close.py" in c for c in stop_cmds)
    # PreToolUse must carry both the Bash matcher AND the Edit|Write|MultiEdit
    # matcher for the lesson hook. Without the second matcher, file-path
    # lessons never fire.
    pre_matchers = [block.get("matcher", "") for block in on_disk["hooks"]["PreToolUse"]]
    assert "Bash" in pre_matchers
    assert any("Edit" in m and "Write" in m and "MultiEdit" in m for m in pre_matchers), (
        f"PreToolUse missing Edit|Write|MultiEdit matcher; got: {pre_matchers}"
    )


def test_merge_hooks_into_settings_is_idempotent(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"

    first = merge_hooks_into_settings(settings_path, hooks_dir)
    second = merge_hooks_into_settings(settings_path, hooks_dir)
    third = merge_hooks_into_settings(settings_path, hooks_dir)

    # Every run yields the exact same structure -- no duplicate blocks,
    # no drift.
    assert first == second == third
    disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert disk == first


def test_merge_hooks_into_settings_preserves_user_hooks(tmp_path: Path):
    # Pre-existing unrelated user hooks should still be present after merge.
    settings_path = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    user_settings = {
        "model": "opus",
        "env": {"FOO": "bar"},
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "~/.claude/hooks/user-custom.py",
                            "timeout": 10,
                        }
                    ],
                }
            ]
        },
    }
    settings_path.write_text(json.dumps(user_settings), encoding="utf-8")

    merge_hooks_into_settings(settings_path, hooks_dir)

    disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert disk["model"] == "opus"
    assert disk["env"] == {"FOO": "bar"}
    # User hook still there; Agam's two Stop hooks appended.
    stop_cmds = [
        inner["command"]
        for block in disk["hooks"]["Stop"]
        for inner in block["hooks"]
    ]
    assert "~/.claude/hooks/user-custom.py" in stop_cmds
    assert any("graph_update.py" in c for c in stop_cmds)
    assert any("session_close.py" in c for c in stop_cmds)


def test_atomic_write_preserves_original_on_failure(tmp_path: Path):
    # Simulate a write failure after the tempfile is opened. The sibling
    # tempfile should be cleaned up and the original file untouched.
    settings_path = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    original = {"model": "opus", "hooks": {}}
    raw_before = json.dumps(original, indent=2) + "\n"
    settings_path.write_text(raw_before, encoding="utf-8")

    with mock.patch(
        "agam.settings_merger.os.replace",
        side_effect=OSError("simulated failure"),
    ):
        with pytest.raises(OSError, match="simulated failure"):
            merge_hooks_into_settings(settings_path, hooks_dir)

    # Original file is byte-identical. No stray tempfile left behind.
    assert settings_path.read_text(encoding="utf-8") == raw_before
    leftovers = list(settings_path.parent.glob(".settings-*.json.tmp"))
    assert leftovers == [], f"tempfile not cleaned up: {leftovers}"


def test_unicode_non_hook_keys_survive_roundtrip(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    user_settings = {
        "displayName": "Kalyan",
        "greeting": "Namaste",
        "hooks": {},
    }
    settings_path.write_text(
        json.dumps(user_settings, ensure_ascii=False),
        encoding="utf-8",
    )

    merge_hooks_into_settings(settings_path, hooks_dir)

    disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert disk["displayName"] == "Kalyan"
    assert disk["greeting"] == "Namaste"

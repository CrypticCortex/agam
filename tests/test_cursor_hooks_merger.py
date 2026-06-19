"""Tests for the Cursor hooks.json merger."""

import json
from pathlib import Path

from agam import cursor_hooks_merger as m


def test_creates_file_with_version(tmp_path):
    hp = tmp_path / "hooks.json"
    merged = m.merge_hooks_into_file(hp, tmp_path / "hooks")
    assert hp.exists()
    assert merged["version"] == 1
    assert "stop" in merged["hooks"]
    assert "sessionEnd" in merged["hooks"]


def test_idempotent(tmp_path):
    hp = tmp_path / "hooks.json"
    m.merge_hooks_into_file(hp, tmp_path / "hooks")
    m.merge_hooks_into_file(hp, tmp_path / "hooks")
    data = json.loads(hp.read_text())
    assert len(data["hooks"]["stop"]) == 1
    assert len(data["hooks"]["sessionEnd"]) == 1


def test_preserves_existing_user_hooks(tmp_path):
    hp = tmp_path / "hooks.json"
    hp.write_text(json.dumps({
        "version": 1,
        "hooks": {
            "afterFileEdit": [{"command": "./my-formatter.sh"}],
            "stop": [{"command": "./my-own-stop.sh"}],
        },
    }))
    merged = m.merge_hooks_into_file(hp, tmp_path / "hooks")
    # User's formatter preserved.
    assert merged["hooks"]["afterFileEdit"][0]["command"] == "./my-formatter.sh"
    # User's stop hook preserved AND agam's stop appended.
    stop_cmds = [b["command"] for b in merged["hooks"]["stop"]]
    assert "./my-own-stop.sh" in stop_cmds
    assert any("cursor_stop.py" in c for c in stop_cmds)


def test_absolute_command_paths(tmp_path):
    hooks_dir = tmp_path / "hooks"
    merged = m.merge_hooks_into_file(tmp_path / "hooks.json", hooks_dir)
    cmd = merged["hooks"]["stop"][0]["command"]
    assert cmd == str(hooks_dir / "cursor_stop.py")
    assert Path(cmd).is_absolute()

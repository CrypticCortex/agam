"""Tests for agent detection and per-agent install wiring."""

import json

import pytest

from agam.agents import ClaudeAgent, CodexAgent, CursorAgent, detect_agents


@pytest.fixture(autouse=True)
def no_path_binaries(monkeypatch):
    """Make detection dir-based by default (no agent CLIs on PATH)."""
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)


def test_detect_none(tmp_path):
    assert detect_agents(tmp_path) == []


def test_detect_claude_only(tmp_path):
    (tmp_path / ".claude").mkdir()
    names = {a.name for a in detect_agents(tmp_path)}
    assert names == {"claude"}


def test_detect_cursor_only(tmp_path):
    (tmp_path / ".cursor").mkdir()
    names = {a.name for a in detect_agents(tmp_path)}
    assert names == {"cursor"}


def test_detect_codex_only(tmp_path):
    (tmp_path / ".codex").mkdir()
    names = {a.name for a in detect_agents(tmp_path)}
    assert names == {"codex"}


def test_detect_both(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    names = {a.name for a in detect_agents(tmp_path)}
    assert names == {"claude", "cursor"}


def test_detect_via_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shutil.which",
        lambda b, *a, **k: (
            f"/usr/bin/{b}" if b in ("cursor", "cursor-agent", "codex") else None
        ),
    )
    names = {a.name for a in detect_agents(tmp_path)}
    assert "cursor" in names
    assert "codex" in names


def test_cursor_install_writes_hooks_and_tools(tmp_path):
    CursorAgent().install(tmp_path)
    hooks = tmp_path / ".cursor" / "hooks"
    assert (hooks / "cursor_stop.py").exists()
    assert (hooks / "cursor_session_end.py").exists()
    tools = tmp_path / ".cursor" / "tools" / "agam"
    assert (tools / "transcripts.py").exists()
    assert (tools / "pending_queue.py").exists()
    assert (tools / "cursor_rule.py").exists()
    cfg = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert "stop" in cfg["hooks"]
    assert "sessionEnd" in cfg["hooks"]


def test_cursor_install_hooks_executable(tmp_path):
    import os
    CursorAgent().install(tmp_path)
    hook = tmp_path / ".cursor" / "hooks" / "cursor_stop.py"
    assert os.access(hook, os.X_OK)


def test_cursor_install_idempotent(tmp_path):
    CursorAgent().install(tmp_path)
    CursorAgent().install(tmp_path)
    cfg = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    assert len(cfg["hooks"]["stop"]) == 1


def test_codex_install_writes_hooks_tools_and_config(tmp_path):
    CodexAgent().install(tmp_path)

    hooks = tmp_path / ".codex" / "hooks" / "agam"
    assert (hooks / "graph_recall.py").exists()
    assert (hooks / "codex_stop.py").exists()
    assert (hooks / "lesson_activate.py").exists()
    assert (hooks / "lesson_activate_post.py").exists()

    tools = tmp_path / ".codex" / "tools" / "agam"
    assert (tools / "pending_queue.py").exists()
    assert (tools / "transcripts.py").exists()

    config = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(config["hooks"]) == {
        "UserPromptSubmit",
        "Stop",
        "PreToolUse",
        "PostToolUse",
    }
    assert {group["matcher"] for group in config["hooks"]["PreToolUse"]} == {
        "Bash",
        "Edit|Write",
    }
    for groups in config["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                assert str(hooks) in handler["command"]
                assert str(tools) in handler["command"]


def test_codex_install_never_overwrites_generic_user_hooks(tmp_path):
    generic_hooks = tmp_path / ".codex" / "hooks"
    generic_hooks.mkdir(parents=True)
    originals = {
        name: f"#!/bin/sh\n# user-owned {name}\n".encode()
        for name in (
            "graph_recall.py",
            "codex_stop.py",
            "lesson_activate.py",
            "lesson_activate_post.py",
        )
    }
    for name, content in originals.items():
        (generic_hooks / name).write_bytes(content)
    user_command = str(generic_hooks / "graph_recall.py")
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "user_setting": {"preserved": True},
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": user_command}
                            ]
                        }
                    ]
                },
            }
        )
    )

    CodexAgent().install(tmp_path)

    for name, content in originals.items():
        assert (generic_hooks / name).read_bytes() == content
        assert (generic_hooks / "agam" / name).exists()

    config = json.loads(hooks_path.read_text())
    assert config["user_setting"] == {"preserved": True}
    commands = [
        handler["command"]
        for groups in config["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert user_command in commands
    agam_commands = [command for command in commands if "AGAM_TOOLS_DIR=" in command]
    assert len(agam_commands) == 5
    assert all(
        str(generic_hooks / "agam") in command for command in agam_commands
    )
    assert all(
        f"{generic_hooks}/{name}" not in command
        for command in agam_commands
        for name in originals
    )


def test_codex_install_is_idempotent(tmp_path):
    CodexAgent().install(tmp_path)
    CodexAgent().install(tmp_path)

    config = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert len(config["hooks"]["UserPromptSubmit"]) == 1
    assert len(config["hooks"]["Stop"]) == 1
    assert len(config["hooks"]["PreToolUse"]) == 2
    assert len(config["hooks"]["PostToolUse"]) == 1


def test_claude_install_writes_hooks_and_settings(tmp_path):
    ClaudeAgent().install(tmp_path)
    hooks = tmp_path / ".claude" / "hooks"
    assert (hooks / "graph_recall.py").exists()
    assert (hooks / "session_close.py").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    # Hooks registered + data home pinned.
    assert "hooks" in settings
    assert settings["env"]["AGAM_DATA_HOME"] == str(tmp_path / ".agam")


def test_claude_install_no_cursor_hooks_leak(tmp_path):
    ClaudeAgent().install(tmp_path)
    hooks = tmp_path / ".claude" / "hooks"
    assert not (hooks / "cursor_stop.py").exists()

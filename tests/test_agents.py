"""Tests for agent detection and per-agent install wiring."""

import json

import pytest

from agam.agents import ClaudeAgent, CursorAgent, detect_agents


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


def test_detect_both(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    names = {a.name for a in detect_agents(tmp_path)}
    assert names == {"claude", "cursor"}


def test_detect_via_path(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b, *a, **k: "/usr/bin/cursor" if b in ("cursor", "cursor-agent") else None)
    names = {a.name for a in detect_agents(tmp_path)}
    assert "cursor" in names


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

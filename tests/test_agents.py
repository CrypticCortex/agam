"""Tests for agent detection and per-agent install wiring."""

import json
import os
import sqlite3
import subprocess
import sys
import tomllib

import pytest

from agam.agents import ClaudeAgent, CodexAgent, CursorAgent, detect_agents
from agam.agents import codex as codex_module
from agam.codex_permissions_merger import PERMISSION_PROFILE
from agam.vault_registry import VaultRole, initialize_registry


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


def test_codex_install_writes_only_safe_hooks_and_config(tmp_path):
    registry = initialize_registry(
        tmp_path / ".agam" / "knowledge" / "scopes" / "registry.json",
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    CodexAgent().install(tmp_path)

    hooks = tmp_path / ".codex" / "hooks" / "agam"
    assert {path.name for path in hooks.iterdir()} == {
        "graph_recall.py",
        "scope_guard.py",
    }
    assert not (tmp_path / ".codex" / "tools" / "agam").exists()

    config = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(config["hooks"]) == {"UserPromptSubmit", "PreToolUse"}
    assert {group["matcher"] for group in config["hooks"]["PreToolUse"]} == {
        "*",
    }
    for groups in config["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                assert str(hooks) in handler["command"]
                assert "AGAM_RECALL_AGENT=codex" in handler["command"]
                assert (
                    f"PYTHONPATH={codex_module._PACKAGE_IMPORT_ROOT}"
                    in handler["command"]
                )
                assert "AGAM_TOOLS_DIR=" not in handler["command"]
    serialized = json.dumps(config)
    assert "codex_stop.py" not in serialized
    assert "lesson_activate" not in serialized
    permissions = tomllib.loads(
        (tmp_path / ".codex" / "config.toml").read_text()
    )["permissions"][PERMISSION_PROFILE]
    knowledge = (tmp_path / ".agam" / "knowledge").resolve()
    assert permissions["extends"] == ":workspace"
    assert permissions["filesystem"][str(knowledge)] == "deny"
    guidance_id = registry.for_role(VaultRole.GUIDANCE).id
    solutions_id = registry.for_role(VaultRole.SOLUTIONS).id
    assert permissions["filesystem"][str(knowledge / "scopes" / guidance_id)] == "read"
    assert permissions["filesystem"][str(knowledge / "scopes" / solutions_id)] == "read"


def test_codex_installed_recall_hook_can_import_packaged_agam(tmp_path):
    CodexAgent().install(tmp_path)
    hook = tmp_path / ".codex" / "hooks" / "agam" / "graph_recall.py"
    scope_root = tmp_path / ".agam" / "knowledge" / "scopes"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(codex_module._PACKAGE_IMPORT_ROOT),
        "AGAM_KNOWLEDGE_SCOPE_ROOT": str(scope_root),
        "AGAM_KNOWLEDGE_CONFIG": str(scope_root / "config.json"),
        "AGAM_ACTIVE_MANIFEST": str(scope_root / "active.json"),
    }

    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(
            {"session_id": "synthetic", "prompt": "synthetic smoke"}
        ),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_codex_install_never_overwrites_generic_user_hooks(tmp_path):
    initialize_registry(
        tmp_path / ".agam" / "knowledge" / "scopes" / "registry.json",
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    generic_hooks = tmp_path / ".codex" / "hooks"
    generic_hooks.mkdir(parents=True)
    originals = {
        name: f"#!/bin/sh\n# user-owned {name}\n".encode()
        for name in (
            "graph_recall.py",
            "scope_guard.py",
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
    assert {path.name for path in (generic_hooks / "agam").iterdir()} == {
        "graph_recall.py",
        "scope_guard.py",
    }
    config = json.loads(hooks_path.read_text())
    assert config["user_setting"] == {"preserved": True}
    commands = [
        handler["command"]
        for groups in config["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert user_command in commands
    agam_commands = [
        command
        for command in commands
        if "AGAM_RECALL_AGENT=codex" in command
    ]
    assert len(agam_commands) == 2
    assert all(
        str(generic_hooks / "agam") in command for command in agam_commands
    )
    assert all(
        f"{generic_hooks}/{name}" not in command
        for command in agam_commands
        for name in originals
    )


def test_codex_install_refuses_owned_hook_symlink_without_touching_target(
    tmp_path,
):
    hooks = tmp_path / ".codex" / "hooks" / "agam"
    hooks.mkdir(parents=True)
    target = tmp_path / "synthetic-user-target.py"
    original = b"# synthetic user target\n"
    target.write_bytes(original)
    (hooks / "graph_recall.py").symlink_to(target)

    with pytest.raises(OSError):
        CodexAgent().install(tmp_path)

    assert target.read_bytes() == original
    assert (hooks / "graph_recall.py").is_symlink()


def test_codex_install_refuses_symlinked_hooks_ancestor(tmp_path):
    external = tmp_path / "synthetic-restricted"
    external.mkdir()
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "hooks").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        CodexAgent().install(tmp_path)

    assert not (external / "agam").exists()


def test_codex_install_is_idempotent(tmp_path):
    CodexAgent().install(tmp_path)
    CodexAgent().install(tmp_path)

    config = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert len(config["hooks"]["UserPromptSubmit"]) == 1
    assert len(config["hooks"]["PreToolUse"]) == 1
    assert set(config["hooks"]) == {"UserPromptSubmit", "PreToolUse"}


def test_claude_install_writes_hooks_and_settings(tmp_path):
    ClaudeAgent().install(tmp_path)
    hooks = tmp_path / ".claude" / "hooks"
    assert (hooks / "graph_recall.py").exists()
    assert (hooks / "session_close.py").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    # Hooks registered + data home pinned.
    assert "hooks" in settings
    assert settings["env"]["AGAM_DATA_HOME"] == str(tmp_path / ".agam")


def test_claude_installed_recall_hook_is_standalone_and_uses_legacy_graph(
    tmp_path,
):
    ClaudeAgent().install(tmp_path)
    hook = tmp_path / ".claude" / "hooks" / "graph_recall.py"
    knowledge = tmp_path / "synthetic-knowledge"
    knowledge.mkdir()
    database = knowledge / "graph.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT,
            description TEXT,
            created TEXT,
            updated TEXT
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            relation TEXT,
            weight REAL DEFAULT 1.0
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER,
            key TEXT,
            value TEXT,
            updated TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO entities(name, type, description) VALUES (?, ?, ?)",
        (
            "synthetic-legacy-marker",
            "project",
            "Unclassified synthetic Claude compatibility marker.",
        ),
    )
    connection.commit()
    connection.close()
    (knowledge / "entity-names.txt").write_text(
        "synthetic-legacy-marker\n", encoding="utf-8"
    )
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(temporary),
        "AGAM_RECALL_AGENT": "claude",
        "AGAM_KG_PATH": str(database),
        "AGAM_KG_DIR": str(knowledge),
        "AGAM_CONTEXT_TOOL": str(tmp_path / "missing-context-tool.py"),
    }

    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(
            {
                "session_id": "synthetic-claude-compat",
                "prompt": "tell me about synthetic-legacy-marker project",
            }
        ),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "synthetic-legacy-marker" in context


def test_claude_install_no_cursor_hooks_leak(tmp_path):
    ClaudeAgent().install(tmp_path)
    hooks = tmp_path / ".claude" / "hooks"
    assert not (hooks / "cursor_stop.py").exists()

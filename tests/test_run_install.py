"""Tests for the neutral-home multi-agent install orchestrator."""

import json
import os
import plistlib
import sqlite3
import tomllib

from agam.codex_permissions_merger import PERMISSION_PROFILE
from agam.installer import run_install
from agam.vault_registry import VaultRole, load_registry


def _answers(tmp_path):
    return {
        "name": "Kalyan",
        "primary-goal": "ship agam",
        "projects-dir": str(tmp_path / "coding"),
        "platform": "mac",
        "bootstrap-now": False,
    }


def test_installs_shared_home_and_both_agents(tmp_path):
    res = run_install(
        _answers(tmp_path), targets=["claude", "cursor"],
        home=tmp_path, write_plist=False,
    )
    agam = tmp_path / ".agam"
    # Shared data home.
    assert (agam / "config.yaml").exists()
    assert (agam / "AGAM.md").exists()
    assert (agam / "prompts" / "work-log.txt").exists()
    assert (agam / "knowledge" / "graph.db").exists()
    policy_path = agam / "knowledge" / "scopes" / "config.json"
    policy = json.loads(policy_path.read_text())
    registry_path = policy_path.with_name("registry.json")
    vaults = load_registry(registry_path)
    assert policy_path.stat().st_mode & 0o777 == 0o600
    assert policy_path.parent.stat().st_mode & 0o777 == 0o700
    assert vaults.for_role(VaultRole.GUIDANCE).name == "Guidance"
    assert vaults.for_role(VaultRole.SOLUTIONS).name == "Solutions"
    assert len(vaults.selected_for("codex")) == 2
    assert len(vaults.selected_for("claude")) == 2
    assert len(vaults.selected_for("cursor")) == 2
    assert policy["agents"]["codex"] == {
        "recall": True,
        "boot-injection": False,
        "capture": False,
    }
    assert not (agam / "knowledge" / "scopes" / "active.json").exists()
    # Shared watchdog hooks/tools copy.
    assert (agam / "hooks" / "agam_watchdog_inner.py").exists()
    assert (agam / "tools" / "agam" / "transcripts.py").exists()
    # Cursor wiring.
    assert (tmp_path / ".cursor" / "hooks" / "cursor_stop.py").exists()
    assert (tmp_path / ".cursor" / "hooks.json").exists()
    # Claude wiring.
    assert (tmp_path / ".claude" / "hooks" / "graph_recall.py").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["env"]["AGAM_DATA_HOME"] == str(agam)
    assert set(res.targets) == {"claude", "cursor"}
    assert res.migration_status == "fresh"


def test_cursor_only(tmp_path):
    res = run_install(
        _answers(tmp_path), targets=["cursor"], home=tmp_path, write_plist=False,
    )
    assert (tmp_path / ".cursor" / "hooks.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert res.targets == ["cursor"]


def test_codex_only(tmp_path):
    res = run_install(
        _answers(tmp_path), targets=["codex"], home=tmp_path, write_plist=False,
    )
    assert (tmp_path / ".codex" / "hooks.json").exists()
    assert (
        tmp_path / ".codex" / "hooks" / "agam" / "graph_recall.py"
    ).exists()
    assert (
        tmp_path / ".codex" / "hooks" / "agam" / "scope_guard.py"
    ).exists()
    assert not (
        tmp_path / ".codex" / "hooks" / "agam" / "codex_stop.py"
    ).exists()
    assert not (tmp_path / ".codex" / "tools" / "agam").exists()
    config = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(config["hooks"]) == {"UserPromptSubmit", "PreToolUse"}
    assert "AGAM_KNOWLEDGE_PROFILE" not in json.dumps(config)
    assert "AGAM_RECALL_AGENT=codex" in json.dumps(config)
    permissions = tomllib.loads(
        (tmp_path / ".codex" / "config.toml").read_text()
    )["permissions"][PERMISSION_PROFILE]
    assert permissions["filesystem"][str((tmp_path / ".agam" / "knowledge").resolve())] == "deny"
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".cursor" / "hooks.json").exists()
    assert res.targets == ["codex"]


def test_install_preserves_existing_scope_policy(tmp_path):
    scopes = tmp_path / ".agam" / "knowledge" / "scopes"
    scopes.mkdir(parents=True)
    policy = scopes / "config.json"
    custom = {"synthetic-user-policy": True}
    policy.write_text(json.dumps(custom))

    run_install(
        _answers(tmp_path), targets=["codex"], home=tmp_path, write_plist=False,
    )

    assert json.loads(policy.read_text()) == custom
    assert not (scopes / "active.json").exists()


def test_install_migrates_fixed_scope_layout_before_creating_registry(tmp_path):
    from tests.test_vault_migration import _legacy_state

    legacy_root = _legacy_state(tmp_path / ".agam" / "knowledge")

    run_install(
        _answers(tmp_path), targets=["codex"], home=tmp_path, write_plist=False,
    )

    registry = json.loads((legacy_root / "registry.json").read_text())
    active = json.loads((legacy_root / "active.json").read_text())
    manifest = json.loads((legacy_root / active["manifest"]).read_text())
    assert set(manifest["stores"]) == {
        vault["id"] for vault in registry["vaults"]
    }


def test_codex_plist_persists_resolved_executable(monkeypatch, tmp_path):
    tool_dir = tmp_path / "custom & toolchain"
    tool_dir.mkdir()
    codex = tool_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)

    monkeypatch.setattr(
        "agam.installer.shutil.which",
        lambda name: str(codex) if name == "codex" else None,
    )
    res = run_install(
        _answers(tmp_path), targets=["codex"], home=tmp_path, write_plist=True,
    )

    assert res.wrote_plist
    plist = tmp_path / "Library" / "LaunchAgents" / "com.agam.watchdog.plist"
    data = plistlib.loads(plist.read_bytes())
    env = data["EnvironmentVariables"]
    assert env["AGAM_LLM_CLI_PIN"] == "codex"
    assert env["AGAM_LLM_CLI_PATH"] == str(codex.resolve())
    assert os.access(env["AGAM_LLM_CLI_PATH"], os.X_OK)


def test_preserves_existing_graph(tmp_path):
    # Pre-seed a graph with a sentinel entity; install must not clobber it.
    kdir = tmp_path / ".agam" / "knowledge"
    kdir.mkdir(parents=True)
    db = kdir / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO entities (name) VALUES ('sentinel-entity')")
    conn.commit()
    conn.close()

    run_install(_answers(tmp_path), targets=["cursor"], home=tmp_path, write_plist=False)

    conn = sqlite3.connect(str(db))
    names = [r[0] for r in conn.execute("SELECT name FROM entities")]
    conn.close()
    assert "sentinel-entity" in names


def test_migrates_legacy_claude(tmp_path):
    # Legacy ~/.claude graph present, no ~/.agam -> migrate then install.
    kdir = tmp_path / ".claude" / "knowledge"
    kdir.mkdir(parents=True)
    db = kdir / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO entities (name) VALUES ('legacy-entity')")
    conn.commit()
    conn.close()

    res = run_install(_answers(tmp_path), targets=["cursor"], home=tmp_path, write_plist=False)
    assert res.migration_status == "migrated"
    migrated = tmp_path / ".agam" / "knowledge" / "graph.db"
    conn = sqlite3.connect(str(migrated))
    names = [r[0] for r in conn.execute("SELECT name FROM entities")]
    conn.close()
    assert "legacy-entity" in names


def test_cursor_queue_does_not_block_legacy_graph_migration(tmp_path):
    kdir = tmp_path / ".claude" / "knowledge"
    kdir.mkdir(parents=True)
    legacy = kdir / "graph.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO entities (name) VALUES ('legacy-with-cursor-queue')")
    conn.commit()
    conn.close()
    queued = tmp_path / ".agam" / "queue" / "cursor.json"
    queued.parent.mkdir(parents=True)
    queued.write_text('{"agent":"cursor"}\n')

    res = run_install(
        _answers(tmp_path), targets=["codex"], home=tmp_path, write_plist=False,
    )

    assert res.migration_status == "migrated"
    assert queued.exists()
    migrated = tmp_path / ".agam" / "knowledge" / "graph.db"
    conn = sqlite3.connect(str(migrated))
    names = [row[0] for row in conn.execute("SELECT name FROM entities")]
    conn.close()
    assert "legacy-with-cursor-queue" in names

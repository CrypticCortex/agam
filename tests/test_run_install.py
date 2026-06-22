"""Tests for the neutral-home multi-agent install orchestrator."""

import json
import sqlite3

from agam.installer import run_install


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

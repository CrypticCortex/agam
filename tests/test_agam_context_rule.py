"""Tests for the `agam_context.py render-rule` Cursor digest."""

import sqlite3
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).parent.parent / "src" / "agam" / "tools" / "agam_context.py"


def _make_kg(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT, type TEXT, description TEXT,
            created TEXT, updated TEXT, last_referenced TEXT
        );
        CREATE TABLE properties (
            entity_id INTEGER, key TEXT, value TEXT, updated TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('voice-fnol-poc', 'project', 'FNOL voice agent', '2026-01-01', '2026-06-18')"
    )
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('pip-in-conda', 'lesson', 'never pip install in conda env', '2026-01-01', '2026-06-17')"
    )
    eid = conn.execute("SELECT id FROM entities WHERE name='pip-in-conda'").fetchone()[0]
    conn.execute(
        "INSERT INTO properties (entity_id, key, value, updated) VALUES (?, 'severity', 'high', '2026-06-17')",
        (eid,),
    )
    conn.commit()
    conn.close()


def _run_render_rule(home, kg):
    env = {
        "AGAM_HOME": str(home),
        "AGAM_KG_PATH": str(kg),
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(home),
    }
    return subprocess.run(
        [sys.executable, str(TOOL), "render-rule"],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_render_rule_has_frontmatter_and_content(tmp_path):
    home = tmp_path / ".agam"
    home.mkdir()
    (home / "config.yaml").write_text('name: Kalyan\nprimary-goal: "ship agam"\n')
    kg = home / "graph.db"
    _make_kg(kg)

    r = _run_render_rule(home, kg)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.startswith("---\nalwaysApply: true\n---")
    assert "User: Kalyan" in out
    assert "ship agam" in out
    assert "voice-fnol-poc" in out
    assert "pip-in-conda" in out  # high-severity lesson surfaced


def test_render_rule_survives_missing_kg(tmp_path):
    home = tmp_path / ".agam"
    home.mkdir()
    r = _run_render_rule(home, home / "missing.db")
    assert r.returncode == 0
    assert "alwaysApply: true" in r.stdout

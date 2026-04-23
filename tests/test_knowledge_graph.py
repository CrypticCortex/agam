"""Tests for the ported knowledge_graph.py tool.

All tests operate on a tempdir-backed DB via AGAM_KG_PATH redirection.
The real DB at ~/.claude/knowledge/graph.db must never be touched.
"""

import os
import pathlib
import sqlite3
import subprocess

import pytest

TOOL = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src"
    / "agam"
    / "tools"
    / "knowledge_graph.py"
)
SCHEMA = (
    pathlib.Path(__file__).resolve().parent.parent
    / "knowledge"
    / "graph-schema.sql"
)


@pytest.fixture
def kg_db(tmp_path):
    """Fresh DB with Agam schema applied, pointed at by AGAM_KG_PATH."""
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()
    return db


def _run(db_path, *args, input_text=None):
    """Invoke the tool via `uv run --script` so the PEP 723 header
    (which declares `networkx` as a dependency for the `path` command)
    is honored. Plain `uv run python <tool>` ignores the inline metadata.
    """
    env = {**os.environ, "AGAM_KG_PATH": str(db_path)}
    return subprocess.run(
        ["uv", "run", "--script", str(TOOL), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        input=input_text,
    )


# ---- core CRUD + query ----------------------------------------------------


def test_entity_add_then_query(kg_db):
    r = _run(kg_db, "entity", "voice-fnol-poc", "project", "Voice-driven claim intake")
    assert r.returncode == 0, r.stderr
    r = _run(kg_db, "query", "voice-fnol-poc")
    assert r.returncode == 0
    assert "voice-fnol-poc" in r.stdout.lower()


def test_relate_then_traverse(kg_db):
    _run(kg_db, "entity", "a", "concept", "alpha")
    _run(kg_db, "entity", "b", "concept", "beta")
    r = _run(kg_db, "relate", "a", "links-to", "b")
    assert r.returncode == 0, r.stderr
    r = _run(kg_db, "traverse", "a", "2")
    assert r.returncode == 0
    assert "b" in r.stdout.lower()


def test_search_fts(kg_db):
    _run(kg_db, "entity", "redis", "tech", "In-memory data store")
    r = _run(kg_db, "search", "in-memory")
    assert r.returncode == 0
    assert "redis" in r.stdout.lower()


def test_stats_empty_db(kg_db):
    r = _run(kg_db, "stats")
    assert r.returncode == 0
    assert r.stdout.strip() != ""


# ---- redirection invariance ----------------------------------------------


def test_agam_kg_path_redirects(tmp_path):
    """AGAM_KG_PATH actually reroutes writes to the tempdir DB."""
    db = tmp_path / "custom.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()

    r = _run(db, "entity", "sentinel", "test", "redirect check")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name FROM entities WHERE name = 'sentinel'"
    ).fetchall()
    conn.close()
    assert rows == [("sentinel",)]


# ---- edge cases spotted during the port ----------------------------------


def test_relate_nonexistent_entity_fails(kg_db):
    """relate should fail loudly when an endpoint does not exist."""
    _run(kg_db, "entity", "only-one", "concept", "lonely")
    r = _run(kg_db, "relate", "only-one", "links-to", "does-not-exist")
    assert r.returncode != 0
    assert "not found" in r.stdout.lower() or "not found" in r.stderr.lower()


def test_traverse_depth_zero(kg_db):
    """Depth 0 on an isolated node should emit the no-connections message."""
    _run(kg_db, "entity", "island", "concept", "no neighbors")
    r = _run(kg_db, "traverse", "island", "0")
    assert r.returncode == 0
    assert "no connections" in r.stdout.lower()


def test_path_between_unconnected_entities(kg_db):
    """path over a disconnected graph must not crash.

    The tool's `path` command builds a networkx graph from relationships
    only, so entities with no edges do not appear as nodes. When the
    endpoints have edges but lie in separate components, we expect a
    "no path" message. When they are edge-less, networkx reports
    "Source ... is not in G" -- still a clean exit 0.
    """
    _run(kg_db, "entity", "x", "concept", "x")
    _run(kg_db, "entity", "y", "concept", "y")
    _run(kg_db, "entity", "x2", "concept", "x2")
    _run(kg_db, "entity", "y2", "concept", "y2")
    # Give x and y edges so they exist in the networkx graph,
    # but keep the two components disjoint.
    _run(kg_db, "relate", "x", "links-to", "x2")
    _run(kg_db, "relate", "y", "links-to", "y2")
    r = _run(kg_db, "path", "x", "y")
    assert r.returncode == 0
    assert "no path" in r.stdout.lower()


def test_entity_update_then_requery(kg_db):
    """Re-adding an entity should update, not duplicate, and stay queryable."""
    _run(kg_db, "entity", "evolving", "concept", "first take")
    r = _run(kg_db, "entity", "evolving", "concept", "second take")
    assert r.returncode == 0
    assert "updated" in r.stdout.lower()

    r = _run(kg_db, "query", "evolving")
    assert r.returncode == 0
    assert "second take" in r.stdout

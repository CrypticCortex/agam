"""Tests for the graph_recall UserPromptSubmit hook.

The hook reads a JSON payload from stdin and emits JSON on stdout using
Claude Code's `hookSpecificOutput` / `additionalContext` contract. The
critical behaviors we pin here:

* Empty KG (schema applied, no entities) -- silent no-op, exit 0.
* Missing KG file entirely -- graceful exit, no traceback.
* Populated KG with a matching entity -- injection with `additionalContext`.
* Real ~/.claude paths are never touched (mtimes unchanged).

All tests drive the hook as a subprocess with `uv run --script` so the
env-var overrides (AGAM_KG_PATH / AGAM_KG_DIR / AGAM_CONTEXT_TOOL) take
effect exactly as they would in a production install.
"""

import json
import os
import pathlib
import sqlite3
import subprocess
import time

import pytest


HOOK = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "hooks" / "graph_recall.py"
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "knowledge" / "graph-schema.sql"

REAL_KG = pathlib.Path(os.path.expanduser("~/.claude/knowledge/graph.db"))
REAL_AGAM_MD = pathlib.Path(os.path.expanduser("~/.claude/agam/AGAM.md"))


def _mtime(path):
    """Return (mtime, size) tuple or None if missing."""
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return None


@pytest.fixture
def kg_env(tmp_path):
    """Fresh KG + sidecar dir + stub agam-context tool + real-path guards.

    Returns (env, kg_path, sidecar_dir, snapshots) where snapshots captures
    mtimes of real ~/.claude files so each test can assert non-interference.
    """
    sidecar = tmp_path / "knowledge"
    sidecar.mkdir()
    kg = sidecar / "graph.db"
    conn = sqlite3.connect(kg)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()

    # Stub agam-context tool: a tiny no-op script so the hook's boot-context
    # subprocess call succeeds but contributes nothing visible.
    stub_tool = tmp_path / "agam-context-stub.py"
    stub_tool.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    stub_tool.chmod(0o755)

    # Point TMPDIR at a fresh directory so session dedup / boot flags do not
    # collide across tests or leak into the real system tmp.
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()

    env = {
        **os.environ,
        "AGAM_KG_PATH": str(kg),
        "AGAM_KG_DIR": str(sidecar),
        "AGAM_CONTEXT_TOOL": str(stub_tool),
        "TMPDIR": str(fake_tmp),
    }
    snapshots = {
        "kg": _mtime(REAL_KG),
        "agam_md": _mtime(REAL_AGAM_MD),
    }
    return env, kg, sidecar, snapshots


def _run_hook(env, payload, cwd):
    r = subprocess.run(
        ["uv", "run", "--script", str(HOOK)],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )
    return r


def _assert_real_files_untouched(snapshots):
    """Re-stat real ~/.claude files and confirm no test wrote to them."""
    assert _mtime(REAL_KG) == snapshots["kg"], (
        "Real ~/.claude/knowledge/graph.db was modified by a test"
    )
    assert _mtime(REAL_AGAM_MD) == snapshots["agam_md"], (
        "Real ~/.claude/agam/AGAM.md was modified by a test"
    )


def test_graph_recall_no_crash_on_empty_kg(kg_env, tmp_path):
    """Empty KG (schema only, zero entities) -> silent no-op, exit 0."""
    env, _, _, snapshots = kg_env
    r = _run_hook(env, {"prompt": "tell me about the voice-fnol-poc project", "session_id": "s-empty"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    # Silent: no DIRECTIVE / KG: header, and no hookSpecificOutput emitted
    assert "DIRECTIVE" not in r.stdout
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_graph_recall_missing_kg_file(tmp_path):
    """KG path does not exist -> hook must exit 0 without crashing."""
    snapshots = {"kg": _mtime(REAL_KG), "agam_md": _mtime(REAL_AGAM_MD)}
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    env = {
        **os.environ,
        "AGAM_KG_PATH": str(tmp_path / "does-not-exist.db"),
        "AGAM_KG_DIR": str(tmp_path / "also-missing"),
        "AGAM_CONTEXT_TOOL": str(tmp_path / "no-such-tool.py"),
        "TMPDIR": str(fake_tmp),
    }
    r = subprocess.run(
        ["uv", "run", "--script", str(HOOK)],
        env=env,
        input=json.dumps({"prompt": "does voice-fnol-poc still exist", "session_id": "s-missing"}),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    # Missing entity cache -> immediate silent exit, nothing on stdout
    assert "DIRECTIVE" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_graph_recall_populated_kg_emits_injection(kg_env, tmp_path):
    """Insert an entity + populate the entity-names cache, then confirm
    the hook emits a `hookSpecificOutput` with `additionalContext` that
    matches the entity name."""
    env, kg, sidecar, snapshots = kg_env

    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("voice-fnol-poc", "project", "Voice FNOL proof of concept project."),
    )
    conn.commit()
    conn.close()

    # The hook reads entity names from the sidecar cache, not directly from
    # SQLite. Write the cache so Stage 1 exact matching can fire.
    (sidecar / "entity-names.txt").write_text("voice-fnol-poc\n")

    prompt = "tell me about the voice-fnol-poc project latency"
    r = _run_hook(env, {"prompt": prompt, "session_id": "s-pop"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    # Injection emitted as JSON on stdout
    assert r.stdout.strip(), "Hook emitted no output for a matching entity"
    parsed = json.loads(r.stdout)
    hso = parsed.get("hookSpecificOutput", {})
    assert hso.get("hookEventName") == "UserPromptSubmit"
    ctx = hso.get("additionalContext", "")
    assert "voice-fnol-poc" in ctx, ctx
    assert "DIRECTIVE" in ctx or "KG:" in ctx, ctx
    _assert_real_files_untouched(snapshots)


def test_graph_recall_skip_short_message(kg_env, tmp_path):
    """Short / trivial messages (e.g. 'ok') must skip without crashing
    and without emitting hookSpecificOutput."""
    env, _, _, snapshots = kg_env
    r = _run_hook(env, {"prompt": "ok", "session_id": "s-skip"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_graph_recall_respects_agam_kg_dir(kg_env, tmp_path):
    """AGAM_KG_DIR must be honored -- the hook should read the entity
    names cache from the env-specified directory, not from the KG's
    implicit parent."""
    env, kg, sidecar, snapshots = kg_env
    # Populate entity + insert into DB
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("agam-sentinel", "project", "Sentinel entity for env-var isolation test."),
    )
    conn.commit()
    conn.close()

    # Point AGAM_KG_DIR at a DIFFERENT directory so if the hook ignored the
    # env var and defaulted to dirname(AGAM_KG_PATH), the cache would be
    # missing -> no match -> no injection.
    alt_dir = tmp_path / "alt-kg-dir"
    alt_dir.mkdir()
    (alt_dir / "entity-names.txt").write_text("agam-sentinel\n")
    env = {**env, "AGAM_KG_DIR": str(alt_dir)}

    r = _run_hook(env, {"prompt": "what about agam-sentinel in this graph", "session_id": "s-dir"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "Expected injection when AGAM_KG_DIR points at a dir with the cache"
    parsed = json.loads(r.stdout)
    ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "agam-sentinel" in ctx
    _assert_real_files_untouched(snapshots)


# ---------------------------------------------------------------------------
# Obsoletion filter (Category 1 -- temporal drift)
# ---------------------------------------------------------------------------


def test_graph_recall_filters_obsolete_entity(kg_env, tmp_path):
    """An entity marked status=obsolete must NOT appear in recall injection.

    This is what stops `stale-feature-bug` from polluting future prompts
    after the bug was actually fixed.
    """
    env, kg, sidecar, snapshots = kg_env

    conn = sqlite3.connect(kg)
    cur = conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("stale-feature-bug", "bug", "Dropped from output schema."),
    )
    eid = cur.lastrowid
    conn.execute(
        "INSERT INTO properties (entity_id, key, value, updated) "
        "VALUES (?, 'status', 'obsolete', datetime('now'))",
        (eid,),
    )
    conn.commit()
    conn.close()
    (sidecar / "entity-names.txt").write_text("stale-feature-bug\n")

    r = _run_hook(
        env,
        {"prompt": "tell me about stale-feature-bug behavior", "session_id": "s-obs"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    # The entity exists in the KG but was obsoleted -- recall should be silent
    # OR emit something that does NOT name the obsolete entity.
    ctx = ""
    if r.stdout.strip():
        parsed = json.loads(r.stdout)
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "stale-feature-bug" not in ctx, (
        f"obsolete entity leaked into recall injection: {ctx}"
    )
    _assert_real_files_untouched(snapshots)


def test_graph_recall_include_obsolete_env_overrides(kg_env, tmp_path):
    """``AGAM_INCLUDE_OBSOLETE=1`` surfaces obsolete entities for forensics."""
    env, kg, sidecar, snapshots = kg_env

    conn = sqlite3.connect(kg)
    cur = conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("old-bug-x", "bug", "Used to be a bug, now historical."),
    )
    eid = cur.lastrowid
    conn.execute(
        "INSERT INTO properties (entity_id, key, value, updated) "
        "VALUES (?, 'status', 'obsolete', datetime('now'))",
        (eid,),
    )
    conn.commit()
    conn.close()
    (sidecar / "entity-names.txt").write_text("old-bug-x\n")

    env = {**env, "AGAM_INCLUDE_OBSOLETE": "1"}
    r = _run_hook(
        env,
        {"prompt": "what about old-bug-x history please", "session_id": "s-obs-on"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "expected recall output when AGAM_INCLUDE_OBSOLETE=1"
    parsed = json.loads(r.stdout)
    ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "old-bug-x" in ctx
    _assert_real_files_untouched(snapshots)


def test_graph_recall_active_entity_unaffected_by_obsolete_sibling(kg_env, tmp_path):
    """Marking entity A obsolete must not affect entity B's recall."""
    env, kg, sidecar, snapshots = kg_env

    conn = sqlite3.connect(kg)
    # Active entity
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('active-foo', 'project', 'Active project.', datetime('now'), datetime('now'))",
    )
    # Obsolete sibling
    cur = conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('obsolete-foo', 'bug', 'Old.', datetime('now'), datetime('now'))",
    )
    eid = cur.lastrowid
    conn.execute(
        "INSERT INTO properties (entity_id, key, value, updated) "
        "VALUES (?, 'status', 'obsolete', datetime('now'))",
        (eid,),
    )
    conn.commit()
    conn.close()
    (sidecar / "entity-names.txt").write_text("active-foo\nobsolete-foo\n")

    r = _run_hook(
        env,
        {"prompt": "discuss active-foo plans here", "session_id": "s-sibling"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()
    parsed = json.loads(r.stdout)
    ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "active-foo" in ctx
    assert "obsolete-foo" not in ctx
    _assert_real_files_untouched(snapshots)

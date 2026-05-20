"""Tests for the graph_update Stop hook.

The hook reads a Stop-event JSON payload from stdin, loads the latest session
jsonl, extracts project / package / research mentions with regex, and updates
the knowledge graph + sidecar caches in place. Output is always silent
(exit 0) -- the hook never blocks session close.

All tests drive the hook as a subprocess with `uv run --script` so the
env-var overrides (AGAM_KG_PATH / AGAM_KG_DIR / AGAM_SESSIONS_DIR /
AGAM_KG_TOOL / AGAM_VAULT_DIR) take effect exactly as on a fresh install.

Real-file guards make sure no test mutates ~/.claude/knowledge/graph.db or
~/.claude/agam/AGAM.md.
"""

import json
import os
import pathlib
import sqlite3
import subprocess
import uuid

import pytest


HOOK = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "hooks" / "graph_update.py"
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "knowledge" / "graph-schema.sql"

REAL_KG = pathlib.Path(os.path.expanduser("~/.claude/knowledge/graph.db"))
REAL_AGAM_MD = pathlib.Path(os.path.expanduser("~/.claude/agam/AGAM.md"))
REAL_NAMES_CACHE = pathlib.Path(os.path.expanduser("~/.claude/knowledge/entity-names.txt"))
REAL_VAULT_MARKER = pathlib.Path(os.path.expanduser("~/.claude/knowledge/vault-stale.marker"))


def _mtime(path):
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return None


def _snapshot_real():
    return {
        "kg": _mtime(REAL_KG),
        "agam_md": _mtime(REAL_AGAM_MD),
        "names_cache": _mtime(REAL_NAMES_CACHE),
        "vault_marker": _mtime(REAL_VAULT_MARKER),
    }


def _assert_real_untouched(snapshots):
    assert _mtime(REAL_KG) == snapshots["kg"], "Real graph.db was modified"
    assert _mtime(REAL_AGAM_MD) == snapshots["agam_md"], "Real AGAM.md was modified"
    assert _mtime(REAL_NAMES_CACHE) == snapshots["names_cache"], (
        "Real ~/.claude/knowledge/entity-names.txt was modified"
    )
    assert _mtime(REAL_VAULT_MARKER) == snapshots["vault_marker"], (
        "Real ~/.claude/knowledge/vault-stale.marker was modified"
    )


def _make_transcript(path: pathlib.Path, *, human_turns: int, text: str = ""):
    """Write a minimal session jsonl with `human_turns` user rows and an
    assistant tail containing `text` (which is where regex extractors look)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = dict(separators=(",", ":"))
    lines = []
    for i in range(human_turns):
        lines.append(json.dumps({"type": "user", "content": f"msg {i}"}, **compact))
    lines.append(json.dumps({"type": "assistant", "content": text}, **compact))
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def hook_env(tmp_path):
    """Fresh KG + sessions dir + stub KG tool + missing vault dir + real-file snapshots."""
    kg_dir = tmp_path / "knowledge"
    kg_dir.mkdir()
    kg = kg_dir / "graph.db"
    conn = sqlite3.connect(kg)
    conn.executescript(SCHEMA.read_text())
    # Seed "Kalyan" so the `works-on` / `researched` relations can be created.
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('Kalyan', 'user', 'Seed user for graph-update tests', "
        "datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()

    sessions = tmp_path / "sessions"
    sessions.mkdir()

    # Stub KG tool (for build-index subprocess call -- no-op so the hook
    # doesn't try to invoke a real CLI on the host).
    stub_tool = tmp_path / "kg-tool-stub.py"
    stub_tool.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    stub_tool.chmod(0o755)

    # Non-existent vault dir -- drift check should short-circuit and never
    # write a marker. Keeps the test hermetic.
    vault = tmp_path / "no-vault"

    # Isolated TMPDIR so dedup flag files don't collide across tests or with
    # real /tmp/graph-update-* flags.
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()

    env = {
        **os.environ,
        "AGAM_KG_PATH": str(kg),
        "AGAM_KG_DIR": str(kg_dir),
        "AGAM_SESSIONS_DIR": str(sessions),
        "AGAM_KG_TOOL": str(stub_tool),
        "AGAM_VAULT_DIR": str(vault),
        "AGAM_USER_ENTITY": "Kalyan",
        "TMPDIR": str(fake_tmp),
    }
    return env, kg, kg_dir, sessions, _snapshot_real()


def _run_hook(env, payload, cwd):
    return subprocess.run(
        ["uv", "run", "--script", str(HOOK)],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )


def _fresh_sid():
    return "test-" + uuid.uuid4().hex[:12]


# ---- Baseline: missing KG / no transcripts ----

def test_missing_kg_exits_cleanly(tmp_path):
    """KG path does not exist -> hook must exit 0 without crashing."""
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    env = {
        **os.environ,
        "AGAM_KG_PATH": str(tmp_path / "does-not-exist.db"),
        "AGAM_KG_DIR": str(tmp_path / "no-kg-dir"),
        "AGAM_SESSIONS_DIR": str(tmp_path / "no-sessions"),
        "AGAM_KG_TOOL": str(tmp_path / "no-tool.py"),
        "AGAM_VAULT_DIR": str(tmp_path / "no-vault"),
        "TMPDIR": str(fake_tmp),
    }
    snapshots = _snapshot_real()
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    _assert_real_untouched(snapshots)


def test_no_transcripts_exits_cleanly(hook_env, tmp_path):
    """Empty sessions dir -> exit 0, no KG writes."""
    env, kg, _, _, snapshots = hook_env
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr

    # KG unchanged -- still just the Kalyan seed.
    conn = sqlite3.connect(kg)
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    conn.close()
    assert count == 1
    _assert_real_untouched(snapshots)


def test_trivial_transcript_skipped(hook_env, tmp_path):
    """Transcript with <3 human turns -> treated as trivial, no extraction."""
    env, kg, _, sessions, snapshots = hook_env
    _make_transcript(sessions / "p" / "t.jsonl", human_turns=2, text="ignored")
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(kg)
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    conn.close()
    assert count == 1  # no new entities
    _assert_real_untouched(snapshots)


# ---- Extraction semantics ----

def test_extracts_project_from_coding_path(hook_env, tmp_path):
    """Transcript mentioning /Users/test/coding/<proj>/ -> new `project` entity
    + `Kalyan -- works-on -- <proj>` relation + last-worked property."""
    env, kg, kg_dir, sessions, snapshots = hook_env
    text = "Worked on /Users/test/coding/agam/src/x.py today."
    _make_transcript(sessions / "p" / "t.jsonl", human_turns=5, text=text)
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(kg)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, type FROM entities WHERE name = 'agam'"
    ).fetchone()
    assert row is not None, "Expected 'agam' project entity"
    assert row["type"] == "project"

    proj_id = row["id"]
    rels = conn.execute(
        "SELECT r.relation FROM relationships r "
        "JOIN entities s ON r.source_id = s.id "
        "WHERE s.name = 'Kalyan' AND r.target_id = ?",
        (proj_id,),
    ).fetchall()
    assert any(row2["relation"] == "works-on" for row2 in rels)

    prop = conn.execute(
        "SELECT value FROM properties WHERE entity_id = ? AND key = 'last-worked'",
        (proj_id,),
    ).fetchone()
    assert prop is not None
    conn.close()

    # entity-names cache refreshed in kg_dir, NOT in real ~/.claude/knowledge.
    cache = (kg_dir / "entity-names.txt").read_text()
    assert "agam" in cache.splitlines()
    _assert_real_untouched(snapshots)


def test_extracts_npm_package_and_relates_to_project(hook_env, tmp_path):
    """`npm install <pkg>` -> new `package` entity + `<proj> -- uses-package -- <pkg>`."""
    env, kg, _, sessions, snapshots = hook_env
    text = (
        "Ran `npm install axios` in /Users/test/coding/agam.\n"
        "Installed it fine."
    )
    _make_transcript(sessions / "p" / "t.jsonl", human_turns=5, text=text)
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(kg)
    conn.row_factory = sqlite3.Row
    pkg = conn.execute("SELECT id, type FROM entities WHERE name = 'axios'").fetchone()
    assert pkg is not None and pkg["type"] == "package"
    rel = conn.execute(
        "SELECT r.relation FROM relationships r "
        "JOIN entities s ON r.source_id = s.id "
        "WHERE s.name = 'agam' AND r.target_id = ?",
        (pkg["id"],),
    ).fetchone()
    assert rel is not None
    assert rel["relation"] == "uses-package"
    conn.close()
    _assert_real_untouched(snapshots)


def test_skips_blacklisted_dir_names(hook_env, tmp_path):
    """`/Users/test/coding/node_modules/...` must NOT become a 'project' entity."""
    env, kg, _, sessions, snapshots = hook_env
    text = "Saw /Users/test/coding/node_modules/axios/package.json in the dump."
    _make_transcript(sessions / "p" / "t.jsonl", human_turns=5, text=text)
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(kg)
    row = conn.execute("SELECT COUNT(*) FROM entities WHERE name = 'node_modules'").fetchone()
    conn.close()
    assert row[0] == 0
    _assert_real_untouched(snapshots)


# ---- Session dedup + env-var isolation ----

def test_session_dedup_flag_prevents_reruns(hook_env, tmp_path):
    """Second invocation with same session_id -> short-circuit, no new entities."""
    env, kg, _, sessions, snapshots = hook_env
    _make_transcript(
        sessions / "p" / "t.jsonl",
        human_turns=5,
        text="In /Users/test/coding/project-a/",
    )
    sid = _fresh_sid()
    r1 = _run_hook(env, {"session_id": sid}, cwd=str(tmp_path))
    assert r1.returncode == 0, r1.stderr

    conn = sqlite3.connect(kg)
    count1 = conn.execute("SELECT COUNT(*) FROM entities WHERE name = 'project-a'").fetchone()[0]
    conn.close()
    assert count1 == 1

    # Overwrite transcript with a different project, re-run with SAME sid.
    _make_transcript(
        sessions / "p" / "t.jsonl",
        human_turns=5,
        text="In /Users/test/coding/project-b/",
    )
    r2 = _run_hook(env, {"session_id": sid}, cwd=str(tmp_path))
    assert r2.returncode == 0, r2.stderr

    conn = sqlite3.connect(kg)
    # Dedup must have fired -- project-b should NOT have been ingested.
    count_b = conn.execute("SELECT COUNT(*) FROM entities WHERE name = 'project-b'").fetchone()[0]
    conn.close()
    assert count_b == 0
    _assert_real_untouched(snapshots)


def test_respects_agam_kg_dir_for_names_cache(hook_env, tmp_path):
    """AGAM_KG_DIR must be where the entity-names cache is written -- not
    the KG's implicit parent."""
    env, kg, _, sessions, snapshots = hook_env

    alt_dir = tmp_path / "alt-kg-dir"
    alt_dir.mkdir()
    env = {**env, "AGAM_KG_DIR": str(alt_dir)}

    _make_transcript(
        sessions / "p" / "t.jsonl",
        human_turns=5,
        text="Working in /Users/test/coding/alpha-project/",
    )
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr

    cache = (alt_dir / "entity-names.txt")
    assert cache.exists(), "entity-names.txt must be written to AGAM_KG_DIR"
    assert "alpha-project" in cache.read_text().splitlines()
    # Default parent-of-KG location must NOT have been used.
    default_cache = kg.parent / "entity-names.txt"
    assert not default_cache.exists()
    _assert_real_untouched(snapshots)


def test_missing_kg_tool_does_not_crash(hook_env, tmp_path):
    """If AGAM_KG_TOOL points at a non-existent path, the hook silently
    skips the build-index rebuild step and still exits 0."""
    env, kg, _, sessions, snapshots = hook_env
    env = {**env, "AGAM_KG_TOOL": str(tmp_path / "definitely-missing.py")}
    _make_transcript(
        sessions / "p" / "t.jsonl",
        human_turns=5,
        text="In /Users/test/coding/beta/",
    )
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(kg)
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE name = 'beta'"
    ).fetchone()[0] == 1
    conn.close()
    _assert_real_untouched(snapshots)


def test_missing_vault_dir_skips_drift_marker(hook_env, tmp_path):
    """If AGAM_VAULT_DIR doesn't exist, no vault-stale.marker is written."""
    env, _, kg_dir, sessions, snapshots = hook_env
    _make_transcript(
        sessions / "p" / "t.jsonl",
        human_turns=5,
        text="In /Users/test/coding/gamma-proj/",
    )
    r = _run_hook(env, {"session_id": _fresh_sid()}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not (kg_dir / "vault-stale.marker").exists()
    _assert_real_untouched(snapshots)

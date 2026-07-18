"""Tests for the graph_recall UserPromptSubmit hook.

The hook reads a JSON payload from stdin and emits JSON on stdout using
Claude Code's `hookSpecificOutput` / `additionalContext` contract. The
critical behaviors we pin here:

* Empty KG (schema applied, no entities) -- silent no-op, exit 0.
* Missing KG file entirely -- graceful exit, no traceback.
* Populated KG with a matching entity -- injection with `additionalContext`.
* Real ~/.claude paths are never touched (mtimes unchanged).

All tests drive the hook as a subprocess against synthetic, versioned scope
stores. No live graph, identity file, or transcript is opened.
"""

import hashlib
import io
import json
import os
import pathlib
import sqlite3
import stat
import subprocess
import sys

import pytest

from agam.hooks import graph_recall
from agam.vault_registry import VaultRole, initialize_registry, load_registry


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
    """Fresh registry-approved portable stores and real-path guards.

    Returns (env, kg_path, sidecar_dir, snapshots) where snapshots captures
    mtimes of real ~/.claude files so each test can assert non-interference.
    """
    data_home = tmp_path / "agam"
    scopes = data_home / "knowledge" / "scopes"
    version = "v-test"
    registry = initialize_registry(
        scopes / "registry.json",
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    guidance_id = registry.for_role(VaultRole.GUIDANCE).id
    solutions_id = registry.for_role(VaultRole.SOLUTIONS).id
    sidecar = scopes / guidance_id / version
    sidecar.mkdir(parents=True)
    kg = sidecar / "graph.db"
    conn = sqlite3.connect(kg)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()

    solutions = scopes / solutions_id / version / "graph.db"
    solutions.parent.mkdir(parents=True)
    conn = sqlite3.connect(solutions)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()

    (scopes / "manifests").mkdir()
    (scopes / "config.json").write_text(
        json.dumps(
            {"agents": {"codex": {
                "recall": True,
                "boot-injection": False,
                "capture": False,
            }}}
        )
    )
    (scopes / "active.json").write_text(
        json.dumps({"version": version, "manifest": f"manifests/{version}.json"})
    )

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
        "AGAM_DATA_HOME": str(data_home),
        "AGAM_KNOWLEDGE_SCOPE_ROOT": str(scopes),
        "AGAM_KNOWLEDGE_CONFIG": str(scopes / "config.json"),
        "AGAM_ACTIVE_MANIFEST": str(scopes / "active.json"),
        "AGAM_RECALL_AGENT": "codex",
        "AGAM_KG_PATH": str(kg),
        "AGAM_KG_DIR": str(sidecar),
        "AGAM_CONTEXT_TOOL": str(stub_tool),
        "PYTHONPATH": str(HOOK.parents[2]),
        "TMPDIR": str(fake_tmp),
        "AGAM_TEST_GUIDANCE_ID": guidance_id,
        "AGAM_TEST_SOLUTIONS_ID": solutions_id,
    }
    _refresh_manifest(env)
    snapshots = {
        "kg": _mtime(REAL_KG),
        "agam_md": _mtime(REAL_AGAM_MD),
    }
    return env, kg, sidecar, snapshots


def _run_hook(env, payload, cwd):
    _refresh_manifest(env)
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )
    return r


def _scope_db(env, scope):
    root = pathlib.Path(env["AGAM_KNOWLEDGE_SCOPE_ROOT"])
    active = json.loads((root / "active.json").read_text())
    return root / scope / active["version"] / "graph.db"


def _refresh_manifest(env):
    """Keep the content-free synthetic manifest aligned with fixture writes."""
    root_value = env.get("AGAM_KNOWLEDGE_SCOPE_ROOT")
    if not root_value:
        return
    root = pathlib.Path(root_value)
    active_path = root / "active.json"
    if not active_path.exists():
        return
    active = json.loads(active_path.read_text())
    version = active["version"]
    stores = {}
    registry = load_registry(root / "registry.json")
    for vault in registry.active:
        scope = vault.id
        database = root / scope / version / "graph.db"
        if not database.exists():
            continue
        stores[scope] = {
            "path": f"{scope}/{version}/graph.db",
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "entities": 0,
            "relationships": 0,
            "properties": 0,
        }
    manifest = root / active["manifest"]
    manifest.write_text(json.dumps({"version": version, "stores": stores}))


def _select_guidance_store_for_module(env, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(graph_recall.tempfile, "tempdir", env["TMPDIR"])
    version, stores = graph_recall._resolve_scoped_stores()
    assert stores
    store = next(
        store for store in stores
        if store.scope == env["AGAM_TEST_GUIDANCE_ID"]
    )
    graph_recall._select_store(version, store)
    return version, store


def _insert_lesson_trigger(database, *, name, pattern, description):
    connection = sqlite3.connect(database)
    cursor = connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES(?,?,?,?,?)",
        (name, "lesson", description, "t", "t"),
    )
    connection.execute(
        "INSERT INTO properties(entity_id,key,value,updated) VALUES(?,?,?,?)",
        (cursor.lastrowid, "trigger-tool", json.dumps([pattern]), "t"),
    )
    connection.commit()
    connection.close()


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


def test_graph_recall_missing_policy_ignores_legacy_graph(tmp_path):
    """Missing policy never falls back to AGAM_KG_PATH."""
    snapshots = {"kg": _mtime(REAL_KG), "agam_md": _mtime(REAL_AGAM_MD)}
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy = legacy_dir / "graph.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(SCHEMA.read_text())
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES('voice-fnol-poc','project','[GENERAL] legacy only','t','t')"
    )
    connection.commit()
    connection.close()
    (legacy_dir / "entity-names.txt").write_text("voice-fnol-poc\n")
    env = {
        **os.environ,
        "AGAM_DATA_HOME": str(tmp_path / "missing-policy"),
        "AGAM_KG_PATH": str(legacy),
        "AGAM_KG_DIR": str(legacy_dir),
            "AGAM_CONTEXT_TOOL": str(tmp_path / "no-such-tool.py"),
            "AGAM_RECALL_AGENT": "codex",
            "PYTHONPATH": str(HOOK.parents[2]),
        "TMPDIR": str(fake_tmp),
    }
    r = subprocess.run(
        [sys.executable, str(HOOK)],
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


def test_graph_recall_malformed_policy_fails_closed(kg_env, tmp_path):
    env, _, _, snapshots = kg_env
    pathlib.Path(env["AGAM_KNOWLEDGE_CONFIG"]).write_text("{")

    r = _run_hook(
        env,
        {"prompt": "tell me about voice-fnol-poc behavior", "session_id": "s-bad"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert not r.stdout.strip()
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
        ("voice-fnol-poc", "project", "[GENERAL] Voice FNOL proof of concept project."),
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
    assert "prior" in ctx.lower()
    assert "advisory" in ctx.lower()
    assert "load-bearing" in ctx.lower()
    assert "ground truth" not in ctx.lower()
    assert "lived experience" not in ctx.lower()
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


def test_graph_recall_ignores_legacy_kg_dir(kg_env, tmp_path):
    """Legacy cache overrides cannot redirect policy-resolved recall."""
    env, kg, sidecar, snapshots = kg_env
    # Populate entity + insert into DB
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("agam-sentinel", "project", "[GENERAL] Sentinel entity for policy isolation."),
    )
    conn.commit()
    conn.close()

    # Point the legacy override at a different directory. The scoped store is
    # still the only source of truth.
    alt_dir = tmp_path / "alt-kg-dir"
    alt_dir.mkdir()
    (alt_dir / "entity-names.txt").write_text("agam-sentinel\n")
    env = {**env, "AGAM_KG_DIR": str(alt_dir)}

    r = _run_hook(env, {"prompt": "what about agam-sentinel in this graph", "session_id": "s-dir"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "Expected injection from the selected portable store"
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
        ("stale-feature-bug", "bug", "[GENERAL] Dropped from output schema."),
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
        ("old-bug-x", "bug", "[GENERAL] Used to be a bug, now historical."),
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
        "VALUES ('active-foo', 'project', '[GENERAL] Active project.', datetime('now'), datetime('now'))",
    )
    # Obsolete sibling
    cur = conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('obsolete-foo', 'bug', '[GENERAL] Old.', datetime('now'), datetime('now'))",
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


def test_graph_recall_rejects_entity_without_general_prefix(kg_env, tmp_path):
    env, kg, _, snapshots = kg_env
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("unlabelled-entry", "lesson", "must stay sealed"),
    )
    conn.commit()
    conn.close()

    r = _run_hook(
        env,
        {"prompt": "tell me about unlabelled-entry behavior", "session_id": "s-prefix"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert "unlabelled-entry" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_graph_recall_rejects_wrong_case_general_prefix_in_lesson_path(
    kg_env, tmp_path
):
    env, kg, _, snapshots = kg_env
    marker = "SYNTHETIC_RESTRICTED_LESSON"
    conn = sqlite3.connect(kg)
    cursor = conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("wrong-case-lesson", "lesson", f"[general] {marker}"),
    )
    conn.execute(
        "INSERT INTO properties(entity_id,key,value,updated) VALUES(?,?,?,?)",
        (cursor.lastrowid, "trigger-tool", '["wrong-case-trigger"]', "t"),
    )
    conn.commit()
    conn.close()

    result = _run_hook(
        env,
        {
            "prompt": "please consider wrong-case-trigger before proceeding",
            "session_id": "s-wrong-case",
        },
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert marker not in result.stdout
    assert "wrong-case-lesson" not in result.stdout
    _assert_real_files_untouched(snapshots)


def test_graph_recall_merges_scopes_in_deterministic_policy_order(kg_env, tmp_path):
    env, guidance, _, snapshots = kg_env
    solutions = _scope_db(env, env["AGAM_TEST_SOLUTIONS_ID"])
    for database, row in (
        (guidance, ("z-craft-rule", "lesson", "[GENERAL] craft marker")),
        (solutions, ("a-repair-fix", "lesson", "[GENERAL] repair marker")),
    ):
        conn = sqlite3.connect(database)
        conn.execute(
            "INSERT INTO entities (name, type, description, created, updated) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            row,
        )
        conn.commit()
        conn.close()

    r = _run_hook(
        env,
        {
            "prompt": "compare z-craft-rule and a-repair-fix carefully",
            "session_id": "s-merge",
        },
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    context = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.index("z-craft-rule") < context.index("a-repair-fix")
    _assert_real_files_untouched(snapshots)


def test_graph_recall_state_is_namespaced_by_version_and_scope(kg_env, tmp_path):
    env, kg, _, _ = kg_env
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("state-marker", "lesson", "[GENERAL] state marker"),
    )
    conn.commit()
    conn.close()

    r = _run_hook(
        env,
        {"prompt": "discuss state-marker behavior now", "session_id": "s-state"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    state_dir = (
        pathlib.Path(env["TMPDIR"]) / f"agam-codex-state-{os.geteuid()}"
    )
    assert state_dir.is_dir()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    state_files = list(state_dir.iterdir())
    state_names = {path.name for path in state_files}
    assert any(
        f"v-test-{env['AGAM_TEST_GUIDANCE_ID']}" in name
        for name in state_names
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in state_files
        if path.is_file()
    )


def test_graph_recall_keeps_codex_boot_context_disabled(kg_env, tmp_path):
    env, _, _, _ = kg_env
    marker = tmp_path / "boot-ran"
    tool = tmp_path / "boot-tool.py"
    tool.write_text(
        "#!/usr/bin/env python3\n"
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "print('PRIVATE BOOT CONTEXT')\n"
    )
    tool.chmod(0o755)
    env = {**env, "AGAM_CONTEXT_TOOL": str(tool)}

    r = _run_hook(
        env,
        {"prompt": "ok", "session_id": "s-no-boot"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    assert "PRIVATE BOOT CONTEXT" not in r.stdout


def test_graph_recall_ignores_stale_unnamespaced_capture_state(kg_env, tmp_path):
    env, kg, _, _ = kg_env
    marker = "SYNTHETIC_STALE_PRIVATE_CORRECTION"
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("correction-anchor", "lesson", "[GENERAL] safe anchor"),
    )
    conn.commit()
    conn.close()
    stale = pathlib.Path(env["TMPDIR"]) / "sycophancy-s-stale.json"
    stale.write_text(json.dumps({"detected": True, "patterns": [marker]}))

    result = _run_hook(
        env,
        {
            "prompt": "tell me about correction-anchor behavior now",
            "session_id": "s-stale",
        },
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert marker not in result.stdout
    assert stale.exists()


def test_graph_recall_cleanup_preserves_unowned_temp_state(kg_env, tmp_path):
    env, _, _, _ = kg_env
    unowned = pathlib.Path(env["TMPDIR"]) / "lesson-triggers-unrelated.json"
    unowned.write_text("synthetic user state")
    os.utime(unowned, (1, 1))

    result = _run_hook(
        env,
        {"prompt": "ok", "session_id": "s-cleanup"},
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert unowned.exists()


def test_graph_recall_rejects_symlink_swap_after_policy_validation(
    kg_env, tmp_path, monkeypatch, capsys
):
    env, _, _, _ = kg_env
    private_marker = "SYNTHETIC_SYMLINK_PRIVATE_MARKER"
    private_db = tmp_path / "sealed-private.db"
    connection = sqlite3.connect(private_db)
    connection.executescript(SCHEMA.read_text())
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES(?,?,?,?,?)",
        (
            "symlink-private-marker",
            "lesson",
            f"[GENERAL] {private_marker}",
            "t",
            "t",
        ),
    )
    connection.commit()
    connection.close()

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(graph_recall.tempfile, "tempdir", env["TMPDIR"])
    from agam.knowledge_scopes import resolve_scope_db as real_resolve
    swapped = False

    def resolve_then_swap(scope, *args, **kwargs):
        nonlocal swapped
        database = real_resolve(scope, *args, **kwargs)
        if scope == env["AGAM_TEST_GUIDANCE_ID"] and database is not None and not swapped:
            swapped = True
            database.unlink()
            database.symlink_to(private_db)
        return database

    monkeypatch.setattr(graph_recall, "resolve_scope_db", resolve_then_swap)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "prompt": "tell me about symlink-private-marker behavior",
                    "session_id": "s-symlink-swap",
                }
            )
        ),
    )

    graph_recall.main()

    output = capsys.readouterr().out
    assert private_marker not in output
    assert "symlink-private-marker" not in output


def test_graph_recall_rejects_byte_identical_inode_swap_after_validation(
    kg_env, tmp_path, monkeypatch, capsys
):
    env, database, _, _ = kg_env
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES(?,?,?,?,?)",
        (
            "inode-stable-marker",
            "lesson",
            "[GENERAL] byte-identical inode marker",
            "t",
            "t",
        ),
    )
    connection.commit()
    connection.close()
    _refresh_manifest(env)

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(graph_recall.tempfile, "tempdir", env["TMPDIR"])
    from agam.knowledge_scopes import resolve_scope_db as real_resolve
    swapped = False

    def resolve_then_replace(scope, *args, **kwargs):
        nonlocal swapped
        resolved = real_resolve(scope, *args, **kwargs)
        if scope == env["AGAM_TEST_GUIDANCE_ID"] and resolved is not None and not swapped:
            swapped = True
            original_identity = resolved.stat().st_ino
            replacement = tmp_path / "byte-identical-replacement.db"
            replacement.write_bytes(resolved.read_bytes())
            os.replace(replacement, resolved)
            assert resolved.stat().st_ino != original_identity
        return resolved

    monkeypatch.setattr(graph_recall, "resolve_scope_db", resolve_then_replace)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "prompt": "tell me about inode-stable-marker behavior",
                    "session_id": "s-inode-swap",
                }
            )
        ),
    )

    graph_recall.main()

    assert "inode-stable-marker" not in capsys.readouterr().out


def test_graph_recall_rejects_symlink_swap_after_store_selection(
    kg_env, tmp_path, monkeypatch
):
    env, database, _, _ = kg_env
    _select_guidance_store_for_module(env, monkeypatch)
    marker = "SYNTHETIC_POST_SELECTION_PRIVATE_MARKER"
    private_db = tmp_path / "post-selection-private.db"
    connection = sqlite3.connect(private_db)
    connection.executescript(SCHEMA.read_text())
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES(?,?,?,?,?)",
        (
            "post-selection-private-marker",
            "lesson",
            f"[GENERAL] {marker}",
            "t",
            "t",
        ),
    )
    connection.commit()
    connection.close()
    database.unlink()
    database.symlink_to(private_db)

    assert graph_recall.load_entity_names() == set()
    assert (
        graph_recall.check_lesson_triggers_in_message(
            "post-selection-private-marker appeared in this synthetic prompt",
            "s-post-selection-symlink",
        )
        == ""
    )


def test_graph_recall_rejects_inode_swap_after_store_selection(
    kg_env, tmp_path, monkeypatch
):
    env, database, _, _ = kg_env
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES(?,?,?,?,?)",
        (
            "post-selection-inode-marker",
            "lesson",
            "[GENERAL] byte-identical post-selection marker",
            "t",
            "t",
        ),
    )
    connection.commit()
    connection.close()
    _refresh_manifest(env)
    _select_guidance_store_for_module(env, monkeypatch)
    replacement = tmp_path / "post-selection-replacement.db"
    replacement.write_bytes(database.read_bytes())
    original_inode = database.stat().st_ino
    os.replace(replacement, database)
    assert database.stat().st_ino != original_inode

    assert graph_recall.load_entity_names() == set()


def test_graph_recall_does_not_block_on_fifo_swap_after_store_selection(
    kg_env, tmp_path
):
    env, _, _, _ = kg_env
    code = """
import json
import os

from agam.hooks import graph_recall

version, stores = graph_recall._resolve_scoped_stores()
store = next(store for store in stores if store.scope == os.environ["AGAM_TEST_GUIDANCE_ID"])
graph_recall._select_store(version, store)
store.database.unlink()
os.mkfifo(store.database)
print(json.dumps(sorted(graph_recall.load_entity_names())))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    assert json.loads(result.stdout) == []


def test_graph_recall_does_not_block_on_fifo_state_file(kg_env, tmp_path):
    env, _, _, _ = kg_env
    code = """
import json
import os

from agam.hooks import graph_recall

state_file = graph_recall._state_path("graph-recall", "s-fifo", "txt")
os.mkfifo(state_file)
print(json.dumps(sorted(graph_recall._read_state_lines(state_file))))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    ("path_attribute", "loader_name"),
    (
        ("IDF_INDEX", "load_idf_index"),
        ("CONCEPT_INDEX", "load_concept_index"),
    ),
)
def test_graph_recall_does_not_block_on_fifo_sidecar(
    kg_env, tmp_path, path_attribute, loader_name
):
    env, _, _, _ = kg_env
    code = f"""
import json
import os

from agam.hooks import graph_recall

version, stores = graph_recall._resolve_scoped_stores()
store = next(store for store in stores if store.scope == os.environ["AGAM_TEST_GUIDANCE_ID"])
graph_recall._select_store(version, store)
os.mkfifo(getattr(graph_recall, {path_attribute!r}))
print(json.dumps(getattr(graph_recall, {loader_name!r})()))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    assert json.loads(result.stdout) == {}


@pytest.mark.parametrize(
    ("path_attribute", "loader_name"),
    (
        ("IDF_INDEX", "load_idf_index"),
        ("CONCEPT_INDEX", "load_concept_index"),
    ),
)
def test_graph_recall_does_not_follow_symlinked_sidecar(
    kg_env, tmp_path, monkeypatch, path_attribute, loader_name
):
    env, _, _, _ = kg_env
    _select_guidance_store_for_module(env, monkeypatch)
    target = tmp_path / f"synthetic-{loader_name}.json"
    target.write_text('{"cross-scope-marker": 42}', encoding="utf-8")
    pathlib.Path(getattr(graph_recall, path_attribute)).symlink_to(target)

    assert getattr(graph_recall, loader_name)() == {}


def test_graph_recall_ignores_symlinked_lesson_cache_content(
    kg_env, tmp_path, monkeypatch
):
    env, _, _, _ = kg_env
    _select_guidance_store_for_module(env, monkeypatch)
    marker = "SYNTHETIC_CROSS_SCOPE_CACHE_MARKER"
    other_scope_cache = tmp_path / "other-scope-cache.json"
    payload = json.dumps(
        {
            "tool": [
                {
                    "pattern": "cross-scope-trigger",
                    "lesson": "other-scope-lesson",
                    "severity": "high",
                    "desc": marker,
                }
            ],
            "error": [],
        }
    )
    other_scope_cache.write_text(payload)
    cache = pathlib.Path(
        graph_recall._state_path(
            "lesson-triggers", "s-cache-symlink", "json"
        )
    )
    cache.symlink_to(other_scope_cache)

    result = graph_recall.check_lesson_triggers_in_message(
        "cross-scope-trigger appeared in this synthetic prompt",
        "s-cache-symlink",
    )

    assert marker not in result
    assert other_scope_cache.read_text() == payload
    assert cache.is_symlink()


def test_graph_recall_does_not_overwrite_symlinked_lesson_cache_target(
    kg_env, tmp_path, monkeypatch
):
    env, database, _, _ = kg_env
    _insert_lesson_trigger(
        database,
        name="allowed-cache-lesson",
        pattern="allowed-cache-trigger",
        description="[GENERAL] allowed cache lesson",
    )
    _refresh_manifest(env)
    _select_guidance_store_for_module(env, monkeypatch)
    sentinel = "SYNTHETIC_CACHE_TARGET_SENTINEL"
    target = tmp_path / "cache-target.txt"
    target.write_text(sentinel)
    cache = pathlib.Path(
        graph_recall._state_path(
            "lesson-triggers", "s-cache-target", "json"
        )
    )
    cache.symlink_to(target)

    result = graph_recall.check_lesson_triggers_in_message(
        "allowed-cache-trigger appeared in this synthetic prompt",
        "s-cache-target",
    )

    assert "allowed-cache-lesson" in result
    assert target.read_text() == sentinel
    assert cache.is_symlink()


def test_graph_recall_does_not_append_through_symlinked_session_file(
    kg_env, tmp_path, monkeypatch
):
    env, _, _, _ = kg_env
    _select_guidance_store_for_module(env, monkeypatch)
    sentinel = "SYNTHETIC_SESSION_TARGET_SENTINEL\n"
    target = tmp_path / "session-target.txt"
    target.write_text(sentinel)
    session_file = pathlib.Path(
        graph_recall._state_path("graph-recall", "s-session-link", "txt")
    )
    session_file.symlink_to(target)
    graph_recall.SESSION_FILE = str(session_file)

    graph_recall.mark_session_seen(["allowed-marker"])

    assert target.read_text() == sentinel
    assert session_file.is_symlink()


def test_graph_recall_does_not_create_through_symlinked_header_flag(
    kg_env, tmp_path, monkeypatch
):
    env, _, _, _ = kg_env
    _select_guidance_store_for_module(env, monkeypatch)
    target = tmp_path / "missing-header-target.txt"
    flag = pathlib.Path(
        graph_recall._state_path(
            "graph-recall-header", "s-header-link", "flag"
        )
    )
    flag.symlink_to(target)

    is_first = graph_recall.get_header_and_mark("s-header-link")

    assert is_first is True
    assert not target.exists()
    assert flag.is_symlink()


def test_graph_recall_does_not_append_through_symlinked_lesson_seen_file(
    kg_env, tmp_path, monkeypatch
):
    env, database, _, _ = kg_env
    _insert_lesson_trigger(
        database,
        name="allowed-seen-lesson",
        pattern="allowed-seen-trigger",
        description="[GENERAL] allowed seen lesson",
    )
    _refresh_manifest(env)
    _select_guidance_store_for_module(env, monkeypatch)
    sentinel = "SYNTHETIC_LESSON_SEEN_TARGET_SENTINEL\n"
    target = tmp_path / "lesson-seen-target.txt"
    target.write_text(sentinel)
    seen_file = pathlib.Path(
        graph_recall._state_path("lesson-seen", "s-seen-link", "txt")
    )
    seen_file.symlink_to(target)

    result = graph_recall.check_lesson_triggers_in_message(
        "allowed-seen-trigger appeared in this synthetic prompt",
        "s-seen-link",
    )

    assert "allowed-seen-lesson" in result
    assert target.read_text() == sentinel
    assert seen_file.is_symlink()


def test_graph_recall_fuzzy_order_is_stable_across_hash_seeds():
    code = """
import json
import sys
import types

rapidfuzz = types.ModuleType("rapidfuzz")
token_set_ratio = object()
partial_ratio = object()
rapidfuzz.fuzz = types.SimpleNamespace(
    token_set_ratio=token_set_ratio,
    partial_ratio=partial_ratio,
)

def extract(query, choices, *, scorer, limit, score_cutoff):
    if scorer is token_set_ratio:
        return []
    return [
        (name, 100, index)
        for index, name in enumerate(choices)
        if query in name
    ][:limit]

rapidfuzz.process = types.SimpleNamespace(extract=extract)
sys.modules["rapidfuzz"] = rapidfuzz

from agam.hooks import graph_recall

result = graph_recall.match_entities_3stage(
    "alphaone betatwo gammathree",
    {"alphaone-item", "betatwo-item", "gammathree-item"},
)
print(json.dumps(result))
"""
    outputs = []
    for seed in ("1", "2", "3"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(HOOK.parents[2]),
            },
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(json.loads(result.stdout))

    expected = ["alphaone-item", "betatwo-item", "gammathree-item"]
    assert outputs == [expected, expected, expected]

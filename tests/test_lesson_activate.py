"""Tests for the lesson_activate pre/post Bash tool hooks.

Both hooks read a JSON payload from stdin and emit JSON on stdout using
Claude Code's `hookSpecificOutput` / `additionalContext` contract.

Critical behaviors pinned here:

* Empty KG (schema applied, no lesson entities) -- silent no-op, exit 0.
* Missing KG file entirely -- graceful exit, no traceback.
* Non-Bash tool_name -- immediate exit, no output.
* Populated KG with a matching lesson -- injection with `additionalContext`.
* Real ~/.claude paths are never touched (mtimes unchanged).

Hooks are driven as subprocesses via `uv run --script` so the AGAM_KG_PATH
override and TMPDIR isolation mirror a production install.
"""

import json
import os
import pathlib
import sqlite3
import subprocess

import pytest


HOOK_PRE = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "hooks" / "lesson_activate.py"
HOOK_POST = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "hooks" / "lesson_activate_post.py"
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
    """Fresh KG + isolated TMPDIR + real-path guards.

    Returns (env, kg_path, snapshots).
    """
    kg = tmp_path / "graph.db"
    conn = sqlite3.connect(kg)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()

    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()

    env = {
        **os.environ,
        "AGAM_KG_PATH": str(kg),
        "TMPDIR": str(fake_tmp),
    }
    snapshots = {
        "kg": _mtime(REAL_KG),
        "agam_md": _mtime(REAL_AGAM_MD),
    }
    return env, kg, snapshots


def _insert_lesson(kg_path, name, description, severity, trigger_tool=None, trigger_error=None):
    """Insert a lesson entity with trigger properties."""
    conn = sqlite3.connect(kg_path)
    cur = conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, 'lesson', ?, datetime('now'), datetime('now'))",
        (name, description),
    )
    eid = cur.lastrowid
    conn.execute(
        "INSERT INTO properties (entity_id, key, value, updated) "
        "VALUES (?, 'severity', ?, datetime('now'))",
        (eid, severity),
    )
    if trigger_tool is not None:
        conn.execute(
            "INSERT INTO properties (entity_id, key, value, updated) "
            "VALUES (?, 'trigger-tool', ?, datetime('now'))",
            (eid, json.dumps(trigger_tool)),
        )
    if trigger_error is not None:
        conn.execute(
            "INSERT INTO properties (entity_id, key, value, updated) "
            "VALUES (?, 'trigger-error', ?, datetime('now'))",
            (eid, json.dumps(trigger_error)),
        )
    conn.commit()
    conn.close()


def _run(hook, env, payload, cwd):
    return subprocess.run(
        ["uv", "run", "--script", str(hook)],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )


def _assert_real_files_untouched(snapshots):
    assert _mtime(REAL_KG) == snapshots["kg"], (
        "Real ~/.claude/knowledge/graph.db was modified by a test"
    )
    assert _mtime(REAL_AGAM_MD) == snapshots["agam_md"], (
        "Real ~/.claude/agam/AGAM.md was modified by a test"
    )


# --- PreToolUse hook tests -------------------------------------------------


def test_pre_empty_kg_silent_noop(kg_env, tmp_path):
    """Empty KG (schema only) -> silent exit 0, no injection."""
    env, _, snapshots = kg_env
    payload = {
        "session_id": "s-pre-empty",
        "tool_name": "Bash",
        "tool_input": {"command": "pip install requests"},
    }
    r = _run(HOOK_PRE, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_pre_missing_kg_graceful(tmp_path):
    """Missing KG file path -> graceful exit 0."""
    snapshots = {"kg": _mtime(REAL_KG), "agam_md": _mtime(REAL_AGAM_MD)}
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    env = {
        **os.environ,
        "AGAM_KG_PATH": str(tmp_path / "nope.db"),
        "TMPDIR": str(fake_tmp),
    }
    payload = {
        "session_id": "s-pre-miss",
        "tool_name": "Bash",
        "tool_input": {"command": "pip install requests"},
    }
    r = subprocess.run(
        ["uv", "run", "--script", str(HOOK_PRE)],
        env=env, input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_pre_non_bash_tool_skipped(kg_env, tmp_path):
    """Non-Bash tool_name -> exit 0, no injection."""
    env, kg, snapshots = kg_env
    _insert_lesson(
        kg, "lesson-ssl-conda", "SSL broken in conda env; use truststore.",
        "high", trigger_tool=["pip install"],
    )
    payload = {
        "session_id": "s-pre-non-bash",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/foo"},
    }
    r = _run(HOOK_PRE, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_pre_matching_lesson_injects(kg_env, tmp_path):
    """Populated KG with matching trigger -> injection emitted."""
    env, kg, snapshots = kg_env
    _insert_lesson(
        kg, "lesson-ssl-conda",
        "SSL is broken in the conda env; use truststore to fix.",
        "high", trigger_tool=["pip install"],
    )
    payload = {
        "session_id": "s-pre-match",
        "tool_name": "Bash",
        "tool_input": {"command": "pip install requests"},
    }
    r = _run(HOOK_PRE, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert r.stdout.strip(), "Expected JSON output for matching lesson"
    parsed = json.loads(r.stdout)
    hso = parsed.get("hookSpecificOutput", {})
    assert hso.get("hookEventName") == "PreToolUse"
    ctx = hso.get("additionalContext", "")
    assert "lesson-ssl-conda" in ctx, ctx
    assert "LESSON ACTIVATION" in ctx, ctx
    _assert_real_files_untouched(snapshots)


def test_pre_no_match_silent(kg_env, tmp_path):
    """Lesson exists but command does not match trigger -> no output."""
    env, kg, snapshots = kg_env
    _insert_lesson(
        kg, "lesson-ssl-conda", "SSL conda issue.",
        "high", trigger_tool=["pip install"],
    )
    payload = {
        "session_id": "s-pre-nomatch",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    }
    r = _run(HOOK_PRE, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_pre_short_command_skipped(kg_env, tmp_path):
    """Commands shorter than 3 chars -> exit 0, no output."""
    env, _, snapshots = kg_env
    payload = {
        "session_id": "s-pre-short",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    r = _run(HOOK_PRE, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


# --- PostToolUse hook tests ------------------------------------------------


def test_post_empty_kg_silent_noop(kg_env, tmp_path):
    """Empty KG -> silent exit 0."""
    env, _, snapshots = kg_env
    payload = {
        "session_id": "s-post-empty",
        "tool_name": "Bash",
        "tool_output": {"stdout": "", "stderr": "CERTIFICATE_VERIFY_FAILED some error"},
    }
    r = _run(HOOK_POST, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_post_missing_kg_graceful(tmp_path):
    """Missing KG path -> graceful exit 0."""
    snapshots = {"kg": _mtime(REAL_KG), "agam_md": _mtime(REAL_AGAM_MD)}
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    env = {
        **os.environ,
        "AGAM_KG_PATH": str(tmp_path / "nope.db"),
        "TMPDIR": str(fake_tmp),
    }
    payload = {
        "session_id": "s-post-miss",
        "tool_name": "Bash",
        "tool_output": {"stdout": "", "stderr": "some failure message"},
    }
    r = subprocess.run(
        ["uv", "run", "--script", str(HOOK_POST)],
        env=env, input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_post_non_bash_tool_skipped(kg_env, tmp_path):
    """Non-Bash tool_name -> exit 0, no injection."""
    env, kg, snapshots = kg_env
    _insert_lesson(
        kg, "lesson-ssl-conda", "SSL conda fix.",
        "high", trigger_error=["certificate_verify_failed"],
    )
    payload = {
        "session_id": "s-post-non-bash",
        "tool_name": "Read",
        "tool_output": {"stdout": "", "stderr": "CERTIFICATE_VERIFY_FAILED something"},
    }
    r = _run(HOOK_POST, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)


def test_post_matching_error_injects(kg_env, tmp_path):
    """Populated KG with matching error pattern -> injection emitted."""
    env, kg, snapshots = kg_env
    _insert_lesson(
        kg, "lesson-ssl-conda",
        "SSL broken in conda env; use truststore to patch certifi bundles.",
        "high", trigger_error=["certificate_verify_failed"],
    )
    payload = {
        "session_id": "s-post-match",
        "tool_name": "Bash",
        "tool_output": {
            "stdout": "",
            "stderr": "ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
        },
    }
    r = _run(HOOK_POST, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert r.stdout.strip(), "Expected JSON output for matching error"
    parsed = json.loads(r.stdout)
    hso = parsed.get("hookSpecificOutput", {})
    assert hso.get("hookEventName") == "PostToolUse"
    ctx = hso.get("additionalContext", "")
    assert "lesson-ssl-conda" in ctx, ctx
    assert "LESSON ACTIVATION" in ctx, ctx
    _assert_real_files_untouched(snapshots)


def test_post_empty_output_skipped(kg_env, tmp_path):
    """Empty tool_output -> exit 0, no injection."""
    env, kg, snapshots = kg_env
    _insert_lesson(
        kg, "lesson-ssl-conda", "SSL fix.",
        "high", trigger_error=["certificate_verify_failed"],
    )
    payload = {
        "session_id": "s-post-empty-out",
        "tool_name": "Bash",
        "tool_output": {"stdout": "", "stderr": ""},
    }
    r = _run(HOOK_POST, env, payload, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "hookSpecificOutput" not in r.stdout
    _assert_real_files_untouched(snapshots)

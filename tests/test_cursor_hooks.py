"""End-to-end tests for the Cursor hook scripts (run as subprocesses)."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOOKS = REPO / "src" / "agam" / "hooks"
TOOLS_SRC = REPO / "src" / "agam" / "tools"
TRANSCRIPTS_SRC = REPO / "src" / "agam" / "transcripts.py"
FIXTURES = Path(__file__).parent / "fixtures" / "cursor"


@pytest.fixture
def tools_dir(tmp_path):
    """A tools dir holding everything the hooks vendor."""
    d = tmp_path / "tools"
    d.mkdir()
    for name in ("pending_queue.py", "cursor_rule.py", "agam_context.py"):
        shutil.copy2(TOOLS_SRC / name, d / name)
    shutil.copy2(TRANSCRIPTS_SRC, d / "transcripts.py")
    return d


def _run(hook, stdin_obj, env_extra):
    import os
    env = {"PATH": os.environ.get("PATH", ""), **env_extra}
    return subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=json.dumps(stdin_obj),
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_session_end_enqueues_real_work(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    data_home.mkdir()
    r = _run(
        "cursor_session_end.py",
        {
            "session_id": "sess-1",
            "transcript_path": str(FIXTURES / "transcript_with_tools.jsonl"),
            "workspace_roots": [str(tmp_path / "myproj")],
        },
        {"AGAM_DATA_HOME": str(data_home), "AGAM_TOOLS_DIR": str(tools_dir)},
    )
    assert r.returncode == 0, r.stderr
    queue = data_home / ".pending-closes.jsonl"
    assert queue.exists()
    entry = json.loads(queue.read_text().strip())
    assert entry["session_id"] == "sess-1"
    assert entry["context"] == "cursor"
    assert entry["transcript_path"].endswith("transcript_with_tools.jsonl")


def test_session_end_skips_trivial(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    data_home.mkdir()
    r = _run(
        "cursor_session_end.py",
        {
            "session_id": "sess-2",
            "transcript_path": str(FIXTURES / "transcript_text_only.jsonl"),
            "workspace_roots": [str(tmp_path)],
        },
        {"AGAM_DATA_HOME": str(data_home), "AGAM_TOOLS_DIR": str(tools_dir)},
    )
    assert r.returncode == 0, r.stderr
    assert not (data_home / ".pending-closes.jsonl").exists()


def test_session_end_no_transcript_is_noop(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    data_home.mkdir()
    r = _run(
        "cursor_session_end.py",
        {"session_id": "s", "transcript_path": str(tmp_path / "nope.jsonl")},
        {"AGAM_DATA_HOME": str(data_home), "AGAM_TOOLS_DIR": str(tools_dir)},
    )
    assert r.returncode == 0
    assert not (data_home / ".pending-closes.jsonl").exists()


def _seed_kg(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, type TEXT, "
        "description TEXT, created TEXT, updated TEXT, last_referenced TEXT);"
        "CREATE TABLE properties (entity_id INTEGER, key TEXT, value TEXT, updated TEXT);"
    )
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('agam', 'project', 'memory layer', '2026-01-01', '2026-06-18')"
    )
    conn.commit()
    conn.close()


def test_stop_writes_rule(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    (data_home / "knowledge").mkdir(parents=True)
    (data_home / "config.yaml").write_text("name: Kalyan\nprimary-goal: ship\n")
    _seed_kg(data_home / "knowledge" / "graph.db")

    workspace = tmp_path / "myproj"
    (workspace / ".git" / "info").mkdir(parents=True)

    r = _run(
        "cursor_stop.py",
        {"status": "completed", "loop_count": 0, "workspace_roots": [str(workspace)]},
        {"AGAM_DATA_HOME": str(data_home), "AGAM_TOOLS_DIR": str(tools_dir)},
    )
    assert r.returncode == 0, r.stderr
    rule = workspace / ".cursor" / "rules" / "agam.mdc"
    assert rule.exists()
    text = rule.read_text()
    assert "alwaysApply: true" in text
    assert "agam" in text
    # git-excluded
    assert "/.cursor/rules/agam.mdc" in (workspace / ".git" / "info" / "exclude").read_text()


def test_stop_noop_without_workspace(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    data_home.mkdir()
    r = _run(
        "cursor_stop.py",
        {"status": "completed", "loop_count": 0, "workspace_roots": []},
        {"AGAM_DATA_HOME": str(data_home), "AGAM_TOOLS_DIR": str(tools_dir)},
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "{}"

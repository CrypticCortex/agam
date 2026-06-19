"""Tests for source-agent provenance across the write path."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCHEMA = REPO / "knowledge" / "graph-schema.sql"
GRAPH_UPDATE = REPO / "src" / "agam" / "hooks" / "graph_update.py"


def test_pending_queue_includes_agent(tmp_path):
    from agam.tools import pending_queue as pq

    q = tmp_path / "q.jsonl"
    pq.replace_for_session(
        q, session_id="s", transcript_path="/t.jsonl", cwd="/w",
        context="cursor", agent="cursor",
    )
    entry = json.loads(q.read_text().strip())
    assert entry["agent"] == "cursor"


def test_pending_queue_default_agent(tmp_path):
    from agam.tools import pending_queue as pq

    q = tmp_path / "q.jsonl"
    pq.enqueue(q, session_id="s", transcript_path="/t", cwd="/w", context="host")
    entry = json.loads(q.read_text().strip())
    assert entry["agent"] == "unknown"


def test_enqueue_file_writes_per_session(tmp_path):
    from agam.tools import pending_queue as pq

    qdir = tmp_path / "queue"
    target = pq.enqueue_file(
        qdir, session_id="abc-123", transcript_path="/t.jsonl", cwd="/w",
        context="cursor", agent="cursor",
    )
    assert target == qdir / "abc-123.json"
    entry = json.loads(target.read_text().strip())
    assert entry["agent"] == "cursor"
    assert entry["session_id"] == "abc-123"


def test_enqueue_file_idempotent_per_session(tmp_path):
    from agam.tools import pending_queue as pq

    qdir = tmp_path / "queue"
    pq.enqueue_file(qdir, session_id="s", transcript_path="/a", cwd="/w", context="cursor", agent="cursor")
    pq.enqueue_file(qdir, session_id="s", transcript_path="/b", cwd="/w", context="cursor", agent="cursor")
    files = list(qdir.glob("*.json"))
    assert len(files) == 1  # same session overwrites, no pile-up
    assert json.loads(files[0].read_text())["transcript_path"] == "/b"


def test_apply_proposals_tags_lesson(tmp_path, monkeypatch):
    from agam.tools import apply_proposals as ap

    monkeypatch.setattr(ap, "SOURCE_AGENT", "cursor")

    agam_md = tmp_path / "AGAM.md"
    agam_md.write_text("# AGAM\n\n## What I've Learned\n\n### Lessons\n\n")
    thisai_md = tmp_path / "THISAI.md"
    thisai_md.write_text("# THISAI\n")
    suvadu_md = tmp_path / "SUVADU.md"
    memory_dir = tmp_path / "memory"

    proposals = {
        "lesson": [{"title": "Test lesson", "body": "[lesson] **Test.** Do the thing. Source: 2026 session."}],
    }
    applied = ap.apply_proposals(
        proposals, agam_md=agam_md, thisai_md=thisai_md,
        suvadu_md=suvadu_md, memory_dir=memory_dir,
    )
    assert applied["lessons"] == 1
    text = agam_md.read_text()
    assert "(via cursor)" in text
    assert "[cursor]" in suvadu_md.read_text()


def test_apply_proposals_no_tag_when_unknown(tmp_path, monkeypatch):
    from agam.tools import apply_proposals as ap

    monkeypatch.setattr(ap, "SOURCE_AGENT", "unknown")
    agam_md = tmp_path / "AGAM.md"
    agam_md.write_text("## What I've Learned\n\n")
    proposals = {"lesson": [{"title": "L", "body": "[lesson] body."}]}
    ap.apply_proposals(
        proposals, agam_md=agam_md, thisai_md=tmp_path / "T.md",
        suvadu_md=tmp_path / "S.md", memory_dir=tmp_path / "m",
    )
    assert "(via" not in agam_md.read_text()


def _make_full_kg(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()


def test_graph_update_stamps_source_agent(tmp_path):
    import uuid
    sid = f"prov-{uuid.uuid4().hex}"  # unique: avoid the /tmp dedup flag
    kg = tmp_path / "graph.db"
    _make_full_kg(kg)

    coding = tmp_path / "coding"
    (coding / "myproj").mkdir(parents=True)

    # Cursor-format transcript: 3+ user turns + a coding path + signal.
    transcript = tmp_path / "t.jsonl"
    lines = []
    for i in range(4):
        lines.append('{"role":"user","message":{"content":[{"type":"text","text":"work"}]}}')
    lines.append(
        '{"role":"assistant","message":{"content":[{"type":"text","text":'
        f'"editing {coding}/myproj/file.py shipped"' "}]}}"
    )
    transcript.write_text("\n".join(lines) + "\n")

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "AGAM_KG_PATH": str(kg),
        "AGAM_HOST_CODING_DIR": str(coding),
    }
    r = subprocess.run(
        [sys.executable, str(GRAPH_UPDATE)],
        input=json.dumps({"session_id": sid, "transcript_path": str(transcript), "agent": "cursor"}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(str(kg))
    rows = conn.execute(
        """SELECT e.name, p.value FROM entities e
           JOIN properties p ON p.entity_id = e.id
           WHERE p.key = 'source-agent'"""
    ).fetchall()
    conn.close()
    assert any(name == "myproj" and val == "cursor" for name, val in rows), rows

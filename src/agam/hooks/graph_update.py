#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///

"""
Stop hook: extracts new knowledge from the session transcript and updates
the knowledge graph. Runs silently -- never blocks session close.

Strategy: scan the transcript for tool calls that reveal project work,
file paths, git operations, and error patterns. Extract entity names
and relationships without an LLM call.

This is intentionally heuristic-based for speed (<200ms). An LLM-based
extractor would be more accurate but costs tokens and adds 10-30s latency.

Environment variables:
    AGAM_KG_PATH        Path to knowledge graph SQLite DB
                        (default: ~/.claude/knowledge/graph.db)
    AGAM_KG_DIR         Directory for KG sidecar caches (entity-names.txt,
                        vault-stale.marker). Defaults to the parent of
                        AGAM_KG_PATH.
    AGAM_SESSIONS_DIR   Directory holding Claude Code session jsonl files
                        (default: ~/.claude/projects)
    AGAM_KG_TOOL        Path to knowledge-graph.py CLI (used for concept-index
                        rebuild). Default: ~/.claude/tools/knowledge-graph.py
    AGAM_VAULT_DIR      Obsidian vault directory. Used only for drift check.
                        (default: ~/claude-knowledge-vault)
"""

import json
import sys
import os
import re
import sqlite3
import glob
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(
    os.environ.get("AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db"))
)
_KG_DIR = Path(
    os.environ.get("AGAM_KG_DIR") or str(DB_PATH.parent)
)
NAMES_CACHE = _KG_DIR / "entity-names.txt"
VAULT_MARKER = _KG_DIR / "vault-stale.marker"
SESSIONS_DIR = Path(
    os.environ.get("AGAM_SESSIONS_DIR", os.path.expanduser("~/.claude/projects"))
)
KG_TOOL = Path(
    os.environ.get("AGAM_KG_TOOL", os.path.expanduser("~/.claude/tools/knowledge-graph.py"))
)
VAULT_DIR = Path(
    os.environ.get("AGAM_VAULT_DIR", os.path.expanduser("~/claude-knowledge-vault"))
)

# Which agent's session produced this update (claude | cursor | unknown). Set in
# main() from the hook stdin ("agent") or the AGAM_SOURCE_AGENT env. Stamped as a
# source-agent property on every entity touched, so a shared graph records which
# agent taught each fact.
SOURCE_AGENT = os.environ.get("AGAM_SOURCE_AGENT", "unknown")


def now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if not DB_PATH.exists():
        sys.exit(0)
    db = sqlite3.connect(str(DB_PATH), timeout=2)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def normalize_name(name):
    """PascalCase/camelCase -> kebab-case. Idempotent on already-kebab names."""
    s = re.sub(r'([a-z])([A-Z])', r'\1-\2', name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', s)
    s = s.replace('_', '-').lower()
    s = re.sub(r'-+', '-', s).strip('-')
    return s


def _stamp_source_agent(db, name):
    """Record which agent last taught this entity (no-op when agent unknown)."""
    if not SOURCE_AGENT or SOURCE_AGENT == "unknown":
        return
    set_prop(db, name, "source-agent", SOURCE_AGENT)


def ensure_entity(db, name, etype, desc=""):
    """Insert or update entity. Returns entity id."""
    name = normalize_name(name)
    row = db.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    if row:
        if desc:
            db.execute(
                "UPDATE entities SET description = ?, updated = ? WHERE id = ?",
                (desc, now(), row[0])
            )
        else:
            db.execute("UPDATE entities SET updated = ? WHERE id = ?", (now(), row[0]))
        _stamp_source_agent(db, name)
        return row[0]
    else:
        cursor = db.execute(
            "INSERT INTO entities (name, type, description, created, updated) VALUES (?, ?, ?, ?, ?)",
            (name, etype, desc, now(), now())
        )
        eid = cursor.lastrowid
        # Sync FTS
        try:
            db.execute(
                "INSERT INTO entities_fts(rowid, name, type, description) VALUES (?, ?, ?, ?)",
                (eid, name, etype, desc)
            )
        except Exception:
            pass
        _stamp_source_agent(db, name)
        return eid


def ensure_relation(db, src_name, relation, tgt_name):
    """Create relationship if both entities exist."""
    src = db.execute("SELECT id FROM entities WHERE name = ?", (src_name,)).fetchone()
    tgt = db.execute("SELECT id FROM entities WHERE name = ?", (tgt_name,)).fetchone()
    if src and tgt:
        try:
            db.execute(
                "INSERT OR IGNORE INTO relationships (source_id, target_id, relation, weight, created) VALUES (?, ?, ?, 0.7, ?)",
                (src[0], tgt[0], relation, now())
            )
        except Exception:
            pass


def set_prop(db, entity_name, key, value):
    """Set a property on an entity."""
    row = db.execute("SELECT id FROM entities WHERE name = ?", (entity_name,)).fetchone()
    if row:
        db.execute(
            "INSERT OR REPLACE INTO properties (entity_id, key, value, updated) VALUES (?, ?, ?, ?)",
            (row[0], key, value, now())
        )


def refresh_cache(db):
    """Regenerate the entity names cache file for the graph-recall hook."""
    names = db.execute("SELECT LOWER(name) FROM entities").fetchall()
    NAMES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(NAMES_CACHE, "w") as f:
        for (name,) in names:
            f.write(name + "\n")


def extract_projects_from_paths(transcript):
    """Extract project names from file paths in tool calls."""
    projects = set()

    # Match paths under the user's coding directory (host or in-container).
    # Host coding dir is env-configurable for v0 releaseability; defaults to ~/coding.
    host_coding = os.environ.get("AGAM_HOST_CODING_DIR", os.path.expanduser("~/coding"))
    host_pattern = re.escape(host_coding)
    for match in re.finditer(rf"(?:{host_pattern}|/workspaces/coding)/([a-zA-Z0-9_-]+)", transcript):
        name = match.group(1)
        if name not in {"node_modules", ".git", "dist", "build", "__pycache__", ".next"}:
            projects.add(name)

    return projects


def extract_git_activity(transcript):
    """Extract git branch names and repo info."""
    branches = set()
    for match in re.finditer(r"git (?:checkout|switch|branch)\s+(?:-[bB]\s+)?([a-zA-Z0-9/_.-]+)", transcript):
        branches.add(match.group(1))
    for match in re.finditer(r"On branch ([a-zA-Z0-9/_.-]+)", transcript):
        branches.add(match.group(1))
    return branches


def extract_npm_packages(transcript):
    """Extract npm package names from install commands."""
    packages = set()
    for match in re.finditer(r"npm (?:install|add|i)\s+(?:--save-dev\s+)?([a-zA-Z0-9@/_.-]+)", transcript):
        pkg = match.group(1)
        if not pkg.startswith("-"):
            packages.add(pkg)
    return packages


def extract_errors(transcript):
    """Extract significant error patterns."""
    errors = []
    # Look for error messages in tool results
    for match in re.finditer(r"(?:Error|ERROR|error):\s*(.{20,100})", transcript):
        msg = match.group(1).strip()
        if msg and not any(skip in msg.lower() for skip in ["enoent", "no such file", "not found"]):
            errors.append(msg[:100])
    return errors[:3]  # Cap at 3 most recent


def extract_research_activity(transcript):
    """Extract research project metadata from /investigate-topic or /research usage."""
    projects = []
    today = datetime.now().strftime("%Y-%m-%d")

    # Match /investigate-topic output header (skill created 2026-04-13)
    for match in re.finditer(
        r"## Investigation: (.+?)\n\*\*Framework:\*\* (\w+)",
        transcript
    ):
        topic = match.group(1).strip()
        framework = match.group(2).strip().lower()
        slug = re.sub(r'[^a-zA-Z0-9-]', '-', topic.lower())[:50].strip('-')
        slug = re.sub(r'-+', '-', slug)
        projects.append({
            'slug': f"research-{today}-{slug}",
            'topic': topic,
            'framework': framework,
            'date': today,
        })

    # Match /research output header (depth can be multi-word like "Deep Investigation")
    for match in re.finditer(
        r"## Research: (.+?)\n\*\*Depth:\*\* ([^|*\n]+?)(?:\s*\||\s*\n)",
        transcript
    ):
        topic = match.group(1).strip()
        depth = match.group(2).strip().lower()
        slug = re.sub(r'[^a-zA-Z0-9-]', '-', topic.lower())[:50].strip('-')
        slug = re.sub(r'-+', '-', slug)
        # Avoid duplicating if same topic was already captured from /investigate-topic
        if not any(p['topic'] == topic for p in projects):
            projects.append({
                'slug': f"research-{today}-{slug}",
                'topic': topic,
                'framework': 'unknown',
                'depth': depth,
                'date': today,
            })

    return projects


def cleanup_stale_tmp():
    """Remove dedup files older than 24h."""
    import time
    cutoff = time.time() - 86400
    for f in glob.glob(os.path.join(tempfile.gettempdir(), "graph-update-*")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.unlink(f)
        except OSError:
            pass


def main():
    cleanup_stale_tmp()

    data = json.load(sys.stdin)
    session_id = data.get("session_id", "unknown")

    # Record the source agent for provenance stamping on entities.
    global SOURCE_AGENT
    SOURCE_AGENT = data.get("agent") or os.environ.get("AGAM_SOURCE_AGENT", "unknown")

    # Dedup: only run once per session
    flag = os.path.join(tempfile.gettempdir(), f"graph-update-{session_id}")
    if os.path.exists(flag):
        sys.exit(0)

    # Resolve the transcript to scan. Cursor passes an explicit transcript_path
    # (and stores transcripts in a different tree), so honor it when present.
    # Claude's legacy path globs the sessions dir for the most recent file.
    transcript_path = data.get("transcript_path", "")
    if transcript_path and os.path.exists(transcript_path):
        target = transcript_path
    else:
        transcripts = sorted(
            glob.glob(str(SESSIONS_DIR / "**" / "*.jsonl"), recursive=True),
            key=os.path.getmtime,
        )
        if not transcripts:
            sys.exit(0)
        target = transcripts[-1]

    # Read transcript (only last 50KB to stay fast)
    with open(target, errors="replace") as f:
        f.seek(max(0, os.path.getsize(target) - 50000))
        transcript = f.read()

    # Skip trivial sessions. Count Claude ("type":"user") and Cursor
    # ("role":"user") user turns -- the rest of the extraction below is raw-text
    # regex and works for either transcript shape.
    user_turns = transcript.count('"type":"user"') + transcript.count('"role":"user"')
    if user_turns < 3:
        sys.exit(0)

    db = get_db()
    changes = 0

    # Extract projects worked on
    projects = extract_projects_from_paths(transcript)
    today = datetime.now().strftime("%Y-%m-%d")

    user_entity = os.environ.get("AGAM_USER_ENTITY", "User").strip() or "User"
    for proj in projects:
        eid = ensure_entity(db, proj, "project")
        if eid:
            set_prop(db, proj, "last-worked", today)
            ensure_relation(db, user_entity, "works-on", proj)
            changes += 1

    # Extract npm packages (new dependencies = new relationships)
    packages = extract_npm_packages(transcript)
    for pkg in packages:
        eid = ensure_entity(db, pkg, "package", f"npm package {pkg}")
        if eid:
            # Try to relate to the most recent project
            if projects:
                proj = sorted(projects)[-1]
                ensure_relation(db, proj, "uses-package", pkg)
            changes += 1

    # Extract research activity from /investigate-topic and /research
    research_projects = extract_research_activity(transcript)
    for rp in research_projects:
        eid = ensure_entity(db, rp['slug'], "research-project", f"Research: {rp['topic']}")
        if eid:
            set_prop(db, rp['slug'], "date", rp['date'])
            if rp.get('framework', 'unknown') != 'unknown':
                set_prop(db, rp['slug'], "framework", rp['framework'])
            if rp.get('depth'):
                set_prop(db, rp['slug'], "depth", rp['depth'])
            ensure_relation(db, user_entity, "researched", rp['slug'])
            changes += 1

    # Error extraction removed -- errors are transient, they pollute the graph
    # with raw compiler output that has no relational value. Bugs should be
    # added manually with meaningful names and descriptions.

    if changes > 0:
        db.commit()
        refresh_cache(db)

        # Rebuild concept index (keeps O(1) lookup in sync with graph)
        if KG_TOOL.exists():
            try:
                subprocess.run(
                    [sys.executable, str(KG_TOOL), "build-index"],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass

        # Check vault drift -- write stale marker if needed
        if VAULT_DIR.exists():
            graph_count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            vault_count = len(list(VAULT_DIR.glob("*.md")))
            drift = abs(graph_count - vault_count)
            if drift > 5 or (graph_count > 0 and drift / graph_count > 0.03):
                VAULT_MARKER.parent.mkdir(parents=True, exist_ok=True)
                with open(VAULT_MARKER, "w") as m:
                    m.write(f"drift={drift}\ngraph={graph_count}\nvault={vault_count}\n")

    db.close()

    # Mark as done
    with open(flag, "w") as f:
        f.write(today)

    sys.exit(0)


if __name__ == "__main__":
    main()

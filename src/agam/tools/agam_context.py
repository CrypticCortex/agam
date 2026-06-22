#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Dynamic identity context assembly for Agam v2.

Usage:
    agam_context.py boot              Session cold-start: identity + trajectory + recent work
    agam_context.py entity <name>     Deep entity context from graph + work-log + git
    agam_context.py direction         Goals, projects, stall signals
    agam_context.py learned           Lessons, corrections, insights
    agam_context.py render-rule       Emit a Cursor .mdc digest (identity + hot graph + lessons)

Environment variables:
    AGAM_HOME          Directory holding AGAM.md / THISAI.md / MUGAM.md
                       (default: ~/.claude/agam)
    AGAM_KG_PATH       Path to knowledge graph SQLite DB
                       (default: ~/.claude/knowledge/graph.db)
    AGAM_WORK_LOG      Path to work-log markdown file
                       (default: ~/.claude/work-log.md)
    AGAM_PROJECTS_DIR  Directory to probe for entity git repos
                       (default: ~/coding)
"""

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


AGAM_HOME = pathlib.Path(
    os.environ.get("AGAM_HOME", os.path.expanduser("~/.claude/agam"))
)
AGAM_KG_PATH = pathlib.Path(
    os.environ.get("AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db"))
)
WORK_LOG = pathlib.Path(
    os.environ.get("AGAM_WORK_LOG", os.path.expanduser("~/.claude/work-log.md"))
)
AGAM_PROJECTS_DIR = pathlib.Path(
    os.environ.get("AGAM_PROJECTS_DIR", os.path.expanduser("~/coding"))
)

AGAM_MD = AGAM_HOME / "AGAM.md"
THISAI_MD = AGAM_HOME / "THISAI.md"
MAX_OUTPUT = 4500  # bytes, accommodates lint findings + identity + goals


def get_db():
    if not AGAM_KG_PATH.exists():
        return None
    db = sqlite3.connect(str(AGAM_KG_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = sqlite3.Row
    return db


def query_entity(db, name):
    """Get entity by name (case-insensitive)."""
    row = db.execute(
        "SELECT * FROM entities WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return dict(row) if row else None


def query_properties(db, entity_id):
    """Get all properties for an entity."""
    rows = db.execute(
        "SELECT key, value FROM properties WHERE entity_id = ?", (entity_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def query_relationships(db, entity_id):
    """Get all relationships (both directions) for an entity."""
    outgoing = db.execute("""
        SELECT r.relation, e.name, e.type, e.description, r.weight
        FROM relationships r JOIN entities e ON r.target_id = e.id
        WHERE r.source_id = ?
    """, (entity_id,)).fetchall()

    incoming = db.execute("""
        SELECT r.relation, e.name, e.type, e.description, r.weight
        FROM relationships r JOIN entities e ON r.source_id = e.id
        WHERE r.target_id = ?
    """, (entity_id,)).fetchall()

    return (
        [dict(r) for r in outgoing],
        [dict(r) for r in incoming],
    )


def recent_work_log(n=3):
    """Get last n work-log entries."""
    if not WORK_LOG.exists():
        return ""
    text = WORK_LOG.read_text()
    entries = re.split(r'\n---\n', text)
    # Filter out the header
    entries = [e.strip() for e in entries if e.strip() and not e.strip().startswith("# Work Log")]
    return "\n\n---\n\n".join(entries[-n:]) if entries else ""


def work_log_grep(term, n=5):
    """Find last n work-log entries mentioning a term."""
    if not WORK_LOG.exists():
        return ""
    text = WORK_LOG.read_text()
    entries = re.split(r'\n---\n', text)
    matches = []
    for e in entries:
        if term.lower() in e.lower():
            matches.append(e.strip())
    return "\n\n---\n\n".join(matches[-n:]) if matches else ""


def git_log_for_path(path, n=10):
    """Get recent git log for a directory."""
    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        return ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{n}"],
            cwd=expanded, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def recently_active_entities(db, n=5):
    """Get n most recently updated entities."""
    rows = db.execute(
        "SELECT name, type, description FROM entities ORDER BY updated DESC LIMIT ?",
        (n,)
    ).fetchall()
    return [dict(r) for r in rows]


def truncate(text, max_bytes=MAX_OUTPUT):
    """Truncate text to fit within byte budget."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[...truncated]"


def scan_project_lessons(db):
    """Scan CWD for project signals, match against lesson context triggers."""
    cwd = Path.cwd()
    signals = set()

    signal_map = {
        "environment.yml": "conda-env",
        "conda.lock": "conda-env",
        ".conda": "conda-env",
        "package.json": "node-project",
        ".nvmrc": "nvm-project",
        "pyproject.toml": "python-project",
        "Pipfile": "python-project",
        "requirements.txt": "python-project",
        "Dockerfile": "docker",
        ".env": "has-secrets",
    }

    for filename, signal in signal_map.items():
        if (cwd / filename).exists():
            signals.add(signal)

    if not signals:
        return ""

    rows = db.execute("""
        SELECT e.name, e.description, p.value
        FROM entities e
        JOIN properties p ON e.id = p.entity_id
        WHERE e.type = 'lesson' AND p.key = 'trigger-context'
    """).fetchall()

    matched = []
    for name, desc, ctx_json in rows:
        try:
            tags = json.loads(ctx_json)
            if signals & set(tags):
                # Get severity
                sev_row = db.execute("""
                    SELECT p.value FROM properties p
                    JOIN entities e ON p.entity_id = e.id
                    WHERE e.name = ? AND p.key = 'severity'
                """, (name,)).fetchone()
                severity = dict(sev_row)["value"] if sev_row else "medium"
                matched.append((name, desc, severity))
        except (json.JSONDecodeError, TypeError):
            continue

    if not matched:
        return ""

    # Sort by severity
    sev_order = {"high": 0, "medium": 1, "low": 2}
    matched.sort(key=lambda m: sev_order.get(m[2], 3))

    lines = ["## Active Lessons (project context)"]
    for name, desc, severity in matched[:3]:
        lines.append(f"- **{name}** [{severity.upper()}]: {desc[:100]}")
    return "\n".join(lines)


def _watchdog_sync_status():
    """Report watchdog health in boot context.

    - Healthy (< 24h): "Last Agam sync: Xh ago (watchdog)"
    - Stale (>= 24h) or missing: "WARNING: watchdog stale ..."
    Returns empty string when the watchdog has not been installed yet.
    """
    import json as _json
    import time as _time

    log_path = AGAM_HOME / ".watchdog-log"
    queue_path = AGAM_HOME / ".pending-closes.jsonl"
    queue_dir = AGAM_HOME / "queue"

    # Post-migration the queue is file-per-session under queue/; the legacy
    # .pending-closes.jsonl may still hold rows for the container watchdog.
    # Count both so the boot status reflects reality.
    queue_depth = 0
    if queue_dir.exists():
        try:
            queue_depth += sum(1 for _ in queue_dir.glob("*.json"))
        except OSError:
            pass
    if queue_path.exists():
        try:
            queue_depth += sum(1 for line in queue_path.read_text().splitlines() if line.strip())
        except OSError:
            pass

    if not log_path.exists():
        if queue_depth == 0:
            return ""
        return f"Watchdog sync: not installed (queue depth {queue_depth})."

    last_ts = 0.0
    try:
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            last_ts = max(last_ts, float(row.get("ts", 0)))
    except OSError:
        return ""

    if last_ts == 0:
        return "Watchdog sync: log empty."

    age_hours = (_time.time() - last_ts) / 3600
    if age_hours < 24:
        return f"Last Agam sync: {age_hours:.1f}h ago (watchdog)."
    return f"WARNING: watchdog stale >{int(age_hours)}h; queue depth {queue_depth}."


# --- Subcommands ---


def cmd_boot():
    """Session cold-start: minimal context, pointers to deeper tools.

    CLAUDE.md + MEMORY.md already provide identity, preferences, and tool docs.
    Boot only adds what they can't: CWD-relevant lessons and lint findings.
    For anything else, the graph-recall hook injects on demand per message.
    """
    parts = []

    # Project-relevant lessons (scan CWD for context signals)
    # This is the primary value of boot -- lessons that apply to THIS project
    db = get_db()
    if db:
        project_lessons = scan_project_lessons(db)
        if project_lessons:
            parts.append(project_lessons)
        db.close()

    # Lint findings (if recent, < 7 days old)
    findings_path = AGAM_HOME / ".lint-findings.md"
    if findings_path.exists():
        import time as _time
        age_days = (_time.time() - findings_path.stat().st_mtime) / 86400
        if age_days < 7:
            findings = findings_path.read_text().strip()
            if findings:
                parts.append("## Attention\n" + findings)

    # Watchdog sync status
    sync_line = _watchdog_sync_status()
    if sync_line:
        parts.append(sync_line)

    # Pointer to deeper context (not the context itself)
    parts.append(
        f"For identity/goals: read {AGAM_MD} and {THISAI_MD}\n"
        f"For recent work: read {WORK_LOG}\n"
        "For entity context: knowledge_graph.py search <term>"
    )

    output = "\n\n".join(parts)
    print(truncate(output))


def cmd_entity(name):
    """Deep entity context from graph + work-log + git."""
    db = get_db()
    if not db:
        print(f"[ERROR] Knowledge graph not found at {AGAM_KG_PATH}")
        return

    entity = query_entity(db, name)
    if not entity:
        print(f"[NOT FOUND] No entity '{name}' in graph")
        db.close()
        return

    parts = []

    # 1. Entity basics
    parts.append(
        f"# {entity['name']} ({entity['type']})\n"
        f"{entity['description']}\n"
        f"Created: {entity['created'][:10]} | Updated: {entity['updated'][:10]}"
    )

    # 2. Properties (rationale first for decisions)
    props = query_properties(db, entity["id"])
    if props:
        rationale = props.pop("rationale", None)
        if rationale:
            parts.append(f"## Rationale\n{rationale}")
        if props:
            prop_lines = ["## Properties"]
            for k, v in props.items():
                prop_lines.append(f"- {k}: {v}")
            parts.append("\n".join(prop_lines))

    # 3. Relationships (depth 1)
    outgoing, incoming = query_relationships(db, entity["id"])

    if outgoing:
        # Group by relation type
        grouped = {}
        for r in outgoing:
            grouped.setdefault(r["relation"], []).append(r)
        lines = ["## Outgoing Relationships"]
        for rel, items in grouped.items():
            lines.append(f"\n### --[{rel}]-->")
            for item in items:
                desc = item["description"][:100] + "..." if len(item["description"]) > 100 else item["description"]
                conf = f" [{item['weight']}]" if item.get("weight", 1.0) != 1.0 else ""
                lines.append(f"- **{item['name']}** ({item['type']}){conf}: {desc}")
        parts.append("\n".join(lines))

    if incoming:
        grouped = {}
        for r in incoming:
            grouped.setdefault(r["relation"], []).append(r)
        lines = ["## Incoming Relationships"]
        for rel, items in grouped.items():
            lines.append(f"\n### <--[{rel}]--")
            for item in items:
                desc = item["description"][:100] + "..." if len(item["description"]) > 100 else item["description"]
                conf = f" [{item['weight']}]" if item.get("weight", 1.0) != 1.0 else ""
                lines.append(f"- **{item['name']}** ({item['type']}){conf}: {desc}")
        parts.append("\n".join(lines))

    # 4. Work-log mentions
    wl = work_log_grep(name, 5)
    if wl:
        parts.append("## Work Log Mentions\n" + wl)

    # 5. Git log (if entity has a path property or we can find it under
    # AGAM_PROJECTS_DIR).
    entity_path = props.get("path", "")
    if not entity_path:
        # Try common name variants within the configured projects directory.
        for candidate_name in [name, name.replace("-", "_"), name.replace("-", "")]:
            candidate = AGAM_PROJECTS_DIR / candidate_name
            if candidate.is_dir():
                entity_path = str(candidate)
                break

    if entity_path:
        gl = git_log_for_path(entity_path)
        if gl:
            parts.append(f"## Git Log ({entity_path})\n```\n{gl}\n```")

    # 6. Correction doc (if linked)
    doc_path = props.get("doc", "")
    if doc_path:
        expanded = os.path.expanduser(doc_path)
        if os.path.exists(expanded):
            doc_content = Path(expanded).read_text().strip()
            parts.append(f"## Correction Detail\n{doc_content}")
        else:
            parts.append(f"## Correction Detail\n[File not found: {doc_path}]")

    db.close()
    output = "\n\n".join(parts)
    print(truncate(output))


def cmd_direction():
    """Current goals, projects, stall signals."""
    if not THISAI_MD.exists():
        print(f"[ERROR] THISAI.md not found at {THISAI_MD}")
        return

    text = THISAI_MD.read_text()

    # Parse stall signals
    stalled = re.findall(r'### (.+?) -- stalled since (\S+)', text)

    parts = [text]

    if stalled:
        parts.append("\n## STALL ALERTS")
        today = datetime.now()
        for name, date_str in stalled:
            try:
                stall_date = datetime.strptime(date_str, "%Y-%m-%d")
                days = (today - stall_date).days
                parts.append(f"- {name}: {days} days stalled (since {date_str})")
            except ValueError:
                parts.append(f"- {name}: stalled since {date_str}")

    # Check graph for stall signals on active goals
    db = get_db()
    if db:
        # Find goal entities with stall-related properties
        rows = db.execute("""
            SELECT e.name, p.key, p.value
            FROM entities e
            JOIN properties p ON e.id = p.entity_id
            WHERE e.type = 'goal' AND p.key IN ('stall_signal', 'status', 'blocker')
        """).fetchall()
        if rows:
            parts.append("\n## Graph Stall Data")
            for r in rows:
                parts.append(f"- {r[0]}.{r[1]} = {r[2]}")
        db.close()

    output = "\n\n".join(parts)
    print(truncate(output))


def cmd_learned():
    """Lessons, corrections, insights from AGAM.md."""
    if not AGAM_MD.exists():
        print(f"[ERROR] AGAM.md not found at {AGAM_MD}")
        return

    text = AGAM_MD.read_text()

    # Extract "What I've Learned" section
    match = re.search(r"(## What I've Learned.*)", text, re.DOTALL)
    if match:
        print(truncate(match.group(1)))
    else:
        print(f"[NOT FOUND] No 'What I've Learned' section in AGAM.md at {AGAM_MD}")


def _load_identity_digest():
    """Pull name + primary-goal from config.yaml (cheap, no yaml dep)."""
    name = goal = ""
    cfg = AGAM_HOME / "config.yaml"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"')
            elif line.startswith("primary-goal:"):
                goal = line.split(":", 1)[1].strip().strip('"')
    return name, goal


def cmd_render_rule(max_entities=12):
    """Emit a Cursor rule (.mdc) carrying identity + hot graph state + lessons.

    This is Cursor's only reliable model-facing channel: a rule file with
    ``alwaysApply: true`` is read into the model's context every prompt. It is
    NOT per-prompt selective (Cursor hooks cannot inject), so we surface the
    highest-signal slice: who the user is, the most recently active entities,
    and any high-severity lessons. The Cursor stop hook regenerates this file
    after each turn so it stays fresh.
    """
    lines = [
        "---",
        "alwaysApply: true",
        "---",
        "# agam memory (auto-generated -- do not edit; refreshed by agam after each turn)",
    ]
    name, goal = _load_identity_digest()
    if name or goal:
        lines.append("")
        if name:
            lines.append(f"User: {name}")
        if goal:
            lines.append(f"Primary goal: {goal}")

    db = get_db()
    if db:
        def _source_agent(name):
            """Provenance tag for an entity, e.g. ' (cursor)'. Empty if unknown."""
            try:
                r = db.execute(
                    """SELECT p.value FROM properties p
                       JOIN entities e ON p.entity_id = e.id
                       WHERE e.name = ? AND p.key = 'source-agent' LIMIT 1""",
                    (name,),
                ).fetchone()
            except sqlite3.Error:
                return ""
            val = (r["value"] if r and hasattr(r, "keys") else (r[0] if r else "")) or ""
            return f" ({val})" if val and val != "unknown" else ""

        ents = recently_active_entities(db, max_entities)
        if ents:
            lines.append("")
            lines.append("## Hot context (recent knowledge-graph state)")
            for e in ents:
                desc = (e.get("description") or "")[:100]
                lines.append(f"- {e['name']} [{e['type']}]{_source_agent(e['name'])}: {desc}")
        try:
            lessons = db.execute(
                """SELECT e.name, e.description
                   FROM entities e JOIN properties p ON e.id = p.entity_id
                   WHERE e.type = 'lesson' AND p.key = 'severity'
                         AND p.value = 'high'
                   LIMIT 5"""
            ).fetchall()
        except sqlite3.Error:
            lessons = []
        if lessons:
            lines.append("")
            lines.append("## Active lessons")
            for row in lessons:
                nm = row["name"] if hasattr(row, "keys") else row[0]
                desc = (row["description"] if hasattr(row, "keys") else row[1]) or ""
                lines.append(f"- {nm}{_source_agent(nm)}: {desc[:100]}")
        db.close()

    lines.append("")
    lines.append(
        "(Recall on Cursor is advisory -- per-prompt injection is unavailable. "
        "Treat the above as your lived experience and cite these entities when relevant.)"
    )
    print(truncate("\n".join(lines)))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "boot":
        cmd_boot()
    elif cmd == "render-rule":
        cmd_render_rule()
    elif cmd == "entity":
        if len(sys.argv) < 3:
            print("Usage: agam_context.py entity <name>")
            sys.exit(1)
        cmd_entity(sys.argv[2])
    elif cmd == "direction":
        cmd_direction()
    elif cmd == "learned":
        cmd_learned()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

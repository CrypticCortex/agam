#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""
agam_lint.py -- Knowledge health check for the Agam shared brain.

Runs heuristic audits (no LLM needed) and writes top findings to
$AGAM_HOME/.lint-findings.md for boot injection.

Usage:
    agam_lint.py           Full lint (10 audits)
    agam_lint.py --quick   Fast lint (contradictions + stale + graph health + watchdog)

Environment variables:
    AGAM_HOME          Directory holding AGAM.md / THISAI.md / MUGAM.md
                       (default: ~/.claude/agam)
    AGAM_KG_PATH       Path to knowledge graph SQLite DB
                       (default: ~/.claude/knowledge/graph.db)
    AGAM_WORK_LOG      Path to work-log markdown file
                       (default: ~/.claude/work-log.md)
    AGAM_SESSIONS_DIR  Directory holding Claude Code session jsonl files
                       (default: ~/.claude/projects)
    AGAM_MEMORY_DIR    Directory holding general memory markdown files
                       (default: ~/.claude/MEMORY)
"""

import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


DB_PATH = Path(
    os.environ.get("AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db"))
)
AGAM_DIR = Path(
    os.environ.get("AGAM_HOME", os.path.expanduser("~/.claude/agam"))
)
WORKLOG = Path(
    os.environ.get("AGAM_WORK_LOG", os.path.expanduser("~/.claude/work-log.md"))
)
SESSIONS_DIR = Path(
    os.environ.get("AGAM_SESSIONS_DIR", os.path.expanduser("~/.claude/projects"))
)
MEMORY_DIR = Path(
    os.environ.get("AGAM_MEMORY_DIR", os.path.expanduser("~/.claude/MEMORY"))
)
THISAI = AGAM_DIR / "THISAI.md"
AGAM_MD = AGAM_DIR / "AGAM.md"
FINDINGS_PATH = AGAM_DIR / ".lint-findings.md"


def get_db():
    if not DB_PATH.exists():
        return None
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


# -- Audit 1: Contradictions --

def lint_contradictions(db):
    """Find entity pairs with potentially conflicting descriptions."""
    print("1. CONTRADICTIONS")
    if not db:
        print("   [SKIP] No graph database\n")
        return []

    rows = db.execute("""
        SELECT e1.name as n1, e1.description as d1, e2.name as n2, e2.description as d2
        FROM entities e1
        JOIN entities e2 ON e1.type = e2.type AND e1.id < e2.id
        WHERE (e1.description LIKE '%replaced by%' OR e1.description LIKE '%deprecated%'
               OR e1.description LIKE '%no longer%' OR e1.description LIKE '%obsolete%'
               OR e1.description LIKE '%superseded%')
        AND e1.type NOT IN ('reasoning')
        LIMIT 20
    """).fetchall()

    findings = []
    if rows:
        for r in rows:
            findings.append(f"   {r['n1']}: \"{r['d1'][:80]}\" vs {r['n2']}: \"{r['d2'][:80]}\"")
        print(f"   {len(rows)} potential conflicts found")
        for f in findings[:5]:
            print(f)
    else:
        print("   None found")
    print()
    return findings


# -- Audit 2: Orphans --

def lint_orphans(db):
    """Find entities with 0 relationships."""
    print("2. ORPHANS")
    if not db:
        print("   [SKIP] No graph database\n")
        return []

    rows = db.execute("""
        SELECT name, type FROM entities
        WHERE id NOT IN (SELECT source_id FROM relationships UNION SELECT target_id FROM relationships)
        ORDER BY type, name
    """).fetchall()

    findings = []
    if rows:
        by_type = {}
        for r in rows:
            by_type.setdefault(r['type'], []).append(r['name'])
        for t, names in sorted(by_type.items()):
            sample = ", ".join(names[:5])
            extra = f" (+{len(names)-5} more)" if len(names) > 5 else ""
            findings.append(f"   [{t}] {sample}{extra}")
        print(f"   {len(rows)} orphan entities")
        for f in findings:
            print(f)
    else:
        print("   None found")
    print()
    return findings


# -- Audit 3: Stale entities --

def lint_stale(db):
    """Find entities with last-worked > 30 days ago + THISAI stall dates."""
    print("3. STALE ENTITIES")
    findings = []

    if db:
        # Suppress entities the user has already marked paused or archived --
        # the recommendation "if paused: status paused" is a no-op for them
        # and the noise drowns out genuinely active-but-stale entities.
        rows = db.execute("""
            SELECT e.name, e.type, p.value as last_worked
            FROM entities e
            JOIN properties p ON e.id = p.entity_id AND p.key = 'last-worked'
            WHERE julianday('now') - julianday(p.value) > 30
              AND NOT EXISTS (
                  SELECT 1 FROM properties s
                  WHERE s.entity_id = e.id
                    AND s.key = 'status'
                    AND s.value IN ('paused', 'archived')
              )
            ORDER BY p.value ASC
            LIMIT 15
        """).fetchall()

        if rows:
            print(f"   {len(rows)} entities not worked on in 30+ days:")
            for r in rows:
                findings.append(f"   {r['name']} ({r['type']}) -- last: {r['last_worked']}")
                print(findings[-1])
        else:
            print("   No stale graph entities")

    # Check THISAI for stall dates
    if THISAI.exists():
        text = THISAI.read_text()
        date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
        dates = date_pattern.findall(text)
        if dates:
            cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            stale_dates = [d for d in dates if d < cutoff]
            if stale_dates:
                oldest = min(stale_dates)
                findings.append(f"   THISAI.md has dates as old as {oldest} (14+ days stale)")
                print(findings[-1])

    if not findings:
        print("   None found")
    print()
    return findings


# -- Audit 4: Belief-evidence cross-check --

def lint_belief_evidence(db):
    """Cross-check AGAM.md beliefs against graph evidence."""
    print("4. BELIEF CROSS-CHECK")
    findings = []

    if not db or not AGAM_MD.exists():
        print("   [SKIP] Missing graph or AGAM.md\n")
        return []

    text = AGAM_MD.read_text()
    # Extract "What I Value" section
    idx = text.find("## What I Value")
    if idx < 0:
        print("   [SKIP] No 'What I Value' section in AGAM.md\n")
        return []

    end = text.find("\n## ", idx + 1)
    beliefs_text = text[idx:end if end > 0 else idx + 2000]

    # Extract individual belief lines (lines starting with - or *)
    belief_lines = [line.strip().lstrip("-* ") for line in beliefs_text.split("\n")
                    if line.strip().startswith(("-", "*")) and len(line.strip()) > 10]

    for belief in belief_lines[:8]:  # Cap at 8 beliefs
        # Extract keywords (words > 4 chars, not common)
        words = [w.lower() for w in re.findall(r'\b\w+\b', belief)
                 if len(w) > 4 and w.lower() not in {"about", "being", "their", "there", "these",
                                                       "those", "would", "should", "could", "which",
                                                       "where", "while", "first", "every", "before"}]
        if not words:
            continue

        # Search graph for each keyword
        total_hits = 0
        for word in words[:3]:
            count = db.execute("""
                SELECT COUNT(*) as c FROM entities_fts WHERE entities_fts MATCH ?
            """, (word,)).fetchone()
            if count:
                total_hits += count['c']

        if total_hits > 0:
            findings.append(f"   \"{belief[:60]}...\" -> {total_hits} graph entities relevant")

    if findings:
        print(f"   {len(findings)} beliefs with graph evidence:")
        for f in findings[:5]:
            print(f)
    else:
        print("   No belief-graph connections found")
    print()
    return findings


# -- Audit 5: Work-log patterns --

def lint_worklog_patterns():
    """Analyze last 30 work-log entries for patterns."""
    print("5. WORK-LOG PATTERNS")
    findings = []

    if not WORKLOG.exists():
        print("   [SKIP] No work-log found\n")
        return []

    text = WORKLOG.read_text()
    # Parse entries (## date headers or ### time headers)
    entries = re.split(r'\n(?=## \d{4}-\d{2}-\d{2})', text)
    entries = [e.strip() for e in entries if e.strip() and re.match(r'## \d{4}', e)]
    recent = entries[-30:] if len(entries) > 30 else entries

    if not recent:
        print("   No entries to analyze\n")
        return []

    # Count project mentions
    project_counts = Counter()
    for entry in recent:
        # Look for project names after | or in ### headers
        projects = re.findall(r'\|\s*([A-Za-z][\w-]+)', entry)
        projects += re.findall(r'###\s*\d+:\d+\s*\|\s*([A-Za-z][\w-]+)', entry)
        for p in projects:
            project_counts[p.lower()] += 1

    # Projects appearing in 5+ of last 10 entries
    last_10 = recent[-10:] if len(recent) > 10 else recent
    frequent = Counter()
    for entry in last_10:
        projects = set(re.findall(r'\|\s*([A-Za-z][\w-]+)', entry))
        for p in projects:
            frequent[p.lower()] += 1

    time_sinks = [(p, c) for p, c in frequent.most_common() if c >= 5]
    if time_sinks:
        for p, c in time_sinks:
            findings.append(f"   Time sink: {p} appeared in {c}/10 recent sessions")

    # Check THISAI goals against work-log
    if THISAI.exists():
        thisai_text = THISAI.read_text()
        goal_pattern = re.compile(r'###\s+(.+)')
        goals = [m.group(1).strip() for m in goal_pattern.finditer(thisai_text)]

        combined_recent = " ".join(recent[-15:]).lower()
        for goal in goals:
            goal_words = [w.lower() for w in goal.split() if len(w) > 3]
            if goal_words and not any(w in combined_recent for w in goal_words):
                findings.append(f"   Stalled goal? \"{goal}\" -- no work-log mention in 15 recent entries")

    # Recurring keywords (potential lesson candidates)
    all_text = " ".join(recent[-10:]).lower()
    problem_words = ["bug", "fix", "broke", "failed", "error", "wrong", "issue", "stuck"]
    problem_count = sum(all_text.count(w) for w in problem_words)
    if problem_count > 8:
        findings.append(f"   High problem density: {problem_count} problem-related words in last 10 entries")

    if findings:
        print(f"   {len(findings)} patterns detected:")
        for f in findings:
            print(f)
    else:
        print("   No notable patterns")
    print()
    return findings


# -- Audit 6: Graph health --

def lint_watchdog_health(_db=None):
    """Agam sync watchdog freshness + queue depth."""
    import json as _json
    import time as _time

    print("X. WATCHDOG HEALTH")
    findings = []

    log_path = AGAM_DIR / ".watchdog-log"
    queue_path = AGAM_DIR / ".pending-closes.jsonl"
    processed_path = AGAM_DIR / ".processed-sessions.jsonl"

    queue_depth = 0
    if queue_path.exists():
        try:
            queue_depth = sum(1 for line in queue_path.read_text().splitlines() if line.strip())
        except OSError:
            pass

    processed_depth = 0
    if processed_path.exists():
        try:
            processed_depth = sum(1 for line in processed_path.read_text().splitlines() if line.strip())
        except OSError:
            pass

    if not log_path.exists():
        print(f"   Log: (not present)")
        print(f"   Queue depth: {queue_depth}")
        if queue_depth > 0:
            findings.append(f"   Watchdog log missing but queue has {queue_depth} entries -- is launchd plist loaded?")
        print()
        return findings

    last_ts = 0.0
    skipped_7d = 0
    seven_days_ago = _time.time() - 7 * 86400
    try:
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            ts = float(row.get("ts", 0))
            last_ts = max(last_ts, ts)
            if ts >= seven_days_ago and row.get("event") in ("failed", "no-container", "daycap-hit"):
                skipped_7d += 1
    except OSError:
        print("   [SKIP] Could not read watchdog log\n")
        return []

    age_hours = (_time.time() - last_ts) / 3600 if last_ts > 0 else float("inf")
    print(f"   Last event: {age_hours:.1f}h ago" if last_ts else "   Last event: (log empty)")
    print(f"   Queue depth: {queue_depth}")
    print(f"   Processed total: {processed_depth}")
    print(f"   Skip/fail events (7d): {skipped_7d}")

    if age_hours > 24:
        findings.append(f"   Watchdog stale: last event {age_hours:.0f}h ago (queue depth {queue_depth})")
    if skipped_7d >= 10:
        findings.append(f"   Watchdog skip/fail rate high: {skipped_7d} non-success events in 7d")

    print()
    return findings


def lint_graph_health(db):
    """Overall graph stats."""
    print("6. GRAPH HEALTH")
    findings = []

    if not db:
        print("   [SKIP] No graph database\n")
        return []

    entities = db.execute("SELECT COUNT(*) as c FROM entities").fetchone()['c']
    rels = db.execute("SELECT COUNT(*) as c FROM relationships").fetchone()['c']
    types = db.execute("SELECT type, COUNT(*) as c FROM entities GROUP BY type ORDER BY c DESC").fetchall()
    rel_types = db.execute("SELECT relation, COUNT(*) as c FROM relationships GROUP BY relation ORDER BY c DESC LIMIT 5").fetchall()

    avg_rels = rels / entities if entities > 0 else 0
    print(f"   Entities: {entities}")
    print(f"   Relationships: {rels}")
    print(f"   Avg rels/entity: {avg_rels:.2f}")
    type_strs = [f"{r['type']}({r['c']})" for r in types[:8]]
    rel_strs = [f"{r['relation']}({r['c']})" for r in rel_types]
    print(f"   Types: {', '.join(type_strs)}")
    print(f"   Top relations: {', '.join(rel_strs)}")

    if avg_rels < 1.0:
        findings.append(f"   Low connectivity: {avg_rels:.2f} avg rels/entity (target: >1.5)")
    if entities > 500 and avg_rels < 1.2:
        findings.append(f"   Graph growing faster than connections -- consider enrichment pass")

    print()
    return findings


# -- Audit 7: Decisions without rationale --

def lint_missing_rationale(db):
    """Find decision entities without a rationale property."""
    print("7. DECISIONS WITHOUT RATIONALE")
    if not db:
        print("   [SKIP] No graph database\n")
        return []

    rows = db.execute("""
        SELECT e.name FROM entities e
        WHERE e.type = 'decision'
        AND e.id NOT IN (SELECT entity_id FROM properties WHERE key = 'rationale')
        ORDER BY e.name
    """).fetchall()

    findings = []
    if rows:
        names = [r['name'] for r in rows]
        print(f"   {len(names)} decisions missing rationale:")
        for n in names:
            findings.append(f"   {n}")
            print(f"   - {n}")
    else:
        print("   All decisions have rationale")
    print()
    return findings


# -- Audit 8: Confidence distribution --

def lint_confidence_distribution(db):
    """Report confidence distribution across relationships."""
    print("8. CONFIDENCE DISTRIBUTION")
    if not db:
        print("   [SKIP] No graph database\n")
        return []

    row = db.execute("""
        SELECT
            SUM(CASE WHEN weight = 1.0 THEN 1 ELSE 0 END) as manual,
            SUM(CASE WHEN weight > 0.3 AND weight < 1.0 THEN 1 ELSE 0 END) as inferred,
            SUM(CASE WHEN weight <= 0.3 THEN 1 ELSE 0 END) as speculative,
            COUNT(*) as total
        FROM relationships
    """).fetchone()

    findings = []
    print(f"   Verified (1.0):     {row['manual']}")
    print(f"   Inferred (0.4-0.9): {row['inferred']}")
    print(f"   Speculative (<=0.3):{row['speculative']}")
    print(f"   Total:              {row['total']}")
    if row['inferred'] and row['manual'] and row['inferred'] > row['manual']:
        findings.append(f"   More inferred ({row['inferred']}) than verified ({row['manual']}) relationships -- consider verification pass")
        print(findings[-1])
    print()
    return findings


# -- Audit 9: Memory anchors (dead path refs) --

_ANCHOR_RE = re.compile(r"`([^`\n]+)`")
_KNOWN_EXT = {
    ".py", ".md", ".json", ".yml", ".yaml", ".ts", ".tsx", ".js", ".jsx",
    ".rs", ".go", ".sh", ".sql", ".html", ".css", ".toml", ".pcf", ".gs",
}


def _looks_like_path(tok):
    tok = tok.strip()
    if not tok or " " in tok or tok.startswith("-") or tok.startswith("http"):
        return False
    if tok.endswith(")") or "(" in tok:
        return False
    # strip trailing :<digits>
    m = re.match(r"^(.*?)(:\d+)?$", tok)
    base = m.group(1) if m else tok
    if any(base.endswith(ext) for ext in _KNOWN_EXT):
        return True
    if "/" in base:
        last = base.rsplit("/", 1)[-1]
        if "." in last and not last.startswith("."):
            return True
    return False


def _resolve(tok, memory_path):
    # strip trailing :<line>
    m = re.match(r"^(.*?)(:\d+)?$", tok)
    path_str = m.group(1) if m else tok

    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p.exists()

    # try relative to memory file's project root
    # memory_path = <projects-dir>/-Users-km-coding-foo/memory/x.md
    # project root guess: walk up looking for a .git ancestor or a coding/ segment
    parts = memory_path.parts
    candidates = []
    if "projects" in parts:
        # translate slug back to absolute: -home-user-coding-foo -> /home/user/coding/foo
        try:
            idx = parts.index("projects")
            slug = parts[idx + 1]
            if slug.startswith("-"):
                root = Path("/" + slug.lstrip("-").replace("-", "/"))
                candidates.append(root / path_str)
                candidates.append(root / "src" / path_str)
        except (IndexError, ValueError):
            pass

    # plus CWD and ~
    candidates.append(Path.cwd() / path_str)
    candidates.append(Path.home() / path_str)

    return any(c.exists() for c in candidates)


def lint_memory_anchors(_db=None):
    """Scan memory files for dead backtick path references."""
    print("9. MEMORY ANCHORS (dead path refs)")

    roots = [
        SESSIONS_DIR,
        MEMORY_DIR,
        AGAM_DIR,
    ]

    total_refs = 0
    dead = []  # (memory_file, ref)
    for root in roots:
        if not root.exists():
            continue
        for mf in root.rglob("*.md"):
            if mf.name.startswith("."):
                continue
            try:
                text = mf.read_text(errors="ignore")
            except Exception:
                continue
            for match in _ANCHOR_RE.finditer(text):
                tok = match.group(1)
                if not _looks_like_path(tok):
                    continue
                total_refs += 1
                if not _resolve(tok, mf):
                    dead.append((mf, tok))

    findings = []
    if dead:
        pct = round(len(dead) / total_refs * 100, 1) if total_refs else 0
        print(f"   {len(dead)} dead refs / {total_refs} total ({pct}%):")
        # group by memory file, show worst offenders
        by_file = Counter(str(mf) for mf, _ in dead)
        for fpath, count in by_file.most_common(5):
            short = fpath.replace(str(Path.home()), "~")
            print(f"   - {short}  ({count} dead)")
        if len(by_file) > 5:
            print(f"   + {len(by_file) - 5} more files")
        findings.append(
            f"   {len(dead)} dead memory refs across {len(by_file)} files -- "
            f"top offender: {Path(by_file.most_common(1)[0][0]).name}"
        )
    else:
        print(f"   [OK] {total_refs} refs checked, all resolve")
    print()
    return findings


# -- Main --

def main():
    quick = "--quick" in sys.argv
    date = datetime.now().strftime("%Y-%m-%d")

    print(f"AGAM LINT -- {date}")
    print("=" * 40)
    if quick:
        print("(quick mode -- 4 audits)\n")
    else:
        print(f"(full mode -- 10 audits)\n")

    db = get_db()

    # Priority order: actionable findings first (stalls, patterns), then structural (contradictions, orphans)
    high_priority = []  # stale, worklog patterns
    med_priority = []   # contradictions, orphans
    low_priority = []   # health stats, beliefs

    # Always run
    med_priority.extend(lint_contradictions(db))
    high_priority.extend(lint_stale(db))
    low_priority.extend(lint_graph_health(db))
    high_priority.extend(lint_watchdog_health(db))

    if not quick:
        med_priority.extend(lint_orphans(db))
        low_priority.extend(lint_belief_evidence(db))
        high_priority.extend(lint_worklog_patterns())
        med_priority.extend(lint_missing_rationale(db))
        low_priority.extend(lint_confidence_distribution(db))
        med_priority.extend(lint_memory_anchors(db))

    if db:
        db.close()

    all_findings = high_priority + med_priority + low_priority

    # Write top findings -- prioritize actionable items
    top = (high_priority[:2] + med_priority[:1])[:3] if all_findings else ["No findings this run."]
    if not top:
        top = all_findings[:3] if all_findings else ["No findings this run."]
    findings_text = f"## Lint Findings ({date})\n\n"
    for i, f in enumerate(top, 1):
        findings_text += f"{i}. {f.strip()}\n"

    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS_PATH.write_text(findings_text)

    print(f"\nTOP FINDINGS (saved to {FINDINGS_PATH}):")
    for i, f in enumerate(top, 1):
        print(f"  {i}. {f.strip()}")

    print(f"\n[OK] Lint complete. {len(all_findings)} total findings.")


if __name__ == "__main__":
    main()

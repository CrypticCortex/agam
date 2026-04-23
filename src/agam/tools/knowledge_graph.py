#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["networkx"]
# ///
"""
Knowledge graph -- SQLite-backed entity-relationship graph with full-text search
and graph traversal. Persistent across sessions. Not flat files.

Usage:
    kg entity <name> <type> "<description>"      Add/update an entity
    kg relate <src> <relation> <target> [conf]     Create a relationship (conf 0-1, default 1.0)
    kg prop <entity> <key> "<value>"              Set a property on an entity
    kg query <entity>                             Show entity + all relationships
    kg uncertain [threshold]                       Low-confidence relationships (default <0.5)
    kg rationale <entity> "<why>"                  Set rationale on a decision entity
    kg decisions [--missing-rationale]             List decisions, flag missing rationale
    kg hubs [n]                                    Top N entities by degree (default 10)
    kg search <text>                              Full-text search across everything
    kg traverse <entity> [depth]                  Graph walk from entity (default depth 2)
    kg path <entity1> <entity2>                   Shortest path between entities
    kg neighbors <entity> [relation_type]         Direct neighbors, optionally filtered
    kg types                                      List entity types with counts
    kg relations                                  List relationship types with counts
    kg recent [n]                                 Recently modified entities (default 10)
    kg stats                                      Graph statistics
    kg dot [entity]                               Mermaid graph (full or ego-graph)
    kg lessons                                     List lessons with trigger status
    kg lessons set <name> <json>                  Set lesson triggers from JSON
    kg lessons check "<command>"                  Test which lessons fire for a command
    kg merge <source> <target>                    Merge two entities (source -> target)
    kg delete-entity <name>                       Remove entity and its relationships
    kg delete-relation <src> <relation> <target>  Remove a specific relationship
    kg export [file]                              Export graph as JSON
    kg import <file>                              Import graph from JSON
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(
    os.environ.get("AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db"))
)


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE,
            type TEXT NOT NULL,
            description TEXT DEFAULT '',
            created TEXT NOT NULL,
            updated TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation)
        );
        CREATE TABLE IF NOT EXISTS properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated TEXT NOT NULL,
            UNIQUE(entity_id, key)
        );
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
        CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_id);
        CREATE INDEX IF NOT EXISTS idx_rel_relation ON relationships(relation);
        CREATE INDEX IF NOT EXISTS idx_prop_entity ON properties(entity_id);
    """)
    # FTS5 for full-text search
    try:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                name, type, description, content=entities, content_rowid=id
            )
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 already exists or not available
    return db


def now():
    return datetime.now(timezone.utc).isoformat()


def sync_fts(db, entity_id):
    row = db.execute(
        "SELECT id, name, type, description FROM entities WHERE id = ?",
        (entity_id,)
    ).fetchone()
    if row:
        db.execute("INSERT OR REPLACE INTO entities_fts(rowid, name, type, description) VALUES (?, ?, ?, ?)", row)


def refresh_cache(db=None):
    """Regenerate entity names cache for graph-recall hook."""
    close = False
    if db is None:
        db = get_db()
        close = True
    cache_path = DB_PATH.parent / "entity-names.txt"
    names = db.execute("SELECT LOWER(name) FROM entities").fetchall()
    with open(cache_path, "w") as f:
        for (name,) in names:
            f.write(name + "\n")
    if close:
        db.close()


def normalize_name(name):
    """PascalCase/camelCase -> kebab-case. Idempotent on already-kebab names."""
    s = re.sub(r'([a-z])([A-Z])', r'\1-\2', name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', s)
    s = s.replace('_', '-').lower()
    s = re.sub(r'-+', '-', s).strip('-')
    return s


def entity_cmd(name, etype, description):
    db = get_db()
    ts = now()
    original = name
    name = normalize_name(name)
    if name != original:
        print(f"[NOTE] Normalized '{original}' -> '{name}'")
    existing = db.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    if existing:
        db.execute(
            "UPDATE entities SET type=?, description=?, updated=? WHERE id=?",
            (etype, description, ts, existing[0])
        )
        sync_fts(db, existing[0])
        db.commit()
        refresh_cache(db)
        print(f"[OK] Updated entity '{name}' (type={etype})")
    else:
        cur = db.execute(
            "INSERT INTO entities (name, type, description, created, updated) VALUES (?, ?, ?, ?, ?)",
            (name, etype, description, ts, ts)
        )
        sync_fts(db, cur.lastrowid)
        db.commit()
        refresh_cache(db)
        print(f"[OK] Created entity '{name}' (type={etype})")


def get_entity_id(db, name):
    row = db.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    if not row:
        print(f"[FAIL] Entity '{name}' not found")
        sys.exit(1)
    return row[0]


def relate_cmd(src, relation, target, confidence=1.0):
    db = get_db()
    src_id = get_entity_id(db, src)
    tgt_id = get_entity_id(db, target)
    ts = now()
    try:
        db.execute(
            "INSERT INTO relationships (source_id, target_id, relation, weight, created) VALUES (?, ?, ?, ?, ?)",
            (src_id, tgt_id, relation, confidence, ts)
        )
        db.commit()
        conf_str = f" [confidence={confidence}]" if confidence != 1.0 else ""
        print(f"[OK] {src} --[{relation}]--> {target}{conf_str}")
    except sqlite3.IntegrityError:
        print(f"[OK] Relationship already exists: {src} --[{relation}]--> {target}")


def prop_cmd(entity, key, value):
    db = get_db()
    eid = get_entity_id(db, entity)
    ts = now()
    db.execute(
        "INSERT INTO properties (entity_id, key, value, updated) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(entity_id, key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (eid, key, value, ts)
    )
    db.commit()
    print(f"[OK] {entity}.{key} = {value}")


def query_cmd(name):
    db = get_db()
    eid = get_entity_id(db, name)
    row = db.execute("SELECT name, type, description, created, updated FROM entities WHERE id=?", (eid,)).fetchone()
    print(f"\n  Entity: {row[0]}")
    print(f"  Type:   {row[1]}")
    print(f"  Desc:   {row[2]}")
    print(f"  Since:  {row[3][:10]}")

    props = db.execute("SELECT key, value FROM properties WHERE entity_id=?", (eid,)).fetchall()
    if props:
        print(f"\n  Properties:")
        for k, v in props:
            print(f"    {k}: {v}")

    outgoing = db.execute("""
        SELECT r.relation, e.name, e.type, r.weight FROM relationships r
        JOIN entities e ON r.target_id = e.id WHERE r.source_id = ?
    """, (eid,)).fetchall()
    incoming = db.execute("""
        SELECT r.relation, e.name, e.type, r.weight FROM relationships r
        JOIN entities e ON r.source_id = e.id WHERE r.target_id = ?
    """, (eid,)).fetchall()

    if outgoing:
        print(f"\n  Outgoing:")
        for rel, tname, ttype, w in outgoing:
            conf = f" [{w}]" if w != 1.0 else ""
            print(f"    --[{rel}]--> {tname} ({ttype}){conf}")
    if incoming:
        print(f"\n  Incoming:")
        for rel, sname, stype, w in incoming:
            conf = f" [{w}]" if w != 1.0 else ""
            print(f"    <--[{rel}]-- {sname} ({stype}){conf}")
    if not outgoing and not incoming:
        print(f"\n  (no relationships)")
    print()


def search_cmd(text):
    db = get_db()
    # try FTS5 first
    results = []
    try:
        rows = db.execute(
            "SELECT rowid, name, type, description FROM entities_fts WHERE entities_fts MATCH ?",
            (text,)
        ).fetchall()
        for _, name, etype, desc in rows:
            results.append((name, etype, desc))
    except sqlite3.OperationalError:
        pass

    # fallback: LIKE search
    if not results:
        pattern = f"%{text}%"
        rows = db.execute(
            "SELECT name, type, description FROM entities WHERE name LIKE ? OR description LIKE ?",
            (pattern, pattern)
        ).fetchall()
        results = list(rows)

    # also search properties
    prop_rows = db.execute("""
        SELECT e.name, p.key, p.value FROM properties p
        JOIN entities e ON p.entity_id = e.id
        WHERE p.value LIKE ?
    """, (f"%{text}%",)).fetchall()

    # also search relationships
    rel_rows = db.execute("""
        SELECT s.name, r.relation, t.name FROM relationships r
        JOIN entities s ON r.source_id = s.id
        JOIN entities t ON r.target_id = t.id
        WHERE r.relation LIKE ?
    """, (f"%{text}%",)).fetchall()

    if not results and not prop_rows and not rel_rows:
        print(f"No matches for '{text}'")
        return

    if results:
        print(f"\n  Entities ({len(results)}):")
        for name, etype, desc in results:
            preview = desc[:120].replace("\n", " ") if desc else "(no description)"
            print(f"    [{etype}] {name} -- {preview}")

    if prop_rows:
        print(f"\n  Properties ({len(prop_rows)}):")
        for ename, key, val in prop_rows:
            print(f"    {ename}.{key} = {val[:100]}")

    if rel_rows:
        print(f"\n  Relationships ({len(rel_rows)}):")
        for src, rel, tgt in rel_rows:
            print(f"    {src} --[{rel}]--> {tgt}")
    print()


def traverse_cmd(name, depth=2):
    db = get_db()
    eid = get_entity_id(db, name)

    visited = set()
    queue = [(eid, 0)]
    edges = []

    while queue:
        current, d = queue.pop(0)
        if current in visited or d > depth:
            continue
        visited.add(current)

        outgoing = db.execute("""
            SELECT r.target_id, r.relation, e.name, e.type, r.weight FROM relationships r
            JOIN entities e ON r.target_id = e.id WHERE r.source_id = ?
        """, (current,)).fetchall()
        incoming = db.execute("""
            SELECT r.source_id, r.relation, e.name, e.type, r.weight FROM relationships r
            JOIN entities e ON r.source_id = e.id WHERE r.target_id = ?
        """, (current,)).fetchall()

        cname = db.execute("SELECT name FROM entities WHERE id=?", (current,)).fetchone()[0]
        for tid, rel, tname, ttype, w in outgoing:
            edges.append((cname, rel, tname, "-->", w))
            if tid not in visited:
                queue.append((tid, d + 1))
        for sid, rel, sname, stype, w in incoming:
            edges.append((sname, rel, cname, "-->", w))
            if sid not in visited:
                queue.append((sid, d + 1))

    if not edges:
        print(f"  '{name}' has no connections within depth {depth}")
        return

    print(f"\n  Graph walk from '{name}' (depth={depth}):")
    seen_edges = set()
    for src, rel, tgt, arrow, w in edges:
        key = (src, rel, tgt)
        if key not in seen_edges:
            seen_edges.add(key)
            conf = f" [{w}]" if w != 1.0 else ""
            print(f"    {src} --[{rel}]--> {tgt}{conf}")
    print(f"\n  Nodes visited: {len(visited)}, Edges: {len(seen_edges)}")
    print()


def path_cmd(name1, name2):
    try:
        import networkx as nx
    except ImportError:
        print("[FAIL] networkx required for path finding")
        sys.exit(1)

    db = get_db()
    id1 = get_entity_id(db, name1)
    id2 = get_entity_id(db, name2)

    G = nx.DiGraph()
    rows = db.execute("""
        SELECT s.name, r.relation, t.name FROM relationships r
        JOIN entities s ON r.source_id = s.id
        JOIN entities t ON r.target_id = t.id
    """).fetchall()
    for src, rel, tgt in rows:
        G.add_edge(src, tgt, relation=rel)
        G.add_edge(tgt, src, relation=f"~{rel}")  # undirected traversal

    try:
        path = nx.shortest_path(G, name1, name2)
        print(f"\n  Shortest path ({len(path)-1} hops):")
        for i in range(len(path) - 1):
            edge_data = G.edges[path[i], path[i+1]]
            print(f"    {path[i]} --[{edge_data['relation']}]--> {path[i+1]}")
        print()
    except nx.NetworkXNoPath:
        print(f"  No path between '{name1}' and '{name2}'")
    except nx.NodeNotFound as e:
        print(f"  {e}")


def neighbors_cmd(name, relation_type=None):
    db = get_db()
    eid = get_entity_id(db, name)

    if relation_type:
        outgoing = db.execute("""
            SELECT r.relation, e.name, e.type FROM relationships r
            JOIN entities e ON r.target_id = e.id WHERE r.source_id = ? AND r.relation = ?
        """, (eid, relation_type)).fetchall()
        incoming = db.execute("""
            SELECT r.relation, e.name, e.type FROM relationships r
            JOIN entities e ON r.source_id = e.id WHERE r.target_id = ? AND r.relation = ?
        """, (eid, relation_type)).fetchall()
    else:
        outgoing = db.execute("""
            SELECT r.relation, e.name, e.type FROM relationships r
            JOIN entities e ON r.target_id = e.id WHERE r.source_id = ?
        """, (eid,)).fetchall()
        incoming = db.execute("""
            SELECT r.relation, e.name, e.type FROM relationships r
            JOIN entities e ON r.source_id = e.id WHERE r.target_id = ?
        """, (eid,)).fetchall()

    total = len(outgoing) + len(incoming)
    filter_msg = f" (filtered: {relation_type})" if relation_type else ""
    print(f"\n  Neighbors of '{name}'{filter_msg}: {total}")
    for rel, n, t in outgoing:
        print(f"    --> [{rel}] {n} ({t})")
    for rel, n, t in incoming:
        print(f"    <-- [{rel}] {n} ({t})")
    print()


def types_cmd():
    db = get_db()
    rows = db.execute("SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY COUNT(*) DESC").fetchall()
    if not rows:
        print("  Empty graph.")
        return
    print("\n  Entity types:")
    for t, c in rows:
        print(f"    {t}: {c}")
    print()


def relations_cmd():
    db = get_db()
    rows = db.execute("SELECT relation, COUNT(*) FROM relationships GROUP BY relation ORDER BY COUNT(*) DESC").fetchall()
    if not rows:
        print("  No relationships.")
        return
    print("\n  Relationship types:")
    for r, c in rows:
        print(f"    {r}: {c}")
    print()


def recent_cmd(n=10):
    db = get_db()
    rows = db.execute(
        "SELECT name, type, description, updated FROM entities ORDER BY updated DESC LIMIT ?", (n,)
    ).fetchall()
    if not rows:
        print("  Empty graph.")
        return
    print(f"\n  Last {n} modified:")
    for name, etype, desc, updated in rows:
        preview = desc[:80].replace("\n", " ") if desc else ""
        print(f"    [{updated[:10]}] [{etype}] {name} -- {preview}")
    print()


def stats_cmd():
    db = get_db()
    entities = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    rels = db.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    props = db.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    types = db.execute("SELECT COUNT(DISTINCT type) FROM entities").fetchone()[0]
    rel_types = db.execute("SELECT COUNT(DISTINCT relation) FROM relationships").fetchone()[0]
    size_kb = DB_PATH.stat().st_size / 1024 if DB_PATH.exists() else 0

    print(f"\n  Knowledge Graph Stats:")
    print(f"    Entities:      {entities}")
    print(f"    Relationships: {rels}")
    print(f"    Properties:    {props}")
    print(f"    Entity types:  {types}")
    print(f"    Relation types:{rel_types}")
    print(f"    DB size:       {size_kb:.1f} KB")
    print(f"    Path:          {DB_PATH}")
    print()


def dot_cmd(entity=None):
    db = get_db()
    print("```mermaid")
    print("graph LR")

    if entity:
        eid = get_entity_id(db, entity)
        # ego graph: direct connections
        rows = db.execute("""
            SELECT s.name, s.type, r.relation, t.name, t.type FROM relationships r
            JOIN entities s ON r.source_id = s.id
            JOIN entities t ON r.target_id = t.id
            WHERE r.source_id = ? OR r.target_id = ?
        """, (eid, eid)).fetchall()
    else:
        rows = db.execute("""
            SELECT s.name, s.type, r.relation, t.name, t.type FROM relationships r
            JOIN entities s ON r.source_id = s.id
            JOIN entities t ON r.target_id = t.id
        """).fetchall()

    seen_nodes = set()
    for src, stype, rel, tgt, ttype in rows:
        if src not in seen_nodes:
            seen_nodes.add(src)
            print(f"    {_mermaid_id(src)}[{src}]")
        if tgt not in seen_nodes:
            seen_nodes.add(tgt)
            print(f"    {_mermaid_id(tgt)}[{tgt}]")
        print(f"    {_mermaid_id(src)} -->|{rel}| {_mermaid_id(tgt)}")

    if not rows:
        print("    empty[No relationships yet]")
    print("```")


def _mermaid_id(name):
    return name.replace(" ", "_").replace("-", "_").replace(".", "_")


def merge_cmd(source, target):
    db = get_db()
    src_id = get_entity_id(db, source)
    tgt_id = get_entity_id(db, target)

    # move all relationships from source to target
    db.execute("UPDATE OR IGNORE relationships SET source_id=? WHERE source_id=?", (tgt_id, src_id))
    db.execute("UPDATE OR IGNORE relationships SET target_id=? WHERE target_id=?", (tgt_id, src_id))
    # move properties
    db.execute("UPDATE OR IGNORE properties SET entity_id=? WHERE entity_id=?", (tgt_id, src_id))
    # delete source
    db.execute("DELETE FROM entities WHERE id=?", (src_id,))
    # rebuild FTS index after structural change
    try:
        db.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
    except Exception:
        pass
    db.commit()
    refresh_cache(db)
    print(f"[OK] Merged '{source}' into '{target}'")


def delete_entity_cmd(name):
    db = get_db()
    eid = get_entity_id(db, name)
    db.execute("DELETE FROM entities WHERE id=?", (eid,))
    try:
        db.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
    except Exception:
        pass
    db.commit()
    refresh_cache(db)
    print(f"[OK] Deleted entity '{name}' and all its relationships")


def delete_relation_cmd(src, relation, target):
    db = get_db()
    src_id = get_entity_id(db, src)
    tgt_id = get_entity_id(db, target)
    cur = db.execute(
        "DELETE FROM relationships WHERE source_id=? AND target_id=? AND relation=?",
        (src_id, tgt_id, relation)
    )
    db.commit()
    if cur.rowcount:
        print(f"[OK] Removed {src} --[{relation}]--> {target}")
    else:
        print(f"[FAIL] Relationship not found")


def export_cmd(filepath=None):
    db = get_db()
    entities = db.execute("SELECT name, type, description, created, updated FROM entities").fetchall()
    rels = db.execute("""
        SELECT s.name, r.relation, t.name, r.weight, r.created FROM relationships r
        JOIN entities s ON r.source_id = s.id JOIN entities t ON r.target_id = t.id
    """).fetchall()
    props = db.execute("""
        SELECT e.name, p.key, p.value FROM properties p JOIN entities e ON p.entity_id = e.id
    """).fetchall()

    data = {
        "exported": now(),
        "entities": [{"name": n, "type": t, "description": d, "created": c, "updated": u} for n, t, d, c, u in entities],
        "relationships": [{"source": s, "relation": r, "target": t, "weight": w, "created": c} for s, r, t, w, c in rels],
        "properties": [{"entity": e, "key": k, "value": v} for e, k, v in props],
    }

    if filepath:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[OK] Exported to {filepath}")
    else:
        print(json.dumps(data, indent=2))


def import_cmd(filepath):
    with open(filepath) as f:
        data = json.load(f)

    db = get_db()
    for e in data.get("entities", []):
        entity_cmd(e["name"], e["type"], e.get("description", ""))

    for r in data.get("relationships", []):
        try:
            relate_cmd(r["source"], r["relation"], r["target"], r.get("weight", 1.0))
        except SystemExit:
            pass

    for p in data.get("properties", []):
        try:
            prop_cmd(p["entity"], p["key"], p["value"])
        except SystemExit:
            pass

    print(f"[OK] Imported from {filepath}")


def uncertain_cmd(threshold=0.5):
    db = get_db()
    rows = db.execute("""
        SELECT s.name, r.relation, t.name, r.weight FROM relationships r
        JOIN entities s ON r.source_id = s.id
        JOIN entities t ON r.target_id = t.id
        WHERE r.weight < ?
        ORDER BY r.weight ASC
    """, (threshold,)).fetchall()
    if not rows:
        print(f"  No relationships with confidence < {threshold}")
        return
    print(f"\n  Uncertain relationships (weight < {threshold}):")
    for src, rel, tgt, w in rows:
        print(f"    {src} --[{rel}]--> {tgt}  [{w}]")
    print(f"\n  Total: {len(rows)}")
    print()


def rationale_cmd(entity, rationale):
    db = get_db()
    eid = get_entity_id(db, entity)
    etype = db.execute("SELECT type FROM entities WHERE id=?", (eid,)).fetchone()[0]
    if etype != "decision":
        print(f"[NOTE] '{entity}' is type '{etype}', not 'decision' -- setting rationale anyway")
    prop_cmd(entity, "rationale", rationale)


def decisions_cmd(missing_only=False):
    db = get_db()
    rows = db.execute(
        "SELECT id, name, description FROM entities WHERE type = 'decision' ORDER BY name"
    ).fetchall()
    if not rows:
        print("  No decision entities.")
        return
    missing = 0
    for eid, name, desc in rows:
        has_rationale = db.execute(
            "SELECT 1 FROM properties WHERE entity_id = ? AND key = 'rationale'", (eid,)
        ).fetchone()
        if missing_only and has_rationale:
            continue
        marker = "" if has_rationale else " [NO RATIONALE]"
        preview = desc[:80].replace("\n", " ") if desc else ""
        print(f"  {name}{marker} -- {preview}")
        if not has_rationale:
            missing += 1
    print(f"\n  Total: {len(rows)} decisions, {missing} missing rationale")
    print()


def hubs_cmd(n=10):
    db = get_db()
    rows = db.execute("""
        SELECT e.name, e.type, e.description,
               (SELECT COUNT(*) FROM relationships WHERE source_id = e.id) as out_deg,
               (SELECT COUNT(*) FROM relationships WHERE target_id = e.id) as in_deg
        FROM entities e
        ORDER BY (out_deg + in_deg) DESC
        LIMIT ?
    """, (n,)).fetchall()
    if not rows:
        print("  Empty graph.")
        return
    print(f"\n  Top {n} hub entities:")
    for name, etype, desc, out_d, in_d in rows:
        total = out_d + in_d
        preview = desc[:60].replace("\n", " ") if desc else ""
        print(f"    {name} ({etype}) -- degree={total} (out={out_d}, in={in_d}) -- {preview}")
    print()


def lessons_cmd(args):
    """Manage lesson triggers for situation-indexed activation."""
    if not args or args[0] in ("list", "ls"):
        lessons_list()
    elif args[0] == "set" and len(args) >= 3:
        lessons_set(args[1], " ".join(args[2:]))
    elif args[0] == "check" and len(args) >= 2:
        lessons_check(" ".join(args[1:]))
    else:
        print("Usage:")
        print("  kg lessons                     List all lessons with trigger status")
        print('  kg lessons set <name> <json>   Set triggers: {"tool":[], "error":[], "context":[], "severity":"high"}')
        print('  kg lessons check "<command>"   Test which lessons would fire for a command')


def lessons_list():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, description FROM entities WHERE type = 'lesson' ORDER BY name"
    ).fetchall()
    if not rows:
        print("  No lesson entities in graph.")
        return

    armed = 0
    passive = 0
    for eid, name, desc in rows:
        props = db.execute(
            "SELECT key, value FROM properties WHERE entity_id = ?", (eid,)
        ).fetchall()
        prop_dict = {k: v for k, v in props}
        has_triggers = any(k.startswith("trigger-") for k in prop_dict)
        severity = prop_dict.get("severity", "unset")

        if has_triggers:
            armed += 1
            print(f"  [ARMED]   {name} (severity={severity})")
        else:
            passive += 1
            print(f"  [PASSIVE] {name}")

        preview = (desc[:100].replace("\n", " ")) if desc else "(no description)"
        print(f"            {preview}")
        for k in ["trigger-tool", "trigger-file", "trigger-error", "trigger-context"]:
            if k in prop_dict:
                print(f"            {k}: {prop_dict[k]}")

    print(f"\n  Total: {armed} armed, {passive} passive, {armed + passive} lessons")


def lessons_set(name, json_str):
    db = get_db()
    eid = get_entity_id(db, name)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"[FAIL] Invalid JSON: {e}")
        sys.exit(1)

    ts = now()
    count = 0
    for key in ["tool", "file", "error", "context"]:
        if key in data:
            prop_key = f"trigger-{key}"
            value = json.dumps(data[key])
            db.execute(
                "INSERT INTO properties (entity_id, key, value, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(entity_id, key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (eid, prop_key, value, ts)
            )
            count += 1

    if "severity" in data:
        db.execute(
            "INSERT INTO properties (entity_id, key, value, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(entity_id, key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (eid, "severity", data["severity"], ts)
        )
        count += 1

    db.commit()
    print(f"[OK] Set {count} trigger properties on {name}")


def lessons_check(command):
    db = get_db()
    command_lower = command.lower()

    rows = db.execute("""
        SELECT e.name, e.description, p.key, p.value
        FROM entities e
        JOIN properties p ON e.id = p.entity_id
        WHERE e.type = 'lesson' AND p.key = 'trigger-tool'
    """).fetchall()

    matched = []
    for name, desc, _key, value in rows:
        try:
            patterns = json.loads(value)
        except json.JSONDecodeError:
            continue
        for pattern in patterns:
            if pattern.lower() in command_lower:
                sev_row = db.execute("""
                    SELECT p.value FROM properties p
                    JOIN entities e ON p.entity_id = e.id
                    WHERE e.name = ? AND p.key = 'severity'
                """, (name,)).fetchone()
                severity = sev_row[0] if sev_row else "unset"
                matched.append((name, desc, pattern, severity))
                break

    if matched:
        matched.sort(key=lambda m: {"high": 0, "medium": 1, "low": 2, "unset": 3}[m[3]])
        print(f"  Lessons that would fire for: {command}")
        for name, desc, pattern, severity in matched:
            print(f"    * {name} [{severity.upper()}]: {desc[:100]}")
            print(f"      Triggered by: '{pattern}' matched in command")
    else:
        print(f"  No lessons match: {command}")


def build_concept_index():
    """Build concept-index.json from graph for O(1) entity lookup by concept terms."""
    from collections import defaultdict

    CONCEPT_INDEX_PATH = os.path.join(os.path.dirname(DB_PATH), "concept-index.json")

    STOP_CONCEPTS = {
        "the", "and", "for", "are", "but", "not", "you", "all", "any",
        "can", "her", "was", "one", "our", "out", "has", "have", "had",
        "been", "from", "this", "that", "they", "them", "then", "than",
        "what", "when", "where", "which", "who", "how", "why",
        "will", "with", "would", "could", "should", "does", "done",
        "make", "made", "just", "also", "into", "over", "such", "take",
        "only", "come", "some", "very", "work", "each", "like", "more",
        "about", "after", "being", "before", "between", "both", "here",
        "there", "these", "those", "under", "first", "still", "every",
        "need", "want", "tell", "show", "give", "keep", "lets", "look",
        "using", "used", "uses", "based", "added", "via", "etc", "new",
        "name", "type", "path", "file", "tool", "data", "code", "test",
        "runs", "main", "true", "false", "none", "null", "default",
    }

    def extract_words(text):
        if not text:
            return set()
        words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", text.lower()))
        return words - STOP_CONCEPTS

    conn = sqlite3.connect(DB_PATH, timeout=2)
    index = defaultdict(set)

    # 1. Entity names and descriptions
    for name, etype, desc in conn.execute("SELECT name, type, description FROM entities"):
        entity_name = name.lower()
        for part in extract_words(name.replace("-", " ").replace("_", " ")):
            if len(part) >= 3:
                index[part].add(entity_name)
        if etype:
            index[etype.lower()].add(entity_name)
        for word in extract_words(desc or ""):
            if len(word) >= 4:
                index[word].add(entity_name)

    # 2. Relationship labels
    for src, rel, tgt in conn.execute("""
        SELECT src.name, r.relation, tgt.name FROM relationships r
        JOIN entities src ON r.source_id = src.id
        JOIN entities tgt ON r.target_id = tgt.id
    """):
        for word in extract_words(rel.replace("-", " ")):
            if len(word) >= 4:
                index[word].add(src.lower())
                index[word].add(tgt.lower())

    # 3. Property values
    for ename, key, value in conn.execute("""
        SELECT e.name, p.key, p.value FROM properties p
        JOIN entities e ON p.entity_id = e.id
    """):
        for word in extract_words(key.replace("-", " ")):
            if len(word) >= 4:
                index[word].add(ename.lower())
        if value and len(value) < 200:
            for word in extract_words(value):
                if len(word) >= 5:
                    index[word].add(ename.lower())

    conn.close()

    # 4. Prune overly broad concepts (>15 entities)
    pruned = {c: sorted(ents) for c, ents in sorted(index.items()) if len(ents) <= 15}
    total_mappings = sum(len(v) for v in pruned.values())

    os.makedirs(os.path.dirname(CONCEPT_INDEX_PATH), exist_ok=True)
    with open(CONCEPT_INDEX_PATH, "w") as f:
        json.dump(pruned, f, indent=2, sort_keys=True)

    print(f"[OK] {len(pruned)} concepts, {total_mappings} mappings -> {CONCEPT_INDEX_PATH}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "entity" and len(sys.argv) >= 5:
        entity_cmd(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
    elif cmd == "relate" and len(sys.argv) >= 5:
        confidence = float(sys.argv[5]) if len(sys.argv) >= 6 else 1.0
        relate_cmd(sys.argv[2], sys.argv[3], sys.argv[4], confidence)
    elif cmd == "prop" and len(sys.argv) >= 5:
        prop_cmd(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
    elif cmd == "query" and len(sys.argv) >= 3:
        query_cmd(sys.argv[2])
    elif cmd == "search" and len(sys.argv) >= 3:
        search_cmd(" ".join(sys.argv[2:]))
    elif cmd == "traverse" and len(sys.argv) >= 3:
        depth = int(sys.argv[3]) if len(sys.argv) >= 4 else 2
        traverse_cmd(sys.argv[2], depth)
    elif cmd == "path" and len(sys.argv) >= 4:
        path_cmd(sys.argv[2], sys.argv[3])
    elif cmd == "neighbors" and len(sys.argv) >= 3:
        rel_type = sys.argv[3] if len(sys.argv) >= 4 else None
        neighbors_cmd(sys.argv[2], rel_type)
    elif cmd == "types":
        types_cmd()
    elif cmd == "relations":
        relations_cmd()
    elif cmd == "recent":
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        recent_cmd(n)
    elif cmd == "stats":
        stats_cmd()
    elif cmd == "dot":
        entity = sys.argv[2] if len(sys.argv) >= 3 else None
        dot_cmd(entity)
    elif cmd == "merge" and len(sys.argv) >= 4:
        merge_cmd(sys.argv[2], sys.argv[3])
    elif cmd == "delete-entity" and len(sys.argv) >= 3:
        delete_entity_cmd(sys.argv[2])
    elif cmd == "delete-relation" and len(sys.argv) >= 5:
        delete_relation_cmd(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "uncertain":
        threshold = float(sys.argv[2]) if len(sys.argv) >= 3 else 0.5
        uncertain_cmd(threshold)
    elif cmd == "rationale" and len(sys.argv) >= 4:
        rationale_cmd(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "decisions":
        missing_only = "--missing-rationale" in sys.argv
        decisions_cmd(missing_only)
    elif cmd == "hubs":
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        hubs_cmd(n)
    elif cmd == "lessons":
        lessons_cmd(sys.argv[2:])
    elif cmd == "refresh-cache":
        refresh_cache()
        print("[OK] Entity names cache refreshed")
    elif cmd == "export":
        filepath = sys.argv[2] if len(sys.argv) >= 3 else None
        export_cmd(filepath)
    elif cmd == "import" and len(sys.argv) >= 3:
        import_cmd(sys.argv[2])
    elif cmd == "build-index":
        build_concept_index()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

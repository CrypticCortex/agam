#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
PreToolUse hook: situation-indexed lesson activation.

Fires on Bash tool calls. Matches command against lesson trigger patterns
stored in the knowledge graph. Injects relevant lessons as additionalContext
so the agent can learn from past mistakes before repeating them.

Session dedup: each lesson fires at most once per session.
Performance: <5ms typical (cached trigger index, substring matching).

Environment variables:
    AGAM_KG_PATH         Path to knowledge graph SQLite DB
                         (default: ~/.claude/knowledge/graph.db)
"""

import json
import os
import sqlite3
import sys
import tempfile

DB_PATH = os.environ.get(
    "AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db")
)
CACHE_MAX_AGE = 3600  # 1 hour

# File paths set in main() after parsing session_id from stdin
CACHE_FILE = ""
SEEN_FILE = ""


def _init_paths(session_id):
    """Set session-stable file paths using session_id instead of PID."""
    global CACHE_FILE, SEEN_FILE
    # Sanitize session_id for use in filenames
    safe_id = session_id.replace("/", "_").replace("\\", "_")[:64]
    CACHE_FILE = os.path.join(tempfile.gettempdir(), f"lesson-triggers-{safe_id}.json")
    SEEN_FILE = os.path.join(tempfile.gettempdir(), f"lesson-seen-{safe_id}.txt")


def load_trigger_index():
    """Load trigger index from cache or build from SQLite."""
    import time

    if CACHE_FILE and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_MAX_AGE:
            try:
                with open(CACHE_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    # Build from SQLite
    index = {"tool": [], "error": []}

    if not os.path.exists(DB_PATH):
        return index

    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")

        rows = conn.execute("""
            SELECT e.name, e.description, p.key, p.value
            FROM entities e
            JOIN properties p ON e.id = p.entity_id
            WHERE e.type = 'lesson' AND p.key IN ('trigger-tool', 'trigger-error', 'severity')
        """).fetchall()

        # Group by entity
        lessons = {}
        for name, desc, key, value in rows:
            if name not in lessons:
                lessons[name] = {"name": name, "desc": desc, "severity": "medium"}
            if key == "severity":
                lessons[name]["severity"] = value
            elif key == "trigger-tool":
                try:
                    patterns = json.loads(value)
                    for p in patterns:
                        index["tool"].append({
                            "pattern": p.lower(),
                            "lesson": name,
                            "severity": None,  # filled below
                        })
                except json.JSONDecodeError:
                    pass
            elif key == "trigger-error":
                try:
                    patterns = json.loads(value)
                    for p in patterns:
                        index["error"].append({
                            "pattern": p.lower(),
                            "lesson": name,
                            "severity": None,
                        })
                except json.JSONDecodeError:
                    pass

        conn.close()

        # Fill severity
        for entry in index["tool"] + index["error"]:
            lesson_data = lessons.get(entry["lesson"], {})
            entry["severity"] = lesson_data.get("severity", "medium")
            entry["desc"] = lesson_data.get("desc", "")

        # Cache
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(index, f)
        except OSError:
            pass

    except Exception:
        pass

    return index


def get_session_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def mark_session_seen(names):
    try:
        with open(SEEN_FILE, "a") as f:
            for name in names:
                f.write(name + "\n")
    except OSError:
        pass


def match_tool_triggers(command, triggers):
    """Match command against tool trigger patterns. Returns list of matches."""
    command_lower = command.lower()
    matched = {}
    for entry in triggers:
        if entry["lesson"] in matched:
            continue
        if entry["pattern"] in command_lower:
            matched[entry["lesson"]] = entry

    return list(matched.values())


def build_context(matches):
    """Build additionalContext string from matched lessons."""
    lines = ["LESSON ACTIVATION (from Agam knowledge graph -- learn from past experience):"]
    for m in matches:
        sev = m["severity"].upper()
        desc = m.get("desc", "")[:120]
        lines.append(f"* {m['lesson']} [{sev}]: {desc}")
        lines.append(f"  Triggered by: '{m['pattern']}' matched in command.")
    lines.append("Apply these lessons to your current action. Do not repeat past mistakes.")
    return "\n".join(lines)


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id", f"fallback-{os.getppid()}")
    _init_paths(session_id)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Only activate on Bash commands
    if tool_name != "Bash":
        sys.exit(0)

    command = tool_input.get("command", "")
    if not command or len(command) < 3:
        sys.exit(0)

    # Load trigger index
    index = load_trigger_index()
    if not index.get("tool"):
        sys.exit(0)

    # Match
    matched = match_tool_triggers(command, index["tool"])
    if not matched:
        sys.exit(0)

    # Session dedup
    seen = get_session_seen()
    new_matches = [m for m in matched if m["lesson"] not in seen]
    if not new_matches:
        sys.exit(0)

    # Sort by severity (high first), cap at 2
    severity_order = {"high": 0, "medium": 1, "low": 2}
    new_matches.sort(key=lambda m: severity_order.get(m["severity"], 3))
    new_matches = new_matches[:2]

    # Build and output
    context = build_context(new_matches)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context
        }
    }
    print(json.dumps(output))

    # Mark seen
    mark_session_seen([m["lesson"] for m in new_matches])


if __name__ == "__main__":
    main()

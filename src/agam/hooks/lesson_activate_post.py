#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
PostToolUse hook: error-reactive lesson activation.

Fires after Bash commands that failed. Matches error output against lesson
trigger-error patterns. Helps the agent self-correct by surfacing relevant
lessons from past experience.

Shares the same trigger cache and session dedup as lesson_activate.py.

Environment variables:
    AGAM_KG_PATH         Path to knowledge graph SQLite DB
                         (default: ~/.claude/knowledge/graph.db)
"""

import json
import os
import re
import sqlite3
import sys
import tempfile

DB_PATH = os.environ.get(
    "AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db")
)
CACHE_MAX_AGE = 3600

# File paths set in main() after parsing session_id from stdin
CACHE_FILE = ""
SEEN_FILE = ""


def _init_paths(session_id):
    """Set session-stable file paths using session_id instead of PID."""
    global CACHE_FILE, SEEN_FILE
    safe_id = session_id.replace("/", "_").replace("\\", "_")[:64]
    CACHE_FILE = os.path.join(tempfile.gettempdir(), f"lesson-triggers-{safe_id}.json")
    SEEN_FILE = os.path.join(tempfile.gettempdir(), f"lesson-seen-{safe_id}.txt")


def load_trigger_index():
    """Load from cache built by lesson_activate.py, or build fresh."""
    import time

    if CACHE_FILE and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_MAX_AGE:
            try:
                with open(CACHE_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    # Build from SQLite (same as lesson_activate.py)
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

        lessons = {}
        for name, desc, key, value in rows:
            if name not in lessons:
                lessons[name] = {"name": name, "desc": desc, "severity": "medium"}
            if key == "severity":
                lessons[name]["severity"] = value
            elif key == "trigger-tool":
                try:
                    for p in json.loads(value):
                        index["tool"].append({"pattern": p.lower(), "lesson": name, "severity": None})
                except json.JSONDecodeError:
                    pass
            elif key == "trigger-error":
                try:
                    for p in json.loads(value):
                        index["error"].append({"pattern": p.lower(), "lesson": name, "severity": None})
                except json.JSONDecodeError:
                    pass

        conn.close()

        for entry in index["tool"] + index["error"]:
            lesson_data = lessons.get(entry["lesson"], {})
            entry["severity"] = lesson_data.get("severity", "medium")
            entry["desc"] = lesson_data.get("desc", "")

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


def match_error_triggers(error_text, triggers):
    """Match error output against error trigger patterns."""
    error_lower = error_text.lower()
    matched = {}
    for entry in triggers:
        if entry["lesson"] in matched:
            continue
        pattern = entry["pattern"]
        # Support simple regex patterns (e.g., "404.*pypi")
        try:
            if re.search(pattern, error_lower):
                matched[entry["lesson"]] = entry
        except re.error:
            # Fall back to substring match
            if pattern in error_lower:
                matched[entry["lesson"]] = entry

    return list(matched.values())


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id", f"fallback-{os.getppid()}")
    _init_paths(session_id)

    tool_name = data.get("tool_name", "")
    tool_output = data.get("tool_output", {})

    # Only activate on failed Bash commands
    if tool_name != "Bash":
        sys.exit(0)

    # Check for failure -- look at stdout/stderr for error indicators
    stdout = tool_output.get("stdout", "") if isinstance(tool_output, dict) else str(tool_output)
    stderr = tool_output.get("stderr", "") if isinstance(tool_output, dict) else ""
    error_text = stdout + "\n" + stderr

    if not error_text or len(error_text.strip()) < 5:
        sys.exit(0)

    # Load trigger index
    index = load_trigger_index()
    if not index.get("error"):
        sys.exit(0)

    # Match
    matched = match_error_triggers(error_text, index["error"])
    if not matched:
        sys.exit(0)

    # Session dedup
    seen = get_session_seen()
    new_matches = [m for m in matched if m["lesson"] not in seen]
    if not new_matches:
        sys.exit(0)

    # Sort by severity, cap at 2
    severity_order = {"high": 0, "medium": 1, "low": 2}
    new_matches.sort(key=lambda m: severity_order.get(m["severity"], 3))
    new_matches = new_matches[:2]

    # Build context
    lines = ["LESSON ACTIVATION (error matched -- from Agam knowledge graph):"]
    for m in new_matches:
        sev = m["severity"].upper()
        desc = m.get("desc", "")[:120]
        lines.append(f"* {m['lesson']} [{sev}]: {desc}")
        lines.append(f"  Error pattern matched: '{m['pattern']}'")
    lines.append("This error has been seen before. Apply the lesson above to fix it.")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(lines)
        }
    }
    print(json.dumps(output))

    mark_session_seen([m["lesson"] for m in new_matches])


if __name__ == "__main__":
    main()

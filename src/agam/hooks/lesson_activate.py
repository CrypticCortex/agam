#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
PreToolUse hook: situation-indexed lesson activation.

Fires on:
* Bash tool calls -- matches the command string against ``trigger-tool``
  patterns stored on lesson entities in the knowledge graph.
* Edit / Write / MultiEdit tool calls -- matches the ``file_path`` against
  ``trigger-file`` patterns. Useful for "you edited X, remember to also
  update Y" style reminders.

Injects matched lessons as additionalContext so the agent can learn from
past experience before repeating mistakes (or, in the file-path case,
remember about co-located files that need to stay in sync).

Lesson entities can carry a ``reminder`` property that surfaces as an
explicit ACTION: line in the injection -- use this for prescriptive
follow-ups ("mirror this change to <other repo>"), not for description.

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
                    cached = json.load(f)
                # Validate schema: must carry all current trigger kinds. Older
                # caches without 'file' force a rebuild so a hook upgrade is
                # picked up without manually invalidating /tmp.
                if all(k in cached for k in ("tool", "error", "file")):
                    return cached
            except (json.JSONDecodeError, OSError):
                pass

    # Build from SQLite
    index = {"tool": [], "error": [], "file": []}

    if not os.path.exists(DB_PATH):
        return index

    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")

        rows = conn.execute("""
            SELECT e.name, e.description, p.key, p.value
            FROM entities e
            JOIN properties p ON e.id = p.entity_id
            WHERE e.type = 'lesson' AND p.key IN (
                'trigger-tool', 'trigger-error', 'trigger-file', 'severity', 'reminder'
            )
        """).fetchall()

        # Group by entity
        lessons = {}
        for name, desc, key, value in rows:
            if name not in lessons:
                lessons[name] = {
                    "name": name,
                    "desc": desc,
                    "severity": "medium",
                    "reminder": "",
                }
            if key == "severity":
                lessons[name]["severity"] = value
            elif key == "reminder":
                lessons[name]["reminder"] = value
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
            elif key == "trigger-file":
                try:
                    patterns = json.loads(value)
                    for p in patterns:
                        index["file"].append({
                            "pattern": p.lower(),
                            "lesson": name,
                            "severity": None,
                        })
                except json.JSONDecodeError:
                    pass

        conn.close()

        # Fill severity + reminder onto every trigger entry so match handlers
        # have all the data they need without a second DB round-trip.
        for entry in index["tool"] + index["error"] + index["file"]:
            lesson_data = lessons.get(entry["lesson"], {})
            entry["severity"] = lesson_data.get("severity", "medium")
            entry["desc"] = lesson_data.get("desc", "")
            entry["reminder"] = lesson_data.get("reminder", "")

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


def match_file_triggers(file_path, triggers):
    """Match a file path against file-path trigger patterns.

    Substring match (lowercased on both sides) -- same convention as
    Bash command matching. Trigger patterns are expected to be unique
    enough that false positives are rare; if needed, lessons can use
    longer suffixes (e.g. ``/hooks/agam-watchdog.sh`` instead of just
    ``agam-watchdog``).
    """
    if not file_path:
        return []
    path_lower = file_path.lower()
    matched = {}
    for entry in triggers:
        if entry["lesson"] in matched:
            continue
        if entry["pattern"] in path_lower:
            matched[entry["lesson"]] = entry
    return list(matched.values())


def build_context(matches, trigger_kind="command"):
    """Build additionalContext string from matched lessons.

    ``trigger_kind`` is the human-readable label for how the match was
    triggered (``"command"`` for Bash, ``"file path"`` for Edit/Write).
    """
    lines = ["LESSON ACTIVATION (from Agam knowledge graph -- learn from past experience):"]
    for m in matches:
        sev = m["severity"].upper()
        desc = m.get("desc", "")[:200]
        reminder = m.get("reminder", "")
        lines.append(f"* {m['lesson']} [{sev}]: {desc}")
        if reminder:
            lines.append(f"  ACTION: {reminder}")
        lines.append(f"  Triggered by: '{m['pattern']}' matched in {trigger_kind}.")
    lines.append("Apply these lessons to your current action. Do not repeat past mistakes.")
    return "\n".join(lines)


def _extract_file_paths(tool_input):
    """Pull ``file_path`` from Edit/Write/MultiEdit tool_input.

    The top-level ``file_path`` is the canonical handle for the file
    being touched (Edit/Write/MultiEdit all share this shape). Keep this
    helper narrow on purpose -- if Claude Code grows new file-touching
    tools later, add them here.
    """
    paths = []
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp:
        paths.append(fp)
    return paths


def _emit(matches, kind):
    """Emit the hookSpecificOutput JSON envelope + mark lessons seen.

    Shared between the Bash and Edit/Write code paths so the output
    format is structurally identical regardless of which trigger fired.
    """
    severity_order = {"high": 0, "medium": 1, "low": 2}
    matches.sort(key=lambda m: severity_order.get(m["severity"], 3))
    matches = matches[:2]
    context = build_context(matches, trigger_kind=kind)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
    mark_session_seen([m["lesson"] for m in matches])


def main():
    data = json.load(sys.stdin)
    session_id = data.get("session_id", f"fallback-{os.getppid()}")
    _init_paths(session_id)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Trigger index is shared across both Bash and Edit/Write code paths.
    # Loading it once keeps the hook fast even when both matchers fire on
    # the same tool sequence (different events, same process invocation
    # cost amortizes via the cache file).
    index = load_trigger_index()

    # Edit / Write / MultiEdit -> file-path triggers.
    if tool_name in ("Edit", "Write", "MultiEdit"):
        if not index.get("file"):
            sys.exit(0)
        file_paths = _extract_file_paths(tool_input)
        if not file_paths:
            sys.exit(0)
        all_matched = []
        for fp in file_paths:
            all_matched.extend(match_file_triggers(fp, index["file"]))
        # Dedup by lesson name (multiple file paths in one tool call may
        # match the same lesson; only fire it once).
        seen_lessons = set()
        matched = []
        for m in all_matched:
            if m["lesson"] in seen_lessons:
                continue
            seen_lessons.add(m["lesson"])
            matched.append(m)
        if not matched:
            sys.exit(0)
        seen = get_session_seen()
        new_matches = [m for m in matched if m["lesson"] not in seen]
        if not new_matches:
            sys.exit(0)
        _emit(new_matches, kind="file path")
        sys.exit(0)

    # Bash -> command triggers (the original behavior).
    if tool_name != "Bash":
        sys.exit(0)

    command = tool_input.get("command", "")
    if not command or len(command) < 3:
        sys.exit(0)

    if not index.get("tool"):
        sys.exit(0)

    matched = match_tool_triggers(command, index["tool"])
    if not matched:
        sys.exit(0)

    seen = get_session_seen()
    new_matches = [m for m in matched if m["lesson"] not in seen]
    if not new_matches:
        sys.exit(0)

    _emit(new_matches, kind="command")


if __name__ == "__main__":
    main()

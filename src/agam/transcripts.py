"""Transcript format adapters.

Claude Code and Cursor both store session transcripts as JSONL, but with
different shapes:

- Claude:  ``{"type":"user", "message":{...}, ...}`` per event.
- Cursor:  ``{"role":"user", "message":{"content":[...]}}`` for turns, plus bare
           ``{"type":"turn_ended", "status":...}`` event markers.

Cursor also flushes its transcript lazily (at session end, not per turn), so the
heuristics here lean on whole-file raw-text scanning rather than precise event
walking -- robust to partial writes and to the exact tool-call JSON shape, which
is not yet pinned down.

The graph_update extraction (project paths, git branches, npm packages) already
scans raw transcript text with regexes, so it is format-agnostic; only the
user-turn counting and the "did real work happen" gate differ per agent. Those
live here.
"""

from __future__ import annotations

import json
import re

# Shared signal vocabulary: a session tail mentioning one of these reads as
# "real work happened" rather than a throwaway Q&A.
SIGNAL_RE = re.compile(
    r"\b(shipped|deployed|committed|decided|fixed|broke|learned|built|"
    r"resolved|debugged|implemented|merged|released)\b",
    re.IGNORECASE,
)

# Evidence that the agent edited code: a Cursor write/edit tool name, or a
# file_path key, anywhere in the raw transcript. Kept broad on purpose because
# the exact tool-call JSON shape is unconfirmed.
_EDIT_EVIDENCE_RE = re.compile(
    r'"(?:Write|StrReplace|Edit|MultiEdit|EditNotebook)"|"file_path"',
)


def cursor_user_turns(path) -> int:
    """Count user turns in a Cursor transcript (``role == "user"`` lines)."""
    n = 0
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("role") == "user":
                n += 1
    return n


def cursor_extract_text(path) -> str:
    """Concatenate every text block from user/assistant messages.

    Used where we want only the human-readable content (e.g. signal scanning on
    the tail) rather than the full JSON.
    """
    parts = []
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = o.get("message")
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text", ""))
    return "\n".join(parts)


def cursor_should_enqueue(path, *, min_turns: int = 6) -> bool:
    """Decide whether a Cursor session is worth the watchdog's LLM pass.

    Mirrors ``session_close.should_enqueue`` for Claude: enough human turns,
    evidence of an edit, and a signal keyword near the end. Raw-text based so it
    tolerates Cursor's lazy flushing and unknown tool JSON.
    """
    if cursor_user_turns(path) < min_turns:
        return False
    try:
        with open(path, errors="replace") as f:
            raw = f.read()
    except OSError:
        return False
    if not _EDIT_EVIDENCE_RE.search(raw):
        return False
    if not SIGNAL_RE.search(raw[-20000:]):
        return False
    return True

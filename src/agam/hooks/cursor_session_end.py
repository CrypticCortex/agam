#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Cursor ``sessionEnd`` hook: enqueue a finished session for the watchdog.

By session end Cursor has fully flushed the transcript, so this is the reliable
point to decide whether a session was real work and, if so, enqueue it for the
shared watchdog (same queue Claude's session_close writes to). The watchdog then
runs graph_update + the LLM work-log/sync pass on the complete transcript.

Input (stdin JSON): ``{session_id, reason, duration_ms, ...}`` + common schema
``{transcript_path, workspace_roots, ...}``.

Environment:
    AGAM_DATA_HOME   Shared data root (default ~/.agam). Queue lives at
                     $AGAM_DATA_HOME/.pending-closes.jsonl.
    AGAM_TOOLS_DIR   Dir holding pending_queue.py + transcripts.py.
    CURSOR_TRANSCRIPT_PATH  Fallback for transcript_path.
    CURSOR_PROJECT_DIR      Fallback for workspace_roots.
"""

import json
import os
import pathlib
import sys

_HOOK_DIR = pathlib.Path(__file__).resolve().parent


def _data_home() -> pathlib.Path:
    env = os.environ.get("AGAM_DATA_HOME")
    return pathlib.Path(env) if env else pathlib.Path(os.path.expanduser("~/.agam"))


def _tools_dir() -> pathlib.Path:
    env = os.environ.get("AGAM_TOOLS_DIR")
    if env:
        return pathlib.Path(env)
    for cand in (_HOOK_DIR.parent / "tools" / "agam", _HOOK_DIR.parent / "tools"):
        if (cand / "pending_queue.py").exists():
            return cand
    return _HOOK_DIR.parent / "tools" / "agam"


def _resolve_transcript(data: dict) -> str:
    tp = data.get("transcript_path") or os.environ.get("CURSOR_TRANSCRIPT_PATH", "")
    return tp or ""


def _workspace_root(data: dict) -> str:
    roots = data.get("workspace_roots") or []
    if isinstance(roots, list) and roots:
        return roots[0]
    return os.environ.get("CURSOR_PROJECT_DIR", "")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    transcript = _resolve_transcript(data)
    if not transcript or not os.path.exists(transcript):
        return 0

    tools_dir = _tools_dir()
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    try:
        import transcripts as tx  # type: ignore[import-not-found]
        import pending_queue as pq  # type: ignore[import-not-found]
    except ImportError:
        return 0

    if not tx.cursor_should_enqueue(transcript):
        return 0

    session_id = data.get("session_id") or data.get("conversation_id") or "cursor-unknown"
    workspace = _workspace_root(data)
    # File-per-session queue under ~/.agam/queue -- drained host-mode by
    # agam_watchdog.sh. This is Cursor's standalone enrichment path; it does not
    # touch Claude's .pending-closes.jsonl flow.
    queue_dir = _data_home() / "queue"
    try:
        pq.enqueue_file(
            queue_dir,
            session_id=session_id,
            transcript_path=transcript,
            cwd=workspace,
            context="cursor",
            agent="cursor",
        )
    except Exception:
        pass  # never block session close on enqueue failure
    return 0


if __name__ == "__main__":
    sys.exit(main())

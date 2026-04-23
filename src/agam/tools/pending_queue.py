"""Pending-queue helpers shared by the session-close hook and watchdog.

Provides line-oriented JSONL queue I/O with fcntl-based exclusive locking so
multiple hook invocations (host + devcontainer) can enqueue safely.

No environment variables here -- callers pass absolute `queue_path`
(`pathlib.Path`) values derived from `AGAM_HOME` or equivalent.
"""

import json
import time
import pathlib
import fcntl


def enqueue(
    queue_path: pathlib.Path,
    *,
    session_id: str,
    transcript_path: str,
    cwd: str,
    context: str,
):
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "context": context,
        "ts": time.time(),
    }
    with open(queue_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(entry) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def read_and_prune(queue_path: pathlib.Path, *, max_age_seconds: int):
    if not queue_path.exists():
        return []
    cutoff = time.time() - max_age_seconds
    kept = []
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("ts", 0) >= cutoff:
            kept.append(e)
    return kept


def replace_for_session(
    queue_path: pathlib.Path,
    *,
    session_id: str,
    transcript_path: str,
    cwd: str,
    context: str,
):
    """Remove any existing queue rows for this session_id, then append one fresh row.
    Atomic: holds an exclusive lock on the queue for the entire read-filter-write.
    """
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    new_entry = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "context": context,
        "ts": time.time(),
    }
    with open(queue_path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            kept = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("session_id") != session_id:
                    kept.append(e)
            kept.append(new_entry)
            f.seek(0)
            f.truncate()
            for e in kept:
                f.write(json.dumps(e) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Codex ``Stop`` hook: normalize and enqueue a worthwhile rollout.

Codex rollout JSONL is an internal, evolving format. Before handing a session
to Agam's shared watchdog, this hook converts useful UI messages and tool events
to a compact Claude-like JSONL snapshot under
``~/.agam/transcripts/codex/<session-id>-<generation>.jsonl``. The watchdog
therefore reads a stable, immutable generation and never needs access to
Codex's private rollout tree.

Retention keeps the pending generation plus two recent superseded/completed
generations per session. Queue, processing, retry, and dead-letter references
override that cap; this deliberately trades a small recent debugging window
for bounded storage. Older completed audit rows remain, marked after their
snapshot link is pruned.

Input (stdin JSON): ``{session_id, transcript_path, cwd}``.

Environment:
    AGAM_DATA_HOME   Shared data root (default ~/.agam).
    AGAM_TOOLS_DIR   Directory holding transcripts.py + pending_queue.py.

Every failure is a no-op: memory enrichment must never block Codex Stop.
"""

import json
import os
import pathlib
import re
import sys
import time
import uuid
from contextlib import contextmanager

import fcntl


_HOOK_DIR = pathlib.Path(__file__).resolve().parent

# A pending generation plus two recent, completed/superseded generations is
# enough for inspection and rollback without retaining a full copy of an
# ever-growing rollout after every Stop. Live work is exempt from this cap.
_RECENT_UNREFERENCED_GENERATIONS = 2


def _data_home() -> pathlib.Path:
    value = os.environ.get("AGAM_DATA_HOME") or os.environ.get("AGAM_HOME")
    return pathlib.Path(value) if value else pathlib.Path(os.path.expanduser("~/.agam"))


def _tools_dir() -> pathlib.Path:
    value = os.environ.get("AGAM_TOOLS_DIR")
    if value:
        return pathlib.Path(value)
    # Installed: ~/.codex/hooks/agam <-> ~/.codex/tools/agam
    # Source:    src/agam/hooks <-> src/agam/tools (+ transcripts.py one level up)
    for candidate in (
        _HOOK_DIR.parent.parent / "tools" / "agam",
        _HOOK_DIR.parent / "tools" / "agam",
        _HOOK_DIR.parent / "tools",
        _HOOK_DIR.parent,
    ):
        if (candidate / "transcripts.py").exists():
            return candidate
    return _HOOK_DIR.parent / "tools" / "agam"


def _safe_session_id(value) -> str:
    value = str(value or "codex-unknown")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80] or "codex-unknown"


def _snapshot_path(data_home: pathlib.Path, session_id: str) -> pathlib.Path:
    """Return a collision-safe, immutable path for one Stop generation.

    A queue entry may already be claimed while Codex emits another Stop for the
    same session. Session-stable filenames let the newer hook replace the file
    underneath that worker. A nanosecond timestamp keeps generations readable;
    UUID entropy makes concurrent hook invocations collision-safe.

    Old generations are pruned only after enqueue by the queue-aware retention
    pass below. Immutable names remain necessary because an older generation
    may already be claimed by the watchdog.
    """
    generation = f"{time.time_ns()}-{uuid.uuid4().hex}"
    filename = f"{_safe_session_id(session_id)}-{generation}.jsonl"
    return data_home / "transcripts" / "codex" / filename


@contextmanager
def _generation_lock(data_home: pathlib.Path):
    """Serialize Codex snapshot creation and retention for this data home.

    A non-blocking lock deliberately skips the newer enrichment pass when two
    Stop hooks overlap. Stop hooks are latency-sensitive, and missing one pass
    is safer than either blocking Codex or pruning another hook's not-yet-
    enqueued immutable generation.
    """
    data_home.mkdir(parents=True, exist_ok=True)
    lock_path = data_home / ".codex-snapshot.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _watchdog_prune_gate(data_home: pathlib.Path):
    """Prevent queue state transitions while references are inspected.

    The watchdog uses ``.watchdog.lock`` as an atomic PID lock before moving a
    queue file into ``processing/``. A populated temporary inode is published
    at that path with an atomic hard link: the shell can therefore observe no
    lock or a complete PID, never the empty create-before-write state it would
    treat as stale. If the watchdog wins publication first (including with a
    conservatively treated stale lock), retention is skipped.
    """
    lock_path = data_home / ".watchdog.lock"
    temp_path = data_home / f".watchdog.lock.codex-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    fd = None
    owned_stat = None
    published = False
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        payload = f"{os.getpid()}\n".encode()
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short write publishing watchdog prune gate")
            offset += written
        os.fsync(fd)
        owned_stat = os.fstat(fd)
        try:
            os.link(temp_path, lock_path)
        except FileExistsError:
            yield False
            return
        published = True
        temp_path.unlink()
        yield True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            current = lock_path.stat()
            if (
                published
                and owned_stat is not None
                and current.st_dev == owned_stat.st_dev
                and current.st_ino == owned_stat.st_ino
            ):
                lock_path.unlink()
        except OSError:
            pass


def _path_key(value) -> str | None:
    if not isinstance(value, (str, os.PathLike)) or not value:
        return None
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(value))))
    except (OSError, TypeError, ValueError):
        return None


def _read_work_entries(root: pathlib.Path) -> tuple[list[tuple[pathlib.Path, dict]], bool]:
    """Read JSON work records below *root*; false means cleanup must abort."""
    if not root.exists():
        return [], True
    entries: list[tuple[pathlib.Path, dict]] = []
    try:
        paths = list(root.rglob("*.json"))
    except OSError:
        return [], False
    for path in paths:
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # An unreadable active record might contain the only reference to
            # an immutable generation. Retention is optional, so abort it.
            return [], False
        if isinstance(value, dict):
            entries.append((path, value))
    return entries, True


def _snapshot_generations(data_home: pathlib.Path, session_id: str) -> list[pathlib.Path]:
    safe_id = _safe_session_id(session_id)
    generation_name = re.compile(
        rf"^{re.escape(safe_id)}-[0-9]+-[0-9a-f]{{32}}\.jsonl$"
    )
    directory = data_home / "transcripts" / "codex"
    if not directory.exists():
        return []
    try:
        snapshots = [
            path
            for path in directory.iterdir()
            if path.is_file() and generation_name.fullmatch(path.name)
        ]
        return sorted(snapshots, key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except OSError:
        return []


def _scrub_completed_reference(path: pathlib.Path, snapshot_key: str) -> bool:
    """Atomically retain a completed archive while retiring its snapshot link."""
    try:
        entry = json.loads(path.read_text())
        if not isinstance(entry, dict):
            return False
        if _path_key(entry.get("transcript_path")) != snapshot_key:
            return True
        entry["transcript_path"] = ""
        entry["snapshot_pruned"] = True
        entry["snapshot_pruned_at"] = time.time()
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(entry) + "\n")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def _prune_snapshots(data_home: pathlib.Path, session_id: str) -> None:
    """Bound completed/superseded generations while preserving live work.

    Queue, processing, retry, legacy claimed, and dead-letter records are live
    for retention purposes and override the count cap. Completed queue records
    keep their audit metadata, but references older than the recent tail are
    atomically marked pruned before the snapshot is removed. Any uncertainty
    skips cleanup; enrichment correctness is more important than disk savings.
    """
    with _watchdog_prune_gate(data_home) as gated:
        if not gated:
            return

        active_entries: list[tuple[pathlib.Path, dict]] = []
        for name in (
            "queue",
            "processing",
            "claimed",  # pre-atomic-claim installations and local upgrades
            "retry",
            "retries",
            ".retries",
            "queue-errors",
        ):
            entries, complete = _read_work_entries(data_home / name)
            if not complete:
                return
            active_entries.extend(entries)

        completed_entries, complete = _read_work_entries(data_home / "processed")
        if not complete:
            return

        active_refs = {
            key
            for _, entry in active_entries
            if (key := _path_key(entry.get("transcript_path"))) is not None
        }
        completed_refs: dict[str, list[pathlib.Path]] = {}
        for path, entry in completed_entries:
            key = _path_key(entry.get("transcript_path"))
            if key is not None:
                completed_refs.setdefault(key, []).append(path)

        snapshots = _snapshot_generations(data_home, session_id)
        unreferenced = [
            path for path in snapshots if _path_key(path) not in active_refs
        ]
        for snapshot in unreferenced[_RECENT_UNREFERENCED_GENERATIONS:]:
            key = _path_key(snapshot)
            if key is None:
                continue
            archives = completed_refs.get(key, [])
            if not all(_scrub_completed_reference(path, key) for path in archives):
                continue
            try:
                snapshot.unlink()
            except OSError:
                # Retention is strictly best-effort and never affects enqueue.
                continue


def run(data: dict, *, data_home: pathlib.Path | None = None, tools_dir: pathlib.Path | None = None) -> int:
    """Process one Stop payload. Always return success, including on bad input."""
    try:
        if not isinstance(data, dict):
            return 0
        transcript_value = data.get("transcript_path")
        if not isinstance(transcript_value, (str, os.PathLike)) or not transcript_value:
            return 0
        transcript = pathlib.Path(transcript_value)
        if not transcript.is_file():
            return 0

        tools_dir = tools_dir or _tools_dir()
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        import pending_queue as pq  # type: ignore[import-not-found]
        import transcripts as tx  # type: ignore[import-not-found]

        if not tx.codex_should_enqueue(transcript):
            return 0

        session_id = str(data.get("session_id") or "codex-unknown")
        data_home = data_home or _data_home()
        with _generation_lock(data_home) as locked:
            if not locked:
                return 0
            snapshot = _snapshot_path(data_home, session_id)
            if tx.codex_write_snapshot(transcript, snapshot) == 0:
                return 0

            cwd = data.get("cwd")
            if not isinstance(cwd, str):
                cwd = ""
            pq.enqueue_file(
                data_home / "queue",
                session_id=session_id,
                transcript_path=str(snapshot),
                cwd=cwd,
                context="codex",
                agent="codex",
            )
            _prune_snapshots(data_home, session_id)
    except Exception:
        # Stop hooks are on the user's critical path. Malformed rollout rows,
        # schema churn, import/install skew, and filesystem errors all degrade
        # to a missed enrichment pass rather than a failed Codex turn.
        return 0
    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    return run(data)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Agam watchdog monitor: queue state + processed log + run actions.

Usage:
    watchdog_monitor.py                       dashboard + interactive menu
    watchdog_monitor.py status                same, non-interactive
    watchdog_monitor.py queue                 pending queue only
    watchdog_monitor.py log [N]               last N watchdog-log events (default 20)
    watchdog_monitor.py processed             last 10 processed sessions
    watchdog_monitor.py kickstart             trigger launchd agent once
    watchdog_monitor.py sync <idx>            force-sync queue row <idx> (bypass gates)
    watchdog_monitor.py sync --all            detached bg drain, survives terminal close
                                              (adds --include-done to also redo 'done' rows,
                                               --foreground to stay attached)
    watchdog_monitor.py sync-all-status       check detached drain: alive? log tail?
    watchdog_monitor.py sync-all-stop         SIGTERM the detached drain (graceful)
    watchdog_monitor.py --json                machine-readable state dump

Detached sync --all:
  - PID written to AGAM_HOME/.sync-all.pid
  - Log written to AGAM_HOME/.sync-all.log (truncated each run)
  - macOS notification on completion / interruption / failure
  - SIGTERM or Ctrl-C stops gracefully AFTER the current row finishes, so
    haiku/sonnet never get cut mid-write and the sidecar stays consistent

Environment variables:
    AGAM_HOME               Directory holding AGAM sidecar state
                            (default: ~/.claude/agam)
    AGAM_HOOKS_DIR          Directory for inner.py inside the container
                            (default: ~/.claude/hooks/)
    AGAM_CONTAINER_PATTERN  Regex-alternation pattern matched against
                            "docker ps" output (default: claude-code|claude-code)
    AGAM_CONTAINER_NAME     Exact container name override (skips discovery)
    AGAM_HOST_CLAUDE_DIR    Host prefix mapped to container /home/node/.claude/
                            (default: ~/.claude/)
    AGAM_HOST_CODING_DIR    Host prefix mapped to container /workspaces/coding/
                            (default: ~/coding/)
    AGAM_CONTAINER_CLAUDE_DIR  Container-side .claude path
                               (default: /home/node/.claude/)
    AGAM_CONTAINER_CODING_DIR  Container-side coding path
                               (default: /workspaces/coding/)
"""

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time


HOME = pathlib.Path(os.path.expanduser("~"))
AGAM_HOME = pathlib.Path(
    os.environ.get("AGAM_HOME", os.path.expanduser("~/.claude/agam"))
)
HOOKS_DIR = pathlib.Path(
    os.environ.get("AGAM_HOOKS_DIR", os.path.expanduser("~/.claude/hooks/"))
)
HOST_CLAUDE_DIR = os.environ.get(
    "AGAM_HOST_CLAUDE_DIR", os.path.expanduser("~/.claude/")
)
HOST_CODING_DIR = os.environ.get(
    "AGAM_HOST_CODING_DIR", os.path.expanduser("~/coding/")
)
CONTAINER_CLAUDE_DIR = os.environ.get(
    "AGAM_CONTAINER_CLAUDE_DIR", "/home/node/.claude/"
)
CONTAINER_CODING_DIR = os.environ.get(
    "AGAM_CONTAINER_CODING_DIR", "/workspaces/coding/"
)
CONTAINER_PATTERN = os.environ.get(
    "AGAM_CONTAINER_PATTERN", "claude-code|claude-code"
)
CONTAINER_NAME_OVERRIDE = os.environ.get("AGAM_CONTAINER_NAME", "")

QUEUE = AGAM_HOME / ".pending-closes.jsonl"
PROCESSED = AGAM_HOME / ".processed-sessions.jsonl"
WLOG = AGAM_HOME / ".watchdog-log"
LOCKDIR = AGAM_HOME / ".watchdog.lock.d"
DAYCAP = AGAM_HOME / f".daycap-{time.strftime('%Y-%m-%d')}"
SYNC_ALL_LOG = AGAM_HOME / ".sync-all.log"
SYNC_ALL_PID = AGAM_HOME / ".sync-all.pid"

# Spec-defined queue dirs + log file used by the augmented status view.
QUEUE_DIR = AGAM_HOME / "queue"
QUEUE_ERRORS_DIR = AGAM_HOME / "queue-errors"
WATCHDOG_LOG = AGAM_HOME / "logs" / "watchdog.log"

MAX_PER_DAY = 8
IDLE_MIN = 600
MAX_AGE = 48 * 3600

INNER_PATH = str(HOOKS_DIR / "agam-watchdog-inner.py")

# Module-level flag set by SIGTERM/SIGINT handler so the drain loop stops
# gracefully after the row currently in flight finishes.
_stop_requested = False


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _age(seconds: float) -> str:
    if seconds < 0:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def _processed_state() -> dict:
    """Return session_id -> latest processed_mtime (0 for legacy rows)."""
    state = {}
    for e in _load_jsonl(PROCESSED):
        sid = e.get("session_id")
        if not sid:
            continue
        pm = e.get("processed_mtime", 0)
        if sid not in state or pm > state[sid]:
            state[sid] = pm
    return state


def _discover_container() -> str | None:
    """Find a running container matching AGAM_CONTAINER_PATTERN, or return the
    AGAM_CONTAINER_NAME override verbatim if set. Returns None if no match or
    docker is unavailable."""
    if CONTAINER_NAME_OVERRIDE:
        return CONTAINER_NAME_OVERRIDE
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Image}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    try:
        pattern = re.compile(CONTAINER_PATTERN, re.IGNORECASE)
    except re.error:
        return None
    for line in r.stdout.splitlines():
        if pattern.search(line):
            parts = line.split()
            if parts:
                return parts[0]
    return None


# Preserve the original function name for existing callers below.
_container_name = _discover_container


def _collect_queue_rows() -> list[dict]:
    """Enrich queue entries with computed fields: age, idle status, processed status."""
    now = time.time()
    processed = _processed_state()
    rows = []
    for e in _load_jsonl(QUEUE):
        sid = e.get("session_id", "?")
        tp = e.get("transcript_path", "")
        try:
            tmtime = os.path.getmtime(tp)
        except OSError:
            tmtime = 0
        enqueued_ago = now - e.get("ts", 0)
        idle_for = now - tmtime if tmtime else -1
        row = dict(e)
        row["_age"] = enqueued_ago
        row["_idle"] = idle_for
        row["_tmtime"] = tmtime
        row["_ready"] = (
            tmtime > 0
            and idle_for >= IDLE_MIN
            and enqueued_ago < MAX_AGE
        )
        row["_state"] = "stale" if enqueued_ago > MAX_AGE else (
            "missing" if tmtime == 0 else (
                "ready" if idle_for >= IDLE_MIN else "waiting"
            )
        )
        if sid in processed:
            pm = processed[sid]
            if tmtime > pm:
                row["_state"] += "*"  # processed-but-advanced
            else:
                row["_state"] = "done"
        rows.append(row)
    return rows


def _container_entry(entry: dict) -> str:
    """Translate host paths to container view for piping into inner.py."""
    out = dict(entry)
    for k in ("transcript_path", "cwd"):
        p = out.get(k, "")
        if p.startswith(HOST_CLAUDE_DIR):
            out[k] = CONTAINER_CLAUDE_DIR + p[len(HOST_CLAUDE_DIR):]
        elif p.startswith(HOST_CODING_DIR):
            out[k] = CONTAINER_CODING_DIR + p[len(HOST_CODING_DIR):]
    # strip private helper fields
    for k in list(out.keys()):
        if k.startswith("_"):
            del out[k]
    return json.dumps(out)


def _count_queue_files(directory: pathlib.Path) -> int:
    """Count .json files directly in directory. Returns 0 if directory is absent."""
    if not directory.exists() or not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("*.json"))


def _tail_log(path: pathlib.Path, n: int) -> list[str]:
    """Return last n lines of path, or [] if it doesn't exist."""
    if not path.exists():
        return []
    try:
        return path.read_text().splitlines()[-n:]
    except OSError:
        return []


def cmd_status(interactive: bool = True) -> int:
    now = time.time()
    container = _discover_container()
    daycap = 0
    if DAYCAP.exists():
        try:
            daycap = int(DAYCAP.read_text().strip() or "0")
        except ValueError:
            pass
    lock_held = LOCKDIR.exists()

    rows = _collect_queue_rows()
    processed = _load_jsonl(PROCESSED)
    events = _load_jsonl(WLOG)

    print("=" * 72)
    print(f"AGAM WATCHDOG MONITOR  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)
    print(f"  container    : {container or '(none running)'}")
    print(f"  lock held    : {'YES (run in progress)' if lock_held else 'no'}")
    print(f"  daycap       : {daycap}/{MAX_PER_DAY} processed today")
    print(f"  queue depth  : {len(rows)} entries")
    print(f"  processed    : {len(processed)} lifetime rows")
    # Container-aware additions (spec: file-based queue dirs + watchdog.log tail)
    qd = _count_queue_files(QUEUE_DIR)
    qed = _count_queue_files(QUEUE_ERRORS_DIR)
    print(f"  queue dir    : {qd} files in {QUEUE_DIR}")
    print(f"  error queue  : {qed} files in {QUEUE_ERRORS_DIR}")
    print()
    print(f"QUEUE  (idle>={IDLE_MIN//60}m required before watchdog picks up)")
    print("-" * 72)
    if not rows:
        print("  (empty)")
    else:
        print(f"  {'idx':<4}{'state':<9}{'sid':<10}{'ctx':<12}{'age':<7}{'idle':<7}{'path'}")
        for i, r in enumerate(rows):
            sid = r.get("session_id", "?")[:8]
            ctx = (r.get("context", "") or "")[:10]
            age = _age(r["_age"])
            idle = _age(r["_idle"]) if r["_idle"] >= 0 else "n/a"
            tp = r.get("transcript_path", "")
            tail = ".../" + "/".join(tp.split("/")[-2:]) if tp else "[missing]"
            print(f"  {i:<4}{r['_state']:<9}{sid:<10}{ctx:<12}{age:<7}{idle:<7}{tail}")
    print()
    print("RECENT PROCESSED  (last 5)")
    print("-" * 72)
    for e in processed[-5:]:
        sid = e.get("session_id", "?")[:8]
        ts = e.get("ts", 0)
        pm = e.get("processed_mtime", 0)
        age = _age(now - ts) if ts else "?"
        print(f"  {sid}  processed {age} ago  processed_mtime={int(pm) if pm else 'legacy'}")
    if not processed:
        print("  (none)")
    print()
    print("RECENT EVENTS  (last 10)")
    print("-" * 72)
    for e in events[-10:]:
        ts = e.get("ts", 0)
        age = _age(now - ts) if ts else "?"
        ev = e.get("event", "?")
        sid = e.get("session_id", "")
        extra = ""
        if ev in ("work-log-done", "agam-sync-done"):
            extra = f" rc={e.get('rc', '?')}"
        elif ev == "failed":
            extra = f" rc={e.get('rc', '?')}"
        elif ev == "partial":
            extra = f" wl={e.get('work_log_ok')} sync={e.get('sync_ok')}"
        sidstr = f" {sid[:8]}" if sid else ""
        print(f"  {age:>6} ago  {ev}{sidstr}{extra}")
    if not events:
        print("  (none)")
    print()
    # Spec: last 10 log lines from AGAM_HOME/logs/watchdog.log
    print("WATCHDOG LOG  (last 10 lines)")
    print("-" * 72)
    tail = _tail_log(WATCHDOG_LOG, 10)
    if not tail:
        print(f"  (no log at {WATCHDOG_LOG})")
    else:
        for line in tail:
            print(f"  {line}")
    print()

    if not interactive:
        return 0

    print("ACTIONS")
    print("-" * 72)
    print("  [k] kickstart launchd watchdog (respects all gates)")
    print("  [s <idx>] force-sync queue row <idx> now (bypasses idle/daycap/dedup)")
    print("  [a] force-sync ALL non-done rows (detached bg; notification on done)")
    print("  [A] same as [a] but in foreground (Ctrl-C to stop)")
    print("  [p] check detached sync --all status")
    print("  [P] stop detached sync --all (SIGTERM, graceful)")
    print("  [r] refresh")
    print("  [l] show last 30 log events")
    print("  [q] quit")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    return _handle_action(choice, rows)


def _handle_action(choice: str, rows: list[dict]) -> int:
    if not choice or choice == "q":
        return 0
    if choice == "r":
        return cmd_status(interactive=True)
    if choice == "k":
        return cmd_kickstart()
    if choice == "a":
        return cmd_sync_all(foreground=False)
    if choice == "A":
        return cmd_sync_all(foreground=True)
    if choice == "p":
        return cmd_sync_all_status()
    if choice == "P":
        return cmd_sync_all_stop()
    if choice == "l":
        return cmd_log(30)
    if choice.startswith("s "):
        try:
            idx = int(choice.split()[1])
        except (ValueError, IndexError):
            print("usage: s <idx>", file=sys.stderr)
            return 2
        return cmd_sync(idx, rows)
    print(f"unknown action: {choice!r}", file=sys.stderr)
    return 2


def cmd_kickstart() -> int:
    uid = os.getuid()
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/com.claude.agam-watchdog"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"kickstart failed: {r.stderr.strip()}", file=sys.stderr)
        return r.returncode
    print(f"kickstart sent. tail {WLOG} to watch.")
    return 0


def cmd_sync(idx: int, rows: list[dict] | None = None) -> int:
    if rows is None:
        rows = _collect_queue_rows()
    if idx < 0 or idx >= len(rows):
        print(f"no row at idx {idx} (queue has {len(rows)} rows)", file=sys.stderr)
        return 2
    entry = rows[idx]
    sid = entry.get("session_id", "?")
    container = _discover_container()
    if not container:
        print("no container running; manual sync needs the coding container", file=sys.stderr)
        return 1
    payload = _container_entry(entry)
    tz = os.environ.get("TZ") or _read_tz() or "UTC"
    print(f"force-syncing {sid} via {container}  (bypassing idle/daycap gates)...")
    print(f"  payload: {payload}")
    print("  streaming inner.py log events...")
    # start_new_session=True isolates the docker exec from our process group
    # so a SIGINT/SIGTERM to the parent doesn't cut the row mid-write. The
    # parent's signal handler flips _stop_requested; the loop checks it
    # *between* rows, after the current docker exec returns cleanly.
    r = subprocess.run(
        ["docker", "exec", "-i", "-e", f"TZ={tz}", container,
         INNER_PATH],
        input=payload, text=True,
        start_new_session=True,
    )
    if r.returncode == 0:
        print(f"sync ok. marking session processed.")
        _record_processed(entry)
    else:
        print(f"sync rc={r.returncode}. session NOT marked processed (will retry).")
    return r.returncode


def _notify(title: str, message: str) -> None:
    """Post a macOS notification via osascript. Best-effort; swallows errors."""
    # Escape double-quotes for AppleScript string literals
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    script = f'display notification "{m}" with title "{t}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _install_stop_handlers() -> None:
    """Handle SIGTERM and SIGINT by flipping _stop_requested. The drain loop
    checks this between rows, so the in-flight docker exec is allowed to finish
    (avoids truncating haiku/sonnet mid-write, which would leak /tmp state)."""
    def _handler(signum, _frame):
        global _stop_requested
        _stop_requested = True
        sys.stderr.write(f"\n[signal {signum}] stop requested; finishing current row...\n")
        sys.stderr.flush()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _sync_all_loop(*, include_done: bool) -> int:
    """The actual drain loop. Called both from foreground and detached child.
    Handles SIGTERM/SIGINT gracefully via _stop_requested flag."""
    global _stop_requested
    _install_stop_handlers()

    rows = _collect_queue_rows()
    if not rows:
        print("(queue empty)")
        _notify("Agam watchdog", "Drain complete: queue was empty")
        return 0
    container = _discover_container()
    if not container:
        print("no container running; manual sync needs the coding container", file=sys.stderr)
        _notify("Agam watchdog", "Drain failed: coding container not running")
        return 1

    targets = [
        (i, r) for i, r in enumerate(rows)
        if include_done or r["_state"] != "done"
    ]
    if not targets:
        print("nothing to sync (all rows already done; use --include-done to override)")
        _notify("Agam watchdog", "Drain complete: nothing to sync")
        return 0

    print(f"Force-syncing {len(targets)} of {len(rows)} rows sequentially via {container}.")
    print("Bypasses idle/daycap/dedup gates. SIGTERM/Ctrl-C stops after current row.")
    print("-" * 72)

    ok, failed = 0, 0
    start = time.time()
    attempted = 0
    for n, (idx, entry) in enumerate(targets, 1):
        if _stop_requested:
            break
        attempted = n
        sid = entry.get("session_id", "?")
        print(f"\n[{n}/{len(targets)}] idx={idx} sid={sid[:8]} state={entry['_state']}")
        sys.stdout.flush()
        rc = cmd_sync(idx, rows=rows)
        if rc == 0:
            ok += 1
        else:
            failed += 1
        # Re-read state after each row: a concurrent watchdog tick may have
        # advanced processed.jsonl while we were running.
        rows = _collect_queue_rows()

    skipped = len(targets) - attempted if _stop_requested else 0
    elapsed = time.time() - start
    print("\n" + "=" * 72)
    status = "interrupted" if _stop_requested else "complete"
    print(f"drain {status}: {ok} ok, {failed} failed, {skipped} not attempted")
    print(f"elapsed: {_age(elapsed)}")

    # Notification summary. Keep it short -- macOS truncates at ~200 chars.
    if _stop_requested:
        _notify("Agam watchdog", f"Drain interrupted: {ok} ok, {failed} failed, {skipped} skipped")
    elif failed:
        _notify("Agam watchdog", f"Drain done with errors: {ok} ok, {failed} failed")
    else:
        _notify("Agam watchdog", f"Drain complete: {ok}/{len(targets)} ok in {_age(elapsed)}")

    return 0 if failed == 0 and not _stop_requested else 1


def _spawn_detached(include_done: bool) -> int:
    """Re-exec self with an internal flag in a new session so it survives
    terminal close. Returns after printing PID + log location.

    We use the explicit internal subcommand `_sync_all_child` rather than
    reusing `sync --all` -- that way the child cannot re-spawn itself if the
    user typoes flags, and scripts can distinguish user-initiated drains from
    the detached worker in ps output.
    """
    if SYNC_ALL_PID.exists():
        try:
            old_pid = int(SYNC_ALL_PID.read_text().strip())
        except ValueError:
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            print(f"sync --all already running (pid={old_pid}).", file=sys.stderr)
            print(f"  log:  {SYNC_ALL_LOG}", file=sys.stderr)
            print(f"  stop: kill {old_pid}   (or: kill -TERM {old_pid})", file=sys.stderr)
            return 1
        # Stale PID file; safe to overwrite.
        SYNC_ALL_PID.unlink(missing_ok=True)

    SYNC_ALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Truncate log so each detached run is self-contained.
    log_fh = open(SYNC_ALL_LOG, "w", buffering=1)  # line-buffered
    log_fh.write(f"# sync --all detached run start: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_fh.flush()

    # Invoke via the script's shebang (not sys.executable). sys.executable points
    # at uv's ephemeral per-invocation Python which gets cleaned up when the
    # parent `uv run` exits. The detached child would inherit that dead path
    # and crash with "ModuleNotFoundError: No module named 'encodings'".
    # Running the script directly re-triggers its shebang (`uv run --script`),
    # giving the child its own managed Python that survives the parent's exit.
    script_path = os.path.abspath(sys.argv[0])
    args = [script_path, "_sync_all_child"]
    if include_done:
        args.append("--include-done")

    # start_new_session=True -> setsid() -> child survives terminal hangup.
    # stdin=DEVNULL so child cannot block on read. stdout/stderr -> log file.
    child = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        close_fds=True,
    )
    SYNC_ALL_PID.write_text(f"{child.pid}\n")
    print(f"Detached sync --all running in background.")
    print(f"  pid:  {child.pid}")
    print(f"  log:  {SYNC_ALL_LOG}   (tail -f to watch)")
    print(f"  stop: kill {child.pid}   (graceful; waits for current row)")
    print(f"You can close this terminal. You'll get a macOS notification when done.")
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def cmd_sync_all(*, include_done: bool = False, foreground: bool = False) -> int:
    """Force-sync every non-done queue row sequentially.

    Default: detaches and runs in background (survives terminal close), writes
    log to AGAM_HOME/.sync-all.log, PID to .sync-all.pid, and posts a
    macOS notification on completion. Kill it gracefully via SIGTERM (or
    whatever signal `kill <pid>` sends by default) -- the loop finishes the
    current row and stops cleanly, so the work-log sidecar stays consistent.

    foreground=True: runs in the current terminal. Useful for debugging.
    Same signal handling -- Ctrl-C finishes current row before exiting.
    """
    if foreground:
        return _sync_all_loop(include_done=include_done)
    return _spawn_detached(include_done)


def _cmd_sync_all_child(*, include_done: bool) -> int:
    """Internal entry point for the detached worker. Not user-facing.
    Installs a cleanup handler to remove the PID file on exit regardless of
    how we're terminated (success, failure, or signal)."""
    import atexit
    atexit.register(lambda: SYNC_ALL_PID.unlink(missing_ok=True))
    rc = _sync_all_loop(include_done=include_done)
    return rc


def cmd_sync_all_status() -> int:
    """Report status of the detached sync --all worker, if any."""
    if not SYNC_ALL_PID.exists():
        print("no detached sync --all worker recorded")
        return 0
    try:
        pid = int(SYNC_ALL_PID.read_text().strip())
    except ValueError:
        print("PID file exists but contents are unreadable; removing it.")
        SYNC_ALL_PID.unlink(missing_ok=True)
        return 1
    if _pid_alive(pid):
        print(f"detached sync --all running (pid={pid})")
        print(f"  log: {SYNC_ALL_LOG}")
        if SYNC_ALL_LOG.exists():
            print("  --- last 10 log lines ---")
            tail = SYNC_ALL_LOG.read_text().splitlines()[-10:]
            for line in tail:
                print(f"  {line}")
        return 0
    print(f"PID {pid} recorded but process is gone -- worker exited.")
    print(f"  log: {SYNC_ALL_LOG}")
    SYNC_ALL_PID.unlink(missing_ok=True)
    return 0


def cmd_sync_all_stop() -> int:
    """Send SIGTERM to the detached worker so it stops after the current row."""
    if not SYNC_ALL_PID.exists():
        print("no detached sync --all worker to stop")
        return 0
    try:
        pid = int(SYNC_ALL_PID.read_text().strip())
    except ValueError:
        print("PID file unreadable; removing")
        SYNC_ALL_PID.unlink(missing_ok=True)
        return 1
    if not _pid_alive(pid):
        print(f"PID {pid} already gone; cleaning up PID file")
        SYNC_ALL_PID.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"SIGTERM sent to pid={pid}. It will stop after the current row finishes.")
        print(f"Tail {SYNC_ALL_LOG} to watch.")
    except OSError as e:
        print(f"failed to signal pid={pid}: {e}", file=sys.stderr)
        return 1
    return 0


def _record_processed(entry: dict) -> None:
    PROCESSED.parent.mkdir(parents=True, exist_ok=True)
    tp = entry.get("transcript_path", "")
    try:
        pm = int(os.path.getmtime(tp))
    except OSError:
        pm = 0
    row = {
        "session_id": entry.get("session_id"),
        "transcript_path": tp,
        "cwd": entry.get("cwd", ""),
        "context": entry.get("context", ""),
        "ts": time.time(),
        "processed_mtime": pm,
    }
    with open(PROCESSED, "a") as f:
        f.write(json.dumps(row) + "\n")


def _read_tz() -> str | None:
    try:
        zi = os.readlink("/etc/localtime")
        if "/zoneinfo/" in zi:
            return zi.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def cmd_queue() -> int:
    rows = _collect_queue_rows()
    if not rows:
        print("(empty)")
        return 0
    for i, r in enumerate(rows):
        print(f"[{i}] {r['_state']:<9} {r.get('session_id', '?')[:8]}  "
              f"age={_age(r['_age'])}  idle={_age(r['_idle']) if r['_idle'] >= 0 else 'n/a'}  "
              f"{r.get('transcript_path', '')}")
    return 0


def cmd_log(n: int = 20) -> int:
    events = _load_jsonl(WLOG)
    now = time.time()
    for e in events[-n:]:
        ts = e.get("ts", 0)
        age = _age(now - ts) if ts else "?"
        extras = {k: v for k, v in e.items() if k not in ("ts", "event", "session_id")}
        ex = " " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        sid = e.get("session_id", "")
        sidstr = f" {sid[:8]}" if sid else ""
        print(f"{age:>6} ago  {e.get('event', '?')}{sidstr}{ex}")
    return 0


def cmd_processed() -> int:
    rows = _load_jsonl(PROCESSED)
    now = time.time()
    for e in rows[-10:]:
        sid = e.get("session_id", "?")[:12]
        age = _age(now - e.get("ts", 0)) if e.get("ts") else "?"
        pm = e.get("processed_mtime", 0)
        print(f"  {sid}  processed {age} ago  pm={int(pm) if pm else 'legacy'}  ctx={e.get('context', '')}")
    return 0


def cmd_json() -> int:
    state = {
        "container": _discover_container(),
        "lock_held": LOCKDIR.exists(),
        "daycap": int(DAYCAP.read_text().strip()) if DAYCAP.exists() else 0,
        "daycap_max": MAX_PER_DAY,
        "queue": _collect_queue_rows(),
        "processed_tail": _load_jsonl(PROCESSED)[-5:],
        "events_tail": _load_jsonl(WLOG)[-10:],
        "queue_dir_count": _count_queue_files(QUEUE_DIR),
        "queue_errors_count": _count_queue_files(QUEUE_ERRORS_DIR),
        "watchdog_log_tail": _tail_log(WATCHDOG_LOG, 10),
    }
    print(json.dumps(state, indent=2, default=str))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] == "status":
        return cmd_status(interactive=(len(argv) < 2))
    cmd = argv[1]
    if cmd == "queue":
        return cmd_queue()
    if cmd == "log":
        n = int(argv[2]) if len(argv) > 2 else 20
        return cmd_log(n)
    if cmd == "processed":
        return cmd_processed()
    if cmd == "kickstart":
        return cmd_kickstart()
    if cmd == "sync":
        if len(argv) < 3:
            print("usage: watchdog_monitor.py sync <idx|--all> [--include-done] [--foreground]", file=sys.stderr)
            return 2
        if argv[2] == "--all":
            rest = argv[3:]
            include_done = "--include-done" in rest
            foreground = "--foreground" in rest
            return cmd_sync_all(include_done=include_done, foreground=foreground)
        return cmd_sync(int(argv[2]))
    if cmd == "sync-all-status":
        return cmd_sync_all_status()
    if cmd == "sync-all-stop":
        return cmd_sync_all_stop()
    if cmd == "_sync_all_child":
        include_done = "--include-done" in argv[2:]
        return _cmd_sync_all_child(include_done=include_done)
    if cmd == "--json":
        return cmd_json()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

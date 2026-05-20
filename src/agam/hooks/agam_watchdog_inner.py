#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Runs inside the `coding` devcontainer via docker exec.
Reads one queue entry (JSON) from stdin. Drives graph-update, work-log (haiku),
agam-sync (sonnet), apply-proposals, and agam-lint in sequence.

Emits a single JSON row per event to $AGAM_HOME/.watchdog-log.

Environment variables:
    AGAM_HOME           Sidecar state dir (default: ~/.claude/agam).
                        Holds .watchdog-log, .processed-sessions.jsonl,
                        .work-log-written.jsonl.
    AGAM_PROMPTS_DIR    Directory holding work-log.txt and agam-sync.txt
                        prompt templates. Default: $AGAM_HOME/prompts.
                        KEY REFACTOR: prompts live alongside Agam identity
                        files rather than inside a skills/ tree.
    AGAM_WORK_LOG       Work log markdown file (default: ~/.claude/work-log.md).
    AGAM_HOOKS_DIR      Dir holding graph_update.py sibling hook.
                        Default: directory of this script.
    AGAM_TOOLS_DIR      Dir holding apply_proposals.py + agam_lint.py.
                        Default: ../tools relative to this script.
"""

import datetime
import json
import os
import pathlib
import subprocess
import sys
import time


HOME = pathlib.Path(os.path.expanduser("~"))

AGAM_HOME = pathlib.Path(
    os.environ.get("AGAM_HOME", str(HOME / ".claude" / "agam"))
)
PROMPTS = pathlib.Path(
    os.environ.get("AGAM_PROMPTS_DIR", str(AGAM_HOME / "prompts"))
)
WORK_LOG_PATH = pathlib.Path(
    os.environ.get("AGAM_WORK_LOG", str(HOME / ".claude" / "work-log.md"))
)

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
HOOKS = pathlib.Path(
    os.environ.get("AGAM_HOOKS_DIR", str(_SCRIPT_DIR))
)
TOOLS = pathlib.Path(
    os.environ.get("AGAM_TOOLS_DIR", str(_SCRIPT_DIR.parent / "tools"))
)

LOG = AGAM_HOME / ".watchdog-log"
PROCESSED = AGAM_HOME / ".processed-sessions.jsonl"
WORK_LOG_WRITTEN = AGAM_HOME / ".work-log-written.jsonl"


def log(session_id: str, event: str, **kw) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "session_id": session_id, "event": event, **kw}
    with open(LOG, "a") as f:
        f.write(json.dumps(row) + "\n")


def fill(template: pathlib.Path, **vars: str) -> str:
    text = template.read_text()
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def run_claude(prompt: str, *, model: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "claude", "-p", "--disable-slash-commands",
            "--permission-mode", "acceptEdits",
            "--model", model,
        ],
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _latest_mtime_for_sid(path: pathlib.Path, sid: str) -> float:
    if not path.exists():
        return 0.0
    latest = 0.0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("session_id") != sid:
            continue
        mtime = row.get("processed_mtime") or 0
        if mtime > latest:
            latest = mtime
    return latest


def _compute_cutoff(
    sid: str,
    *,
    processed_path: pathlib.Path = PROCESSED,
    work_log_path: pathlib.Path = WORK_LOG_WRITTEN,
) -> tuple[str, str]:
    """Return (since_iso, mode) for a given session_id.

    Scans TWO files for prior activity:
    - .processed-sessions.jsonl: full-flow completions
    - .work-log-written.jsonl:   partial completions (haiku appended to
                                 work-log.md but sonnet/apply may have died)

    Takes the MAX mtime across both. This protects against kill-mid-drain
    duplication: if haiku finishes + appends but the drain dies before sonnet,
    the sidecar still records the append, so the next retry's _compute_cutoff
    returns a cutoff >= the append point. Haiku's delta mode then sees no new
    content and emits SKIP, avoiding a duplicate work-log entry.
    """
    latest = max(
        _latest_mtime_for_sid(processed_path, sid),
        _latest_mtime_for_sid(work_log_path, sid),
    )
    if latest == 0:
        return "SESSION-START", "fresh"
    since_iso = datetime.datetime.fromtimestamp(latest).isoformat(timespec="seconds")
    return since_iso, "continuation"


def _record_work_log_written(sid: str, transcript_path: str) -> None:
    """Write a marker row to .work-log-written.jsonl right after a successful append.
    Used by _compute_cutoff to avoid duplicate work-log entries on mid-drain kill+retry."""
    WORK_LOG_WRITTEN.parent.mkdir(parents=True, exist_ok=True)
    try:
        pm = int(os.path.getmtime(transcript_path))
    except OSError:
        pm = int(time.time())
    row = {
        "session_id": sid,
        "transcript_path": transcript_path,
        "ts": time.time(),
        "processed_mtime": pm,
    }
    with open(WORK_LOG_WRITTEN, "a") as f:
        f.write(json.dumps(row) + "\n")


def _append_work_log(body_path: pathlib.Path, project: str, today: str, now: str, sid: str, *, mode: str = "fresh") -> bool:
    """Mechanically append haiku's body text to the work log with date/time header.
    The real ~/.claude/ tree is a protected-file scope for claude-p, but plain
    Python writes are fine.

    mode="fresh":        first time logging this session
    mode="continuation": re-processing a session that already has a log entry;
                         header gets "(continued)" suffix and content is the delta only.

    Returns True if content was actually appended, False if skipped (SKIP sentinel / empty body).
    Caller uses the return value to decide whether to write a sidecar marker.
    """
    body = body_path.read_text().strip()
    if not body or body.strip().upper() == "SKIP":
        log(sid, "work-log-skip", reason="trivial-session" if body else "empty")
        return False
    target = WORK_LOG_PATH
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Work Log\n\nPersonal record of what I worked on across Claude sessions.\n\n")
    existing = target.read_text()
    day_header = f"## {today}"
    suffix = " (continued)" if mode == "continuation" else ""
    if day_header in existing:
        snippet = f"\n### {now} | {project}{suffix}\n\n{body}\n"
    elif mode == "continuation":
        snippet = f"\n---\n\n## {today}\n\n### {now} | {project}{suffix}\n\n{body}\n"
    else:
        snippet = f"\n---\n\n## {today} | {project} | {now}\n\n{body}\n"
    with open(target, "a") as f:
        f.write(snippet)
    log(sid, "work-log-appended", project=project, bytes=len(snippet), mode=mode)
    return True


JSON_SCHEMA_HINT = """

## Output format

After completing analysis, write a JSON file to {proposals_path} with this exact schema:

```json
{
  "signals": ["SHIPPED" | "BUILT" | "DECIDED" | ...],
  "thisai_projects": [{"name": "Project Name", "note": "progress note"}],
  "thisai_goals": [{"name": "Goal Name", "note": "progress note"}],
  "memory": [{"filename": "x.md", "type": "user|feedback|project|reference", "description": "hook", "content": "body"}],
  "lesson": [{"title": "Short title", "body": "[lesson] **Title.** Explanation. Source: DATE session."}],
  "insight": [{"title": "Short title", "body": "[insight] **Title.** Explanation. Source: DATE session."}],
  "correction": [{"title": "Short title", "body": "[correction] **Title** (DATE). Summary."}],
  "obsolete": [{"name": "entity-name", "reason": "why no longer current"}]
}
```

Include only keys with actual content. Omit empty arrays. Write the JSON file and then exit.

The ``obsolete`` key marks entities whose underlying fact is no longer true
(bug fixed, decision reversed, project removed). High bar: only emit when the
transcript shows the change explicitly. The graph_recall hook will skip
obsoleted entities so the model stops reasoning about stale state.
"""


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("no entry on stdin", file=sys.stderr)
        return 2
    entry = json.loads(raw)
    sid = entry.get("session_id", "unknown")
    transcript = entry.get("transcript_path", "")
    cwd = entry.get("cwd", "")
    project = pathlib.Path(cwd).name or "unknown"

    log(sid, "start", transcript=transcript, cwd=cwd)

    # Step a: graph-update (pure Python, no LLM)
    graph_update = HOOKS / "graph_update.py"
    if graph_update.exists():
        try:
            subprocess.run(
                [str(graph_update)],
                input=json.dumps({"session_id": sid, "transcript_path": transcript}),
                text=True,
                timeout=30,
                check=False,
            )
            log(sid, "graph-update-done")
        except subprocess.TimeoutExpired:
            log(sid, "graph-update-timeout")
    else:
        log(sid, "graph-update-missing")

    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%H:%M")

    # Step b: work-log (haiku). Haiku writes body to /tmp; we append here.
    # The work log file is a protected "sensitive file" for claude-p even
    # with acceptEdits -- mechanical Python append bypasses that.
    since_iso, cutoff_mode = _compute_cutoff(sid)
    log(sid, "cutoff", since_iso=since_iso, mode=cutoff_mode)
    wlog_out = pathlib.Path(f"/tmp/work-log-entry-{sid}.md")
    wlog_prompt = fill(
        PROMPTS / "work-log.txt",
        JSONL_PATH=transcript,
        PROJECT_NAME=project,
        SESSION_ID=sid,
        CONTEXT_SUMMARY="(watchdog: derive context from transcript)",
        SESSION_SIGNALS="AUTO",
        DATE=today,
        TIME=now,
        OUTPUT_PATH=str(wlog_out),
        SINCE_ISO=since_iso,
    )
    work_log_ok = False
    try:
        r = run_claude(wlog_prompt, model="claude-haiku-4-5", timeout=180)
        log(sid, "work-log-done", rc=r.returncode, body_written=wlog_out.exists())
        if wlog_out.exists():
            appended = _append_work_log(wlog_out, project, today, now, sid, mode=cutoff_mode)
            if appended:
                _record_work_log_written(sid, transcript)
            try:
                wlog_out.unlink(missing_ok=True)
            except OSError:
                pass
            work_log_ok = True
        elif r.returncode == 0:
            # Haiku completed cleanly but skipped writing OUTPUT_PATH. Treat as implicit SKIP
            # so the session marks processed instead of looping in the retry queue forever.
            log(sid, "work-log-skip", reason="no-output-file")
            work_log_ok = True
    except subprocess.TimeoutExpired:
        log(sid, "work-log-timeout")

    # Step c: agam-sync (sonnet) -> proposals JSON
    proposals_path = pathlib.Path(f"/tmp/proposals-{sid}.json")
    schema_block = JSON_SCHEMA_HINT.replace("{proposals_path}", str(proposals_path))
    sync_prompt = fill(
        PROMPTS / "agam-sync.txt",
        JSONL_PATH=transcript,
        SESSION_SIGNALS="AUTO",
        CONTEXT_SUMMARY="(watchdog: derive context from transcript)",
    ) + schema_block
    sync_ok = False
    try:
        r = run_claude(sync_prompt, model="claude-sonnet-4-6", timeout=180)
        log(sid, "agam-sync-done", rc=r.returncode, proposals_written=proposals_path.exists())
        sync_ok = (r.returncode == 0)
    except subprocess.TimeoutExpired:
        log(sid, "agam-sync-timeout")

    # Step d: apply
    if proposals_path.exists():
        applier = TOOLS / "apply_proposals.py"
        r = subprocess.run([str(applier), str(proposals_path)], timeout=30, capture_output=True, text=True)
        log(sid, "apply-done", rc=r.returncode, stdout=r.stdout.strip(), stderr=r.stderr.strip())
        try:
            proposals_path.unlink(missing_ok=True)
        except OSError:
            pass

    # Step e: lint (fire and forget)
    lint = TOOLS / "agam_lint.py"
    if lint.exists():
        subprocess.Popen([str(lint), "--quick"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(sid, "lint-fired")

    if work_log_ok and sync_ok:
        log(sid, "done")
        return 0
    log(sid, "partial", work_log_ok=work_log_ok, sync_ok=sync_ok)
    return 1


if __name__ == "__main__":
    sys.exit(main())

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
import tempfile
import time
from collections import deque


HOME = pathlib.Path(os.path.expanduser("~"))

AGAM_HOME = pathlib.Path(
    os.environ.get("AGAM_HOME")
    or os.environ.get("AGAM_DATA_HOME")
    or str(HOME / ".agam")
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


# Top-level event types that carry no proposal-relevant signal. They hold
# attachments, git snapshots, queue bookkeeping, UI title management. Sonnet
# does not need any of them to write proposals. Drop entirely.
_DROP_TOPLEVEL_TYPES = {
    "attachment", "file-history-snapshot", "queue-operation",
    "last-prompt", "custom-title", "permission-mode", "agent-name",
    "ai-title", "system",
}

# Top-level metadata keys to keep. Everything else is internal plumbing
# (uuids, parentUuid, requestId, hook bookkeeping, snapshot data, etc.)
# that adds bytes without aiding proposal generation.
_KEEP_TOPLEVEL_KEYS = {"type", "timestamp", "message", "gitBranch", "cwd"}

# message.* keys to keep. Everything else is API metadata (model, usage,
# stop_reason, container, context_management).
_KEEP_MESSAGE_KEYS = {"role", "content"}

# Per-block content types within message.content[] -- drop entirely.
_DROP_BLOCK_TYPES = {"image"}

# Caps for content that can balloon. Sonnet needs to know WHAT a tool did,
# not the entire payload it returned.
_TOOL_RESULT_CAP = 600
_TOOL_USE_INPUT_CAP = 800

# Sliding-window size for pre-cutoff context. On continuation runs we keep
# the last N events from BEFORE since_iso so sonnet can link new turns to
# their immediate antecedents (the M1<->M14 lesson-linkage case). Pure
# delta loses that context. Matches Mem0's m=10 default.
_CONTEXT_WINDOW_SIZE = 10


def _compact_block(block: dict) -> dict | None:
    """Compact a single message.content[] block. Return None to drop it."""
    btype = block.get("type", "")
    if btype in _DROP_BLOCK_TYPES:
        return None
    if btype == "thinking":
        return {"type": "thinking", "thinking": block.get("thinking", "")}
    if btype == "tool_use":
        inp = block.get("input")
        if isinstance(inp, dict):
            new_inp = {}
            for k, v in inp.items():
                if isinstance(v, str) and len(v) > _TOOL_USE_INPUT_CAP:
                    new_inp[k] = v[:_TOOL_USE_INPUT_CAP] + f"...[+{len(v) - _TOOL_USE_INPUT_CAP}]"
                else:
                    new_inp[k] = v
        elif isinstance(inp, str) and len(inp) > _TOOL_USE_INPUT_CAP:
            new_inp = inp[:_TOOL_USE_INPUT_CAP] + f"...[+{len(inp) - _TOOL_USE_INPUT_CAP}]"
        else:
            new_inp = inp
        return {"type": "tool_use", "name": block.get("name"), "input": new_inp}
    if btype == "tool_result":
        c = block.get("content")
        out = {"type": "tool_result"}
        if block.get("is_error"):
            out["is_error"] = True
        if isinstance(c, str):
            out["content"] = (c[:_TOOL_RESULT_CAP] + f"...[+{len(c) - _TOOL_RESULT_CAP}]") if len(c) > _TOOL_RESULT_CAP else c
        elif isinstance(c, list):
            new_c = []
            for sub in c:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    t = sub.get("text", "")
                    if len(t) > _TOOL_RESULT_CAP:
                        new_c.append({"type": "text", "text": t[:_TOOL_RESULT_CAP] + f"...[+{len(t) - _TOOL_RESULT_CAP}]"})
                    else:
                        new_c.append({"type": "text", "text": t})
                elif isinstance(sub, dict) and sub.get("type") == "image":
                    continue
            out["content"] = new_c
        else:
            out["content"] = c
        return out
    if btype == "text":
        return {"type": "text", "text": block.get("text", "")}
    return block


def _compact_event(event: dict) -> dict | None:
    """Return a compacted event, or None if the entire event should be dropped."""
    t = event.get("type", "")
    if t in _DROP_TOPLEVEL_TYPES:
        return None

    out = {k: event[k] for k in _KEEP_TOPLEVEL_KEYS if k in event}

    msg = event.get("message")
    if isinstance(msg, dict):
        new_msg = {k: msg[k] for k in _KEEP_MESSAGE_KEYS if k in msg}
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                compacted = _compact_block(block)
                if compacted is not None:
                    new_content.append(compacted)
            new_msg["content"] = new_content
        elif isinstance(content, str):
            new_msg["content"] = content
        out["message"] = new_msg

    return out


def _slice_transcript_for_sonnet(
    jsonl_path: str,
    since_iso: str,
    sid: str,
    *,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[str, int, int]:
    """Return (path, window_count, delta_count) for sonnet.

    Strategy: field-level compaction + sliding-window cutoff. On continuation
    runs we drop events that fall below `since_iso` EXCEPT for the last
    _CONTEXT_WINDOW_SIZE pre-cutoff events, which are preserved so sonnet
    can link new turns to their immediate antecedents. On fresh runs the
    window is empty and every event passes through.

    Compaction drops (regardless of cutoff):
      - top-level event types with no proposal signal (attachment,
        file-history-snapshot, queue-operation, custom-title, last-prompt,
        permission-mode, agent-name, ai-title, system)
      - top-level metadata noise (uuid, parentUuid, sessionId, requestId,
        hookCount, hookInfos, etc.)
      - message-level API metadata (model, usage, stop_reason, container)
      - image blocks; thinking-block signature; tool_use id+caller;
        tool_result tool_use_id
      - tool_result content capped to 600 chars + tool_use input strings
        capped to 800 chars

    Returns the compacted file path (or the original if no work was needed),
    plus counts of how many events fell in the window vs the delta. Counts
    are 0 when the original is returned unchanged.
    """
    try:
        size = os.path.getsize(jsonl_path)
    except OSError:
        return jsonl_path, 0, 0

    cutoff_str = since_iso if since_iso and since_iso != "SESSION-START" else None
    if size <= max_bytes and cutoff_str is None:
        return jsonl_path, 0, 0

    # mkstemp gives a 0600 file at an unguessable path under $TMPDIR, closing
    # the symlink-clobber race that a predictable /tmp/<sid> name would carry.
    # The sid is kept in the prefix purely for human debugging of /tmp.
    fd, tmp_name = tempfile.mkstemp(prefix=f"agam-sync-slice-{sid}-", suffix=".jsonl")
    os.close(fd)
    out_path = pathlib.Path(tmp_name)
    window: deque[str] = deque(maxlen=_CONTEXT_WINDOW_SIZE)
    delta_lines: list[str] = []
    try:
        with open(jsonl_path, errors="replace") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                compacted = _compact_event(e)
                if compacted is None:
                    continue
                line = json.dumps(compacted) + "\n"
                ts = compacted.get("timestamp", "")
                if cutoff_str and ts and ts < cutoff_str:
                    window.append(line)
                else:
                    delta_lines.append(line)

        out_path.write_text("".join(window) + "".join(delta_lines))
        return str(out_path), len(window), len(delta_lines)
    except OSError:
        # mkstemp already created the file -- don't leak it on early exit.
        out_path.unlink(missing_ok=True)
        return jsonl_path, 0, 0


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


# Which LLM CLI drives enrichment. Set by agam_watchdog.sh's host probe:
# "claude" when on PATH, else "cursor-agent". Lets Cursor enrich the graph
# standalone on a host with no Claude installed.
LLM_CLI = os.environ.get("AGAM_LLM_CLI", "claude")

# Graph-only mode: do the deterministic graph_update and skip the LLM layer
# (work-log + agam-sync). Set this where the agent CLI can't write files in
# headless mode (e.g. host `claude -p`), so we enrich the graph without burning
# tokens on model calls that can't persist their output.
GRAPH_ONLY = os.environ.get("AGAM_GRAPH_ONLY", "").strip() == "1"


def run_claude(prompt: str, *, model: str, timeout: int) -> subprocess.CompletedProcess:
    """Run the enrichment prompt through whichever agent CLI is available.

    claude: prompt on stdin, --model honored (haiku/sonnet slugs).
    cursor-agent: prompt as a positional arg, --force for headless file writes.
      Cursor uses its own model names, so we let it pick its default rather than
      passing a claude-specific slug.
    """
    if LLM_CLI == "cursor-agent":
        return subprocess.run(
            ["cursor-agent", "-p", "--force", "--output-format", "text", prompt],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    # Pass the prompt as a positional argument, not stdin. Host `claude -p`
    # rejects piped stdin in some builds ("Input must be provided ... when using
    # --print"); the positional form works on host and in containers alike.
    return subprocess.run(
        [
            "claude", "-p", prompt,
            "--disable-slash-commands",
            "--permission-mode", "acceptEdits",
            "--model", model,
        ],
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
  "lesson": [{"title": "Short title", "severity": "high|medium|low", "body": "[lesson] **Title.** Explanation. Source: DATE session."}],
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
    agent = entry.get("agent", "unknown")
    project = pathlib.Path(cwd).name or "unknown"

    log(sid, "start", transcript=transcript, cwd=cwd)

    # Step a: graph-update (pure Python, no LLM). This is the durable enrichment
    # floor -- it writes entities/relationships + source-agent provenance with no
    # model call, so it works even on a host where `claude -p` can't write files.
    graph_update = HOOKS / "graph_update.py"
    graph_update_ok = False
    if graph_update.exists():
        try:
            r = subprocess.run(
                [str(graph_update)],
                input=json.dumps({
                    "session_id": sid,
                    "transcript_path": transcript,
                    "agent": agent,
                }),
                text=True,
                timeout=30,
                check=False,
            )
            graph_update_ok = (r.returncode == 0)
            log(sid, "graph-update-done", rc=r.returncode)
        except subprocess.TimeoutExpired:
            log(sid, "graph-update-timeout")
    else:
        log(sid, "graph-update-missing")

    # Graph-only mode stops here: deterministic enrichment done, skip the LLM
    # layer entirely (no token spend).
    if GRAPH_ONLY:
        log(sid, "graph-only-done", graph_update_ok=graph_update_ok)
        return 0 if graph_update_ok else 1

    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%H:%M")

    # Step b: work-log (haiku). Haiku writes body to /tmp; we append here.
    # The work log file is a protected "sensitive file" for claude-p even
    # with acceptEdits -- mechanical Python append bypasses that.
    since_iso, cutoff_mode = _compute_cutoff(sid)
    log(sid, "cutoff", since_iso=since_iso, mode=cutoff_mode)
    # mkstemp: 0600 file at an unguessable path so a pre-planted symlink at a
    # predictable /tmp/work-log-entry-<sid>.md can't redirect haiku's write.
    _wfd, _wname = tempfile.mkstemp(prefix=f"work-log-entry-{sid}-", suffix=".md")
    os.close(_wfd)
    wlog_out = pathlib.Path(_wname)
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
        # mkstemp pre-creates the file, so "did haiku write?" is a size check,
        # not an existence check.
        has_body = wlog_out.exists() and wlog_out.stat().st_size > 0
        log(sid, "work-log-done", rc=r.returncode, body_written=has_body)
        if has_body:
            appended = _append_work_log(wlog_out, project, today, now, sid, mode=cutoff_mode)
            if appended:
                _record_work_log_written(sid, transcript)
            work_log_ok = True
        elif r.returncode == 0:
            # Haiku completed cleanly but skipped writing OUTPUT_PATH. Treat as implicit SKIP
            # so the session marks processed instead of looping in the retry queue forever.
            log(sid, "work-log-skip", reason="no-output-file")
            work_log_ok = True
    except subprocess.TimeoutExpired:
        log(sid, "work-log-timeout")
    finally:
        wlog_out.unlink(missing_ok=True)

    # Step c: agam-sync (sonnet) -> proposals JSON
    # Slice the transcript first. Big transcripts exceed sonnet's context and
    # burn the timeout budget on tool reads. Continuation runs preserve a
    # sliding window of pre-cutoff context alongside the delta so cross-turn
    # lesson linkage survives.
    sliced_path, window_n, delta_n = _slice_transcript_for_sonnet(transcript, since_iso, sid)
    if sliced_path != transcript:
        try:
            orig_size = os.path.getsize(transcript)
            new_size = os.path.getsize(sliced_path)
            log(sid, "transcript-sliced",
                orig_mb=round(orig_size / 1024 / 1024, 2),
                sliced_mb=round(new_size / 1024 / 1024, 2),
                window_events=window_n,
                delta_events=delta_n,
                cutoff_mode=cutoff_mode)
        except OSError:
            pass

    # mkstemp for the same symlink-safety reason as the work-log temp above:
    # apply_proposals.py applies this file's content to identity files, so a
    # predictable path would be an attractive redirect target.
    _pfd, _pname = tempfile.mkstemp(prefix=f"proposals-{sid}-", suffix=".json")
    os.close(_pfd)
    proposals_path = pathlib.Path(_pname)
    schema_block = JSON_SCHEMA_HINT.replace("{proposals_path}", str(proposals_path))
    sync_prompt = fill(
        PROMPTS / "agam-sync.txt",
        JSONL_PATH=sliced_path,
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

    # Clean up the sliced temp now that all sonnet calls are done.
    if sliced_path != transcript:
        try:
            pathlib.Path(sliced_path).unlink(missing_ok=True)
        except OSError:
            pass

    # Step d: apply (only if sonnet actually wrote proposals; mkstemp pre-creates
    # an empty file, so gate on size).
    try:
        if proposals_path.exists() and proposals_path.stat().st_size > 0:
            applier = TOOLS / "apply_proposals.py"
            r = subprocess.run(
                [str(applier), str(proposals_path)],
                timeout=30, capture_output=True, text=True,
                env=dict(os.environ, AGAM_SOURCE_AGENT=agent),
            )
            log(sid, "apply-done", rc=r.returncode, stdout=r.stdout.strip(), stderr=r.stderr.strip())
    finally:
        proposals_path.unlink(missing_ok=True)

    # Step e: lint (fire and forget)
    lint = TOOLS / "agam_lint.py"
    if lint.exists():
        subprocess.Popen([str(lint), "--quick"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(sid, "lint-fired")

    # Graph enrichment is the floor: if it succeeded, the session is processed
    # even when the LLM layer (work-log / agam-sync) could not run (e.g. host
    # claude -p without file-write tools). The LLM layer is best-effort on top.
    if graph_update_ok or (work_log_ok and sync_ok):
        log(sid, "done", graph_update_ok=graph_update_ok,
            work_log_ok=work_log_ok, sync_ok=sync_ok)
        return 0
    log(sid, "partial", graph_update_ok=graph_update_ok,
        work_log_ok=work_log_ok, sync_ok=sync_ok)
    return 1


if __name__ == "__main__":
    sys.exit(main())

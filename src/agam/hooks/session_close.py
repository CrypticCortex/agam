#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///

"""
Stop hook: inspects the just-closed session's transcript and, if the session
looks like real work (>= 6 human turns, at least one Edit/Write, a signal
keyword in the tail), enqueues a row into the watchdog's pending-close queue.

Environment variables:
    AGAM_SESSIONS_DIR   Directory holding Claude Code session jsonl files
                        (default: ~/.claude/projects)
    AGAM_HOME           Directory holding the Agam sidecar state
                        (default: ~/.claude/agam). The pending-close queue
                        lives at $AGAM_HOME/.pending-closes.jsonl.
"""

import json
import sys
import os
import re
import glob
import pathlib

# The hook runs as a standalone PEP 723 script via `uv run --script`, so it
# cannot rely on package-style imports. Vendor the pending_queue helpers by
# adding the right tools dir to sys.path. There are two valid layouts:
#   installed:  ~/.claude/hooks/  <->  ~/.claude/tools/agam/
#   source:     src/agam/hooks/   <->  src/agam/tools/
# Probe both -- the first one carrying pending_queue.py wins. Without the
# installed-first probe, a first OSS user hit ``ModuleNotFoundError`` because
# the original hook only pointed at the flat tools/ root.
_HOOK_DIR = pathlib.Path(__file__).resolve().parent
for _candidate in (_HOOK_DIR.parent / "tools" / "agam", _HOOK_DIR.parent / "tools"):
    if (_candidate / "pending_queue.py").exists():
        sys.path.insert(0, str(_candidate))
        break
import pending_queue as pq  # noqa: E402

SIGNAL_RE = re.compile(
    r"\b(shipped|deployed|committed|decided|fixed|broke|learned|built|resolved|debugged|implemented|merged|released)\b",
    re.IGNORECASE,
)


def detect_context(cwd: str) -> str:
    return "devcontainer" if cwd.startswith("/workspaces/") else "host"


_HOST_HOME = os.environ.get("AGAM_HOST_HOME", os.path.expanduser("~"))
_HOST_CODING_DIR = os.environ.get("AGAM_HOST_CODING_DIR", os.path.join(_HOST_HOME, "coding"))


def _host_path(p: str) -> str:
    """Translate container-view paths to host-view.
    Keeps the queue in host-view regardless of whether the hook ran inside the coding container.
    Host home + coding dir are env-configurable so this works for any  user."""
    if p.startswith("/home/node/.claude/"):
        return os.path.join(_HOST_HOME, ".claude") + "/" + p[len("/home/node/.claude/"):]
    if p.startswith("/workspaces/coding/"):
        return _HOST_CODING_DIR + "/" + p[len("/workspaces/coding/"):]
    return p


def should_enqueue(transcript_content: str) -> bool:
    if transcript_content.count('"type":"user"') < 6:
        return False
    if '"name":"Edit"' not in transcript_content and '"name":"Write"' not in transcript_content:
        return False
    if not SIGNAL_RE.search(transcript_content[-20000:]):
        return False
    return True


def run(hook_input: dict, *, transcripts_root: pathlib.Path, queue_path: pathlib.Path) -> int:
    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd", "")

    matches = glob.glob(str(transcripts_root / "**" / f"{session_id}.jsonl"), recursive=True)
    if not matches:
        sys.stderr.write(
            f"session-close-hook: no transcript found for session_id={session_id}\n"
        )
        return 0
    transcript_path = matches[0]

    with open(transcript_path) as f:
        content = f.read()

    if not should_enqueue(content):
        return 0

    context = detect_context(cwd)
    pq.replace_for_session(
        queue_path,
        session_id=session_id,
        transcript_path=_host_path(transcript_path),
        cwd=_host_path(cwd),
        context=context,
        agent="claude",
    )
    return 0


def main():
    transcripts_root = pathlib.Path(
        os.environ.get("AGAM_SESSIONS_DIR", os.path.expanduser("~/.claude/projects"))
    )
    agam_home = pathlib.Path(
        os.environ.get("AGAM_HOME", os.path.expanduser("~/.claude/agam"))
    )
    data = json.load(sys.stdin)
    rc = run(
        data,
        transcripts_root=transcripts_root,
        queue_path=agam_home / ".pending-closes.jsonl",
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()

"""Tests for the session_close Stop hook.

The hook is a PEP 723 uv script: it reads JSON from stdin, inspects the
just-closed session's transcript, and if the session looks like real work
(>= 6 human turns, an Edit/Write, plus a trailing signal keyword) enqueues
a row into the pending-closes queue at $AGAM_HOME/.pending-closes.jsonl.

All tests drive the hook as a subprocess with `uv run --script` so that the
env-var overrides (AGAM_SESSIONS_DIR / AGAM_HOME) take effect exactly as
they would on a fresh install.

We also assert that the real ~/.claude/agam and ~/.claude/knowledge files
are never modified by a test run -- port safety net, not source behavior.
"""

import json
import os
import pathlib
import subprocess
import time

import pytest


HOOK = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "hooks" / "session_close.py"

REAL_KG = pathlib.Path(os.path.expanduser("~/.claude/knowledge/graph.db"))
REAL_AGAM_MD = pathlib.Path(os.path.expanduser("~/.claude/agam/AGAM.md"))
REAL_PENDING = pathlib.Path(os.path.expanduser("~/.claude/agam/.pending-closes.jsonl"))


def _mtime(path):
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return None


def _snapshot_real():
    return {
        "kg": _mtime(REAL_KG),
        "agam_md": _mtime(REAL_AGAM_MD),
        "pending": _mtime(REAL_PENDING),
    }


def _assert_real_untouched(snapshots):
    assert _mtime(REAL_KG) == snapshots["kg"], "Real graph.db was modified"
    assert _mtime(REAL_AGAM_MD) == snapshots["agam_md"], "Real AGAM.md was modified"
    assert _mtime(REAL_PENDING) == snapshots["pending"], (
        "Real ~/.claude/agam/.pending-closes.jsonl was modified"
    )


def _write_transcript(root: pathlib.Path, *, human_turns: int, has_edit: bool, signal: bool, name: str = "sample"):
    root.mkdir(parents=True, exist_ok=True)
    f = root / f"{name}.jsonl"
    compact = dict(separators=(",", ":"))
    lines = []
    for i in range(human_turns):
        lines.append(json.dumps({"type": "user", "content": f"msg {i}"}, **compact))
    if has_edit:
        lines.append(json.dumps({"type": "tool_use", "name": "Edit", "input": {}}, **compact))
    if signal:
        lines.append(json.dumps({"type": "assistant", "content": "Fixed the bug and shipped it."}, **compact))
    else:
        lines.append(json.dumps({"type": "assistant", "content": "hmm ok"}, **compact))
    f.write_text("\n".join(lines) + "\n")
    return f


@pytest.fixture
def hook_env(tmp_path):
    """AGAM_SESSIONS_DIR + AGAM_HOME pointing at tmp dirs, plus real-file snapshots."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    agam_home = tmp_path / "agam-home"
    agam_home.mkdir()
    env = {
        **os.environ,
        "AGAM_SESSIONS_DIR": str(sessions),
        "AGAM_HOME": str(agam_home),
        # Fix the host coding dir so container-path-to-host translation is
        # deterministic regardless of the test runner's $HOME.
        "AGAM_HOST_HOME": "/Users/test",
        "AGAM_HOST_CODING_DIR": "/Users/test/coding",
    }
    return env, sessions, agam_home, _snapshot_real()


def _run_hook(env, payload, cwd):
    return subprocess.run(
        ["uv", "run", "--script", str(HOOK)],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )


# ---- Behavioural gates (ported from test_session_close_hook.py) ----

def test_short_session_does_not_enqueue(hook_env, tmp_path):
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=3, has_edit=True, signal=True, name="s1")
    r = _run_hook(env, {"session_id": "s1", "cwd": "/Users/test"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert not (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)


def test_no_edit_or_write_does_not_enqueue(hook_env, tmp_path):
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=10, has_edit=False, signal=True, name="s1")
    r = _run_hook(env, {"session_id": "s1", "cwd": "/Users/test"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)


def test_no_signal_keyword_does_not_enqueue(hook_env, tmp_path):
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=10, has_edit=True, signal=False, name="s1")
    r = _run_hook(env, {"session_id": "s1", "cwd": "/Users/test"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)


def test_all_gates_pass_enqueues_with_host_context(hook_env, tmp_path):
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=10, has_edit=True, signal=True, name="s-host")
    r = _run_hook(
        env,
        {"session_id": "s-host", "cwd": "/Users/test/coding/foo"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    queue = agam_home / ".pending-closes.jsonl"
    assert queue.exists(), r.stderr
    entries = [json.loads(line) for line in queue.read_text().strip().splitlines()]
    assert len(entries) == 1
    assert entries[0]["session_id"] == "s-host"
    assert entries[0]["context"] == "host"
    assert "ts" in entries[0]
    _assert_real_untouched(snapshots)


def test_devcontainer_cwd_detected_as_devcontainer_context(hook_env, tmp_path):
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=10, has_edit=True, signal=True, name="s-dc")
    r = _run_hook(
        env,
        {"session_id": "s-dc", "cwd": "/workspaces/coding/bar"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    queue = agam_home / ".pending-closes.jsonl"
    entries = [json.loads(line) for line in queue.read_text().strip().splitlines()]
    assert entries[0]["context"] == "devcontainer"
    _assert_real_untouched(snapshots)


def test_no_transcripts_exits_cleanly(hook_env, tmp_path):
    env, _sessions, agam_home, snapshots = hook_env
    r = _run_hook(env, {"session_id": "s", "cwd": "/"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)


def test_picks_transcript_matching_session_id_not_latest_mtime(hook_env, tmp_path):
    """Hook must select the jsonl whose basename is <session_id>.jsonl, not
    the latest-mtime jsonl across all projects."""
    env, sessions, agam_home, snapshots = hook_env
    my_jsonl = _write_transcript(
        sessions / "p1", human_turns=10, has_edit=True, signal=True, name="my-sid"
    )
    old_mtime = time.time() - 600
    os.utime(my_jsonl, (old_mtime, old_mtime))
    other_jsonl = _write_transcript(
        sessions / "p2", human_turns=10, has_edit=True, signal=True, name="other-sid"
    )
    assert os.path.getmtime(other_jsonl) > os.path.getmtime(my_jsonl)

    r = _run_hook(env, {"session_id": "my-sid", "cwd": "/Users/test"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    queue = agam_home / ".pending-closes.jsonl"
    entries = [json.loads(line) for line in queue.read_text().strip().splitlines()]
    assert len(entries) == 1
    assert entries[0]["session_id"] == "my-sid"
    assert entries[0]["transcript_path"].endswith("my-sid.jsonl")
    _assert_real_untouched(snapshots)


def test_skips_enqueue_when_no_matching_transcript(hook_env, tmp_path):
    """If no {session_id}.jsonl exists, hook must exit 0 without enqueueing."""
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(
        sessions / "p1", human_turns=10, has_edit=True, signal=True, name="other-sid"
    )
    r = _run_hook(env, {"session_id": "missing-sid", "cwd": "/Users/test"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)


def test_enqueue_normalizes_cwd_to_host_view(hook_env, tmp_path):
    """Container-view cwd (/workspaces/coding/...) is rewritten to host view in
    the queue row."""
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=10, has_edit=True, signal=True, name="s-dc")
    r = _run_hook(
        env,
        {"session_id": "s-dc", "cwd": "/workspaces/coding/bar"},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    queue = agam_home / ".pending-closes.jsonl"
    entries = [json.loads(line) for line in queue.read_text().strip().splitlines()]
    assert entries[0]["context"] == "devcontainer"
    assert entries[0]["cwd"] == "/Users/test/coding/bar"
    _assert_real_untouched(snapshots)


# ---- Env-var override + real-file guards ----

def test_uses_agam_home_and_sessions_dir_env_vars(hook_env, tmp_path):
    """Hook honors AGAM_SESSIONS_DIR for transcript discovery AND AGAM_HOME
    for the queue location -- NOT the ~/.claude defaults."""
    env, sessions, agam_home, snapshots = hook_env
    _write_transcript(sessions / "p1", human_turns=10, has_edit=True, signal=True, name="s-env")
    r = _run_hook(env, {"session_id": "s-env", "cwd": "/Users/test"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    # Queue must land under AGAM_HOME, not real ~/.claude/agam
    assert (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)


def test_queue_path_independent_of_sessions_dir(tmp_path):
    """AGAM_HOME alone controls queue location, even if AGAM_SESSIONS_DIR
    points somewhere unrelated."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    agam_home = tmp_path / "elsewhere" / "agam"
    # Don't pre-create agam_home -- hook's replace_for_session should mkdir it.
    env = {
        **os.environ,
        "AGAM_SESSIONS_DIR": str(sessions),
        "AGAM_HOME": str(agam_home),
        # Fix the host coding dir so container-path-to-host translation is
        # deterministic regardless of the test runner's $HOME.
        "AGAM_HOST_HOME": "/Users/test",
        "AGAM_HOST_CODING_DIR": "/Users/test/coding",
    }
    _write_transcript(sessions / "p1", human_turns=10, has_edit=True, signal=True, name="s-sep")
    snapshots = _snapshot_real()
    r = _run_hook(env, {"session_id": "s-sep", "cwd": "/Users/test/coding/x"}, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (agam_home / ".pending-closes.jsonl").exists()
    _assert_real_untouched(snapshots)

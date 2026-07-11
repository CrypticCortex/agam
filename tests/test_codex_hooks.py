"""End-to-end tests for the Codex Stop hook."""

import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).parent.parent
HOOK = REPO / "src" / "agam" / "hooks" / "codex_stop.py"
TOOLS_SRC = REPO / "src" / "agam" / "tools"
TRANSCRIPTS_SRC = REPO / "src" / "agam" / "transcripts.py"
FIXTURE = Path(__file__).parent / "fixtures" / "codex" / "rollout_with_edits.jsonl"


@pytest.fixture
def tools_dir(tmp_path):
    directory = tmp_path / "tools"
    directory.mkdir()
    shutil.copy2(TOOLS_SRC / "pending_queue.py", directory / "pending_queue.py")
    shutil.copy2(TRANSCRIPTS_SRC, directory / "transcripts.py")
    return directory


def _run(stdin, *, data_home: Path, tools_dir: Path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "AGAM_DATA_HOME": str(data_home),
        "AGAM_TOOLS_DIR": str(tools_dir),
    }
    input_text = stdin if isinstance(stdin, str) else json.dumps(stdin)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _snapshots(data_home: Path):
    return list((data_home / "transcripts" / "codex").glob("*.jsonl"))


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("agam_test_codex_stop", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stop_normalizes_and_enqueues_codex_session(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    cwd = tmp_path / "widget"
    cwd.mkdir()
    result = _run(
        {
            "session_id": "codex-session-1",
            "transcript_path": str(FIXTURE),
            "cwd": str(cwd),
        },
        data_home=data_home,
        tools_dir=tools_dir,
    )

    assert result.returncode == 0, result.stderr
    queue_file = data_home / "queue" / "codex-session-1.json"
    assert queue_file.is_file()

    entry = json.loads(queue_file.read_text())
    snapshot = Path(entry["transcript_path"])
    assert snapshot.is_file()
    assert snapshot.parent == data_home / "transcripts" / "codex"
    assert snapshot.name.startswith("codex-session-1-")
    assert entry["session_id"] == "codex-session-1"
    assert entry["transcript_path"] == str(snapshot)
    assert entry["cwd"] == str(cwd)
    assert entry["context"] == "codex"
    assert entry["agent"] == "codex"

    rows = [json.loads(line) for line in snapshot.read_text().splitlines()]
    assert rows
    assert all(row["type"] in {"user", "assistant"} for row in rows)


def test_stop_sanitizes_snapshot_filename(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    result = _run(
        {
            "session_id": "../../unsafe/session",
            "transcript_path": str(FIXTURE),
            "cwd": str(tmp_path),
        },
        data_home=data_home,
        tools_dir=tools_dir,
    )
    assert result.returncode == 0
    snapshots = list((data_home / "transcripts" / "codex").glob("*.jsonl"))
    assert len(snapshots) == 1
    assert snapshots[0].parent == data_home / "transcripts" / "codex"
    queue_files = list((data_home / "queue").glob("*.json"))
    assert len(queue_files) == 1


def test_stop_keeps_claimed_snapshot_immutable_across_generations(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    source = tmp_path / "growing-rollout.jsonl"
    shutil.copy2(FIXTURE, source)
    payload = {
        "session_id": "same-session",
        "transcript_path": str(source),
        "cwd": str(tmp_path / "widget"),
    }

    first = _run(payload, data_home=data_home, tools_dir=tools_dir)
    assert first.returncode == 0, first.stderr
    pending = data_home / "queue" / "same-session.json"
    first_entry = json.loads(pending.read_text())
    first_snapshot = Path(first_entry["transcript_path"])
    first_contents = first_snapshot.read_bytes()

    # Simulate the watchdog atomically claiming the first queue generation.
    claimed_dir = data_home / "processing" / "claim.test"
    claimed_dir.mkdir(parents=True)
    pending.replace(claimed_dir / pending.name)

    with source.open("a") as rollout:
        rollout.write(json.dumps({
            "timestamp": "2026-07-01T10:06:00Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "Released immutable snapshot generation two.",
                "phase": "final",
            },
        }) + "\n")

    second = _run(payload, data_home=data_home, tools_dir=tools_dir)
    assert second.returncode == 0, second.stderr
    second_entry = json.loads(pending.read_text())
    second_snapshot = Path(second_entry["transcript_path"])

    # A failed generation is requeued under a generation-specific name. Both
    # it and the in-flight claim must survive retention even after enough newer
    # Stops to exceed the normal recent-generation cap.
    retry = data_home / "queue" / "same-session.retry-claim.test.json"
    pending.replace(retry)
    for _ in range(6):
        newer = _run(payload, data_home=data_home, tools_dir=tools_dir)
        assert newer.returncode == 0, newer.stderr

    assert first_snapshot != second_snapshot
    assert first_snapshot.is_file()
    assert second_snapshot.is_file()
    assert first_snapshot.read_bytes() == first_contents
    assert b"generation two" not in first_snapshot.read_bytes()
    assert b"generation two" in second_snapshot.read_bytes()
    # Two live older generations + current pending + the two-generation recent
    # tail. Live queue work intentionally overrides the count cap.
    assert len(_snapshots(data_home)) <= 5


def test_stop_bounds_repeated_superseded_snapshots(tmp_path, tools_dir):
    """Repeated Stops keep immutable names without quadratic disk growth."""
    data_home = tmp_path / ".agam"
    payload = {
        "session_id": "long-session",
        "transcript_path": str(FIXTURE),
        "cwd": str(tmp_path),
    }

    for _ in range(10):
        result = _run(payload, data_home=data_home, tools_dir=tools_dir)
        assert result.returncode == 0, result.stderr

    pending = json.loads((data_home / "queue" / "long-session.json").read_text())
    snapshots = _snapshots(data_home)
    assert len(snapshots) == 3  # pending generation + two recent predecessors
    assert Path(pending["transcript_path"]) in snapshots


def test_stop_retires_old_completed_snapshot_links_but_keeps_audit_rows(
    tmp_path, tools_dir
):
    data_home = tmp_path / ".agam"
    payload = {
        "session_id": "completed-session",
        "transcript_path": str(FIXTURE),
        "cwd": str(tmp_path),
    }
    processed = data_home / "processed"
    processed.mkdir(parents=True)
    first_snapshot = None
    first_archive = processed / "completed-session.0.json"

    for generation in range(5):
        result = _run(payload, data_home=data_home, tools_dir=tools_dir)
        assert result.returncode == 0, result.stderr
        pending = data_home / "queue" / "completed-session.json"
        entry = json.loads(pending.read_text())
        if generation == 0:
            first_snapshot = Path(entry["transcript_path"])
        pending.replace(processed / f"completed-session.{generation}.json")

    # One more pending generation triggers retention after five completed runs.
    result = _run(payload, data_home=data_home, tools_dir=tools_dir)
    assert result.returncode == 0, result.stderr

    assert first_snapshot is not None and not first_snapshot.exists()
    retired = json.loads(first_archive.read_text())
    assert retired["transcript_path"] == ""
    assert retired["snapshot_pruned"] is True
    assert len(_snapshots(data_home)) == 3


def test_stop_defers_retention_while_watchdog_owns_queue_lock(tmp_path, tools_dir):
    data_home = tmp_path / ".agam"
    data_home.mkdir()
    watchdog_lock = data_home / ".watchdog.lock"
    watchdog_lock.write_text(f"{os.getpid()}\n")
    payload = {
        "session_id": "watchdog-race",
        "transcript_path": str(FIXTURE),
        "cwd": str(tmp_path),
    }

    for _ in range(5):
        assert _run(payload, data_home=data_home, tools_dir=tools_dir).returncode == 0
    assert len(_snapshots(data_home)) == 5

    watchdog_lock.unlink()
    assert _run(payload, data_home=data_home, tools_dir=tools_dir).returncode == 0
    assert len(_snapshots(data_home)) == 3


def test_watchdog_prune_gate_atomically_publishes_populated_pid(
    tmp_path, monkeypatch
):
    """The shell must never observe the prune gate as an empty stale lock."""
    hook = _load_hook_module()
    real_link = os.link
    observations = []

    def checked_link(source, destination):
        source = Path(source)
        destination = Path(destination)
        assert not destination.exists()
        assert source.read_text() == f"{os.getpid()}\n"
        real_link(source, destination)
        # A hard-link publication exposes the already-populated inode in one
        # namespace operation; there is no create-before-write interval.
        assert destination.read_text() == f"{os.getpid()}\n"
        observations.append(destination)

    monkeypatch.setattr(hook.os, "link", checked_link)
    lock_path = tmp_path / ".watchdog.lock"
    with hook._watchdog_prune_gate(tmp_path) as gated:
        assert gated is True
        assert lock_path.read_text() == f"{os.getpid()}\n"

    assert observations == [lock_path]
    assert not lock_path.exists()
    assert list(tmp_path.glob(".watchdog.lock.codex-*.tmp")) == []


def test_watchdog_prune_gate_preserves_competing_atomic_winner(
    tmp_path, monkeypatch
):
    hook = _load_hook_module()
    real_link = os.link
    lock_path = tmp_path / ".watchdog.lock"

    def watchdog_wins_before_link(source, destination):
        Path(destination).write_text("424242\n")
        real_link(source, destination)  # deterministically raises FileExistsError

    monkeypatch.setattr(hook.os, "link", watchdog_wins_before_link)
    with hook._watchdog_prune_gate(tmp_path) as gated:
        assert gated is False

    assert lock_path.read_text() == "424242\n"
    assert list(tmp_path.glob(".watchdog.lock.codex-*.tmp")) == []


def test_overlapping_stop_skips_before_creating_unreferenced_generation(
    tmp_path, tools_dir
):
    data_home = tmp_path / ".agam"
    data_home.mkdir()
    generation_lock = (data_home / ".codex-snapshot.lock").open("a+")
    fcntl.flock(generation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run(
            {
                "session_id": "concurrent",
                "transcript_path": str(FIXTURE),
                "cwd": str(tmp_path),
            },
            data_home=data_home,
            tools_dir=tools_dir,
        )
    finally:
        fcntl.flock(generation_lock, fcntl.LOCK_UN)
        generation_lock.close()

    assert result.returncode == 0
    assert _snapshots(data_home) == []
    assert not (data_home / "queue" / "concurrent.json").exists()


def test_stop_skips_trivial_rollout(tmp_path, tools_dir):
    transcript = tmp_path / "trivial.jsonl"
    transcript.write_text(json.dumps({
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "hello"},
    }) + "\n")
    data_home = tmp_path / ".agam"
    result = _run(
        {"session_id": "trivial", "transcript_path": str(transcript), "cwd": str(tmp_path)},
        data_home=data_home,
        tools_dir=tools_dir,
    )
    assert result.returncode == 0
    assert not (data_home / "queue").exists()
    assert not (data_home / "transcripts").exists()


@pytest.mark.parametrize("payload", ["not-json", {}, {"transcript_path": "/no/such/file"}])
def test_stop_never_blocks_on_bad_input(tmp_path, tools_dir, payload):
    result = _run(payload, data_home=tmp_path / ".agam", tools_dir=tools_dir)
    assert result.returncode == 0

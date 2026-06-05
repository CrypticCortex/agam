"""Tests for the delta-summarization helpers in agam_watchdog_inner.py:
- _compute_cutoff: decides SESSION-START vs continuation based on
  .processed-sessions.jsonl and .work-log-written.jsonl sidecars.
- _append_work_log: formats the snippet differently for fresh vs continuation
  mode and honors the SKIP sentinel / empty-body short-circuits.

Plus two port-specific tests that pin down the AGAM_PROMPTS_DIR refactor:
prompts are read from a configurable dir (default: AGAM_HOME/prompts), not
from ~/.claude/skills/session-close/prompts anymore.

The tests load the module via importlib so the real `claude -p` subprocess in
main() is never invoked -- we only exercise pure helpers. Real ~/.claude
files are never written to because every helper under test takes explicit
paths or goes through monkeypatched module globals.
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile


INNER_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src" / "agam" / "hooks" / "agam_watchdog_inner.py"
)

REAL_KG = pathlib.Path(os.path.expanduser("~/.claude/knowledge/graph.db"))
REAL_AGAM_MD = pathlib.Path(os.path.expanduser("~/.claude/agam/AGAM.md"))
REAL_WORK_LOG = pathlib.Path(os.path.expanduser("~/.claude/work-log.md"))


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
        "work_log": _mtime(REAL_WORK_LOG),
    }


def _assert_real_untouched(snapshots):
    assert _mtime(REAL_KG) == snapshots["kg"], "Real graph.db was modified"
    assert _mtime(REAL_AGAM_MD) == snapshots["agam_md"], "Real AGAM.md was modified"
    assert _mtime(REAL_WORK_LOG) == snapshots["work_log"], "Real work-log.md was modified"


def _load_inner():
    # Force a fresh import each call so tests that manipulate env vars pick up
    # the updated module-level constants.
    sys.modules.pop("agam_watchdog_inner", None)
    spec = importlib.util.spec_from_file_location("agam_watchdog_inner", INNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- _compute_cutoff -------------------------------------------------------

def test_compute_cutoff_returns_session_start_when_no_processed_file():
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "missing.jsonl"
        since, mode = inner._compute_cutoff("sid-x", processed_path=p)
        assert since == "SESSION-START"
        assert mode == "fresh"
    _assert_real_untouched(snapshots)


def test_compute_cutoff_returns_session_start_when_sid_not_in_file():
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "processed.jsonl"
        p.write_text(json.dumps({"session_id": "other", "processed_mtime": 1776000000}) + "\n")
        since, mode = inner._compute_cutoff("sid-x", processed_path=p)
        assert since == "SESSION-START"
        assert mode == "fresh"
    _assert_real_untouched(snapshots)


def test_compute_cutoff_returns_iso_for_prior_processed_sid():
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "processed.jsonl"
        p.write_text(json.dumps({"session_id": "sid-x", "processed_mtime": 1776000000}) + "\n")
        since, mode = inner._compute_cutoff("sid-x", processed_path=p)
        assert mode == "continuation"
        # 1776000000 in local time -- just check it parses as ISO
        assert "T" in since
        assert since.count(":") == 2  # hh:mm:ss
    _assert_real_untouched(snapshots)


def test_compute_cutoff_picks_latest_mtime_across_rows():
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "processed.jsonl"
        p.write_text(
            json.dumps({"session_id": "sid-x", "processed_mtime": 1776000000}) + "\n"
            + json.dumps({"session_id": "sid-x", "processed_mtime": 1776900000}) + "\n"
            + json.dumps({"session_id": "sid-x", "processed_mtime": 1776500000}) + "\n"
        )
        since, mode = inner._compute_cutoff("sid-x", processed_path=p)
        assert mode == "continuation"
        import datetime
        expected = datetime.datetime.fromtimestamp(1776900000).isoformat(timespec="seconds")
        assert since == expected
    _assert_real_untouched(snapshots)


def test_compute_cutoff_ignores_legacy_rows_without_mtime():
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "processed.jsonl"
        # legacy rows (no processed_mtime)
        p.write_text(json.dumps({"session_id": "sid-x"}) + "\n")
        since, mode = inner._compute_cutoff("sid-x", processed_path=p)
        assert since == "SESSION-START"
        assert mode == "fresh"
    _assert_real_untouched(snapshots)


def test_compute_cutoff_reads_work_log_written_when_processed_absent():
    """Kill-mid-drain scenario: haiku appended + sidecar row written, but sonnet
    died before processed-sessions.jsonl got its row. Next retry's cutoff must
    still advance so haiku's delta mode sees no new content and emits SKIP."""
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        processed = pathlib.Path(d) / "processed.jsonl"  # intentionally missing
        wlw = pathlib.Path(d) / "work-log-written.jsonl"
        wlw.write_text(json.dumps({"session_id": "sid-x", "processed_mtime": 1776500000}) + "\n")
        since, mode = inner._compute_cutoff("sid-x", processed_path=processed, work_log_path=wlw)
        assert mode == "continuation"
        import datetime
        expected = datetime.datetime.fromtimestamp(1776500000).isoformat(timespec="seconds")
        assert since == expected
    _assert_real_untouched(snapshots)


def test_compute_cutoff_picks_max_across_processed_and_work_log_written():
    """Both files have rows for the sid; the later mtime wins regardless of which file it's in."""
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        processed = pathlib.Path(d) / "processed.jsonl"
        wlw = pathlib.Path(d) / "work-log-written.jsonl"
        processed.write_text(json.dumps({"session_id": "sid-x", "processed_mtime": 1776000000}) + "\n")
        wlw.write_text(json.dumps({"session_id": "sid-x", "processed_mtime": 1776900000}) + "\n")
        since, mode = inner._compute_cutoff("sid-x", processed_path=processed, work_log_path=wlw)
        assert mode == "continuation"
        import datetime
        expected = datetime.datetime.fromtimestamp(1776900000).isoformat(timespec="seconds")
        assert since == expected
    _assert_real_untouched(snapshots)


def test_compute_cutoff_ignores_other_sids_in_work_log_written():
    snapshots = _snapshot_real()
    inner = _load_inner()
    with tempfile.TemporaryDirectory() as d:
        processed = pathlib.Path(d) / "processed.jsonl"
        wlw = pathlib.Path(d) / "work-log-written.jsonl"
        wlw.write_text(json.dumps({"session_id": "other-sid", "processed_mtime": 1776900000}) + "\n")
        since, mode = inner._compute_cutoff("sid-x", processed_path=processed, work_log_path=wlw)
        assert since == "SESSION-START"
        assert mode == "fresh"
    _assert_real_untouched(snapshots)


# ---- _append_work_log ------------------------------------------------------

def _stub_log_target(monkeypatch, tmp_home):
    """Redirect WORK_LOG_PATH + LOG in the inner module so _append_work_log
    writes into a temp dir instead of the real work log / watchdog log."""
    inner = _load_inner()
    agam_home = tmp_home / ".claude" / "agam"
    agam_home.mkdir(parents=True)
    monkeypatch.setattr(inner, "WORK_LOG_PATH", tmp_home / ".claude" / "work-log.md")
    monkeypatch.setattr(inner, "LOG", agam_home / ".watchdog-log")
    monkeypatch.setattr(inner, "AGAM_HOME", agam_home)
    monkeypatch.setattr(inner, "WORK_LOG_WRITTEN", agam_home / ".work-log-written.jsonl")
    return inner


def test_append_work_log_fresh_mode_new_day(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("did the thing")
        inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="fresh")
        log_text = (home / ".claude" / "work-log.md").read_text()
        assert "## 2026-04-22 | proj-a | 10:00" in log_text
        assert "(continued)" not in log_text
        assert "did the thing" in log_text
    _assert_real_untouched(snapshots)


def test_append_work_log_fresh_mode_existing_day(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        # Pre-populate with today's header
        target = home / ".claude" / "work-log.md"
        target.write_text("# Work Log\n\n## 2026-04-22 | proj-a | 09:00\n\nmorning entry\n")
        body = home / "body.md"
        body.write_text("afternoon entry")
        inner._append_work_log(body, "proj-b", "2026-04-22", "14:00", "sid-2", mode="fresh")
        log_text = target.read_text()
        assert "### 14:00 | proj-b" in log_text
        assert "(continued)" not in log_text
    _assert_real_untouched(snapshots)


def test_append_work_log_continuation_under_existing_day(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        target = home / ".claude" / "work-log.md"
        target.write_text("# Work Log\n\n## 2026-04-22 | proj-a | 09:00\n\nmorning entry\n")
        body = home / "body.md"
        body.write_text("resumed and added feature X")
        inner._append_work_log(body, "proj-a", "2026-04-22", "14:00", "sid-1", mode="continuation")
        log_text = target.read_text()
        assert "### 14:00 | proj-a (continued)" in log_text
        assert "resumed and added feature X" in log_text
    _assert_real_untouched(snapshots)


def test_append_work_log_continuation_on_new_day_creates_day_header(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        target = home / ".claude" / "work-log.md"
        # Only yesterday's entry exists
        target.write_text("# Work Log\n\n## 2026-04-21 | proj-a | 15:00\n\nyesterday\n")
        body = home / "body.md"
        body.write_text("resumed today")
        inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="continuation")
        log_text = target.read_text()
        # New day header added (without project/time, since this is a continuation)
        assert "## 2026-04-22\n" in log_text
        assert "### 10:00 | proj-a (continued)" in log_text
    _assert_real_untouched(snapshots)


def test_append_work_log_skip_sentinel_writes_nothing(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("SKIP")
        inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="continuation")
        # work-log.md should not exist (we never wrote)
        assert not (home / ".claude" / "work-log.md").exists()
    _assert_real_untouched(snapshots)


def test_append_work_log_empty_body_writes_nothing(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("   \n   ")
        inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="fresh")
        assert not (home / ".claude" / "work-log.md").exists()
    _assert_real_untouched(snapshots)


def test_append_work_log_returns_true_when_content_written(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("did real work")
        assert inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="fresh") is True
    _assert_real_untouched(snapshots)


def test_append_work_log_returns_false_on_skip_sentinel(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("SKIP")
        assert inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="continuation") is False
    _assert_real_untouched(snapshots)


def test_append_work_log_returns_false_on_empty_body(monkeypatch):
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("")
        assert inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="fresh") is False
    _assert_real_untouched(snapshots)


def test_append_work_log_no_op_when_body_empty_file_returns_false(monkeypatch):
    """Defensive check: completely empty body file still returns False and writes nothing.
    Exercises the empty-body branch so the inner.py caller's implicit-SKIP logic is reachable."""
    snapshots = _snapshot_real()
    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        inner = _stub_log_target(monkeypatch, home)
        body = home / "body.md"
        body.write_text("")
        result = inner._append_work_log(body, "proj-a", "2026-04-22", "10:00", "sid-1", mode="fresh")
        assert result is False
        assert not (home / ".claude" / "work-log.md").exists()
    _assert_real_untouched(snapshots)


# ---- AGAM_PROMPTS_DIR refactor (port-specific) -----------------------------

def test_prompts_dir_honors_agam_prompts_dir_env(monkeypatch, tmp_path):
    """KEY REFACTOR: if AGAM_PROMPTS_DIR is set, PROMPTS at module load time
    must point there -- NOT to the old ~/.claude/skills/session-close/prompts
    location."""
    snapshots = _snapshot_real()
    custom = tmp_path / "my-prompts"
    custom.mkdir()
    monkeypatch.setenv("AGAM_PROMPTS_DIR", str(custom))
    # Also pin AGAM_HOME somewhere harmless so we can verify PROMPTS ignores it.
    agam_home = tmp_path / "agam-home"
    agam_home.mkdir()
    monkeypatch.setenv("AGAM_HOME", str(agam_home))

    inner = _load_inner()
    assert inner.PROMPTS == custom
    # Default fallback (AGAM_HOME/prompts) must NOT have been used.
    assert inner.PROMPTS != agam_home / "prompts"
    _assert_real_untouched(snapshots)


def test_prompts_dir_defaults_to_agam_home_prompts(monkeypatch, tmp_path):
    """When AGAM_PROMPTS_DIR is unset, PROMPTS defaults to $AGAM_HOME/prompts.
    This is the fallback the installer will set up for a plain `pip install agam`."""
    snapshots = _snapshot_real()
    monkeypatch.delenv("AGAM_PROMPTS_DIR", raising=False)
    agam_home = tmp_path / "custom-agam"
    agam_home.mkdir()
    monkeypatch.setenv("AGAM_HOME", str(agam_home))

    inner = _load_inner()
    assert inner.PROMPTS == agam_home / "prompts"
    # Must NOT have fallen back to the host's ~/.claude/skills path.
    assert "skills/session-close" not in str(inner.PROMPTS)
    _assert_real_untouched(snapshots)


# ---- _slice_transcript_for_sonnet sliding window --------------------------

def _write_transcript(path: pathlib.Path, events: list[dict]) -> None:
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _user_event(ts: str, text: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def test_slice_returns_original_on_fresh_run_under_threshold(tmp_path):
    """Fresh runs (since_iso == SESSION-START) on small transcripts should
    return the path unchanged with zero window/delta counts."""
    snapshots = _snapshot_real()
    inner = _load_inner()
    t = tmp_path / "tiny.jsonl"
    _write_transcript(t, [_user_event("2026-06-04T10:00:00", "hello")])

    path, win_n, delta_n = inner._slice_transcript_for_sonnet(
        str(t), "SESSION-START", "sid-fresh"
    )
    assert path == str(t)
    assert win_n == 0
    assert delta_n == 0
    _assert_real_untouched(snapshots)


def test_slice_preserves_last_n_pre_cutoff_events_as_window(tmp_path):
    """The sliding window must keep exactly _CONTEXT_WINDOW_SIZE events from
    BEFORE the cutoff, plus everything at-or-after the cutoff. M1<->M14
    linkage relies on this -- pure delta would lose pre-cutoff context."""
    snapshots = _snapshot_real()
    inner = _load_inner()

    # 15 pre-cutoff + 3 post-cutoff. Window size is 10 so window keeps
    # events 5..14 (last 10 pre-cutoff). Delta keeps the 3 post-cutoff.
    events = []
    for i in range(15):
        events.append(_user_event(f"2026-06-04T10:{i:02d}:00", f"pre-{i}"))
    events.append(_user_event("2026-06-04T11:00:00", "post-A"))
    events.append(_user_event("2026-06-04T11:01:00", "post-B"))
    events.append(_user_event("2026-06-04T11:02:00", "post-C"))

    t = tmp_path / "with-cutoff.jsonl"
    _write_transcript(t, events)

    # Force slicing path even though file is tiny: any non-SESSION-START
    # cutoff makes the function compact + slice.
    cutoff = "2026-06-04T10:30:00"
    path, win_n, delta_n = inner._slice_transcript_for_sonnet(
        str(t), cutoff, "sid-window"
    )
    assert path != str(t), "slicing must produce a temp file when cutoff is set"
    assert win_n == inner._CONTEXT_WINDOW_SIZE
    assert delta_n == 3

    sliced = _read_jsonl(path)
    # Window first, then delta. The earliest pre-cutoff events (0..4) are
    # dropped; events 5..14 survive in window order; then the three post.
    texts = [r["message"]["content"][0]["text"] for r in sliced]
    assert texts == [f"pre-{i}" for i in range(5, 15)] + ["post-A", "post-B", "post-C"]

    # Clean up the temp slice.
    pathlib.Path(path).unlink(missing_ok=True)
    _assert_real_untouched(snapshots)


def test_compact_block_caps_tool_use_string_input():
    """tool_use blocks where `input` is a bare string (not a dict) must be
    truncated to _TOOL_USE_INPUT_CAP and carry the "[+N]" suffix, matching
    the behavior already applied to string values nested in a dict input."""
    snapshots = _snapshot_real()
    inner = _load_inner()

    big = "x" * (inner._TOOL_USE_INPUT_CAP + 500)
    block = {"type": "tool_use", "name": "Bash", "input": big}
    out = inner._compact_block(block)

    assert out is not None
    assert out["type"] == "tool_use"
    assert out["name"] == "Bash"
    assert isinstance(out["input"], str)
    assert len(out["input"]) < len(big), "string input should have been capped"
    assert out["input"].startswith("x" * inner._TOOL_USE_INPUT_CAP)
    assert out["input"].endswith("...[+500]")
    _assert_real_untouched(snapshots)


def test_slice_uses_secure_tempfile_path(tmp_path):
    """The sliced temp file must come from tempfile.mkstemp -- random suffix
    in the filename so a local attacker who guesses the sid cannot pre-place
    a symlink at the path."""
    snapshots = _snapshot_real()
    inner = _load_inner()
    t = tmp_path / "src.jsonl"
    _write_transcript(t, [_user_event("2026-06-04T10:00:00", "hi")])

    path, _, _ = inner._slice_transcript_for_sonnet(
        str(t), "2026-06-04T09:00:00", "predictable-sid"
    )

    # mkstemp inserts a random component between prefix and suffix, so the
    # full path is NOT the literal /tmp/agam-sync-slice-<sid>.jsonl pattern.
    assert path != f"/tmp/agam-sync-slice-predictable-sid.jsonl"
    name = pathlib.Path(path).name
    assert name.startswith("agam-sync-slice-predictable-sid-")
    assert name.endswith(".jsonl")
    # Random middle segment must be non-empty -- that is what closes the race.
    middle = name[len("agam-sync-slice-predictable-sid-"):-len(".jsonl")]
    assert len(middle) > 0

    pathlib.Path(path).unlink(missing_ok=True)
    _assert_real_untouched(snapshots)


def test_slice_drops_attachment_events_via_compaction(tmp_path):
    """Compaction layer must drop events whose top-level type is in
    _DROP_TOPLEVEL_TYPES (attachment, queue-operation, etc.) regardless
    of where they fall vs the cutoff."""
    snapshots = _snapshot_real()
    inner = _load_inner()

    events = [
        _user_event("2026-06-04T10:00:00", "keep-me"),
        {"type": "attachment", "timestamp": "2026-06-04T10:01:00", "content": "x" * 5000},
        {"type": "queue-operation", "timestamp": "2026-06-04T10:02:00", "op": "drain"},
        _user_event("2026-06-04T10:03:00", "also-keep"),
    ]
    t = tmp_path / "noisy.jsonl"
    _write_transcript(t, events)

    path, win_n, delta_n = inner._slice_transcript_for_sonnet(
        str(t), "2026-06-04T09:59:00", "sid-drop"
    )
    sliced = _read_jsonl(path)
    types = [r.get("type") for r in sliced]
    assert "attachment" not in types
    assert "queue-operation" not in types
    assert types == ["user", "user"]
    # Both kept events are post-cutoff so they land in delta, not window.
    assert win_n == 0
    assert delta_n == 2

    pathlib.Path(path).unlink(missing_ok=True)
    _assert_real_untouched(snapshots)

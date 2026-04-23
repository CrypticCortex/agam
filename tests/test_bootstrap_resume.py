"""Tests for agam.bootstrap orchestration (Task 22).

``run_bootstrap`` ties scan + per-transcript extract + reconcile together
with a durable state file, supports resume, and fires macOS notifications
at 50% + done. All tests mock both ``extract_from_transcript`` and
``reconcile_candidates`` -- no real ``docker exec``, no network, no
osascript, no writes to the real ``~/.claude/``.
"""

from __future__ import annotations

import json
import signal

import pytest

from agam import bootstrap
from agam.bootstrap import (
    _install_sigint_handler,
    _load_state,
    _notify,
    _save_state,
    run_bootstrap,
)


# ---- helpers -------------------------------------------------------------


def _seed_transcripts(projects_dir, n):
    """Create ``n`` non-empty JSONL files under ``projects_dir/proj``."""
    proj = projects_dir / "proj"
    proj.mkdir(parents=True)
    paths = []
    for i in range(n):
        p = proj / f"session-{i}.jsonl"
        p.write_text(
            '{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8"
        )
        paths.append(p)
    return sorted(paths)


def _stub_reconcile(*args, **kwargs):
    """Default reconcile stub: echoes ``{entities: [...], relationships: []}``."""
    candidates = args[0] if args else kwargs.get("candidates", [])
    ents = [c for c in candidates if c.get("kind") == "entity"]
    rels = [c for c in candidates if c.get("kind") == "relationship"]
    return {
        "entities": [{"name": e.get("name", "?"), "type": e.get("type", "?")} for e in ents],
        "relationships": [
            {
                "source": r.get("source"),
                "relation": r.get("relation"),
                "target": r.get("target"),
            }
            for r in rels
        ],
    }


# ---- _save_state / _load_state ------------------------------------------


def test_save_state_atomic(tmp_path):
    """``_save_state`` writes contents and leaves no .tmp sibling behind."""
    target = tmp_path / "state.json"
    state = {"processed": ["a"], "candidates": [{"kind": "entity", "name": "X"}]}

    _save_state(target, state)

    assert target.exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == state
    # No stray tempfile in the same dir.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_load_state_returns_none_for_missing(tmp_path):
    assert _load_state(tmp_path / "nope.json") is None


def test_load_state_survives_malformed_json(tmp_path):
    """Garbage on disk should return ``None`` and not raise."""
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    assert _load_state(p) is None


# ---- _notify -------------------------------------------------------------


def test_notify_silent_on_osascript_missing(monkeypatch):
    """If ``osascript`` isn't on PATH, ``_notify`` must not raise."""

    def boom(*args, **kwargs):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(bootstrap.subprocess, "run", boom)
    # Should not raise.
    _notify("hello")


def test_notify_invokes_osascript_command(monkeypatch):
    """``_notify`` shells out to ``osascript`` with the message embedded."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    _notify("hello-world")
    assert calls, "expected subprocess.run to be called"
    joined = " ".join(calls[0])
    assert "osascript" in joined
    assert "hello-world" in joined


# ---- run_bootstrap -------------------------------------------------------


def test_run_bootstrap_happy_path(monkeypatch, tmp_path):
    """Two transcripts -> both extracted, reconcile called, state file deleted."""
    projects = tmp_path / "projects"
    transcripts = _seed_transcripts(projects, 2)

    call_order = []

    def fake_extract(path, model="haiku-4-5", chunk_tokens=50_000):
        call_order.append(path)
        return [{"kind": "entity", "name": f"E{path.stem}", "type": "project"}]

    def fake_reconcile(candidates, **kwargs):
        return {"entities": [{"name": "merged"}], "relationships": []}

    monkeypatch.setattr(bootstrap, "extract_from_transcript", fake_extract)
    monkeypatch.setattr(bootstrap, "reconcile_candidates", fake_reconcile)

    state_path = tmp_path / "state.json"
    result = run_bootstrap(
        projects,
        days=None,
        state_path=state_path,
        notify_fn=lambda msg: None,
    )

    assert result == {"entities": [{"name": "merged"}], "relationships": []}
    assert len(call_order) == 2
    # State file cleaned up on success.
    assert not state_path.exists()


def test_run_bootstrap_resume_skips_processed(monkeypatch, tmp_path):
    """Pre-seed state marks first transcript processed -> extract only runs on 2nd."""
    projects = tmp_path / "projects"
    transcripts = _seed_transcripts(projects, 2)

    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "processed": [str(transcripts[0])],
                "candidates": [
                    {"kind": "entity", "name": "Prior", "type": "project"}
                ],
            }
        ),
        encoding="utf-8",
    )

    extracted = []

    def fake_extract(path, model="haiku-4-5", chunk_tokens=50_000):
        extracted.append(path)
        return [{"kind": "entity", "name": f"E{path.stem}", "type": "project"}]

    reconcile_inputs = []

    def fake_reconcile(candidates, **kwargs):
        reconcile_inputs.append(list(candidates))
        return {"entities": [], "relationships": []}

    monkeypatch.setattr(bootstrap, "extract_from_transcript", fake_extract)
    monkeypatch.setattr(bootstrap, "reconcile_candidates", fake_reconcile)

    run_bootstrap(
        projects,
        days=None,
        state_path=state_path,
        notify_fn=lambda msg: None,
        resume=True,
    )

    assert extracted == [transcripts[1]], (
        f"expected only the 2nd transcript to be extracted, got {extracted}"
    )
    # Reconcile saw the prior candidate plus the new one.
    assert reconcile_inputs, "reconcile_candidates was never called"
    seen_names = [c.get("name") for c in reconcile_inputs[0]]
    assert "Prior" in seen_names
    assert any(n and n.startswith("E") for n in seen_names)


def test_run_bootstrap_state_saved_between_transcripts(monkeypatch, tmp_path):
    """Exception on 2nd extract -> state file has 1st transcript's progress."""
    projects = tmp_path / "projects"
    transcripts = _seed_transcripts(projects, 2)

    calls = {"n": 0}

    def fake_extract(path, model="haiku-4-5", chunk_tokens=50_000):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"kind": "entity", "name": "first", "type": "project"}]
        raise RuntimeError("boom on 2nd transcript")

    monkeypatch.setattr(bootstrap, "extract_from_transcript", fake_extract)
    # Reconcile must NOT run because we crash before it.
    monkeypatch.setattr(
        bootstrap,
        "reconcile_candidates",
        lambda *a, **kw: pytest.fail("reconcile should not be called"),
    )

    state_path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="boom on 2nd"):
        run_bootstrap(
            projects,
            days=None,
            state_path=state_path,
            notify_fn=lambda msg: None,
        )

    # State persisted between transcripts.
    assert state_path.exists()
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert loaded["processed"] == [str(transcripts[0])]
    assert loaded["candidates"] == [
        {"kind": "entity", "name": "first", "type": "project"}
    ]


def test_run_bootstrap_notify_at_50_percent(monkeypatch, tmp_path):
    """With 4 transcripts, a 50% notification fires once after the 2nd."""
    projects = tmp_path / "projects"
    _seed_transcripts(projects, 4)

    monkeypatch.setattr(
        bootstrap,
        "extract_from_transcript",
        lambda p, **kw: [{"kind": "entity", "name": p.stem, "type": "project"}],
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_candidates",
        lambda *a, **kw: {"entities": [], "relationships": []},
    )

    notifications = []
    run_bootstrap(
        projects,
        days=None,
        state_path=tmp_path / "state.json",
        notify_fn=notifications.append,
    )

    assert any("50%" in m for m in notifications), (
        f"expected a 50% notification, got {notifications}"
    )
    assert sum(1 for m in notifications if "50%" in m) == 1, (
        "50% notification should fire exactly once"
    )
    assert any("done" in m.lower() for m in notifications)


def test_run_bootstrap_notify_on_done(monkeypatch, tmp_path):
    """Final notification fires after reconcile, message contains 'done'."""
    projects = tmp_path / "projects"
    _seed_transcripts(projects, 1)

    monkeypatch.setattr(
        bootstrap,
        "extract_from_transcript",
        lambda p, **kw: [],
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_candidates",
        lambda *a, **kw: {"entities": [], "relationships": []},
    )

    notifications = []
    run_bootstrap(
        projects,
        days=None,
        state_path=tmp_path / "state.json",
        notify_fn=notifications.append,
    )

    assert notifications, "at least a 'done' notification should fire"
    assert "done" in notifications[-1].lower()


def test_run_bootstrap_zero_transcripts(monkeypatch, tmp_path):
    """Empty projects dir -> empty result, no notifications, no state file."""
    projects = tmp_path / "projects"
    projects.mkdir()

    def fail_extract(*a, **kw):
        pytest.fail("extract must not run on zero transcripts")

    def fail_reconcile(*a, **kw):
        pytest.fail("reconcile must not run on zero transcripts")

    monkeypatch.setattr(bootstrap, "extract_from_transcript", fail_extract)
    monkeypatch.setattr(bootstrap, "reconcile_candidates", fail_reconcile)

    notifications = []
    state_path = tmp_path / "state.json"
    result = run_bootstrap(
        projects,
        days=None,
        state_path=state_path,
        notify_fn=notifications.append,
    )

    assert result == {"entities": [], "relationships": []}
    assert notifications == []
    assert not state_path.exists()


def test_run_bootstrap_state_survives_malformed_json(monkeypatch, tmp_path):
    """Garbage state file -> start clean, don't crash."""
    projects = tmp_path / "projects"
    transcripts = _seed_transcripts(projects, 1)

    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    calls = []

    def fake_extract(path, **kw):
        calls.append(path)
        return [{"kind": "entity", "name": "X", "type": "project"}]

    monkeypatch.setattr(bootstrap, "extract_from_transcript", fake_extract)
    monkeypatch.setattr(
        bootstrap,
        "reconcile_candidates",
        lambda *a, **kw: {"entities": [], "relationships": []},
    )

    result = run_bootstrap(
        projects,
        days=None,
        state_path=state_path,
        notify_fn=lambda m: None,
    )

    assert result == {"entities": [], "relationships": []}
    # Should have re-processed from scratch despite the garbage state.
    assert calls == [transcripts[0]]


def test_run_bootstrap_uses_default_state_path_under_home(
    monkeypatch, tmp_path
):
    """With no ``state_path``, default is ``$HOME/.claude/.agam-bootstrap-state.json``."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    projects = tmp_path / "projects"
    _seed_transcripts(projects, 1)

    recorded = {}

    def fake_save_state(path, state):
        recorded["path"] = path
        # Still write so delete-on-success works.
        path.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(bootstrap, "_save_state", fake_save_state)
    monkeypatch.setattr(
        bootstrap,
        "extract_from_transcript",
        lambda p, **kw: [],
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_candidates",
        lambda *a, **kw: {"entities": [], "relationships": []},
    )

    run_bootstrap(projects, days=None, notify_fn=lambda m: None)

    assert "path" in recorded, "_save_state should have been invoked"
    assert str(recorded["path"]).endswith(
        "/.claude/.agam-bootstrap-state.json"
    )
    assert str(home) in str(recorded["path"])


# ---- SIGINT handler ------------------------------------------------------


def test_install_sigint_handler_invokes_flush_and_returns_previous():
    """Installed handler calls flush_fn; returns the prior handler for restore."""
    prior = signal.getsignal(signal.SIGINT)
    try:
        flushed = []
        returned = _install_sigint_handler(lambda: flushed.append(True))
        # The previous handler is whatever was installed before us.
        assert returned == prior

        installed = signal.getsignal(signal.SIGINT)
        assert callable(installed)
        # Invoke the handler directly with a fake frame; it must call flush_fn
        # and then re-raise via default SIGINT behavior. We catch KeyboardInterrupt.
        with pytest.raises((KeyboardInterrupt, SystemExit)):
            installed(signal.SIGINT, None)
        assert flushed == [True]
    finally:
        signal.signal(signal.SIGINT, prior)

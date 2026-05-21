"""Tests for agam.bootstrap Haiku extraction pipeline (Task 20).

All tests mock ``_run_claude`` or ``subprocess.run``. No real ``docker exec``,
no real ``claude -p`` invocation, no network. The integration test inside
``agam-oss-test`` is optional and skipped unless a container is wired up.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from agam import bootstrap
from agam.bootstrap import (
    _run_claude,
    extract_all,
    extract_from_transcript,
)


# ---- bootstrap layer's invocation behavior ------------------------------
# (Container discovery + raw subprocess.run shape are tested in
#  tests/test_invoker.py against the Invoker cascade. Here we only assert
#  the bootstrap-level contract: _run_claude delegates to the cascade and
#  fails actionably when nothing's available.)


def test_run_claude_errors_when_no_invoker_available(monkeypatch):
    """No host claude, no docker, no container -> SystemExit with detail."""
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    monkeypatch.setattr("shutil.which", lambda cmd: None)  # no claude, no docker

    with pytest.raises(SystemExit) as ei:
        _run_claude("hi", "haiku-4-5", timeout=30)
    msg = str(ei.value).lower()
    assert "no claude invoker" in msg
    assert "host" in msg and "container" in msg


def test_run_claude_delegates_to_invoker_run(monkeypatch):
    """When an invoker is healthy, _run_claude returns its stdout."""

    class _StubInvoker:
        name = "stub"

        def run(self, prompt, model, *, timeout):
            assert prompt == "hello"
            assert model == "haiku-4-5"
            assert timeout == 42
            return "STUB-OUTPUT"

    monkeypatch.setattr("agam.invoker.resolve_invoker", lambda: _StubInvoker())
    out = _run_claude("hello", "haiku-4-5", timeout=42)
    assert out == "STUB-OUTPUT"


def test_discover_container_shim_returns_name_via_cascade(monkeypatch):
    """Legacy ``_discover_container`` shim still works for tests that
    monkeypatch it, returning the container name picked by ContainerInvoker."""
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    monkeypatch.setenv("AGAM_CONTAINER_PATTERN", "claude-code")
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/docker" if cmd == "docker" else None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            stdout="devbox claude-code:latest\n", stderr="", returncode=0
        ),
    )
    assert bootstrap._discover_container() == "devbox"


def test_discover_container_shim_returns_none_when_no_match(monkeypatch):
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/docker" if cmd == "docker" else None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            stdout="nginx nginx:latest\n", stderr="", returncode=0
        ),
    )
    assert bootstrap._discover_container() is None


# ---- extract_from_transcript --------------------------------------------


def _stream_json_result(payload: dict) -> str:
    """Build a fake claude -p stream-json stdout with a terminal result line."""
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": "thinking"}}),
        json.dumps({"type": "result", "result": json.dumps(payload)}),
    ]
    return "\n".join(lines) + "\n"


def test_extract_uses_docker_exec(tmp_path, monkeypatch):
    """``extract_from_transcript`` passes the requested model to _run_claude."""

    calls = []

    def fake_run_claude(prompt, model, *, timeout=300):
        calls.append((model, len(prompt)))
        return _stream_json_result({"entities": [], "relationships": []})

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    t = tmp_path / "s1.jsonl"
    t.write_text('{"role":"user","content":"hello"}\n')

    result = extract_from_transcript(t, model="haiku-4-5")

    assert calls, "expected at least one _run_claude invocation"
    assert calls[0][0] == "haiku-4-5"
    assert isinstance(result, list)


def test_extract_parses_entities_and_relationships(tmp_path, monkeypatch):
    """Candidates returned by the model flow back to the caller."""

    payload = {
        "entities": [{"name": "Agam", "type": "project"}],
        "relationships": [
            {"source": "Agam", "relation": "uses", "target": "SQLite"}
        ],
    }

    monkeypatch.setattr(
        bootstrap,
        "_run_claude",
        lambda prompt, model, *, timeout=300: _stream_json_result(payload),
    )

    t = tmp_path / "s1.jsonl"
    t.write_text('{"role":"user","content":"hello"}\n')

    result = extract_from_transcript(t, model="haiku-4-5")

    # Two candidates: one entity + one relationship.
    assert len(result) == 2
    kinds = {c["kind"] for c in result}
    assert kinds == {"entity", "relationship"}


def test_extract_chunks_large_transcript(tmp_path, monkeypatch):
    """Transcripts over ``chunk_tokens`` trigger multiple model calls."""

    calls = []

    def fake_run_claude(prompt, model, *, timeout=300):
        calls.append(len(prompt))
        return _stream_json_result({"entities": [], "relationships": []})

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    # chunk_tokens=100 means 100*4 = 400 chars per chunk.
    # Build valid JSONL that is also ~1200 chars so chunking kicks in.
    t = tmp_path / "big.jsonl"
    lines = []
    filler = "x" * 100
    for i in range(10):
        lines.append(json.dumps({"role": "user", "content": f"{filler}{i}"}))
    t.write_text("\n".join(lines) + "\n")
    assert t.stat().st_size > 1000

    extract_from_transcript(t, model="haiku-4-5", chunk_tokens=100)

    assert len(calls) >= 2, f"expected chunking, got {len(calls)} calls"


def test_extract_skips_malformed_jsonl(tmp_path, monkeypatch, capsys):
    """A truncated JSONL line is logged and skipped, extraction continues."""

    monkeypatch.setattr(
        bootstrap,
        "_run_claude",
        lambda prompt, model, *, timeout=300: _stream_json_result(
            {"entities": [], "relationships": []}
        ),
    )

    t = tmp_path / "mixed.jsonl"
    # One valid, one truncated.
    t.write_text('{"role":"user","content":"ok"}\n{"role":"user","content":\n')

    result = extract_from_transcript(t, model="haiku-4-5")

    assert isinstance(result, list)
    err = capsys.readouterr().err
    assert "malformed" in err.lower() or "skip" in err.lower()


def test_extract_zero_candidates_returns_empty(tmp_path, monkeypatch):
    """Model returning no entities is not an error."""

    monkeypatch.setattr(
        bootstrap,
        "_run_claude",
        lambda prompt, model, *, timeout=300: _stream_json_result(
            {"entities": [], "relationships": []}
        ),
    )

    t = tmp_path / "empty.jsonl"
    t.write_text('{"role":"user","content":"hi"}\n')

    assert extract_from_transcript(t, model="haiku-4-5") == []


def test_extract_handles_malformed_stream_json(tmp_path, monkeypatch):
    """Non-JSON lines in stream-json output don't crash the parser."""

    def fake_run_claude(prompt, model, *, timeout=300):
        # Mix garbage with a valid result line.
        return (
            "not-json\n"
            + json.dumps(
                {
                    "type": "result",
                    "result": json.dumps(
                        {"entities": [{"name": "X", "type": "thing"}]}
                    ),
                }
            )
            + "\n"
        )

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    t = tmp_path / "s.jsonl"
    t.write_text('{"role":"user","content":"hi"}\n')

    result = extract_from_transcript(t, model="haiku-4-5")

    assert len(result) == 1
    assert result[0]["kind"] == "entity"
    assert result[0]["name"] == "X"


# ---- extract_all ---------------------------------------------------------


def test_extract_all_runs_parallel(tmp_path, monkeypatch):
    """Each transcript in the input list produces at least one model call."""

    calls = []

    def fake_run_claude(prompt, model, *, timeout=300):
        calls.append(model)
        return _stream_json_result(
            {
                "entities": [{"name": f"E{len(calls)}", "type": "x"}],
                "relationships": [],
            }
        )

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    transcripts = []
    for i in range(3):
        p = tmp_path / f"s{i}.jsonl"
        p.write_text(f'{{"role":"user","content":"m{i}"}}\n')
        transcripts.append(p)

    result = extract_all(transcripts, model="haiku-4-5", max_workers=3)

    assert len(calls) == 3
    # Each transcript contributed one entity.
    assert len(result) == 3
    assert {c["name"] for c in result} == {"E1", "E2", "E3"}


# ---- integration (optional, container-gated) ----------------------------


@pytest.mark.skipif(
    os.environ.get("AGAM_OSS_LIVE") != "1",
    reason="live integration gated by AGAM_OSS_LIVE=1 (run only inside agam-oss-test)",
)
def test_integration_live_container(tmp_path):  # pragma: no cover
    """Smoke test: exercise the real ``docker exec claude -p`` path.

    Intentionally small prompt, ``haiku-4-5`` model. Skipped by default --
    only runs inside ``agam-oss-test`` with ``AGAM_OSS_LIVE=1``.
    """
    t = tmp_path / "tiny.jsonl"
    t.write_text('{"role":"user","content":"say nothing"}\n')
    result = extract_from_transcript(t, model="haiku-4-5")
    assert isinstance(result, list)

"""Tests for agam.bootstrap Sonnet reconciliation pass (Task 21).

All tests mock ``_run_claude``. No real ``docker exec``, no network, no
writes to the real ``~/.claude/``. The double-failure path writes to a
test-owned directory via a monkeypatched ``HOME`` or via the injectable
``candidates_path`` argument.
"""

from __future__ import annotations

import json

import pytest

from agam import bootstrap
from agam.bootstrap import (
    _dedupe_entities,
    _dedupe_relationships,
    _parse_reconciliation_response,
    reconcile_candidates,
)


# ---- helpers -------------------------------------------------------------


def _stream_json_result(payload: dict) -> str:
    """Build a fake claude -p stream-json stdout with a terminal result line."""
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result", "result": json.dumps(payload)}),
    ]
    return "\n".join(lines) + "\n"


# ---- client-side dedup ---------------------------------------------------


def test_dedupe_entities_merges_properties():
    """Two variants of the same entity merge into one via property union."""
    entities = [
        {"name": "Python", "type": "language", "props": {"year": 1991}},
        {"name": "python", "type": "language", "props": {"creator": "Guido"}},
    ]
    result = _dedupe_entities(entities)
    assert len(result) == 1
    assert result[0]["props"] == {"year": 1991, "creator": "Guido"}


def test_dedupe_entities_is_case_insensitive():
    """Case variants of ``name`` collapse into a single entity."""
    entities = [
        {"name": "Agam"},
        {"name": "agam"},
        {"name": "AGAM"},
    ]
    result = _dedupe_entities(entities)
    assert len(result) == 1


def test_dedupe_relationships_by_tuple():
    """Relationships dedupe by exact ``(source, relation, target)``."""
    rels = [
        {"source": "A", "relation": "uses", "target": "B"},
        {"source": "A", "relation": "uses", "target": "B"},
        {"source": "A", "relation": "calls", "target": "B"},
    ]
    result = _dedupe_relationships(rels)
    assert len(result) == 2


# ---- reconcile_candidates ------------------------------------------------


def test_reconcile_dedupes_by_entity_name(monkeypatch, tmp_path):
    """Three candidate batches mentioning ``Python`` collapse to one entity."""
    payload = {
        "entities": [
            {
                "name": "Python",
                "type": "language",
                "description": "Merged description",
            }
        ],
        "relationships": [],
    }

    def fake_run_claude(prompt, model, *, timeout=600):
        return _stream_json_result(payload)

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    candidates = [
        {"kind": "entity", "name": "Python", "type": "language"},
        {"kind": "entity", "name": "python", "description": "interpreter"},
        {"kind": "entity", "name": "PYTHON", "type": "language"},
    ]
    result = reconcile_candidates(
        candidates, candidates_path=tmp_path / "save.json"
    )
    assert isinstance(result, dict)
    assert list(result.keys()) == ["entities", "relationships"]
    assert len(result["entities"]) == 1
    assert result["entities"][0]["name"] == "Python"


def test_reconcile_retries_on_invalid_json(monkeypatch, tmp_path):
    """First invocation returns garbage, second returns valid JSON."""
    calls = []

    def fake_run_claude(prompt, model, *, timeout=600):
        calls.append(prompt)
        if len(calls) == 1:
            return "not-json-at-all\n"
        return _stream_json_result({"entities": [], "relationships": []})

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    result = reconcile_candidates(
        [{"kind": "entity", "name": "X"}],
        candidates_path=tmp_path / "save.json",
    )
    assert len(calls) == 2
    # Second prompt should include a stricter JSON-only suffix.
    assert "ONLY VALID JSON" in calls[1]
    assert result == {"entities": [], "relationships": []}


def test_reconcile_saves_candidates_on_double_failure(tmp_path, monkeypatch):
    """Two back-to-back JSON failures write candidates and SystemExit."""

    def fake_run_claude(prompt, model, *, timeout=600):
        return "garbage\n"

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    save_path = tmp_path / "candidates.json"
    candidates = [
        {"kind": "entity", "name": "Alpha"},
        {"kind": "relationship", "source": "A", "relation": "uses", "target": "B"},
    ]

    with pytest.raises(SystemExit) as ei:
        reconcile_candidates(candidates, candidates_path=save_path)

    assert "reconciliation failed" in str(ei.value).lower()
    assert str(save_path) in str(ei.value)
    assert save_path.exists()
    saved = json.loads(save_path.read_text())
    # Save the deduped, regrouped candidates so a later pass can resume.
    assert "entities" in saved and "relationships" in saved


def test_reconcile_client_side_dedup_before_prompt(monkeypatch, tmp_path):
    """100 duplicates should collapse to 1 before hitting the model."""
    captured = []

    def fake_run_claude(prompt, model, *, timeout=600):
        captured.append(prompt)
        return _stream_json_result({"entities": [], "relationships": []})

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    candidates = [
        {"kind": "entity", "name": "Python", "type": "language"}
    ] * 100
    reconcile_candidates(
        candidates, candidates_path=tmp_path / "save.json"
    )
    assert captured, "expected one _run_claude call"
    # Massive dedup: the literal "Python" should appear far fewer than 100
    # times. Headers/schema text may mention it once or twice, but not 100.
    assert captured[0].count("Python") < 10


def test_reconcile_regroups_flat_candidates(monkeypatch, tmp_path):
    """Flat ``[{kind: entity, ...}, {kind: relationship, ...}]`` is regrouped."""
    captured = []

    def fake_run_claude(prompt, model, *, timeout=600):
        captured.append(prompt)
        return _stream_json_result({"entities": [], "relationships": []})

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    candidates = [
        {"kind": "entity", "name": "Agam", "type": "project"},
        {
            "kind": "relationship",
            "source": "Agam",
            "relation": "uses",
            "target": "SQLite",
        },
    ]
    reconcile_candidates(candidates, candidates_path=tmp_path / "save.json")
    prompt = captured[0]
    # The prompt should embed the nested shape the reconciliation model expects.
    assert '"entities"' in prompt
    assert '"relationships"' in prompt
    # And the entity payload should be discoverable inside the prompt.
    assert "Agam" in prompt
    assert "SQLite" in prompt


def test_reconcile_uses_sonnet_and_long_timeout(monkeypatch, tmp_path):
    """Reconciliation should call sonnet with a 600s timeout."""
    captured = {}

    def fake_run_claude(prompt, model, *, timeout=600):
        captured["model"] = model
        captured["timeout"] = timeout
        return _stream_json_result({"entities": [], "relationships": []})

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    reconcile_candidates(
        [{"kind": "entity", "name": "X"}],
        candidates_path=tmp_path / "save.json",
    )
    assert captured["model"] == "sonnet-4-6"
    assert captured["timeout"] == 600


def test_reconcile_default_candidates_path_honours_home(
    monkeypatch, tmp_path
):
    """Default save path lives under ``$HOME/.claude/`` -- no real HOME writes."""

    # Point HOME at tmp_path so os.path.expanduser resolves there.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()

    def fake_run_claude(prompt, model, *, timeout=600):
        return "garbage\n"

    monkeypatch.setattr(bootstrap, "_run_claude", fake_run_claude)

    with pytest.raises(SystemExit) as ei:
        reconcile_candidates([{"kind": "entity", "name": "X"}])

    expected = tmp_path / ".claude" / ".agam-bootstrap-candidates.json"
    assert expected.exists()
    assert str(expected) in str(ei.value)


# ---- _parse_reconciliation_response --------------------------------------


def test_parse_reconciliation_response_happy_path():
    """Stream-json with a valid result object round-trips back."""
    payload = {
        "entities": [{"name": "A"}],
        "relationships": [{"source": "A", "relation": "r", "target": "B"}],
    }
    raw = _stream_json_result(payload)
    assert _parse_reconciliation_response(raw) == payload


def test_parse_reconciliation_response_raises_on_garbage():
    """Unparseable output raises ``json.JSONDecodeError`` for retry to catch."""
    with pytest.raises(json.JSONDecodeError):
        _parse_reconciliation_response("not json at all\n")

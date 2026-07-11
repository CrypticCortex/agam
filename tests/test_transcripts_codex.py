"""Tests for defensive Codex rollout ingestion and normalization."""

import json
from pathlib import Path

from agam import transcripts


FIXTURE = Path(__file__).parent / "fixtures" / "codex" / "rollout_with_edits.jsonl"


def _write_events(path: Path, events: list[dict], *, malformed: bool = False) -> Path:
    lines = [json.dumps(event) for event in events]
    if malformed:
        lines.insert(1, '{"partial":')
    path.write_text("\n".join(lines) + "\n")
    return path


def test_canonical_user_events_are_not_double_counted():
    assert transcripts.codex_user_turns(FIXTURE) == 6


def test_extract_text_prefers_ui_events_and_drops_private_records():
    text = transcripts.codex_extract_text(FIXTURE)
    assert text.count("Add retry support") == 1
    assert text.count("Implemented retry support") == 1
    assert "opaque-private-data" not in text
    assert "not retained" not in text


def test_real_work_gate_detects_patch_events():
    assert transcripts.codex_has_edit(FIXTURE) is True
    assert transcripts.codex_should_enqueue(FIXTURE) is True


def test_ambiguous_response_user_material_is_private_and_not_enqueueable(tmp_path):
    events = []
    for i in range(6):
        events.append({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": f"INTERNAL environment/developer/subagent task {i}: fixed secret",
                }],
            },
        })
    events.extend([
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "const result = await tools.apply_patch(\"*** Begin Patch\")",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Built the requested change."}],
            },
        },
    ])
    path = _write_events(tmp_path / "fallback.jsonl", events, malformed=True)

    assert transcripts.codex_user_turns(path) == 0
    assert transcripts.codex_has_edit(path) is True
    assert transcripts.codex_should_enqueue(path) is False
    assert "Built the requested change" in transcripts.codex_extract_text(path)
    assert "INTERNAL environment/developer/subagent" not in transcripts.codex_extract_text(path)

    snapshot = tmp_path / "fallback-snapshot.jsonl"
    transcripts.codex_write_snapshot(path, snapshot)
    assert "INTERNAL environment/developer/subagent" not in snapshot.read_text()
    assert "Built the requested change" in snapshot.read_text()


def test_failed_patch_is_not_edit_evidence(tmp_path):
    events = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "fixed"}}
        for _ in range(6)
    ]
    events.append({
        "type": "event_msg",
        "payload": {
            "type": "patch_apply_end",
            "success": False,
            "status": "failed",
            "changes": {"/Users/example/coding/widget/x.py": {}},
        },
    })
    path = _write_events(tmp_path / "failed.jsonl", events)
    assert transcripts.codex_has_edit(path) is False
    assert transcripts.codex_should_enqueue(path) is False


def test_snapshot_is_compact_claude_like_and_atomic(tmp_path):
    destination = tmp_path / "snapshots" / "codex.jsonl"
    count = transcripts.codex_write_snapshot(FIXTURE, destination)
    rows = [json.loads(line) for line in destination.read_text().splitlines()]

    assert count == len(rows) == 12
    assert sum(row["type"] == "user" for row in rows) >= 6
    assert all(set(row) <= {"type", "timestamp", "message"} for row in rows)
    assert all(row["message"]["role"] in {"user", "assistant"} for row in rows)

    raw = destination.read_text()
    assert "opaque-private-data" not in raw
    assert "base_instructions" not in raw
    assert raw.count("Add retry support to the client") == 1
    assert "/Users/example/coding/widget/src/client.py" in raw
    assert '"name": "apply_patch"' in raw


def test_missing_or_malformed_rollout_is_a_safe_noop(tmp_path):
    missing = tmp_path / "missing.jsonl"
    malformed = tmp_path / "bad.jsonl"
    malformed.write_text("not json\n[]\nnull\n")

    for path in (missing, malformed):
        assert transcripts.codex_user_turns(path) == 0
        assert transcripts.codex_extract_text(path) == ""
        assert transcripts.codex_has_edit(path) is False
        assert transcripts.codex_should_enqueue(path) is False

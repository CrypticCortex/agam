"""Tests for the Cursor transcript parser."""

from pathlib import Path

import pytest

from agam import transcripts

FIXTURES = Path(__file__).parent / "fixtures" / "cursor"


@pytest.fixture
def text_only():
    return FIXTURES / "transcript_text_only.jsonl"


@pytest.fixture
def with_tools():
    return FIXTURES / "transcript_with_tools.jsonl"


def test_user_turns_text_only(text_only):
    assert transcripts.cursor_user_turns(text_only) == 2


def test_user_turns_with_tools(with_tools):
    assert transcripts.cursor_user_turns(with_tools) == 6


def test_extract_text_nonempty(text_only):
    text = transcripts.cursor_extract_text(text_only)
    assert "What can I help you with" in text


def test_should_enqueue_true_for_real_work(with_tools):
    assert transcripts.cursor_should_enqueue(with_tools) is True


def test_should_enqueue_false_for_trivial(text_only):
    assert transcripts.cursor_should_enqueue(text_only) is False


def test_should_enqueue_false_without_edit_evidence(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = []
    for _ in range(8):
        lines.append('{"role":"user","message":{"content":[{"type":"text","text":"shipped it"}]}}')
    p.write_text("\n".join(lines) + "\n")
    # 8 turns + signal keyword, but no edit evidence -> not enqueued.
    assert transcripts.cursor_should_enqueue(p) is False

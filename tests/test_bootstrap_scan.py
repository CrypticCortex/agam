"""Tests for agam.bootstrap transcript scanner + cost estimator (Task 19).

All tests use tmp_path fixtures seeded with synthetic JSONL. The real
~/.claude/projects/ tree must never be touched.
"""

from __future__ import annotations

import os
import time

import pytest

from agam.bootstrap import estimate_cost, scan_transcripts


# ---- scan_transcripts ----------------------------------------------------


def test_scan_filters_by_days(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    old = projects / "old.jsonl"
    old.write_text("{}\n")
    os.utime(old, (time.time() - 60 * 86400, time.time() - 60 * 86400))
    new = projects / "new.jsonl"
    new.write_text("{}\n")

    result = scan_transcripts(projects, days=30)

    assert len(result) == 1
    assert result[0].name == "new.jsonl"


def test_scan_returns_empty_for_missing_dir(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"

    result = scan_transcripts(missing, days=30)

    assert result == []
    # Warning should be logged to stderr.
    captured = capsys.readouterr()
    assert captured.err  # some diagnostic text emitted


def test_scan_handles_nested_projects(tmp_path):
    projects = tmp_path / "projects"
    (projects / "proj-a").mkdir(parents=True)
    (projects / "proj-b").mkdir(parents=True)
    (projects / "proj-a" / "s1.jsonl").write_text("{}\n")
    (projects / "proj-b" / "s2.jsonl").write_text("{}\n")

    result = scan_transcripts(projects, days=30)

    names = {p.name for p in result}
    assert names == {"s1.jsonl", "s2.jsonl"}


def test_scan_skips_empty_files(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    empty = projects / "empty.jsonl"
    empty.touch()  # 0 bytes
    populated = projects / "populated.jsonl"
    populated.write_text("{}\n")

    result = scan_transcripts(projects, days=30)

    assert len(result) == 1
    assert result[0].name == "populated.jsonl"


def test_scan_days_none_includes_all(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    ancient = projects / "ancient.jsonl"
    ancient.write_text("{}\n")
    os.utime(ancient, (time.time() - 365 * 86400, time.time() - 365 * 86400))
    recent = projects / "recent.jsonl"
    recent.write_text("{}\n")

    result = scan_transcripts(projects, days=None)

    assert len(result) == 2


def test_scan_returns_sorted(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    for name in ("c.jsonl", "a.jsonl", "b.jsonl"):
        (projects / name).write_text("{}\n")

    result = scan_transcripts(projects, days=30)

    assert [p.name for p in result] == sorted(p.name for p in result)
    assert result == sorted(result)


def test_scan_skips_non_jsonl(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "notes.txt").write_text("hi\n")
    (projects / "session.jsonl").write_text("{}\n")
    (projects / "log.json").write_text("{}\n")

    result = scan_transcripts(projects, days=30)

    assert len(result) == 1
    assert result[0].name == "session.jsonl"


# ---- estimate_cost -------------------------------------------------------


def test_cost_estimator_multiplies_tokens():
    cost = estimate_cost(
        total_tokens=1_000_000,
        haiku_rate=0.80,
        sonnet_input_rate=3.00,
        reconcile_fraction=0.1,
    )
    # haiku: 1M * $0.80 = $0.80; sonnet reconcile: 0.1M * $3 = $0.30; total ~$1.10
    assert 1.0 < cost < 1.2


def test_cost_estimator_zero_tokens():
    assert estimate_cost(total_tokens=0) == 0.0


def test_cost_estimator_default_rates():
    explicit = estimate_cost(
        total_tokens=5_000_000,
        haiku_rate=0.80,
        sonnet_input_rate=3.00,
        reconcile_fraction=0.1,
    )
    default = estimate_cost(total_tokens=5_000_000)
    assert explicit == pytest.approx(default)

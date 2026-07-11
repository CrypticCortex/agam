"""Focused regressions for the agent-neutral TUI brain display.

Every filesystem probe is redirected to ``tmp_path``.  In particular, these
tests must not infer wiring from whatever happens to be installed under the
developer's real home directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Import the TUI with every operational path isolated to ``tmp_path``."""
    monkeypatch.setenv("HOME", str(tmp_path))

    from agam import tui as module

    data_home = tmp_path / ".agam"
    monkeypatch.setattr(module, "HOME", tmp_path)
    monkeypatch.setattr(module, "DATA_HOME", data_home)
    monkeypatch.setattr(module, "AGAM", data_home)
    monkeypatch.setattr(module, "KG_DB", data_home / "knowledge" / "graph.db")
    monkeypatch.setattr(module, "WORKLOG", data_home / "work-log.md")
    monkeypatch.setattr(module, "HOOKS_DIR", data_home / "hooks")
    monkeypatch.setattr(module, "TOOLS_DIR", data_home / "tools")
    monkeypatch.setattr(module, "QUEUE_PATH", data_home / ".pending-closes.jsonl")
    monkeypatch.setattr(module, "ARCHIVE_PATH", data_home / ".pending-closes.archive.jsonl")
    monkeypatch.setattr(module, "NEW_QUEUE_DIR", data_home / "queue")
    monkeypatch.setattr(module, "PROCESSED", data_home / ".processed-sessions.jsonl")
    monkeypatch.setattr(module, "WLOG", data_home / ".watchdog-log")
    monkeypatch.setattr(module, "SHARED_PROCESSED", data_home / "processed")
    monkeypatch.setattr(module, "SHARED_ERRORS", data_home / "queue-errors")
    monkeypatch.setattr(module, "SHARED_WLOG", data_home / "logs" / "watchdog.log")
    monkeypatch.setattr(module, "CURSOR_PROCESSED", data_home / "processed")
    monkeypatch.setattr(module, "CURSOR_ERRORS", data_home / "queue-errors")
    monkeypatch.setattr(module, "CURSOR_WLOG", data_home / "logs" / "watchdog.log")
    return module


def _write(path: Path, body: str = "# installed by Agam\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_data_home_defaults_to_neutral_agam_home(tui, monkeypatch, tmp_path):
    monkeypatch.delenv("AGAM_DATA_HOME", raising=False)
    assert tui._data_home(tmp_path) == tmp_path / ".agam"

    override = tmp_path / "shared-brain"
    monkeypatch.setenv("AGAM_DATA_HOME", str(override))
    assert tui._data_home(tmp_path) == override


def test_identity_and_graph_fall_back_to_legacy_until_migrated(tui, tmp_path):
    shared = tmp_path / ".agam"
    (shared / "queue").mkdir(parents=True)
    legacy_identity = tmp_path / ".claude" / "agam"
    legacy_identity.mkdir(parents=True)
    legacy_graph = tmp_path / ".claude" / "knowledge" / "graph.db"
    legacy_graph.parent.mkdir(parents=True)
    legacy_graph.write_bytes(b"legacy graph placeholder")

    assert tui._identity_home(tmp_path, shared) == legacy_identity
    assert tui._knowledge_db(tmp_path, shared) == legacy_graph

    (shared / "AGAM.md").write_text("# shared\n")
    shared_graph = shared / "knowledge" / "graph.db"
    shared_graph.parent.mkdir(parents=True)
    shared_graph.write_bytes(b"shared graph placeholder")

    assert tui._identity_home(tmp_path, shared) == shared
    assert tui._knowledge_db(tmp_path, shared) == shared_graph


def test_wired_agents_only_reports_agam_hooks_in_stable_order(tui, tmp_path):
    # Generic agent configuration is not evidence that Agam is wired into it.
    _write(
        tmp_path / ".claude" / "settings.json",
        json.dumps({"hooks": {"Stop": [{"command": "/tmp/user-hook.py"}]}}),
    )
    _write(
        tmp_path / ".cursor" / "hooks.json",
        json.dumps({"version": 1, "hooks": {"stop": [{"command": "/tmp/user-hook.py"}]}}),
    )
    _write(
        tmp_path / ".codex" / "hooks.json",
        json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "/tmp/user-hook.py"}]}]}}),
    )
    assert tui._wired_agents(tmp_path) == []

    # These are the canonical hook files installed by each Agam target.
    _write(tmp_path / ".codex" / "hooks" / "agam" / "codex_stop.py")
    _write(tmp_path / ".cursor" / "hooks" / "cursor_stop.py")
    # The local personal install still uses this legacy dash-named hook.
    _write(tmp_path / ".claude" / "hooks" / "graph-recall.py")

    assert tui._wired_agents(tmp_path) == ["claude", "cursor", "codex"]


def test_codex_has_a_distinct_non_grey_agent_style(tui):
    styles = {agent: tui._agent_style(agent) for agent in ("claude", "cursor", "codex")}

    assert all(isinstance(style, str) for style in styles.values())
    assert len(set(styles.values())) == 3
    assert "grey" not in styles["codex"].lower()


def test_brain_bar_renders_a_travelling_codex_wire(tui):
    bar = tui.BrainBar()
    bar._agents = ["codex"]
    bar._frame = 0

    first = bar._agent_row(5)
    assert first is not None
    assert first.plain.startswith("codex ")
    assert "●" in first.plain
    assert first.plain.endswith("▸ ")

    bar._frame = 2
    second = bar._agent_row(5)
    assert second is not None
    assert second.plain != first.plain


def test_brain_bar_summary_includes_codex_provenance(tui):
    bar = tui.BrainBar()
    bar._agents = ["claude", "cursor", "codex"]
    bar._frame = 0
    bar._total = 19
    bar._prov = {"claude": 3, "cursor": 5, "codex": 11}

    rendered = bar._build().plain

    assert "3 minds · 19 memories" in rendered
    assert "claude 3" in rendered
    assert "cursor 5" in rendered
    assert "codex 11" in rendered


def test_overview_includes_codex_provenance(tui, monkeypatch):
    monkeypatch.setattr(tui, "_read_queue", lambda: [])
    monkeypatch.setattr(tui, "_daily_cap", lambda: 8)
    monkeypatch.setattr(tui, "_last_drain_ts", lambda: None)
    monkeypatch.setattr(tui, "_error_count", lambda: 0)
    monkeypatch.setattr(tui, "_invoker", lambda: ("host", "green"))
    monkeypatch.setattr(tui, "_draining", lambda: False)
    monkeypatch.setattr(tui, "_today_growth", lambda: 0)
    monkeypatch.setattr(
        tui,
        "_provenance_counts",
        lambda: [("codex", 11), ("cursor", 5), ("claude", 3)],
    )
    monkeypatch.setattr(tui, "_kg_query", lambda *_args, **_kwargs: [(19,)])

    rendered = tui.render_overview().plain

    assert "claude 3" in rendered
    assert "cursor 5" in rendered
    assert "codex 11" in rendered


def test_invoker_is_healthy_when_only_codex_is_on_path(tui, monkeypatch):
    monkeypatch.setattr(tui, "_container_name", lambda: None)
    monkeypatch.setattr(
        tui.shutil,
        "which",
        lambda command: "/tmp/bin/codex" if command == "codex" else None,
    )

    assert tui._invoker() == ("host", "green")

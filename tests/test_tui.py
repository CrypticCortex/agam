"""Focused regressions for the agent-neutral TUI brain display.

Every filesystem probe is redirected to ``tmp_path``.  In particular, these
tests must not infer wiring from whatever happens to be installed under the
developer's real home directory.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


GUIDANCE_ID = "vault_" + "a" * 24
SOLUTIONS_ID = "vault_" + "b" * 24
CUSTOM_ID = "vault_" + "c" * 24


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
    monkeypatch.setattr(module, "SCOPES_ROOT", data_home / "knowledge" / "scopes")
    monkeypatch.setattr(
        module,
        "REGISTRY_PATH",
        data_home / "knowledge" / "scopes" / "registry.json",
    )
    from agam.vault_registry import initialize_registry

    initialize_registry(
        module.REGISTRY_PATH,
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
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
    shared_graph.parent.mkdir(parents=True, exist_ok=True)
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


def test_brain_bar_summary_includes_safe_vault_counts(tui):
    bar = tui.BrainBar()
    bar._agents = ["claude", "cursor", "codex"]
    bar._frame = 0
    bar._total = 19
    bar._vault_counts = {"Craft": 8, "Repairs": 11}

    rendered = bar._build().plain

    assert "3 minds · 19 memories" in rendered
    assert "Craft 8" in rendered
    assert "Repairs 11" in rendered


def test_overview_includes_codex_safe_vault_counts(tui, monkeypatch):
    from agam.vaults import VaultSummary

    monkeypatch.setattr(tui, "_read_queue", lambda: [])
    monkeypatch.setattr(tui, "_daily_cap", lambda: 8)
    monkeypatch.setattr(tui, "_last_drain_ts", lambda: None)
    monkeypatch.setattr(tui, "_error_count", lambda: 0)
    monkeypatch.setattr(tui, "_invoker", lambda: ("host", "green"))
    monkeypatch.setattr(tui, "_draining", lambda: False)
    class Catalog:
        def summaries(self):
            return (
                VaultSummary(GUIDANCE_ID, "v-test", 8, 0, 0, True, name="Craft"),
                VaultSummary(SOLUTIONS_ID, "v-test", 11, 0, 0, True, name="Repairs"),
                VaultSummary(CUSTOM_ID, "v-test", 40, 0, 0, False, name="Project North"),
            )

    monkeypatch.setattr(tui, "_vault_catalog", lambda: Catalog())

    rendered = tui.render_overview().plain

    assert "19 Codex-readable memories" in rendered
    assert "Craft 8" in rendered
    assert "Repairs 11" in rendered
    assert "Project North" not in rendered


def test_invoker_is_healthy_when_only_codex_is_on_path(tui, monkeypatch):
    monkeypatch.setattr(tui, "_container_name", lambda: None)
    monkeypatch.setattr(
        tui.shutil,
        "which",
        lambda command: "/tmp/bin/codex" if command == "codex" else None,
    )

    assert tui._invoker() == ("host", "green")


def test_operator_console_has_vault_and_both_queue_views(tui, monkeypatch):
    from agam.review_queue import ReviewItem
    from agam.vaults import VaultEntity, VaultSummary

    class FakeCatalog:
        def summaries(self):
            return (
                VaultSummary(GUIDANCE_ID, "v-test", 2, 0, 0, True, name="Craft"),
                VaultSummary(SOLUTIONS_ID, "v-test", 1, 0, 0, True, name="Repairs"),
                VaultSummary(CUSTOM_ID, "v-test", 4, 0, 0, False, name="Project North"),
                VaultSummary("vault_" + "d" * 24, "v-test", 3, 0, 0, False, name="Private Notes"),
            )

        def entities(self, scope, *, query="", limit=200):
            rows = {
                GUIDANCE_ID: (
                    VaultEntity(1, "verify-first", "lesson", "[GENERAL] prove it", "2026"),
                    VaultEntity(2, "focused-diffs", "lesson", "[GENERAL] stay narrow", "2026"),
                ),
                SOLUTIONS_ID: (
                    VaultEntity(1, "shared-seam", "resolution", "[GENERAL] fix seam", "2026"),
                ),
            }[scope]
            return tuple(row for row in rows if query.lower() in row.name.lower())

    class FakeReviewQueue:
        def items(self):
            return (ReviewItem("item_" + "ab" * 12, "ambiguous_scope", False, "2026"),)

    monkeypatch.setattr(tui, "_vault_catalog", lambda: FakeCatalog())
    monkeypatch.setattr(tui, "_review_queue", lambda: FakeReviewQueue())

    async def scenario():
        app = tui.AgamApp()
        async with app.run_test(size=(120, 42)) as pilot:
            tabs = app.query_one("#tabs", tui.TabbedContent)
            assert [pane.id for pane in tabs.query(tui.TabPane)] == [
                "overview",
                "vaults",
                "sessions",
                "reviews",
                "worklog",
                "activity",
                "health",
            ]
            assert app.query_one("#vault-rail", tui.DataTable).row_count == 4
            assert app.query_one("#vault-table", tui.DataTable).row_count == 2
            assert app.query_one("#review-table", tui.DataTable).row_count == 1
            await pilot.press("2")
            assert tabs.active == "vaults"

    asyncio.run(scenario())


def test_vault_filter_updates_the_selected_safe_vault(tui, monkeypatch):
    from agam.vaults import VaultEntity, VaultSummary

    class FakeCatalog:
        def summaries(self):
            return (
                VaultSummary(GUIDANCE_ID, "v-test", 2, 0, 0, True, name="Craft"),
                VaultSummary(SOLUTIONS_ID, "v-test", 0, 0, 0, True, name="Repairs"),
            )

        def entities(self, scope, *, query="", limit=200):
            rows = (
                VaultEntity(1, "verify-first", "lesson", "[GENERAL] prove it", "2026"),
                VaultEntity(2, "focused-diffs", "lesson", "[GENERAL] stay narrow", "2026"),
            )
            return tuple(row for row in rows if query.lower() in row.name.lower())

    monkeypatch.setattr(tui, "_vault_catalog", lambda: FakeCatalog())
    monkeypatch.setattr(tui, "_review_queue", lambda: None)

    async def scenario():
        app = tui.AgamApp()
        async with app.run_test(size=(90, 32)) as pilot:
            await pilot.press("2")
            field = app.query_one("#vault-filter", tui.Input)
            field.focus()
            await pilot.press("f", "o", "c", "u", "s")
            assert app.query_one("#vault-table", tui.DataTable).row_count == 1

    asyncio.run(scenario())


def test_archive_one_file_queue_row_preserves_audit_record(tui):
    tui.NEW_QUEUE_DIR.mkdir(parents=True)
    queued = tui.NEW_QUEUE_DIR / "session-1.json"
    queued.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/tmp/project",
                "context": "codex",
                "agent": "codex",
                "ts": 1,
            }
        )
    )
    row = tui._read_queue()[0]

    tui.AgamApp()._archive_queue_entry(row)

    assert not queued.exists()
    archived = [json.loads(line) for line in tui.ARCHIVE_PATH.read_text().splitlines()]
    assert archived == [
        {
            "agent": "codex",
            "context": "codex",
            "cwd": "/tmp/project",
            "session_id": "session-1",
            "transcript_path": "/tmp/transcript.jsonl",
            "ts": 1,
        }
    ]


def test_sync_one_file_queue_row_targets_exact_generation(tui, monkeypatch, tmp_path):
    calls = []
    shell = tmp_path / "agam_watchdog.sh"
    shell.write_text("#!/bin/bash\n")
    monkeypatch.setattr(tui, "WATCHDOG_SHELL", shell)
    monkeypatch.setattr(
        tui.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    entry = {
        "_queue_source": "file",
        "_queue_file": str(tui.NEW_QUEUE_DIR / "codex-generation.json"),
    }

    tui.AgamApp()._start_queue_sync(entry, 7)

    command, kwargs = calls[0]
    assert command == ["/bin/bash", str(shell)]
    assert kwargs["env"]["AGAM_QUEUE_FILE"] == "codex-generation.json"
    assert kwargs["env"]["AGAM_HOME"] == str(tui.DATA_HOME)


def test_sync_one_legacy_row_uses_its_source_index(tui, monkeypatch, tmp_path):
    calls = []
    monitor = tmp_path / "watchdog_monitor.py"
    monitor.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(tui, "WATCHDOG_MONITOR", monitor)
    monkeypatch.setattr(
        tui.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    entry = {"_queue_source": "legacy", "_queue_index": 2}

    tui.AgamApp()._start_queue_sync(entry, 7)

    assert calls[0][0] == [str(monitor), "sync", "2"]


def test_vault_view_renders_fail_closed_state_without_manifest(tui, monkeypatch):
    def unavailable():
        raise RuntimeError("no manifest")

    monkeypatch.setattr(tui, "_vault_catalog", unavailable)
    monkeypatch.setattr(tui, "_review_queue", lambda: None)

    async def scenario():
        app = tui.AgamApp()
        async with app.run_test(size=(90, 32)):
            assert app.query_one("#vault-rail", tui.DataTable).row_count == 1
            assert app.query_one("#vault-table", tui.DataTable).row_count == 1

    asyncio.run(scenario())


def test_tui_exposes_keyboard_first_vault_lifecycle_actions(tui):
    actions = {binding.action for binding in tui.AgamApp.BINDINGS}

    assert {
        "add_vault",
        "rename_vault",
        "archive_vault",
        "toggle_vault_access",
    }.issubset(actions)


def test_tui_add_callback_creates_restricted_custom_vault(tui, monkeypatch):
    from agam.vault_registry import initialize_registry, load_registry

    initialize_registry(
        tui.REGISTRY_PATH,
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    app = tui.AgamApp()
    monkeypatch.setattr(app, "_populate_vaults", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: None)

    app._on_add_vault(("Project North", "north only"))

    added = load_registry(tui.REGISTRY_PATH).vaults[-1]
    assert added.name == "Project North"
    assert added.access.value == "restricted"
    assert added.routing_hint == "north only"

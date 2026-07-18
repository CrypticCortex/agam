"""Synthetic end-to-end proof for Codex's scoped knowledge boundary.

The test uses only temporary databases and a fake Haiku runner. It never
opens a live Agam graph, identity file, transcript, or project repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from agam.agents import CodexAgent
from agam.knowledge_classifier import classify_graph
from agam.knowledge_materializer import materialize_graph
from agam.knowledge_scopes import (
    resolve_agent_capabilities,
    resolve_effective_scopes,
)
from agam.vault_registry import VaultRole, add_vault, initialize_registry


SCHEMA = Path(__file__).parents[1] / "knowledge" / "graph-schema.sql"

GENERAL_GUIDANCE = "synthetic-portable-guidance"
GENERAL_SOLUTION = "synthetic-portable-solution"
RESTRICTED_WORK = "synthetic-restricted-work"
RESTRICTED_PERSONAL = "synthetic-restricted-personal"
REVIEW_ONLY = "synthetic-review-only"
LEGACY_ONLY = "synthetic-legacy-only"


def _create_graph(path: Path, names: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO entities(name,type,description,created,updated) "
            "VALUES(?,?,?,?,?)",
            [
                (name, "lesson", f"description for {name}", "t", "t")
                for name in names
            ],
        )
        connection.commit()
    finally:
        connection.close()


class _FakeHaikuRunner:
    model = "haiku"

    def __init__(self, routes: dict[str, str | None]) -> None:
        self.routes = routes

    def __call__(self, prompt: str) -> str:
        request = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
        results = []
        for item in request["items"]:
            bundle = json.loads(item["content"])
            vault_id = self.routes[bundle["entity"]["name"]]
            results.append(
                {
                    "id": item["id"],
                    "vault_id": vault_id,
                    "confidence": 0.0 if vault_id is None else 0.99,
                    "reason_code": "synthetic_e2e",
                }
            )
        return json.dumps({"items": results})


def _write_codex_policy(scopes: Path) -> None:
    (scopes / "config.json").write_text(
        json.dumps(
            {"agents": {"codex": {
                "recall": True,
                "boot-injection": False,
                "capture": False,
            }}}
        ),
        encoding="utf-8",
    )


def _scope_entity_names(scopes: Path, scope: str, version: str) -> set[str]:
    connection = sqlite3.connect(scopes / scope / version / "graph.db")
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM entities")
        }
    finally:
        connection.close()


def test_classify_materialize_wire_and_recall_stay_inside_codex_ceiling(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    knowledge = home / ".agam" / "knowledge"
    sealed = knowledge / "sealed"
    source = sealed / "source.db"
    staging = sealed / "staging" / "classified.db"
    scopes = knowledge / "scopes"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGAM_DATA_HOME", str(home / ".agam"))

    registry = initialize_registry(
        scopes / "registry.json",
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    guidance_id = registry.for_role(VaultRole.GUIDANCE).id
    solutions_id = registry.for_role(VaultRole.SOLUTIONS).id
    restricted_work_id = add_vault(scopes / "registry.json", name="Project North").id
    restricted_personal_id = add_vault(scopes / "registry.json", name="Private Notes").id

    _create_graph(
        source,
        (
            GENERAL_GUIDANCE,
            GENERAL_SOLUTION,
            RESTRICTED_WORK,
            RESTRICTED_PERSONAL,
            REVIEW_ONLY,
        ),
    )
    classified = classify_graph(
        source,
        staging,
        _FakeHaikuRunner(
            {
                GENERAL_GUIDANCE: guidance_id,
                GENERAL_SOLUTION: solutions_id,
                RESTRICTED_WORK: restricted_work_id,
                RESTRICTED_PERSONAL: restricted_personal_id,
                REVIEW_ONLY: "vault_" + "f" * 24,
            }
        ),
        model="haiku",
        batch_size=4,
        registry_path=scopes / "registry.json",
    )
    materialized = materialize_graph(
        source,
        staging,
        scopes,
        SCHEMA,
        source_sha256=classified.source_sha256,
        source_snapshot_sha256=classified.source_snapshot_sha256,
        staging_sha256=classified.staging_sha256,
        version="v-synthetic-e2e",
        created_at="2026-07-18T12:34:56Z",
        registry_path=scopes / "registry.json",
    )
    _write_codex_policy(scopes)

    # The restricted rows really were routed into separate physical stores;
    # absence from recall below is therefore an authorization result.
    assert _scope_entity_names(scopes, restricted_work_id, materialized.version) == {
        RESTRICTED_WORK
    }
    assert _scope_entity_names(scopes, restricted_personal_id, materialized.version) == {
        RESTRICTED_PERSONAL
    }
    assert resolve_effective_scopes(
        "codex",
        config_path=scopes / "config.json",
        active_path=scopes / "active.json",
        env={},
        scope_root=scopes,
    ) == (guidance_id, solutions_id)
    assert resolve_agent_capabilities("codex", scope_root=scopes) == {
        "recall": True,
        "boot-injection": False,
        "capture": False,
    }

    CodexAgent().install(home)
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"UserPromptSubmit", "PreToolUse"}
    assert hooks["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert "Stop" not in hooks["hooks"]
    permission_config = (home / ".codex" / "config.toml").read_text()
    assert "permissions.agam_scoped" in permission_config
    assert restricted_work_id not in permission_config
    assert restricted_personal_id not in permission_config

    # Populate the old shared graph and explicitly advertise it through the
    # legacy variables. Scoped recall must still never fall back to it.
    legacy = knowledge / "graph.db"
    _create_graph(legacy, (LEGACY_ONLY,))
    hook = home / ".codex" / "hooks" / "agam" / "graph_recall.py"
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "TMPDIR": str(state),
        "AGAM_DATA_HOME": str(home / ".agam"),
        "AGAM_KNOWLEDGE_SCOPE_ROOT": str(scopes),
        "AGAM_KNOWLEDGE_CONFIG": str(scopes / "config.json"),
        "AGAM_ACTIVE_MANIFEST": str(scopes / "active.json"),
        "AGAM_RECALL_AGENT": "codex",
        "AGAM_KG_PATH": str(legacy),
        "AGAM_KG_DIR": str(legacy.parent),
    }
    prompt = (
        "Compare synthetic-portable-guidance and synthetic-portable-solution "
        "with synthetic-restricted-work, synthetic-restricted-personal, and "
        "synthetic-review-only and synthetic-legacy-only before deciding."
    )
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"session_id": "synthetic-e2e", "prompt": prompt}),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert GENERAL_GUIDANCE in context
    assert GENERAL_SOLUTION in context
    assert RESTRICTED_WORK not in context
    assert RESTRICTED_PERSONAL not in context
    assert REVIEW_ONLY not in context
    assert LEGACY_ONLY not in context
    assert "boot" not in context.lower()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == classified.source_sha256

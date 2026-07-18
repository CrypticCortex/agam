"""Immutable publication tests for registry-defined vaults."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agam.knowledge_classifier import classify_graph
from agam.knowledge_materializer import materialize_graph
from agam.vault_registry import (
    VaultRole,
    add_vault,
    initialize_registry,
    load_registry,
)


class _Runner:
    model = "haiku"

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes

    def __call__(self, prompt: str) -> str:
        payload = json.loads(prompt.split("\nINPUT_JSON:\n", 1)[1])
        output = []
        for item in payload["items"]:
            bundle = json.loads(item["content"])
            output.append(
                {
                    "id": item["id"],
                    "vault_id": self.routes[bundle["entity"]["name"]],
                    "confidence": 0.99,
                    "reason_code": "matched_route",
                }
            )
        return json.dumps({"items": output})


def _graph(path: Path) -> Path:
    schema = (Path(__file__).parents[1] / "knowledge" / "graph-schema.sql").read_text()
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    for name in ("craft-note", "repair-note", "project-note"):
        connection.execute(
            "INSERT INTO entities(name,type,description,created,updated) "
            "VALUES(?,?,?,?,?)",
            (
                name,
                "lesson",
                f"synthetic content for {name}",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
    connection.commit()
    connection.close()
    return path


def test_materializer_publishes_one_opaque_store_per_active_vault(tmp_path):
    root = tmp_path / "scopes"
    registry_path = root / "registry.json"
    initialized = initialize_registry(
        registry_path,
        guidance_name="Craft Notes",
        solutions_name="Repair Notes",
    )
    custom = add_vault(registry_path, name="Project North", routing_hint="north")
    registry = load_registry(registry_path)
    guidance = registry.for_role(VaultRole.GUIDANCE)
    solutions = registry.for_role(VaultRole.SOLUTIONS)
    source = _graph(tmp_path / "source.db")
    staging = tmp_path / "staging.db"
    classified = classify_graph(
        source,
        staging,
        _Runner(
            {
                "craft-note": guidance.id,
                "repair-note": solutions.id,
                "project-note": custom.id,
            }
        ),
        registry_path=registry_path,
    )

    summary = materialize_graph(
        source,
        staging,
        root,
        Path(__file__).parents[1] / "knowledge" / "graph-schema.sql",
        source_sha256=classified.source_sha256,
        source_snapshot_sha256=classified.source_snapshot_sha256,
        staging_sha256=classified.staging_sha256,
        registry_path=registry_path,
        version="v-dynamic",
    )

    assert [store.scope for store in summary.stores] == [
        vault.id for vault in registry.active
    ]
    assert [store.entities for store in summary.stores] == [1, 1, 1]
    manifest = json.loads((root / "manifests" / "v-dynamic.json").read_text())
    assert set(manifest["stores"]) == {vault.id for vault in registry.active}
    assert all("Craft Notes" not in entry["path"] for entry in manifest["stores"].values())

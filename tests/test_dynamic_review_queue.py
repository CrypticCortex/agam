"""User-selected vault resolution tests for sealed review items."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agam.knowledge_classifier import classify_graph
from agam.review_queue import ReviewQueue
from agam.vault_registry import add_vault, initialize_registry


class _UnknownRunner:
    model = "haiku"

    def __call__(self, prompt: str) -> str:
        item = json.loads(prompt.split("\nINPUT_JSON:\n", 1)[1])["items"][0]
        return json.dumps(
            {
                "items": [
                    {
                        "id": item["id"],
                        "vault_id": "vault_999999999999999999999999",
                        "confidence": 0.99,
                        "reason_code": "no_safe_match",
                    }
                ]
            }
        )


def _source(path: Path) -> Path:
    schema = (Path(__file__).parents[1] / "knowledge" / "graph-schema.sql").read_text()
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) VALUES(?,?,?,?,?)",
        (
            "ambiguous-note",
            "lesson",
            "synthetic ambiguous content",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    return path


def test_review_resolution_accepts_active_custom_vault_id(tmp_path):
    root = tmp_path / "scopes"
    registry_path = root / "registry.json"
    initialize_registry(
        registry_path,
        guidance_name="Craft",
        solutions_name="Repairs",
    )
    custom = add_vault(registry_path, name="Project North")
    source = _source(tmp_path / "source.db")
    staging = tmp_path / "staging.db"
    classify_graph(
        source,
        staging,
        _UnknownRunner(),
        registry_path=registry_path,
    )
    queue = ReviewQueue(source, staging, registry_path=registry_path)
    opaque_id = queue.items()[0].opaque_id

    summary = queue.resolve(opaque_id, vault_id=custom.id)

    assert summary.review == 0
    connection = sqlite3.connect(staging)
    row = connection.execute(
        "SELECT vault_id,privacy,kind,reason_code FROM agam_classifications"
    ).fetchone()
    connection.close()
    assert row == (custom.id, "RESTRICTED", "CUSTOM", "user_confirmed")

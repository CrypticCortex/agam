"""Opaque vault-routing contracts for the sealed classifier."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agam.knowledge_classifier import (
    ClassificationRequestItem,
    KnowledgeKind,
    PrivacyLabel,
    build_classifier_prompt,
    classify_graph,
    parse_classifier_response,
)
from agam.vault_registry import add_vault, initialize_registry, load_registry


ITEM = "item_111111111111111111111111"


def _registry(tmp_path):
    path = tmp_path / "registry.json"
    initialize_registry(
        path,
        guidance_name="My Compass",
        solutions_name="Things That Worked",
    )
    add_vault(path, name="Project North", routing_hint="north project only")
    return load_registry(path)


def _response(vault_id: str, confidence: float = 0.95) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "id": ITEM,
                    "vault_id": vault_id,
                    "confidence": confidence,
                    "reason_code": "matched_route",
                }
            ]
        }
    )


def test_prompt_offers_active_opaque_routes_with_user_names(tmp_path):
    registry = _registry(tmp_path)

    prompt = build_classifier_prompt(
        [ClassificationRequestItem(ITEM, "synthetic inert content")],
        vaults=registry.active,
    )

    payload = prompt.split("ROUTES_JSON:\n", 1)[1].split("\nINPUT_JSON:\n", 1)[0]
    routes = json.loads(payload)["routes"]
    assert [route["vault_id"] for route in routes] == [
        vault.id for vault in registry.active
    ]
    assert [route["name"] for route in routes] == [
        "My Compass",
        "Things That Worked",
        "Project North",
    ]
    assert routes[-1]["hint"] == "north project only"


def test_response_routes_to_verified_vault_and_derives_generic_labels(tmp_path):
    registry = _registry(tmp_path)
    custom = registry.active[-1]

    result = parse_classifier_response(
        _response(custom.id), [ITEM], vaults=registry.active
    )[0]

    assert result.vault_id == custom.id
    assert result.privacy is PrivacyLabel.RESTRICTED
    assert result.kind is KnowledgeKind.CUSTOM


def test_unknown_or_low_confidence_route_becomes_review(tmp_path):
    registry = _registry(tmp_path)

    unknown = parse_classifier_response(
        _response("vault_999999999999999999999999"),
        [ITEM],
        vaults=registry.active,
    )[0]
    uncertain = parse_classifier_response(
        _response(registry.active[0].id, confidence=0.3),
        [ITEM],
        vaults=registry.active,
    )[0]

    assert unknown.vault_id is None
    assert unknown.privacy is PrivacyLabel.REVIEW
    assert uncertain.vault_id is None
    assert uncertain.kind is KnowledgeKind.REVIEW


class _Runner:
    model = "haiku"

    def __init__(self, vault_id: str) -> None:
        self.vault_id = vault_id

    def __call__(self, prompt: str) -> str:
        payload = json.loads(prompt.split("\nINPUT_JSON:\n", 1)[1])
        return json.dumps(
            {
                "items": [
                    {
                        "id": item["id"],
                        "vault_id": self.vault_id,
                        "confidence": 0.99,
                        "reason_code": "matched_route",
                    }
                    for item in payload["items"]
                ]
            }
        )


def _graph(path: Path) -> Path:
    schema = (
        Path(__file__).parents[1]
        / "knowledge"
        / "graph-schema.sql"
    ).read_text()
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    connection.execute(
        "INSERT INTO entities(name,type,description,created,updated) "
        "VALUES(?,?,?,?,?)",
        (
            "synthetic-note",
            "lesson",
            "portable synthetic text",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    return path


def test_classify_graph_seals_opaque_vault_assignment(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = initialize_registry(
        registry_path,
        guidance_name="My Compass",
        solutions_name="Things That Worked",
    )
    target = registry.for_role(registry.vaults[0].role)
    source = _graph(tmp_path / "source.db")
    staging = tmp_path / "staging.db"

    classify_graph(
        source,
        staging,
        _Runner(target.id),
        registry_path=registry_path,
    )

    connection = sqlite3.connect(staging)
    row = connection.execute(
        "SELECT vault_id,privacy,kind FROM agam_classifications"
    ).fetchone()
    description = connection.execute("SELECT description FROM entities").fetchone()[0]
    connection.close()
    assert row == (target.id, "PORTABLE", "GUIDANCE")
    assert description.startswith(f"[VAULT:{target.id}]")

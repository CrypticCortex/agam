from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agam.knowledge_classifier import classify_graph
from agam.vault_registry import VaultRole, initialize_registry


def _seed(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            last_referenced TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated TEXT NOT NULL
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL,
            created TEXT NOT NULL
        );
        INSERT INTO entities VALUES(
            1,'ambiguous-node','lesson','ambiguous body','2026-01-01','2026-01-01',NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _items(prompt):
    return json.loads(prompt.split("INPUT_JSON:\n", 1)[1])["items"]


def _runner(privacy="REVIEW", kind="REVIEW", reason="ambiguous_scope"):
    def run(prompt):
        item = _items(prompt)[0]
        return json.dumps(
            {
                "items": [
                    {
                        "id": item["id"],
                        "privacy": privacy,
                        "kind": kind,
                        "confidence": 0.0 if privacy == "REVIEW" else 0.95,
                        "reason_code": reason,
                    }
                ]
            }
        )

    run.model = "haiku"
    return run


def _vault_runner(vault_id, confidence=0.95, reason="matched_route"):
    def run(prompt):
        item = _items(prompt)[0]
        return json.dumps(
            {
                "items": [
                    {
                        "id": item["id"],
                        "vault_id": vault_id,
                        "confidence": confidence,
                        "reason_code": reason,
                    }
                ]
            }
        )

    run.model = "haiku"
    return run


@pytest.fixture
def review_run(tmp_path):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed(source)
    classify_graph(source, staging, _runner())
    return source, staging


def test_review_queue_lists_opaque_metadata_and_reveals_content_on_detail(review_run):
    from agam.review_queue import ReviewQueue

    source, staging = review_run
    queue = ReviewQueue(source, staging)

    items = queue.items()
    assert len(items) == 1
    assert items[0].opaque_id.startswith("item_")
    assert items[0].reason_code == "ambiguous_scope"
    assert not hasattr(items[0], "description")

    detail = queue.detail(items[0].opaque_id)
    assert detail.name == "ambiguous-node"
    assert detail.description == "[REVIEW] ambiguous body"


def test_review_queue_retries_one_item_with_supplied_haiku_runner(review_run):
    from agam.review_queue import ReviewQueue

    source, staging = review_run
    queue = ReviewQueue(source, staging)
    opaque_id = queue.items()[0].opaque_id

    summary = queue.retry(
        opaque_id,
        _runner("PORTABLE", "GUIDANCE", "portable_guidance"),
    )

    assert summary.review == 0
    assert queue.items() == ()


def test_review_queue_applies_explicit_user_decision_through_sealed_path(review_run):
    from agam.review_queue import ReviewQueue

    source, staging = review_run
    queue = ReviewQueue(source, staging)
    opaque_id = queue.items()[0].opaque_id

    summary = queue.resolve(opaque_id, privacy="PORTABLE", kind="SOLUTION")

    assert summary.review == 0
    conn = sqlite3.connect(staging)
    row = conn.execute(
        "SELECT privacy,kind,reason_code,integrity FROM agam_classifications"
    ).fetchone()
    conn.close()
    assert row[:3] == ("PORTABLE", "SOLUTION", "user_confirmed")
    assert row[3]


def test_review_queue_rejects_invalid_manual_route(review_run):
    from agam.review_queue import ReviewQueue, ReviewQueueError

    source, staging = review_run
    queue = ReviewQueue(source, staging)
    opaque_id = queue.items()[0].opaque_id

    with pytest.raises(ReviewQueueError, match="invalid_review_route"):
        queue.resolve(opaque_id, privacy="PORTABLE", kind="REVIEW")


def test_review_queue_detects_staging_tampering_before_retry(review_run):
    from agam.review_queue import ReviewQueue
    from agam.knowledge_classifier import ClassifierContractError

    source, staging = review_run
    queue = ReviewQueue(source, staging)
    opaque_id = queue.items()[0].opaque_id
    conn = sqlite3.connect(staging)
    conn.execute("UPDATE entities SET name='tampered'")
    conn.commit()
    conn.close()

    with pytest.raises(ClassifierContractError, match="staging_tampered"):
        queue.retry(opaque_id, _runner("PORTABLE", "GUIDANCE", "portable_guidance"))


def test_review_queue_publishes_resolved_items_as_new_immutable_version(
    review_run, tmp_path
):
    from agam.review_queue import ReviewQueue

    source, staging = review_run
    queue = ReviewQueue(source, staging)
    queue.resolve(
        queue.items()[0].opaque_id,
        privacy="PORTABLE",
        kind="SOLUTION",
    )
    scopes = tmp_path / "scopes"
    for directory in (scopes, scopes / "manifests", scopes / ".builds"):
        directory.mkdir(parents=True, exist_ok=True)
    schema = Path(__file__).parents[1] / "knowledge" / "graph-schema.sql"

    summary = queue.publish(scopes, schema, version="v-reviewed")

    assert summary.version == "v-reviewed"
    active = json.loads((scopes / "active.json").read_text())
    assert active["version"] == "v-reviewed"
    conn = sqlite3.connect(scopes / "portable-solutions" / "v-reviewed" / "graph.db")
    names = [row[0] for row in conn.execute("SELECT name FROM entities")]
    conn.close()
    assert names == ["ambiguous-node"]


def test_review_validation_never_consumes_an_incomplete_classifier_run(tmp_path):
    from agam.review_queue import ReviewQueue, ReviewQueueError

    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed(source)

    def interrupt(_prompt):
        raise KeyboardInterrupt

    interrupt.model = "haiku"
    with pytest.raises(KeyboardInterrupt):
        classify_graph(source, staging, interrupt)

    conn = sqlite3.connect(staging)
    before = conn.execute(
        "SELECT COUNT(*) FROM agam_classifications WHERE privacy IS NULL"
    ).fetchone()[0]
    conn.close()

    with pytest.raises(ReviewQueueError, match="review_run_incomplete"):
        ReviewQueue(source, staging).items()

    conn = sqlite3.connect(staging)
    after = conn.execute(
        "SELECT COUNT(*) FROM agam_classifications WHERE privacy IS NULL"
    ).fetchone()[0]
    conn.close()
    assert before == after == 1


def test_review_queue_discovers_active_run_and_persists_updated_pointer(tmp_path):
    from agam.review_queue import ReviewQueue

    data_home = tmp_path / ".agam"
    source = data_home / "knowledge" / "sealed" / "sources" / "source.db"
    staging = data_home / "knowledge" / "sealed" / "staging" / "run.db"
    source.parent.mkdir(parents=True)
    staging.parent.mkdir(parents=True)
    _seed(source)

    scopes = data_home / "knowledge" / "scopes"
    registry_path = scopes / "registry.json"
    registry = initialize_registry(
        registry_path,
        guidance_name="Craft",
        solutions_name="Repairs",
    )
    target = registry.for_role(VaultRole.GUIDANCE)
    summary = classify_graph(
        source,
        staging,
        _vault_runner(target.id, confidence=0.1, reason="needs_review"),
        registry_path=registry_path,
    )
    manifests = scopes / "manifests"
    manifests.mkdir(parents=True)
    version = "v-review"
    (manifests / f"{version}.json").write_text(
        json.dumps(
            {
                "version": version,
                "source_sha256": summary.source_sha256,
                "source_snapshot_sha256": summary.source_snapshot_sha256,
                "staging_sha256": summary.staging_sha256,
                "model": "haiku",
                "stores": {},
            }
        )
    )
    (scopes / "active.json").write_text(
        json.dumps(
            {"version": version, "manifest": f"manifests/{version}.json"}
        )
    )

    queue = ReviewQueue.discover(data_home)
    opaque_id = queue.items()[0].opaque_id
    queue.resolve(opaque_id, vault_id=target.id)

    pointer = data_home / "knowledge" / "sealed" / "review-active.json"
    assert pointer.exists()
    assert ReviewQueue.discover(data_home).items() == ()

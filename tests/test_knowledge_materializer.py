"""Synthetic contract tests for physically scoped knowledge materialization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest

import agam.knowledge_materializer as materializer_module
from agam.knowledge_classifier import classify_graph, sha256_file
from agam.knowledge_materializer import (
    MaterializationContractError,
    MaterializationSummary,
    materialize_graph,
)
from agam.knowledge_scopes import load_active_manifest


SCHEMA = Path(__file__).parents[1] / "knowledge" / "graph-schema.sql"


def _with_model(runner, model: str = "haiku"):
    runner.model = model
    return runner


def _seed_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA.read_text())
    connection.executemany(
        "INSERT INTO entities(id,name,type,description,created,updated) "
        "VALUES(?,?,?,?,?,?)",
        (
            (1, "portable-style", "lesson", "style marker", "t", "t"),
            (2, "portable-fix", "lesson", "fix marker", "t", "t"),
            (3, "portable-other", "note", "other marker", "t", "t"),
            (4, "client-style", "lesson", "client marker", "t", "t"),
            (5, "private-fix", "lesson", "private marker", "t", "t"),
            (6, "uncertain", "note", "review marker", "t", "t"),
            (7, "client-fix", "lesson", "client fix marker", "t", "t"),
            (8, "client-other", "note", "client other marker", "t", "t"),
            (9, "private-style", "lesson", "private style marker", "t", "t"),
            (10, "private-other", "note", "private other marker", "t", "t"),
        ),
    )
    connection.executemany(
        "INSERT INTO properties(id,entity_id,key,value,updated) VALUES(?,?,?,?,?)",
        (
            (11, 1, "style-key", "style-value", "t"),
            (12, 2, "fix-key", "fix-value", "t"),
            (13, 4, "client-key", "client-value", "t"),
        ),
    )
    connection.executemany(
        "INSERT INTO relationships(id,source_id,target_id,relation,weight,created) "
        "VALUES(?,?,?,?,?,?)",
        (
            (21, 1, 1, "same-style", 1.0, "t"),
            (22, 1, 2, "cross-general-kind", 1.0, "t"),
            (23, 4, 4, "same-client", 1.0, "t"),
            (24, 4, 5, "cross-private", 1.0, "t"),
        ),
    )
    connection.commit()
    connection.close()


def _classifications() -> dict[str, tuple[str, str]]:
    return {
        "portable-style": ("PORTABLE", "GUIDANCE"),
        "portable-fix": ("PORTABLE", "SOLUTION"),
        "portable-other": ("PORTABLE", "CUSTOM"),
        "client-style": ("RESTRICTED", "GUIDANCE"),
        "private-fix": ("RESTRICTED", "SOLUTION"),
        "uncertain": ("REVIEW", "REVIEW"),
        "client-fix": ("RESTRICTED", "SOLUTION"),
        "client-other": ("RESTRICTED", "CUSTOM"),
        "private-style": ("RESTRICTED", "GUIDANCE"),
        "private-other": ("RESTRICTED", "CUSTOM"),
    }


def _build_classified_graph(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    _seed_source(source)
    labels = _classifications()

    def runner(prompt: str) -> str:
        request = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
        items = []
        for item in request["items"]:
            bundle = json.loads(item["content"])
            privacy, kind = labels[bundle["entity"]["name"]]
            items.append(
                {
                    "id": item["id"],
                    "privacy": privacy,
                    "kind": kind,
                    "confidence": 0.96 if privacy != "REVIEW" else 0.0,
                    "reason_code": "synthetic_route",
                }
            )
        return json.dumps({"items": items})

    classification = classify_graph(source, staging, _with_model(runner), batch_size=6)
    return source, staging, classification


def _materialize(tmp_path: Path, *, version: str = "v-test-001"):
    source, staging, classified = _build_classified_graph(tmp_path)
    scopes_root = tmp_path / "published" / "scopes"
    summary = materialize_graph(
        source,
        staging,
        scopes_root,
        SCHEMA,
        source_sha256=classified.source_sha256,
        source_snapshot_sha256=classified.source_snapshot_sha256,
        staging_sha256=classified.staging_sha256,
        version=version,
        created_at="2026-07-18T12:34:56Z",
    )
    return source, staging, scopes_root, classified, summary


def _rows(database: Path, table: str):
    connection = sqlite3.connect(database)
    try:
        return connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        connection.close()


def test_routes_every_supported_pair_and_preserves_ids_properties_and_same_scope_edges(
    tmp_path,
):
    source, staging, scopes_root, classified, summary = _materialize(tmp_path)

    expected_entities = {
        "restricted": [
            (8, "client-other", "[RESTRICTED]"),
            (10, "private-other", "[RESTRICTED]"),
        ],
        "portable-guidance": [(1, "portable-style", "[PORTABLE]")],
        "portable-solutions": [(2, "portable-fix", "[PORTABLE]")],
    }
    for scope, expected in expected_entities.items():
        database = scopes_root / scope / summary.version / "graph.db"
        entities = _rows(database, "entities")
        assert [(row[0], row[1], row[3][: len(prefix)]) for row, (_, _, prefix) in zip(entities, expected, strict=True)] == expected

    assert _rows(scopes_root / "portable-guidance" / summary.version / "graph.db", "properties") == [
        (11, 1, "style-key", "style-value", "t")
    ]
    assert _rows(scopes_root / "portable-solutions" / summary.version / "graph.db", "properties") == [
        (12, 2, "fix-key", "fix-value", "t")
    ]
    assert _rows(scopes_root / "restricted" / summary.version / "graph.db", "relationships") == []
    assert _rows(scopes_root / "portable-guidance" / summary.version / "graph.db", "relationships") == [
        (21, 1, 1, "same-style", 1.0, "t")
    ]
    assert _rows(scopes_root / "portable-solutions" / summary.version / "graph.db", "relationships") == []
    assert all(
        "portable-other" not in str(_rows(scopes_root / scope / summary.version / "graph.db", "entities"))
        and "uncertain" not in str(_rows(scopes_root / scope / summary.version / "graph.db", "entities"))
        for scope in expected_entities
    )
    assert sha256_file(source) == classified.source_sha256
    assert sha256_file(staging) == classified.staging_sha256


def test_late_restricted_property_cannot_be_classified_or_materialized_general(
    tmp_path,
):
    source = tmp_path / "source.db"
    staging = tmp_path / "staging.db"
    scopes_root = tmp_path / "scopes"
    _seed_source(source)
    connection = sqlite3.connect(source)
    connection.executemany(
        "INSERT INTO properties(id,entity_id,key,value,updated) VALUES(?,?,?,?,?)",
        [
            (100 + offset, 1, f"property-{offset:02d}", "benign", "t")
            for offset in range(32)
        ]
        + [(132, 1, "zz-restricted", "SYNTHETIC_RESTRICTED_LATE_MARKER", "t")],
    )
    connection.commit()
    connection.close()
    labels = _classifications()

    def general_if_visible_runner(prompt: str) -> str:
        request = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
        items = []
        for item in request["items"]:
            bundle = json.loads(item["content"])
            privacy, kind = labels[bundle["entity"]["name"]]
            items.append(
                {
                    "id": item["id"],
                    "privacy": privacy,
                    "kind": kind,
                    "confidence": 0.96 if privacy != "REVIEW" else 0.0,
                    "reason_code": "synthetic_route",
                }
            )
        return json.dumps({"items": items})

    classified = classify_graph(
        source, staging, _with_model(general_if_visible_runner), batch_size=6
    )
    connection = sqlite3.connect(staging)
    classification = connection.execute(
        "SELECT privacy,kind,reason_code FROM agam_classifications WHERE entity_id=1"
    ).fetchone()
    connection.close()

    summary = materialize_graph(
        source,
        staging,
        scopes_root,
        SCHEMA,
        source_sha256=classified.source_sha256,
        source_snapshot_sha256=classified.source_snapshot_sha256,
        staging_sha256=classified.staging_sha256,
        version="v-late-restricted",
        created_at="2026-07-18T12:34:56Z",
    )

    assert classification == ("REVIEW", "REVIEW", "input_incomplete")
    assert _rows(
        scopes_root / "portable-guidance" / summary.version / "graph.db", "entities"
    ) == []
    for scope in ("portable-guidance", "portable-solutions", "restricted"):
        properties = _rows(
            scopes_root / scope / summary.version / "graph.db", "properties"
        )
        assert "SYNTHETIC_RESTRICTED_LATE_MARKER" not in repr(properties)


def test_manifest_and_activation_are_inside_scope_root_and_content_free(tmp_path):
    source, staging, scopes_root, classified, summary = _materialize(tmp_path)
    active = scopes_root / "active.json"
    manifest_path = scopes_root / "manifests" / f"{summary.version}.json"

    assert active.exists()
    assert manifest_path.exists()
    assert not (scopes_root.parent / "active.json").exists()
    pointer = json.loads(active.read_text())
    manifest = json.loads(manifest_path.read_text())
    assert pointer == {
        "version": summary.version,
        "manifest": f"manifests/{summary.version}.json",
    }
    assert set(manifest) == {
        "version",
        "source_sha256",
        "source_snapshot_sha256",
        "staging_sha256",
        "model",
        "created_at",
        "stores",
    }
    assert manifest["source_snapshot_sha256"] == classified.source_snapshot_sha256
    assert set(manifest["stores"]) == {
        "portable-guidance",
        "portable-solutions",
        "restricted",
    }
    assert all(
        set(entry)
        == {"path", "sha256", "entities", "relationships", "properties"}
        for entry in manifest["stores"].values()
    )
    serialized = json.dumps(manifest)
    for marker in (
        "portable-style",
        "portable-fix",
        "client-style",
        "private-fix",
        str(source),
        str(staging),
    ):
        assert marker not in serialized
    # Fixed-layout manifests remain sealed until the metadata migration creates
    # a registry-backed activation.
    assert load_active_manifest(active, scope_root=scopes_root) is None


def test_output_fts_counts_hashes_and_permissions(tmp_path):
    _, _, scopes_root, _, summary = _materialize(tmp_path)
    stores = {store.scope: store for store in summary.stores}
    assert stores["portable-guidance"].entities == 1
    assert stores["portable-guidance"].properties == 1
    assert stores["portable-guidance"].relationships == 1

    for scope, store in stores.items():
        database = scopes_root / scope / summary.version / "graph.db"
        assert sha256_file(database) == store.sha256
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
    guidance = scopes_root / "portable-guidance" / summary.version / "graph.db"
    connection = sqlite3.connect(guidance)
    matches = connection.execute(
        "SELECT name FROM entities_fts_v2 WHERE entities_fts_v2 MATCH ?",
        ('"portable-style"',),
    ).fetchall()
    connection.close()
    assert matches == [("portable-style",)]


def test_missing_fts_tables_fail_closed_without_activation(monkeypatch, tmp_path):
    source, staging, classified = _build_classified_graph(tmp_path / "input")
    schema = tmp_path / "schema-without-fts.sql"
    schema.write_text(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            last_referenced TEXT
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL NOT NULL,
            created TEXT NOT NULL
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated TEXT NOT NULL
        );
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        materializer_module,
        "OFFICIAL_GRAPH_SCHEMA_SHA256",
        hashlib.sha256(schema.read_bytes()).hexdigest(),
    )
    scopes_root = tmp_path / "scopes"

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            scopes_root,
            schema,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-no-fts",
        )

    assert error.value.code == "fts_rebuild_failed"
    assert not (scopes_root / "active.json").exists()


def test_untrusted_schema_cannot_attach_or_mutate_an_external_database(tmp_path):
    source, staging, classified = _build_classified_graph(tmp_path / "input")
    victim = tmp_path / "victim.db"
    victim_connection = sqlite3.connect(victim)
    victim_connection.execute("CREATE TABLE retained(value TEXT)")
    victim_connection.commit()
    victim_connection.close()
    hostile_schema = tmp_path / "hostile-schema.sql"
    hostile_schema.write_text(
        SCHEMA.read_text(encoding="utf-8")
        + f"\nATTACH DATABASE '{victim}' AS victim;"
        + "\nCREATE TABLE victim.intrusion(value TEXT);",
        encoding="utf-8",
    )

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            tmp_path / "scopes",
            hostile_schema,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-hostile-schema",
        )

    assert error.value.code == "invalid_schema"
    victim_connection = sqlite3.connect(victim)
    intrusion_count = victim_connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='intrusion'"
    ).fetchone()[0]
    victim_connection.close()
    assert intrusion_count == 0
    assert not (tmp_path / "scopes" / "active.json").exists()


@pytest.mark.parametrize(
    "created_at",
    [
        "SYNTHETIC_PRIVATE_MARKER",
        "2026-07-18T12:34:56+05:30",
        "2026-02-30T12:34:56Z",
        "2026-07-18T12:34:56.1234567Z",
    ],
)
def test_created_at_rejects_noncanonical_or_content_bearing_values(
    tmp_path, created_at
):
    source, staging, classified = _build_classified_graph(tmp_path / "input")

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            tmp_path / "scopes",
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-bad-created-at",
            created_at=created_at,
        )

    assert error.value.code == "invalid_created_at"
    assert created_at not in str(error.value)
    assert not (tmp_path / "scopes" / "active.json").exists()


def test_version_collision_keeps_current_activation_unchanged(tmp_path):
    source, staging, scopes_root, classified, summary = _materialize(tmp_path)
    active = scopes_root / "active.json"
    original_active = active.read_bytes()

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            scopes_root,
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version=summary.version,
        )

    assert error.value.code == "version_collision"
    assert active.read_bytes() == original_active


def test_activation_write_failure_preserves_previous_pointer(
    monkeypatch, tmp_path
):
    source, staging, scopes_root, classified, _ = _materialize(
        tmp_path, version="v-one"
    )
    active = scopes_root / "active.json"
    original_active = active.read_bytes()
    real_atomic_json = materializer_module._atomic_json

    def fail_activation(path, payload):
        if path.name == "active.json":
            raise OSError("synthetic activation failure")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(materializer_module, "_atomic_json", fail_activation)
    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            scopes_root,
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-two",
        )

    assert error.value.code == "materialization_failed"
    assert active.read_bytes() == original_active


def test_publication_renames_are_followed_by_ordered_directory_fsyncs(
    monkeypatch, tmp_path
):
    source, staging, classified = _build_classified_graph(tmp_path / "input")
    scopes_root = tmp_path / "scopes"
    events = []
    real_replace = materializer_module.os.replace

    def tracked_replace(source_path, destination_path):
        destination = Path(destination_path)
        events.append(("replace", destination))
        return real_replace(source_path, destination_path)

    def tracked_directory_fsync(directory):
        events.append(("fsync-directory", Path(directory)))

    monkeypatch.setattr(materializer_module.os, "replace", tracked_replace)
    monkeypatch.setattr(
        materializer_module,
        "_fsync_directory",
        tracked_directory_fsync,
        raising=False,
    )

    materialize_graph(
        source,
        staging,
        scopes_root,
        SCHEMA,
        source_sha256=classified.source_sha256,
        source_snapshot_sha256=classified.source_snapshot_sha256,
        staging_sha256=classified.staging_sha256,
        version="v-fsync-order",
        created_at="2026-07-18T12:34:56Z",
    )

    replace_indices = [
        index for index, event in enumerate(events) if event[0] == "replace"
    ]
    assert len(replace_indices) == 5
    for index in replace_indices:
        destination = events[index][1]
        assert events[index + 1] == ("fsync-directory", destination.parent)

    manifest = scopes_root / "manifests" / "v-fsync-order.json"
    active = scopes_root / "active.json"
    manifest_replace = events.index(("replace", manifest))
    manifest_fsync = events.index(("fsync-directory", manifest.parent))
    active_replace = events.index(("replace", active))
    active_fsync = events.index(("fsync-directory", active.parent), active_replace)
    assert manifest_replace < manifest_fsync < active_replace < active_fsync


def test_post_activation_directory_fsync_failure_is_reported_as_uncertain(
    tmp_path, monkeypatch
):
    source, staging, scopes_root, classified, _ = _materialize(
        tmp_path, version="v-before-uncertain"
    )
    active = scopes_root / "active.json"
    original = active.read_bytes()
    real_fsync = materializer_module._fsync_directory

    def fail_active_parent(directory):
        if Path(directory) == scopes_root:
            raise OSError("synthetic post-activation fsync failure")
        return real_fsync(directory)

    monkeypatch.setattr(
        materializer_module, "_fsync_directory", fail_active_parent
    )

    with pytest.raises(MaterializationContractError) as raised:
        materialize_graph(
            source,
            staging,
            scopes_root,
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-activation-uncertain",
        )

    assert raised.value.code == "activation_uncertain"
    assert active.read_bytes() != original
    assert json.loads(active.read_text())["version"] == "v-activation-uncertain"


def test_tampered_classification_is_not_materialized(tmp_path):
    source, staging, classified = _build_classified_graph(tmp_path)
    connection = sqlite3.connect(staging)
    connection.execute(
        "UPDATE agam_classifications SET privacy='PORTABLE',kind='GUIDANCE' "
        "WHERE entity_id=5"
    )
    connection.execute(
        "UPDATE entities SET description='[PORTABLE] forged' WHERE id=5"
    )
    connection.commit()
    connection.close()

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            tmp_path / "scopes",
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=sha256_file(staging),
            version="v-tampered",
        )

    assert error.value.code == "invalid_integrity"


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE entities SET name=? WHERE id=1", ("forged-name",)),
        ("UPDATE entities SET type=? WHERE id=1", ("forged-type",)),
        ("UPDATE properties SET value=? WHERE entity_id=1", ("forged-property",)),
        (
            "UPDATE relationships SET relation=? WHERE id=21",
            ("forged-relation",),
        ),
        (
            "UPDATE agam_classifier_meta SET value=? WHERE key='model'",
            ("forged-model",),
        ),
    ],
)
def test_run_integrity_tampering_is_rejected_without_changing_active(
    tmp_path, statement, parameters
):
    source, staging, scopes_root, classified, _ = _materialize(
        tmp_path, version="v-one"
    )
    active = scopes_root / "active.json"
    original_active = active.read_bytes()
    connection = sqlite3.connect(staging)
    connection.execute(statement, parameters)
    connection.commit()
    connection.close()

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            scopes_root,
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=sha256_file(staging),
            version="v-two",
        )

    assert error.value.code == "invalid_integrity"
    assert active.read_bytes() == original_active


def test_concurrent_staging_wal_change_fails_without_changing_active(
    monkeypatch, tmp_path
):
    source, staging, scopes_root, classified, _ = _materialize(
        tmp_path, version="v-one"
    )
    active = scopes_root / "active.json"
    original_active = active.read_bytes()
    writer = sqlite3.connect(staging)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    expected_staging_hash = sha256_file(staging)
    original_copy_scope = materializer_module._copy_scope
    changed = False

    def change_after_first_copy(*args, **kwargs):
        nonlocal changed
        result = original_copy_scope(*args, **kwargs)
        if not changed:
            writer.execute(
                "UPDATE properties SET value='wal-forged' WHERE entity_id=1"
            )
            writer.commit()
            changed = True
        return result

    monkeypatch.setattr(materializer_module, "_copy_scope", change_after_first_copy)
    try:
        with pytest.raises(MaterializationContractError) as error:
            materialize_graph(
                source,
                staging,
                scopes_root,
                SCHEMA,
                source_sha256=classified.source_sha256,
                source_snapshot_sha256=classified.source_snapshot_sha256,
                staging_sha256=expected_staging_hash,
                version="v-two",
            )
    finally:
        writer.close()

    assert changed is True
    assert error.value.code == "staging_changed"
    assert active.read_bytes() == original_active


def test_empty_wal_sidecar_is_semantically_absent(tmp_path):
    staging = tmp_path / "staging.db"
    staging.touch()
    staging.with_name(f"{staging.name}-wal").touch()

    assert materializer_module._sqlite_wal_state(staging) is None


def test_staging_inode_swap_is_rejected_even_when_bytes_match(
    monkeypatch, tmp_path
):
    source, staging, scopes_root, classified, _ = _materialize(
        tmp_path, version="v-one"
    )
    active = scopes_root / "active.json"
    original_active = active.read_bytes()
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(staging, replacement)
    original_copy_scope = materializer_module._copy_scope
    swapped = False

    def swap_after_first_copy(*args, **kwargs):
        nonlocal swapped
        result = original_copy_scope(*args, **kwargs)
        if not swapped:
            os.replace(replacement, staging)
            swapped = True
        return result

    monkeypatch.setattr(materializer_module, "_copy_scope", swap_after_first_copy)
    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            scopes_root,
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-two",
        )

    assert swapped is True
    assert error.value.code == "staging_changed"
    assert active.read_bytes() == original_active


def test_source_inode_swap_after_classification_is_rejected_when_bytes_match(
    tmp_path,
):
    source, staging, classified = _build_classified_graph(tmp_path / "input")
    replacement = tmp_path / "replacement-source.db"
    shutil.copyfile(source, replacement)
    os.replace(replacement, source)
    scopes_root = tmp_path / "scopes"

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            scopes_root,
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-source-swapped",
        )

    assert error.value.code == "source_changed"
    assert not (scopes_root / "active.json").exists()


def test_output_root_with_symlinked_parent_is_rejected(tmp_path):
    source, staging, classified = _build_classified_graph(tmp_path / "input")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(MaterializationContractError) as error:
        materialize_graph(
            source,
            staging,
            alias_parent / "scopes",
            SCHEMA,
            source_sha256=classified.source_sha256,
            source_snapshot_sha256=classified.source_snapshot_sha256,
            staging_sha256=classified.staging_sha256,
            version="v-escape",
        )

    assert error.value.code == "invalid_output_root"

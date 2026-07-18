from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest


FIRST = "vault_111111111111111111111111"
SECOND = "vault_222222222222222222222222"
THIRD = "vault_333333333333333333333333"
FOURTH = "vault_444444444444444444444444"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _graph(path: Path, rows: list[tuple[int, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            created TEXT,
            updated TEXT,
            last_referenced TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated TEXT
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL,
            created TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO entities(id,name,type,description,created,updated) "
        "VALUES(?,?,?,?,?,?)",
        [(*row, "2026-07-19", "2026-07-19") for row in rows],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def scoped_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "scopes"
    version = "v-test"
    rows = {
        FIRST: [
            (1, "verify-before-claim", "lesson", f"[VAULT:{FIRST}] Run a direct proof."),
            (2, "small-diffs", "lesson", f"[VAULT:{FIRST}] Keep changes focused."),
        ],
        SECOND: [
            (1, "retry-shared-seam", "resolution", f"[VAULT:{SECOND}] Fix the shared seam."),
        ],
        THIRD: [(1, "project-north", "project", f"[VAULT:{THIRD}] restricted")],
        FOURTH: [(1, "project-south", "project", f"[VAULT:{FOURTH}] restricted")],
    }
    stores = {}
    for scope, entities in rows.items():
        db = root / scope / version / "graph.db"
        _graph(db, entities)
        stores[scope] = {
            "path": f"{scope}/{version}/graph.db",
            "sha256": _sha256(db),
            "entities": len(entities),
            "relationships": 0,
            "properties": 0,
        }
    manifest = {"version": version, "stores": stores}
    manifests = root / "manifests"
    manifests.mkdir(parents=True)
    (manifests / f"{version}.json").write_text(json.dumps(manifest))
    (root / "active.json").write_text(
        json.dumps({"version": version, "manifest": f"manifests/{version}.json"})
    )
    (root / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vaults": [
                    {"id": FIRST, "name": "Craft", "role": "guidance", "access": "portable", "state": "active", "routing_hint": ""},
                    {"id": SECOND, "name": "Repairs", "role": "solutions", "access": "portable", "state": "active", "routing_hint": ""},
                    {"id": THIRD, "name": "Project North", "role": "custom", "access": "restricted", "state": "active", "routing_hint": ""},
                    {"id": FOURTH, "name": "Project South", "role": "custom", "access": "restricted", "state": "active", "routing_hint": ""},
                ],
                "agents": {"codex": [FIRST, SECOND]},
            }
        )
    )
    (root / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "codex": {
                        "recall": True,
                        "boot-injection": False,
                        "capture": False,
                    }
                },
            }
        )
    )
    return root


def test_vault_catalog_shows_all_stores_but_only_codex_scopes_are_readable(scoped_root):
    from agam.vaults import VaultCatalog

    catalog = VaultCatalog(scoped_root, agent="codex")

    summaries = catalog.summaries()
    assert [item.scope for item in summaries] == [
        FIRST,
        SECOND,
        THIRD,
        FOURTH,
    ]
    assert [item.readable for item in summaries] == [True, True, False, False]
    assert [item.entities for item in summaries] == [2, 1, 1, 1]
    assert all(item.version == "v-test" for item in summaries)


def test_vault_query_is_searchable_and_returns_general_rows(scoped_root):
    from agam.vaults import VaultCatalog

    catalog = VaultCatalog(scoped_root, agent="codex")

    rows = catalog.entities(FIRST, query="proof")
    assert [(row.name, row.kind) for row in rows] == [
        ("verify-before-claim", "lesson")
    ]
    assert rows[0].description.startswith(f"[VAULT:{FIRST}]")


def test_vault_query_rejects_restricted_scope(scoped_root):
    from agam.vaults import VaultAccessError, VaultCatalog

    catalog = VaultCatalog(scoped_root, agent="codex")

    with pytest.raises(VaultAccessError, match="scope_not_readable"):
        catalog.entities(THIRD)


def test_vault_query_fails_closed_on_non_general_row(scoped_root):
    from agam.vaults import VaultAccessError, VaultCatalog

    db = scoped_root / FIRST / "v-test" / "graph.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE entities SET description='[VAULT:vault_999999999999999999999999] escaped' WHERE id=1"
    )
    conn.commit()
    conn.close()

    # Refresh the declared digest so this reaches the content-label guard.
    manifest_path = scoped_root / "manifests" / "v-test.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stores"][FIRST]["sha256"] = _sha256(db)
    manifest_path.write_text(json.dumps(manifest))

    catalog = VaultCatalog(scoped_root, agent="codex")
    with pytest.raises(VaultAccessError, match="wrong_vault_content"):
        catalog.entities(FIRST)


def test_vault_query_fails_closed_on_digest_mismatch(scoped_root):
    from agam.vaults import VaultAccessError, VaultCatalog

    db = scoped_root / SECOND / "v-test" / "graph.db"
    db.write_bytes(db.read_bytes() + b"tamper")

    catalog = VaultCatalog(scoped_root, agent="codex")
    with pytest.raises(VaultAccessError, match="store_unavailable"):
        catalog.entities(SECOND)

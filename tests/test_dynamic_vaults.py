"""Read-only catalog tests for user-defined registry names."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from agam.vault_registry import add_vault, initialize_registry, load_registry
from agam.vaults import VaultAccessError, VaultCatalog


def _state(tmp_path):
    root = tmp_path / "scopes"
    registry_path = root / "registry.json"
    initialize_registry(
        registry_path,
        guidance_name="My Craft",
        solutions_name="Repair Library",
        agents=("codex",),
    )
    add_vault(registry_path, name="Project North")
    registry = load_registry(registry_path)
    (root / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "codex": {
                        "recall": True,
                        "boot-injection": False,
                        "capture": False,
                    }
                }
            }
        )
    )
    stores = {}
    for vault in registry.active:
        database = root / vault.id / "v-test" / "graph.db"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE entities(id INTEGER PRIMARY KEY,name TEXT,type TEXT,description TEXT,updated TEXT)"
        )
        connection.execute(
            "CREATE TABLE properties(id INTEGER,entity_id INTEGER,key TEXT,value TEXT,updated TEXT)"
        )
        connection.execute(
            "CREATE TABLE relationships(id INTEGER,source_id INTEGER,target_id INTEGER,relation TEXT,weight REAL,created TEXT)"
        )
        connection.execute(
            "INSERT INTO entities VALUES(1,?,?,?,?)",
            (
                f"note-{vault.id[-4:]}",
                "lesson",
                f"[VAULT:{vault.id}] synthetic content",
                "2026-01-01",
            ),
        )
        connection.commit()
        connection.close()
        stores[vault.id] = {
            "path": f"{vault.id}/v-test/graph.db",
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "entities": 1,
            "relationships": 0,
            "properties": 0,
        }
    manifests = root / "manifests"
    manifests.mkdir()
    (manifests / "v-test.json").write_text(
        json.dumps({"version": "v-test", "stores": stores})
    )
    (root / "active.json").write_text(
        json.dumps({"version": "v-test", "manifest": "manifests/v-test.json"})
    )
    return root, registry


def test_catalog_uses_user_names_and_masks_unselected_vault(tmp_path):
    root, registry = _state(tmp_path)
    catalog = VaultCatalog(root, agent="codex")

    summaries = catalog.summaries()
    assert [item.name for item in summaries] == [
        "My Craft",
        "Repair Library",
        "Project North",
    ]
    assert [item.scope for item in summaries] == [vault.id for vault in registry.vaults]
    assert [item.readable for item in summaries] == [True, True, False]
    assert catalog.entities(registry.vaults[0].id)[0].description.startswith("[VAULT:")
    with pytest.raises(VaultAccessError, match="scope_not_readable"):
        catalog.entities(registry.vaults[-1].id)

"""Copy-only migration tests for pre-registry vault layouts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agam.vault_migration import VaultMigrationError, migrate_fixed_layout
from agam.vault_registry import VaultRole, initialize_registry, load_registry


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _legacy_state(tmp_path: Path) -> Path:
    root = tmp_path / "scopes"
    version = "v-old"
    stores = {}
    for index, old_id in enumerate(("legacy-a", "legacy-b", "legacy-c"), 1):
        database = root / old_id / version / "graph.db"
        database.parent.mkdir(parents=True)
        database.write_bytes(f"synthetic-store-{index}".encode())
        stores[old_id] = {
            "path": f"{old_id}/{version}/graph.db",
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "entities": index,
            "relationships": 0,
            "properties": 0,
        }
    _write(
        root / "config.json",
        {
            "profiles": {"portable": {"scopes": ["legacy-a", "legacy-b"]}},
            "agents": {
                "codex": {
                    "ceiling": ["legacy-a", "legacy-b"],
                    "profile": "portable",
                    "recall": True,
                    "boot-injection": False,
                    "capture": False,
                }
            },
        },
    )
    _write(
        root / "manifests" / f"{version}.json",
        {"version": version, "stores": stores, "model": "haiku"},
    )
    _write(
        root / "active.json",
        {"version": version, "manifest": f"manifests/{version}.json"},
    )
    return root


def test_dry_run_reports_mapping_without_writing(tmp_path):
    root = _legacy_state(tmp_path)

    summary = migrate_fixed_layout(root, apply=False)

    assert summary.status == "dry-run"
    assert summary.vaults == 3
    assert summary.copied_stores == 0
    assert not (root / "registry.json").exists()


def test_apply_copies_stores_and_retains_original_artifacts(tmp_path):
    root = _legacy_state(tmp_path)
    original = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.glob("legacy-*/v-old/graph.db")
    }

    summary = migrate_fixed_layout(root, apply=True)

    registry = load_registry(root / "registry.json")
    assert registry.for_role(VaultRole.GUIDANCE).name == "Guidance"
    assert registry.for_role(VaultRole.SOLUTIONS).name == "Solutions"
    assert summary.status == "migrated"
    assert summary.copied_stores == 3
    assert all(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in original.items())
    active = json.loads((root / "active.json").read_text())
    manifest = json.loads((root / active["manifest"]).read_text())
    assert set(manifest["stores"]) == {vault.id for vault in registry.active}
    assert all((root / entry["path"]).exists() for entry in manifest["stores"].values())
    assert any((root / "migration-backups").iterdir())


def test_migration_is_idempotent_after_registry_exists(tmp_path):
    root = _legacy_state(tmp_path)
    first = migrate_fixed_layout(root, apply=True)

    second = migrate_fixed_layout(root, apply=True)

    assert first.version == second.version
    assert second.status == "already-migrated"
    assert second.copied_stores == 0


def test_recovers_registry_created_before_legacy_layout_was_migrated(tmp_path):
    root = _legacy_state(tmp_path)
    premature = initialize_registry(
        root / "registry.json",
        guidance_name="My Craft",
        solutions_name="Repair Library",
        agents=("codex",),
    )
    protected_ids = tuple(vault.id for vault in premature.vaults)

    preview = migrate_fixed_layout(root)

    assert preview.status == "dry-run"
    assert preview.vaults == 3

    migrated = migrate_fixed_layout(root, apply=True)
    registry = load_registry(root / "registry.json")
    assert migrated.status == "migrated"
    assert tuple(vault.id for vault in registry.vaults[:2]) == protected_ids
    assert [vault.name for vault in registry.vaults] == [
        "My Craft",
        "Repair Library",
        "Vault 1",
    ]
    assert registry.agents["codex"] == protected_ids
    backup = next((root / "migration-backups").iterdir())
    assert (backup / "registry.json").exists()


def test_rejects_path_shaped_legacy_store_id(tmp_path):
    root = _legacy_state(tmp_path)
    manifest_path = root / "manifests" / "v-old.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stores"]["../escape"] = manifest["stores"].pop("legacy-c")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(VaultMigrationError, match="invalid_legacy_metadata"):
        migrate_fixed_layout(root)

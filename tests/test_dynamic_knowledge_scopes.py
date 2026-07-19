"""Registry-driven policy tests for physically separated vaults."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agam.knowledge_scopes import (
    load_active_manifest,
    resolve_agent_capabilities,
    resolve_effective_scopes,
)
from agam.vault_registry import (
    add_vault,
    archive_vault,
    initialize_registry,
    rename_vault,
    set_agent_access,
)


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _state(tmp_path: Path):
    root = tmp_path / "scopes"
    registry_path = root / "registry.json"
    registry = initialize_registry(
        registry_path,
        guidance_name="Engineering Compass",
        solutions_name="Repair Notes",
        agents=("codex", "claude"),
    )
    custom = add_vault(
        registry_path,
        name="Project North",
        routing_hint="project north only",
    )
    set_agent_access(
        registry_path,
        "claude",
        [vault.id for vault in registry.vaults] + [custom.id],
    )
    config = _write_json(
        root / "config.json",
        {
            "agents": {
                "codex": {
                    "recall": True,
                    "boot-injection": False,
                    "capture": False,
                },
                "claude": {
                    "recall": True,
                    "boot-injection": False,
                    "capture": False,
                },
            }
        },
    )
    version = "v-test"
    stores = {}
    for vault in (*registry.vaults, custom):
        database = root / vault.id / version / "graph.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch()
        stores[vault.id] = {
            "path": f"{vault.id}/{version}/graph.db",
            "sha256": hashlib.sha256(b"").hexdigest(),
            "entities": 0,
            "relationships": 0,
            "properties": 0,
        }
    manifest = _write_json(
        root / "manifests" / f"{version}.json",
        {"version": version, "stores": stores},
    )
    active = _write_json(
        root / "active.json",
        {"version": version, "manifest": str(manifest.relative_to(root))},
    )
    return root, registry, custom, config, active


def test_effective_vaults_follow_registry_selection_and_order(tmp_path):
    root, registry, custom, config, active = _state(tmp_path)

    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        scope_root=root,
        env={},
    ) == tuple(vault.id for vault in registry.vaults)
    assert resolve_effective_scopes(
        "claude",
        config_path=config,
        active_path=active,
        scope_root=root,
        env={},
    ) == (*tuple(vault.id for vault in registry.vaults), custom.id)


def test_environment_can_narrow_but_not_expand_selection(tmp_path):
    root, registry, custom, config, active = _state(tmp_path)
    first = registry.vaults[0].id

    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        scope_root=root,
        env={"AGAM_KNOWLEDGE_SCOPES": first},
    ) == (first,)
    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        scope_root=root,
        env={"AGAM_KNOWLEDGE_SCOPES": custom.id},
    ) == ()


def test_manifest_with_unknown_vault_fails_closed(tmp_path):
    root, _, _, _, active = _state(tmp_path)
    manifest_path = root / "manifests" / "v-test.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stores"]["vault_999999999999999999999999"] = next(
        iter(manifest["stores"].values())
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert load_active_manifest(active, scope_root=root) is None


def test_missing_registry_fails_closed(tmp_path):
    root, _, _, config, active = _state(tmp_path)
    (root / "registry.json").unlink()

    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        scope_root=root,
        env={},
    ) == ()


def test_agent_capabilities_are_separate_from_vault_selection(tmp_path):
    root, _, _, config, _ = _state(tmp_path)

    assert resolve_agent_capabilities(
        "codex", config_path=config, scope_root=root
    ) == {"recall": True, "boot-injection": False, "capture": False}
    assert resolve_agent_capabilities(
        "unknown", config_path=config, scope_root=root
    ) == {"recall": False, "boot-injection": False, "capture": False}


def test_display_name_change_does_not_invalidate_published_manifest(tmp_path):
    root, registry, _, _, active = _state(tmp_path)
    manifest_path = root / "manifests" / "v-test.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["registry_sha256"] = hashlib.sha256(
        (root / "registry.json").read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rename_vault(root / "registry.json", registry.vaults[0].id, "New Label")

    assert load_active_manifest(active, scope_root=root) is not None


def test_archiving_vault_keeps_other_published_stores_available(tmp_path):
    root, registry, custom, config, active = _state(tmp_path)

    archive_vault(root / "registry.json", custom.id)

    assert load_active_manifest(active, scope_root=root) is not None
    assert resolve_effective_scopes(
        "claude",
        config_path=config,
        active_path=active,
        scope_root=root,
        env={},
    ) == tuple(vault.id for vault in registry.vaults)

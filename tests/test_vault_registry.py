"""Contracts for user-defined vault metadata and lifecycle operations."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agam.vault_registry import (
    RegistryError,
    VaultAccess,
    VaultRole,
    VaultState,
    add_vault,
    archive_vault,
    initialize_registry,
    load_registry,
    rename_vault,
    restore_vault,
    set_agent_access,
)


def _record(
    vault_id: str,
    name: str,
    role: str,
    access: str,
    state: str = "active",
    routing_hint: str = "",
) -> dict[str, str]:
    return {
        "id": vault_id,
        "name": name,
        "role": role,
        "access": access,
        "state": state,
        "routing_hint": routing_hint,
    }


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _valid_payload() -> dict[str, object]:
    first = "vault_111111111111111111111111"
    second = "vault_222222222222222222222222"
    custom = "vault_333333333333333333333333"
    return {
        "schema_version": 1,
        "vaults": [
            _record(first, "How I Build", "guidance", "portable"),
            _record(second, "Fix Library", "solutions", "portable"),
            _record(custom, "Client Alpha", "custom", "restricted"),
        ],
        "agents": {
            "codex": [first, second],
            "claude": [first, second, custom],
        },
    }


def test_loads_strict_registry_and_preserves_order(tmp_path):
    registry = load_registry(_write(tmp_path / "registry.json", _valid_payload()))

    assert [vault.name for vault in registry.vaults] == [
        "How I Build",
        "Fix Library",
        "Client Alpha",
    ]
    assert registry.for_role(VaultRole.GUIDANCE).name == "How I Build"
    assert registry.for_role(VaultRole.SOLUTIONS).name == "Fix Library"
    assert [vault.name for vault in registry.selected_for("codex")] == [
        "How I Build",
        "Fix Library",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(schema_version=2),
        lambda value: value["vaults"].append(
            _record("named-path", "Other", "custom", "restricted")
        ),
        lambda value: value["vaults"].append(
            _record(
                "vault_444444444444444444444444",
                "how i build",
                "custom",
                "restricted",
            )
        ),
        lambda value: value["vaults"].append(
            _record(
                "vault_444444444444444444444444",
                "Other",
                "guidance",
                "portable",
            )
        ),
        lambda value: value["agents"].update(
            codex=["vault_999999999999999999999999"]
        ),
    ],
)
def test_rejects_invalid_registry_shapes(tmp_path, mutate):
    payload = _valid_payload()
    mutate(payload)

    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path / "registry.json", payload))


def test_rejects_archived_agent_selection(tmp_path):
    payload = _valid_payload()
    payload["vaults"][2]["state"] = "archived"

    with pytest.raises(RegistryError, match="invalid_agent_selection"):
        load_registry(_write(tmp_path / "registry.json", payload))


def test_initialize_creates_user_named_protected_roles(tmp_path):
    path = tmp_path / "registry.json"

    registry = initialize_registry(
        path,
        guidance_name="My Operating Notes",
        solutions_name="Patterns That Worked",
        agents=("codex",),
    )

    assert registry.for_role(VaultRole.GUIDANCE).name == "My Operating Notes"
    assert registry.for_role(VaultRole.SOLUTIONS).name == "Patterns That Worked"
    assert all(vault.access is VaultAccess.PORTABLE for vault in registry.vaults)
    assert all(vault.state is VaultState.ACTIVE for vault in registry.vaults)
    assert len(registry.selected_for("codex")) == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_custom_lifecycle_keeps_stable_id(tmp_path):
    path = tmp_path / "registry.json"
    initialize_registry(
        path,
        guidance_name="Craft",
        solutions_name="Playbook",
        agents=("codex",),
    )

    created = add_vault(path, name="Project One", routing_hint="project one")
    assert created.role is VaultRole.CUSTOM
    assert created.access is VaultAccess.RESTRICTED
    assert created.state is VaultState.ACTIVE

    renamed = rename_vault(path, created.id, "Project Archive")
    assert renamed.id == created.id
    assert renamed.name == "Project Archive"

    set_agent_access(path, "codex", [created.id])
    archived = archive_vault(path, created.id)
    assert archived.state is VaultState.ARCHIVED
    assert load_registry(path).selected_for("codex") == ()
    assert path.exists()

    restored = restore_vault(path, created.id)
    assert restored.id == created.id
    assert restored.state is VaultState.ACTIVE


def test_protected_roles_can_be_renamed_but_not_archived(tmp_path):
    path = tmp_path / "registry.json"
    registry = initialize_registry(
        path,
        guidance_name="Craft",
        solutions_name="Playbook",
    )
    protected = registry.for_role(VaultRole.GUIDANCE)

    assert rename_vault(path, protected.id, "My Craft").name == "My Craft"
    with pytest.raises(RegistryError, match="protected_vault"):
        archive_vault(path, protected.id)


def test_failed_atomic_replace_preserves_previous_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    initialize_registry(
        path,
        guidance_name="Craft",
        solutions_name="Playbook",
    )
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("agam.vault_registry.os.replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        add_vault(path, name="Other")

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".registry.json.*.tmp"))


def test_path_helpers_accept_only_opaque_vault_ids(monkeypatch, tmp_path):
    from agam import paths

    monkeypatch.setenv("AGAM_DATA_HOME", str(tmp_path))
    vault_id = "vault_abcdefabcdefabcdefabcdef"

    assert paths.vault_registry_path() == (
        tmp_path / "knowledge" / "scopes" / "registry.json"
    )
    assert paths.scope_dir(vault_id) == tmp_path / "knowledge" / "scopes" / vault_id
    with pytest.raises(ValueError):
        paths.scope_dir("../named-vault")

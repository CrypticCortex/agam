"""Copy-only migration from fixed store identifiers to an opaque registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agam.vault_registry import (
    VaultRole,
    add_vault,
    initialize_registry,
    is_vault_id,
    load_registry,
    set_agent_access,
)


_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")


class VaultMigrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MigrationSummary:
    status: str
    vaults: int
    copied_stores: int
    version: str


def _json_object(path: Path) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise VaultMigrationError("invalid_legacy_metadata") from None
    if not isinstance(value, dict):
        raise VaultMigrationError("invalid_legacy_metadata")
    return value


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_selection(policy: dict, agent: str) -> tuple[str, ...]:
    agents = policy.get("agents")
    profiles = policy.get("profiles")
    if not isinstance(agents, dict) or not isinstance(profiles, dict):
        return ()
    entry = agents.get(agent)
    if not isinstance(entry, dict):
        return ()
    ceiling = entry.get("ceiling")
    profile = profiles.get(entry.get("profile"))
    selected = profile.get("scopes") if isinstance(profile, dict) else None
    if not isinstance(ceiling, list) or not isinstance(selected, list):
        return ()
    allowed = set(item for item in ceiling if isinstance(item, str))
    return tuple(item for item in selected if isinstance(item, str) and item in allowed)


def _load_legacy(root: Path) -> tuple[dict, dict, dict, Path]:
    pointer_path = root / "active.json"
    pointer = _json_object(pointer_path)
    if set(pointer) != {"version", "manifest"}:
        raise VaultMigrationError("invalid_legacy_metadata")
    version = pointer.get("version")
    manifest_rel = pointer.get("manifest")
    if (
        not isinstance(version, str)
        or _VERSION_RE.fullmatch(version) is None
        or not isinstance(manifest_rel, str)
    ):
        raise VaultMigrationError("invalid_legacy_metadata")
    manifest_path = (root / manifest_rel).resolve(strict=True)
    try:
        manifest_path.relative_to((root / "manifests").resolve(strict=True))
    except (OSError, ValueError):
        raise VaultMigrationError("invalid_legacy_metadata") from None
    manifest = _json_object(manifest_path)
    if manifest.get("version") != version or not isinstance(manifest.get("stores"), dict):
        raise VaultMigrationError("invalid_legacy_metadata")
    policy = _json_object(root / "config.json")
    return pointer, manifest, policy, manifest_path


def migrate_fixed_layout(
    scope_root: str | Path, *, apply: bool = False
) -> MigrationSummary:
    root = Path(scope_root)
    registry_path = root / "registry.json"
    pointer, manifest, policy, manifest_path = _load_legacy(root)
    registry = None
    if registry_path.exists() or registry_path.is_symlink():
        registry = load_registry(registry_path)
    store_ids = tuple(manifest["stores"])
    if not all(
        isinstance(item, str) and _VERSION_RE.fullmatch(item)
        for item in store_ids
    ):
        raise VaultMigrationError("invalid_legacy_metadata")
    opaque_ids = tuple(is_vault_id(item) for item in store_ids)
    if any(opaque_ids):
        if (
            registry is None
            or not all(opaque_ids)
            or not set(store_ids).issubset(vault.id for vault in registry.vaults)
        ):
            raise VaultMigrationError("invalid_legacy_metadata")
        return MigrationSummary(
            "already-migrated",
            len(registry.vaults),
            0,
            str(pointer.get("version", "")),
        )

    old_ids = store_ids
    portable = _legacy_selection(policy, "codex")
    if len(portable) != 2 or not set(portable).issubset(old_ids):
        raise VaultMigrationError("portable_roles_unavailable")
    new_version = f"{pointer['version']}-registry"
    if _VERSION_RE.fullmatch(new_version) is None:
        raise VaultMigrationError("invalid_legacy_metadata")
    if not apply:
        return MigrationSummary("dry-run", len(old_ids), 0, new_version)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "migration-backups" / f"{stamp}-{secrets.token_hex(4)}"
    backup.mkdir(parents=True, mode=0o700)
    backup_sources = [root / "config.json", root / "active.json", manifest_path]
    if registry is not None:
        backup_sources.append(registry_path)
    for source in backup_sources:
        shutil.copy2(source, backup / source.name)

    temporary_registry = root / f".registry-migration-{secrets.token_hex(6)}.json"
    if registry is None:
        registry = initialize_registry(
            temporary_registry,
            guidance_name="Guidance",
            solutions_name="Solutions",
        )
    else:
        shutil.copy2(registry_path, temporary_registry)
        os.chmod(temporary_registry, 0o600)
        registry = load_registry(temporary_registry)
    mapping = {
        portable[0]: registry.for_role(VaultRole.GUIDANCE).id,
        portable[1]: registry.for_role(VaultRole.SOLUTIONS).id,
    }
    custom_number = 0
    for old_id in old_ids:
        if old_id in mapping:
            continue
        custom_number += 1
        record = add_vault(temporary_registry, name=f"Vault {custom_number}")
        mapping[old_id] = record.id

    registry = load_registry(temporary_registry)
    old_agents = policy.get("agents") if isinstance(policy.get("agents"), dict) else {}
    for agent in old_agents:
        if agent not in registry.agents:
            selected = tuple(
                mapping[item]
                for item in _legacy_selection(policy, agent)
                if item in mapping
            )
            set_agent_access(temporary_registry, agent, selected)
    registry = load_registry(temporary_registry)

    new_stores = {}
    copied = 0
    for old_id, entry in manifest["stores"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise VaultMigrationError("invalid_legacy_metadata")
        source = (root / entry["path"]).resolve(strict=True)
        try:
            source.relative_to((root / old_id / pointer["version"]).resolve(strict=True))
        except (OSError, ValueError):
            raise VaultMigrationError("invalid_legacy_store") from None
        if source.name != "graph.db" or _digest(source) != entry.get("sha256"):
            raise VaultMigrationError("invalid_legacy_store")
        new_id = mapping[old_id]
        destination = root / new_id / new_version / "graph.db"
        if destination.exists() or destination.is_symlink():
            raise VaultMigrationError("migration_collision")
        destination.parent.mkdir(parents=True, mode=0o700)
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
        copied += 1
        new_entry = dict(entry)
        new_entry["path"] = f"{new_id}/{new_version}/graph.db"
        new_stores[new_id] = new_entry

    capabilities = {}
    for agent, entry in old_agents.items():
        if not isinstance(entry, dict):
            continue
        capabilities[agent] = {
            name: entry.get(name) is True
            for name in ("recall", "boot-injection", "capture")
        }
    new_policy = {"agents": capabilities}
    registry_hash = _digest(temporary_registry)
    new_manifest = {
        key: value
        for key, value in manifest.items()
        if key not in {"version", "stores", "registry_sha256"}
    }
    new_manifest.update(
        {
            "version": new_version,
            "registry_sha256": registry_hash,
            "stores": new_stores,
        }
    )
    new_manifest_path = root / "manifests" / f"{new_version}.json"
    if new_manifest_path.exists() or new_manifest_path.is_symlink():
        raise VaultMigrationError("migration_collision")
    _atomic_json(new_manifest_path, new_manifest)
    os.replace(temporary_registry, registry_path)
    try:
        temporary_registry.with_name(
            f".{temporary_registry.name}.lock"
        ).unlink()
    except FileNotFoundError:
        pass
    _atomic_json(root / "config.json", new_policy)
    _atomic_json(
        root / "active.json",
        {"version": new_version, "manifest": f"manifests/{new_version}.json"},
    )
    return MigrationSummary("migrated", len(registry.vaults), copied, new_version)

"""Fail-closed policy resolution for physically separated knowledge stores.

This module deals only in configuration and content-free manifests. It must
never open a graph database or fall back to the legacy shared graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agam import paths
from agam.vault_registry import RegistryError, is_vault_id, load_registry


_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MANIFEST_KEYS = frozenset(
    {
        "version",
        "stores",
        "source_sha256",
        "source_snapshot_sha256",
        "staging_sha256",
        "registry_sha256",
        "model",
        "created_at",
    }
)
_STORE_KEYS = frozenset(
    {"path", "sha256", "entities", "relationships", "properties"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_JSON_BYTES = 1024 * 1024


def _open_regular_file(path: Path) -> tuple[int, os.stat_result] | None:
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError:
        return None
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        return None
    return descriptor, opened


def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _read_regular_file(path: Path, *, limit: int) -> bytes | None:
    opened = _open_regular_file(path)
    if opened is None:
        return None
    descriptor, before = opened
    if before.st_size > limit:
        os.close(descriptor)
        return None
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
        after = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if not _same_file_state(before, after):
        return None
    return b"".join(chunks)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    raw = _read_regular_file(path, limit=_MAX_JSON_BYTES)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256_file(path: Path) -> str | None:
    opened = _open_regular_file(path)
    if opened is None:
        return None
    descriptor, before = opened
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if not _same_file_state(before, after):
        return None
    return digest.hexdigest()


def _contained_file(path: Path, parent: Path) -> Path | None:
    """Resolve an existing regular file and prove it stays below *parent*."""
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_parent)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path if resolved_path.is_file() else None


def _valid_version(value: object) -> str | None:
    if isinstance(value, str) and _VERSION_RE.fullmatch(value):
        return value
    return None


def _valid_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_manifest_metadata(manifest: Mapping[str, Any]) -> bool:
    for hash_key in (
        "source_sha256",
        "source_snapshot_sha256",
        "staging_sha256",
        "registry_sha256",
    ):
        declared_hash = manifest.get(hash_key)
        if declared_hash is not None and (
            not isinstance(declared_hash, str)
            or _SHA256_RE.fullmatch(declared_hash) is None
        ):
            return False
    model = manifest.get("model")
    if model is not None and (
        not isinstance(model, str) or not model or len(model) > 128
    ):
        return False
    created_at = manifest.get("created_at")
    return created_at is None or (
        isinstance(created_at, str) and 0 < len(created_at) <= 64
    )


def load_active_manifest(
    active_path: str | Path | None = None,
    *,
    scope_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load a content-free active manifest pointer, returning ``None`` on doubt."""
    root = Path(scope_root) if scope_root is not None else paths.scopes_dir()
    pointer_path = Path(active_path) if active_path is not None else root / "active.json"
    try:
        resolved_root = root.resolve(strict=True)
        if pointer_path.resolve(strict=True) != resolved_root / "active.json":
            return None
    except (OSError, RuntimeError):
        return None
    try:
        registry = load_registry(resolved_root / "registry.json")
    except RegistryError:
        return None
    pointer = _read_json_object(pointer_path)
    if pointer is None or set(pointer) != {"version", "manifest"}:
        return None

    version = _valid_version(pointer.get("version"))
    manifest_value = pointer.get("manifest")
    if version is None or not isinstance(manifest_value, str):
        return None
    manifest_rel = Path(manifest_value)
    if manifest_rel.is_absolute():
        return None

    manifest_path = _contained_file(
        resolved_root / manifest_rel, resolved_root / "manifests"
    )
    if manifest_path is None or manifest_path.name != f"{version}.json":
        return None

    manifest = _read_json_object(manifest_path)
    if (
        manifest is None
        or not {"version", "stores"}.issubset(manifest)
        or not set(manifest).issubset(_MANIFEST_KEYS)
        or manifest.get("version") != version
        or not isinstance(manifest.get("stores"), dict)
        or not _valid_manifest_metadata(manifest)
    ):
        return None

    stores = manifest["stores"]
    declared_registry_hash = manifest.get("registry_sha256")
    if (
        declared_registry_hash is not None
        and _sha256_file(resolved_root / "registry.json") != declared_registry_hash
    ):
        return None
    active_ids = {vault.id for vault in registry.active}
    if not set(stores).issubset(active_ids):
        return None
    for entry in stores.values():
        if (
            not isinstance(entry, dict)
            or set(entry) != _STORE_KEYS
            or not isinstance(entry.get("path"), str)
        ):
            return None
        store_hash = entry.get("sha256")
        if store_hash is not None and (
            not isinstance(store_hash, str) or _SHA256_RE.fullmatch(store_hash) is None
        ):
            return None
        for count_key in ("entities", "relationships", "properties"):
            if count_key in entry and not _valid_count(entry[count_key]):
                return None
    return manifest


def resolve_scope_db(
    scope: str,
    manifest: Mapping[str, Any],
    active_path: str | Path | None = None,
    *,
    scope_root: str | Path | None = None,
) -> Path | None:
    """Resolve one manifest store only if it is in its exact versioned root."""
    if not is_vault_id(scope):
        return None
    version = _valid_version(manifest.get("version"))
    stores = manifest.get("stores")
    if version is None or not isinstance(stores, Mapping):
        return None
    entry = stores.get(scope)
    if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
        return None

    rel_path = Path(entry["path"])
    if rel_path.is_absolute():
        return None
    root = Path(scope_root) if scope_root is not None else paths.scopes_dir()
    pointer_path = Path(active_path) if active_path is not None else root / "active.json"
    try:
        resolved_root = root.resolve(strict=True)
        if pointer_path.resolve(strict=True) != resolved_root / "active.json":
            return None
        registry = load_registry(resolved_root / "registry.json")
        if scope not in {vault.id for vault in registry.active}:
            return None
        declared_scope = resolved_root / scope
        if declared_scope.resolve(strict=True) != declared_scope:
            return None
        expected_parent = declared_scope / version
        if expected_parent.resolve(strict=True) != expected_parent:
            return None
    except (OSError, RuntimeError, RegistryError):
        return None
    candidate = _contained_file(resolved_root / rel_path, expected_parent)
    if candidate is None:
        return None
    try:
        if candidate.parent != expected_parent.resolve(strict=True):
            return None
    except (OSError, RuntimeError):
        return None
    if candidate.name != "graph.db":
        return None
    declared_hash = entry.get("sha256")
    if not isinstance(declared_hash, str) or _sha256_file(candidate) != declared_hash:
        return None
    # Consumers must still open promptly and re-check immutable version state;
    # pathname and digest validation cannot fully eliminate an open-time TOCTOU.
    return candidate


def _load_policy(config_path: Path) -> dict[str, Any] | None:
    policy = _read_json_object(config_path)
    if policy is None or set(policy) != {"agents"}:
        return None
    agents = policy.get("agents")
    if not isinstance(agents, dict):
        return None
    allowed_agent_keys = {"recall", "boot-injection", "capture"}
    for name, value in agents.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            return None
        if set(value) != allowed_agent_keys:
            return None
        for capability in ("recall", "boot-injection", "capture"):
            if not isinstance(value[capability], bool):
                return None
    return policy


def resolve_effective_scopes(
    agent: str,
    profile: str | None = None,
    *,
    config_path: str | Path | None = None,
    active_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    scope_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Return readable vault IDs after intersecting policy and active stores.

    Every parse, validation, or path error returns an empty tuple. Environment
    overrides can narrow the registry selection but never expand it.
    """
    if profile is not None:
        return ()
    environment = os.environ if env is None else env
    root = Path(scope_root) if scope_root is not None else paths.scopes_dir()
    config = Path(config_path) if config_path is not None else root / "config.json"
    active = Path(active_path) if active_path is not None else root / "active.json"
    try:
        resolved_root = root.resolve(strict=True)
        if config.resolve(strict=True) != resolved_root / "config.json":
            return ()
        if active.resolve(strict=True) != resolved_root / "active.json":
            return ()
    except (OSError, RuntimeError):
        return ()

    policy = _load_policy(config)
    manifest = load_active_manifest(active, scope_root=resolved_root)
    try:
        registry = load_registry(resolved_root / "registry.json")
    except RegistryError:
        return ()
    if policy is None or manifest is None:
        return ()
    agent_policy = policy["agents"].get(agent)
    if not isinstance(agent_policy, dict) or agent_policy.get("recall") is not True:
        return ()
    if environment.get("AGAM_KNOWLEDGE_PROFILE") is not None:
        return ()
    requested = {vault.id for vault in registry.selected_for(agent)}
    env_scopes = environment.get("AGAM_KNOWLEDGE_SCOPES")
    if env_scopes is not None:
        narrowed = {part.strip() for part in env_scopes.split(",") if part.strip()}
        if not all(is_vault_id(item) for item in narrowed):
            return ()
        requested.intersection_update(narrowed)
    return tuple(
        vault.id
        for vault in registry.active
        if vault.id in requested
        and resolve_scope_db(
            vault.id, manifest, active, scope_root=resolved_root
        )
        is not None
    )


def resolve_agent_capabilities(
    agent: str,
    *,
    config_path: str | Path | None = None,
    scope_root: str | Path | None = None,
) -> dict[str, bool]:
    """Return explicitly enabled capabilities; every missing value is false."""
    disabled = {"recall": False, "boot-injection": False, "capture": False}
    root = Path(scope_root) if scope_root is not None else paths.scopes_dir()
    config = Path(config_path) if config_path is not None else root / "config.json"
    try:
        resolved_root = root.resolve(strict=True)
        if config.resolve(strict=True) != resolved_root / "config.json":
            return disabled
    except (OSError, RuntimeError):
        return disabled
    policy = _load_policy(config)
    if policy is None or not isinstance(policy["agents"].get(agent), dict):
        return disabled
    configured = policy["agents"][agent]
    return {
        capability: configured.get(capability) is True for capability in disabled
    }

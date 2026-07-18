"""Verified, read-only access to physically separated Agam vaults.

The catalog exposes content-free metadata for every published vault, but row
access is constrained by the selected agent's effective scope policy.  It never
falls back to the legacy mixed graph.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agam.knowledge_scopes import (
    load_active_manifest,
    resolve_effective_scopes,
    resolve_scope_db,
)
from agam.vault_registry import (
    RegistryError,
    VaultRegistry,
    VaultRole,
    load_registry,
)


_MAX_RESULTS = 500


class VaultAccessError(RuntimeError):
    """A stable, content-free vault access failure."""


@dataclass(frozen=True)
class VaultSummary:
    scope: str
    version: str
    entities: int
    relationships: int
    properties: int
    readable: bool
    name: str = ""
    role: str = "custom"
    access: str = "restricted"
    state: str = "active"


@dataclass(frozen=True)
class VaultEntity:
    entity_id: int
    name: str
    kind: str
    description: str
    updated: str | None


class VaultCatalog:
    """Resolve one active manifest and expose only policy-approved row data."""

    def __init__(self, scope_root: str | Path, *, agent: str = "codex") -> None:
        self.scope_root = Path(scope_root)
        self.agent = agent
        self.active_path = self.scope_root / "active.json"
        self.config_path = self.scope_root / "config.json"
        self.registry_path = self.scope_root / "registry.json"
        self._manifest_cache: dict | None = None
        self._readable_cache: frozenset[str] | None = None
        self._registry_cache: VaultRegistry | None = None

    def _registry(self) -> VaultRegistry:
        if self._registry_cache is not None:
            return self._registry_cache
        try:
            self._registry_cache = load_registry(self.registry_path)
        except RegistryError:
            raise VaultAccessError("registry_unavailable") from None
        return self._registry_cache

    def _manifest(self) -> dict:
        if self._manifest_cache is not None:
            return self._manifest_cache
        manifest = load_active_manifest(
            self.active_path, scope_root=self.scope_root
        )
        if manifest is None:
            raise VaultAccessError("active_manifest_unavailable")
        self._manifest_cache = manifest
        return manifest

    def _readable_scopes(self) -> frozenset[str]:
        if self._readable_cache is not None:
            return self._readable_cache
        self._readable_cache = frozenset(
            resolve_effective_scopes(
                self.agent,
                config_path=self.config_path,
                active_path=self.active_path,
                scope_root=self.scope_root,
            )
        )
        return self._readable_cache

    def summaries(self) -> tuple[VaultSummary, ...]:
        registry = self._registry()
        try:
            manifest = self._manifest()
            stores = manifest["stores"]
            readable = self._readable_scopes()
            version = manifest["version"]
        except VaultAccessError:
            stores = {}
            readable = frozenset()
            version = "-"
        output = []
        for vault in registry.vaults:
            store = stores.get(vault.id)
            counts = store if isinstance(store, dict) else {}
            output.append(
                VaultSummary(
                    scope=vault.id,
                    version=version,
                    entities=int(counts.get("entities", 0)),
                    relationships=int(counts.get("relationships", 0)),
                    properties=int(counts.get("properties", 0)),
                    readable=vault.id in readable,
                    name=vault.name,
                    role=vault.role.value,
                    access=vault.access.value,
                    state=vault.state.value,
                )
            )
        return tuple(output)

    def _database(self, scope: str) -> Path:
        try:
            vault = self._registry().by_id(scope)
        except RegistryError:
            raise VaultAccessError("scope_not_readable") from None
        selected = {item.id for item in self._registry().selected_for(self.agent)}
        if not vault.active or scope not in selected:
            raise VaultAccessError("scope_not_readable")
        if scope not in self._readable_scopes():
            raise VaultAccessError("store_unavailable")
        manifest = self._manifest()
        database = resolve_scope_db(
            scope,
            manifest,
            self.active_path,
            scope_root=self.scope_root,
        )
        if database is None:
            raise VaultAccessError("store_unavailable")
        return database

    def _connect(self, scope: str) -> sqlite3.Connection:
        database = self._database(scope)
        try:
            vault = self._registry().by_id(scope)
        except RegistryError:
            raise VaultAccessError("scope_not_readable") from None
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{database.as_uri()}?mode=ro&immutable=1", uri=True
            )
            connection.execute("PRAGMA query_only=ON")
            prefix = f"[VAULT:{scope}]%"
            if vault.role is VaultRole.CUSTOM:
                escaped = connection.execute(
                    "SELECT 1 FROM entities WHERE description NOT LIKE ? LIMIT 1",
                    (prefix,),
                ).fetchone()
            else:
                escaped = connection.execute(
                    "SELECT 1 FROM entities "
                    "WHERE description NOT LIKE ? "
                    "AND substr(description, 1, 9) != '[GENERAL]' LIMIT 1",
                    (prefix,),
                ).fetchone()
            if escaped is not None:
                connection.close()
                raise VaultAccessError("wrong_vault_content")
            return connection
        except VaultAccessError:
            raise
        except sqlite3.Error:
            if connection is not None:
                connection.close()
            raise VaultAccessError("store_unavailable") from None

    def entities(
        self, scope: str, *, query: str = "", limit: int = 200
    ) -> tuple[VaultEntity, ...]:
        selected_limit = max(1, min(int(limit), _MAX_RESULTS))
        connection = self._connect(scope)
        try:
            if query:
                pattern = f"%{query}%"
                rows = connection.execute(
                    "SELECT id,name,type,description,updated FROM entities "
                    "WHERE name LIKE ? OR type LIKE ? OR description LIKE ? "
                    "ORDER BY updated DESC,name LIMIT ?",
                    (pattern, pattern, pattern, selected_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,name,type,description,updated FROM entities "
                    "ORDER BY updated DESC,name LIMIT ?",
                    (selected_limit,),
                ).fetchall()
        except sqlite3.Error:
            raise VaultAccessError("store_query_failed") from None
        finally:
            connection.close()
        return tuple(VaultEntity(*row) for row in rows)

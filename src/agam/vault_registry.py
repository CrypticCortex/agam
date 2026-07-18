"""Validated, content-free registry for user-defined knowledge vaults."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar


_VAULT_ID_RE = re.compile(r"vault_[0-9a-f]{24}")
_AGENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_NAME_CHARS = 64
_MAX_HINT_CHARS = 512
_ROOT_KEYS = frozenset({"schema_version", "vaults", "agents"})
_VAULT_KEYS = frozenset(
    {"id", "name", "role", "access", "state", "routing_hint"}
)


class VaultRole(str, Enum):
    GUIDANCE = "guidance"
    SOLUTIONS = "solutions"
    CUSTOM = "custom"


class VaultAccess(str, Enum):
    PORTABLE = "portable"
    RESTRICTED = "restricted"


class VaultState(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class RegistryError(ValueError):
    """Stable validation failure that never includes registry content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VaultRecord:
    id: str
    name: str
    role: VaultRole
    access: VaultAccess
    state: VaultState
    routing_hint: str = ""

    @property
    def active(self) -> bool:
        return self.state is VaultState.ACTIVE


@dataclass(frozen=True, slots=True)
class VaultRegistry:
    schema_version: int
    vaults: tuple[VaultRecord, ...]
    agents: Mapping[str, tuple[str, ...]]

    def for_role(self, role: VaultRole) -> VaultRecord:
        matches = tuple(vault for vault in self.vaults if vault.role is role)
        if len(matches) != 1:
            raise RegistryError("invalid_protected_roles")
        return matches[0]

    def by_id(self, vault_id: str) -> VaultRecord:
        for vault in self.vaults:
            if vault.id == vault_id:
                return vault
        raise RegistryError("unknown_vault")

    def selected_for(self, agent: str) -> tuple[VaultRecord, ...]:
        selected = frozenset(self.agents.get(agent, ()))
        return tuple(vault for vault in self.vaults if vault.id in selected)

    @property
    def active(self) -> tuple[VaultRecord, ...]:
        return tuple(vault for vault in self.vaults if vault.active)


def is_vault_id(value: object) -> bool:
    return isinstance(value, str) and _VAULT_ID_RE.fullmatch(value) is not None


def _clean_text(value: object, *, maximum: int, code: str, empty: bool) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise RegistryError(code)
    if (not empty and not value) or len(value) > maximum:
        raise RegistryError(code)
    if any(not character.isprintable() for character in value):
        raise RegistryError(code)
    return value


def _parse_record(value: object) -> VaultRecord:
    if not isinstance(value, dict) or set(value) != _VAULT_KEYS:
        raise RegistryError("invalid_vault")
    vault_id = value.get("id")
    if not is_vault_id(vault_id):
        raise RegistryError("invalid_vault_id")
    name = _clean_text(
        value.get("name"), maximum=_MAX_NAME_CHARS, code="invalid_vault_name", empty=False
    )
    hint = _clean_text(
        value.get("routing_hint"),
        maximum=_MAX_HINT_CHARS,
        code="invalid_routing_hint",
        empty=True,
    )
    try:
        role = VaultRole(value.get("role"))
        access = VaultAccess(value.get("access"))
        state = VaultState(value.get("state"))
    except (TypeError, ValueError):
        raise RegistryError("invalid_vault") from None
    if role is VaultRole.CUSTOM and access is not VaultAccess.RESTRICTED:
        raise RegistryError("invalid_custom_access")
    if role is not VaultRole.CUSTOM and (
        access is not VaultAccess.PORTABLE or state is not VaultState.ACTIVE
    ):
        raise RegistryError("invalid_protected_vault")
    return VaultRecord(vault_id, name, role, access, state, hint)


def parse_registry(value: object) -> VaultRegistry:
    if not isinstance(value, dict) or set(value) != _ROOT_KEYS:
        raise RegistryError("invalid_registry")
    if value.get("schema_version") != 1:
        raise RegistryError("unsupported_registry_version")
    raw_vaults = value.get("vaults")
    raw_agents = value.get("agents")
    if not isinstance(raw_vaults, list) or not isinstance(raw_agents, dict):
        raise RegistryError("invalid_registry")
    vaults = tuple(_parse_record(item) for item in raw_vaults)
    ids = [vault.id for vault in vaults]
    names = [vault.name.casefold() for vault in vaults]
    if len(ids) != len(set(ids)) or len(names) != len(set(names)):
        raise RegistryError("duplicate_vault")
    for role in (VaultRole.GUIDANCE, VaultRole.SOLUTIONS):
        if sum(vault.role is role for vault in vaults) != 1:
            raise RegistryError("invalid_protected_roles")

    known = {vault.id: vault for vault in vaults}
    agents: dict[str, tuple[str, ...]] = {}
    for agent, selected in raw_agents.items():
        if (
            not isinstance(agent, str)
            or _AGENT_RE.fullmatch(agent) is None
            or not isinstance(selected, list)
            or not all(isinstance(item, str) for item in selected)
            or len(selected) != len(set(selected))
        ):
            raise RegistryError("invalid_agent_selection")
        if any(item not in known or not known[item].active for item in selected):
            raise RegistryError("invalid_agent_selection")
        agents[agent] = tuple(selected)
    return VaultRegistry(1, vaults, MappingProxyType(agents))


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError:
        raise RegistryError("registry_unavailable") from None
    try:
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_REGISTRY_BYTES:
            raise RegistryError("registry_unavailable")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, _MAX_REGISTRY_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_REGISTRY_BYTES:
                raise RegistryError("registry_unavailable")
        closed = os.fstat(descriptor)
    except OSError:
        raise RegistryError("registry_unavailable") from None
    finally:
        os.close(descriptor)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if identity(opened) != identity(closed):
        raise RegistryError("registry_changed")
    return b"".join(chunks)


def load_registry(path: str | Path) -> VaultRegistry:
    try:
        value = json.loads(_read_regular(Path(path)).decode("utf-8"))
    except RegistryError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise RegistryError("invalid_registry") from None
    return parse_registry(value)


def _payload(registry: VaultRegistry) -> dict[str, object]:
    return {
        "schema_version": registry.schema_version,
        "vaults": [
            {
                "id": vault.id,
                "name": vault.name,
                "role": vault.role.value,
                "access": vault.access.value,
                "state": vault.state.value,
                "routing_hint": vault.routing_hint,
            }
            for vault in registry.vaults
        ],
        "agents": {name: list(selected) for name, selected in registry.agents.items()},
    }


def _atomic_write(path: Path, registry: VaultRegistry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(_payload(registry), handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _new_id(existing: Iterable[str] = ()) -> str:
    taken = frozenset(existing)
    while True:
        candidate = f"vault_{secrets.token_hex(12)}"
        if candidate not in taken:
            return candidate


def initialize_registry(
    path: str | Path,
    *,
    guidance_name: str,
    solutions_name: str,
    agents: Iterable[str] = (),
) -> VaultRegistry:
    target = Path(path)
    if target.exists() or target.is_symlink():
        return load_registry(target)
    first = _new_id()
    second = _new_id((first,))
    records = (
        VaultRecord(
            first,
            _clean_text(
                guidance_name,
                maximum=_MAX_NAME_CHARS,
                code="invalid_vault_name",
                empty=False,
            ),
            VaultRole.GUIDANCE,
            VaultAccess.PORTABLE,
            VaultState.ACTIVE,
        ),
        VaultRecord(
            second,
            _clean_text(
                solutions_name,
                maximum=_MAX_NAME_CHARS,
                code="invalid_vault_name",
                empty=False,
            ),
            VaultRole.SOLUTIONS,
            VaultAccess.PORTABLE,
            VaultState.ACTIVE,
        ),
    )
    agent_map: dict[str, tuple[str, ...]] = {}
    for agent in agents:
        if not isinstance(agent, str) or _AGENT_RE.fullmatch(agent) is None:
            raise RegistryError("invalid_agent_selection")
        agent_map[agent] = (first, second)
    registry = parse_registry(
        {
            "schema_version": 1,
            "vaults": [
                {
                    "id": item.id,
                    "name": item.name,
                    "role": item.role.value,
                    "access": item.access.value,
                    "state": item.state.value,
                    "routing_hint": item.routing_hint,
                }
                for item in records
            ],
            "agents": {name: list(selected) for name, selected in agent_map.items()},
        }
    )
    _atomic_write(target, registry)
    return registry


T = TypeVar("T")


def _mutate(
    path: str | Path, operation: Callable[[VaultRegistry], tuple[VaultRegistry, T]]
) -> T:
    target = Path(path)
    lock_path = target.with_name(f".{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = load_registry(target)
        updated, result = operation(current)
        validated = parse_registry(_payload(updated))
        _atomic_write(target, validated)
        return result
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def add_vault(
    path: str | Path, *, name: str, routing_hint: str = ""
) -> VaultRecord:
    def operation(registry: VaultRegistry) -> tuple[VaultRegistry, VaultRecord]:
        record = VaultRecord(
            _new_id(vault.id for vault in registry.vaults),
            _clean_text(
                name,
                maximum=_MAX_NAME_CHARS,
                code="invalid_vault_name",
                empty=False,
            ),
            VaultRole.CUSTOM,
            VaultAccess.RESTRICTED,
            VaultState.ACTIVE,
            _clean_text(
                routing_hint,
                maximum=_MAX_HINT_CHARS,
                code="invalid_routing_hint",
                empty=True,
            ),
        )
        return replace(registry, vaults=(*registry.vaults, record)), record

    return _mutate(path, operation)


def rename_vault(path: str | Path, vault_id: str, name: str) -> VaultRecord:
    clean_name = _clean_text(
        name, maximum=_MAX_NAME_CHARS, code="invalid_vault_name", empty=False
    )

    def operation(registry: VaultRegistry) -> tuple[VaultRegistry, VaultRecord]:
        current = registry.by_id(vault_id)
        updated = replace(current, name=clean_name)
        vaults = tuple(updated if vault.id == vault_id else vault for vault in registry.vaults)
        return replace(registry, vaults=vaults), updated

    return _mutate(path, operation)


def archive_vault(path: str | Path, vault_id: str) -> VaultRecord:
    def operation(registry: VaultRegistry) -> tuple[VaultRegistry, VaultRecord]:
        current = registry.by_id(vault_id)
        if current.role is not VaultRole.CUSTOM:
            raise RegistryError("protected_vault")
        updated = replace(current, state=VaultState.ARCHIVED)
        vaults = tuple(updated if vault.id == vault_id else vault for vault in registry.vaults)
        agents = {
            name: tuple(item for item in selected if item != vault_id)
            for name, selected in registry.agents.items()
        }
        return replace(registry, vaults=vaults, agents=agents), updated

    return _mutate(path, operation)


def restore_vault(path: str | Path, vault_id: str) -> VaultRecord:
    def operation(registry: VaultRegistry) -> tuple[VaultRegistry, VaultRecord]:
        current = registry.by_id(vault_id)
        if current.role is not VaultRole.CUSTOM:
            raise RegistryError("protected_vault")
        updated = replace(current, state=VaultState.ACTIVE)
        vaults = tuple(updated if vault.id == vault_id else vault for vault in registry.vaults)
        return replace(registry, vaults=vaults), updated

    return _mutate(path, operation)


def set_agent_access(
    path: str | Path, agent: str, vault_ids: Iterable[str]
) -> tuple[str, ...]:
    selected = tuple(vault_ids)

    def operation(registry: VaultRegistry) -> tuple[VaultRegistry, tuple[str, ...]]:
        agents = dict(registry.agents)
        agents[agent] = selected
        return replace(registry, agents=agents), selected

    return _mutate(path, operation)

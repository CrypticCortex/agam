"""Fail-closed materialization of classified knowledge into physical scopes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agam.knowledge_classifier import (
    KnowledgeKind,
    PrivacyLabel,
    _load_integrity_key,
    _sqlite_snapshot_sha256,
    _validate_checkpoint,
    _validate_run_integrity,
    sha256_file,
)
from agam.vault_registry import RegistryError, load_registry


SCOPES = ("portable-guidance", "portable-solutions", "restricted")
OFFICIAL_GRAPH_SCHEMA_SHA256 = (
    "c9647c0e0b6ad320486ca7f71bc3959cb2a3d7d71489845503d6017bec2cd91c"
)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_FILE_ID_RE = re.compile(r"[0-9]+")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_CREATED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z"
)
_PREFIX_RE = re.compile(
    r"^\[(?:VAULT:vault_[0-9a-f]{24}|[A-Z_]+)\](?:\s|$)"
)
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_META_KEYS = {
    "schema_version",
    "source_device",
    "source_inode",
    "source_sha256",
    "source_snapshot_sha256",
    "run_id",
    "model",
    "key_sha256",
}


class MaterializationContractError(ValueError):
    """A content-free materialization validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StoreSummary:
    scope: str
    sha256: str
    entities: int
    relationships: int
    properties: int


@dataclass(frozen=True, slots=True)
class MaterializationSummary:
    version: str
    source_sha256: str
    source_snapshot_sha256: str
    staging_sha256: str
    stores: tuple[StoreSummary, ...]


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _AuxiliaryFileState:
    identity: _FileIdentity
    sha256: str


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _generated_version() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def _validate_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise MaterializationContractError("invalid_hash")
    return value


def _validate_version(value: object) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise MaterializationContractError("invalid_version")
    return value


def _regular_file(path: str | Path, code: str) -> Path:
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            raise MaterializationContractError(code)
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except MaterializationContractError:
        raise
    except (OSError, RuntimeError, TypeError):
        raise MaterializationContractError(code) from None
    if not stat.S_ISREG(mode):
        raise MaterializationContractError(code)
    return resolved


def _file_identity(path: Path, code: str) -> _FileIdentity:
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError:
        raise MaterializationContractError(code) from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise MaterializationContractError(code)
    return _FileIdentity(file_stat.st_dev, file_stat.st_ino)


def _verify_file_state(
    path: Path,
    identity: _FileIdentity,
    expected_hash: str,
    code: str,
) -> None:
    """Bind a path to its original inode and bytes across a hash read."""
    before = _file_identity(path, code)
    current_hash = sha256_file(path)
    after = _file_identity(path, code)
    if before != identity or after != identity or current_hash != expected_hash:
        raise MaterializationContractError(code)


def _sqlite_wal_state(path: Path) -> _AuxiliaryFileState | None:
    """Fingerprint a WAL without opening or exposing any database content."""
    wal = path.with_name(f"{path.name}-wal")
    try:
        if not wal.exists() and not wal.is_symlink():
            return None
        identity = _file_identity(wal, "staging_changed")
        wal_hash = sha256_file(wal)
        if wal_hash is None:
            raise MaterializationContractError("staging_changed")
        _verify_file_state(wal, identity, wal_hash, "staging_changed")
        if wal_hash == hashlib.sha256(b"").hexdigest():
            return None
        return _AuxiliaryFileState(identity, wal_hash)
    except MaterializationContractError:
        raise
    except OSError:
        raise MaterializationContractError("staging_changed") from None


def _load_official_schema(path: Path, identity: _FileIdentity) -> str:
    try:
        schema_bytes = path.read_bytes()
    except OSError:
        raise MaterializationContractError("invalid_schema") from None
    schema_hash = hashlib.sha256(schema_bytes).hexdigest()
    _verify_file_state(path, identity, schema_hash, "invalid_schema")
    if schema_hash != OFFICIAL_GRAPH_SCHEMA_SHA256:
        raise MaterializationContractError("invalid_schema")
    try:
        return schema_bytes.decode("utf-8")
    except UnicodeError:
        raise MaterializationContractError("invalid_schema") from None


def _validate_created_at(value: object) -> str:
    if not isinstance(value, str) or _CREATED_AT_RE.fullmatch(value) is None:
        raise MaterializationContractError("invalid_created_at")
    format_string = "%Y-%m-%dT%H:%M:%SZ"
    if "." in value:
        format_string = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        datetime.strptime(value, format_string)
    except ValueError:
        raise MaterializationContractError("invalid_created_at") from None
    return value


def _secure_directory(path: str | Path) -> Path:
    candidate = Path(path)
    try:
        absolute = candidate.absolute()
        if any(
            ancestor.is_symlink()
            for ancestor in (absolute, *absolute.parents)
            if ancestor.exists() or ancestor.is_symlink()
        ):
            raise MaterializationContractError("invalid_output_root")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = candidate.resolve(strict=True)
        if resolved != absolute:
            raise MaterializationContractError("invalid_output_root")
        mode = resolved.stat().st_mode
        if not stat.S_ISDIR(mode):
            raise MaterializationContractError("invalid_output_root")
        os.chmod(resolved, 0o700)
    except MaterializationContractError:
        raise
    except (OSError, RuntimeError, TypeError):
        raise MaterializationContractError("invalid_output_root") from None
    return resolved


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        metadata = dict(
            connection.execute("SELECT key,value FROM agam_classifier_meta")
        )
    except sqlite3.Error:
        raise MaterializationContractError("invalid_provenance") from None
    if (
        set(metadata) != _META_KEYS
        or metadata.get("schema_version") != "1"
        or _HASH_RE.fullmatch(metadata.get("source_sha256", "")) is None
        or _HASH_RE.fullmatch(metadata.get("source_snapshot_sha256", "")) is None
        or _HASH_RE.fullmatch(metadata.get("key_sha256", "")) is None
        or _MODEL_RE.fullmatch(metadata.get("model", "")) is None
        or _FILE_ID_RE.fullmatch(metadata.get("source_device", "")) is None
        or _FILE_ID_RE.fullmatch(metadata.get("source_inode", "")) is None
    ):
        raise MaterializationContractError("invalid_provenance")
    return metadata


def _validate_and_route(
    connection: sqlite3.Connection,
    staging: Path,
    expected_source_hash: str,
    expected_source_snapshot_hash: str,
    expected_source_identity: _FileIdentity,
    allowed_vault_ids: frozenset[str] | None = None,
) -> tuple[dict[int, str | None], str, bytes]:
    metadata = _metadata(connection)
    if metadata["source_sha256"] != expected_source_hash:
        raise MaterializationContractError("source_hash_mismatch")
    if metadata["source_snapshot_sha256"] != expected_source_snapshot_hash:
        raise MaterializationContractError("source_snapshot_mismatch")
    if (
        int(metadata["source_device"]) != expected_source_identity.device
        or int(metadata["source_inode"]) != expected_source_identity.inode
    ):
        raise MaterializationContractError("source_changed")
    try:
        integrity_key = _load_integrity_key(staging, create=False)
        if hashlib.sha256(integrity_key).hexdigest() != metadata["key_sha256"]:
            raise MaterializationContractError("invalid_provenance")
        _validate_checkpoint(
            connection,
            integrity_key,
            metadata["source_snapshot_sha256"],
            metadata["run_id"],
        )
        _validate_run_integrity(connection, integrity_key, required=True)
    except MaterializationContractError:
        raise
    except Exception:
        raise MaterializationContractError("invalid_integrity") from None

    try:
        entity_count = connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rows = connection.execute(
            "SELECT e.id,e.description,c.vault_id,c.privacy,c.kind,c.confidence,c.reason_code,"
            "c.failed,c.classified_at,c.integrity "
            "FROM entities e LEFT JOIN agam_classifications c ON c.entity_id=e.id "
            "ORDER BY e.id"
        ).fetchall()
    except sqlite3.Error:
        raise MaterializationContractError("invalid_classification") from None
    if len(rows) != entity_count:
        raise MaterializationContractError("classification_incomplete")

    routes: dict[int, str | None] = {}
    for row in rows:
        (
            entity_id,
            description,
            vault_id,
            privacy_raw,
            kind_raw,
            confidence,
            reason_code,
            failed,
            classified_at,
            integrity,
        ) = row
        try:
            privacy = PrivacyLabel(privacy_raw)
            kind = KnowledgeKind(kind_raw)
        except (TypeError, ValueError):
            raise MaterializationContractError("invalid_classification") from None
        if (
            not isinstance(entity_id, int)
            or isinstance(entity_id, bool)
            or not isinstance(description, str)
            or _PREFIX_RE.match(description) is None
            or (
                vault_id is None
                and not description.startswith(f"[{privacy.value}]")
            )
            or (
                vault_id is not None
                and not description.startswith(f"[VAULT:{vault_id}]")
            )
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
            or not isinstance(reason_code, str)
            or _REASON_RE.fullmatch(reason_code) is None
            or failed not in (0, 1)
            or not isinstance(classified_at, str)
            or not classified_at
            or not isinstance(integrity, str)
            or not integrity
            or (privacy is PrivacyLabel.REVIEW) != (kind is KnowledgeKind.REVIEW)
        ):
            raise MaterializationContractError("invalid_classification")

        route: str | None = None
        if vault_id is not None:
            if (
                allowed_vault_ids is None
                or vault_id not in allowed_vault_ids
                or privacy is PrivacyLabel.REVIEW
                or kind is KnowledgeKind.REVIEW
            ):
                raise MaterializationContractError("invalid_classification")
            route = vault_id
        elif privacy is PrivacyLabel.RESTRICTED and kind is KnowledgeKind.CUSTOM:
            route = "restricted"
        elif privacy is PrivacyLabel.PORTABLE and kind is KnowledgeKind.GUIDANCE:
            route = "portable-guidance"
        elif privacy is PrivacyLabel.PORTABLE and kind is KnowledgeKind.SOLUTION:
            route = "portable-solutions"
        routes[entity_id] = route
    return routes, metadata["model"], integrity_key


def _apply_schema(database: Path, schema: str) -> sqlite3.Connection:
    descriptor = os.open(
        database,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(schema)
    except sqlite3.Error:
        connection.close()
        raise MaterializationContractError("invalid_schema") from None
    return connection


def _copy_scope(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    routes: dict[int, str | None],
    scope: str,
    *,
    opaque: bool = False,
) -> tuple[int, int, int]:
    entity_ids = tuple(entity_id for entity_id, route in routes.items() if route == scope)
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        entities = source.execute(
            f"SELECT id,name,type,description,created,updated,last_referenced "
            f"FROM entities WHERE id IN ({placeholders}) ORDER BY id",
            entity_ids,
        ).fetchall()
        if opaque and any(
            not row[3].startswith(f"[VAULT:{scope}]") for row in entities
        ):
            raise MaterializationContractError("vault_prefix_required")
        if not opaque and scope in {"portable-guidance", "portable-solutions"} and any(
            not row[3].startswith("[PORTABLE]") for row in entities
        ):
            raise MaterializationContractError("general_prefix_required")
        target.executemany(
            "INSERT INTO entities(id,name,type,description,created,updated,last_referenced) "
            "VALUES(?,?,?,?,?,?,?)",
            entities,
        )
        properties = source.execute(
            f"SELECT id,entity_id,key,value,updated FROM properties "
            f"WHERE entity_id IN ({placeholders}) ORDER BY id",
            entity_ids,
        ).fetchall()
        target.executemany(
            "INSERT INTO properties(id,entity_id,key,value,updated) VALUES(?,?,?,?,?)",
            properties,
        )
        relationships = source.execute(
            f"SELECT id,source_id,target_id,relation,weight,created FROM relationships "
            f"WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders}) "
            "ORDER BY id",
            entity_ids + entity_ids,
        ).fetchall()
        target.executemany(
            "INSERT INTO relationships(id,source_id,target_id,relation,weight,created) "
            "VALUES(?,?,?,?,?,?)",
            relationships,
        )
    else:
        entities = []
        properties = []
        relationships = []

    for table in ("entities_fts", "entities_fts_v2"):
        try:
            target.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        except sqlite3.Error:
            raise MaterializationContractError("fts_rebuild_failed") from None
    target.commit()
    return len(entities), len(relationships), len(properties)


def _fsync_directory(directory: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _PostReplaceSyncError(OSError):
    """A pathname changed but its parent durability is not confirmed."""


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise OSError
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    try:
        _fsync_directory(path.parent)
    except OSError:
        raise _PostReplaceSyncError from None


def materialize_graph(
    source_path: str | Path,
    staging_path: str | Path,
    scopes_root: str | Path,
    schema_path: str | Path,
    *,
    source_sha256: str,
    source_snapshot_sha256: str,
    staging_sha256: str,
    registry_path: str | Path | None = None,
    version: str | None = None,
    created_at: str | None = None,
) -> MaterializationSummary:
    """Build immutable vault databases and atomically activate a manifest."""
    expected_source_hash = _validate_hash(source_sha256)
    expected_source_snapshot_hash = _validate_hash(source_snapshot_sha256)
    expected_staging_hash = _validate_hash(staging_sha256)
    selected_version = _validate_version(version or _generated_version())
    if created_at is None:
        created_at = _now()
    created_at = _validate_created_at(created_at)

    source = _regular_file(source_path, "invalid_source")
    staging = _regular_file(staging_path, "invalid_staging")
    schema_file = _regular_file(schema_path, "invalid_schema")
    source_identity = _file_identity(source, "invalid_source")
    staging_identity = _file_identity(staging, "invalid_staging")
    schema_identity = _file_identity(schema_file, "invalid_schema")
    staging_wal_state = _sqlite_wal_state(staging)
    try:
        same_input = os.path.samefile(source, staging)
    except OSError:
        raise MaterializationContractError("source_staging_conflict") from None
    if same_input:
        raise MaterializationContractError("source_staging_conflict")
    try:
        _verify_file_state(
            source, source_identity, expected_source_hash, "source_hash_mismatch"
        )
        _verify_file_state(
            staging,
            staging_identity,
            expected_staging_hash,
            "staging_hash_mismatch",
        )
    except MaterializationContractError:
        raise
    try:
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro", uri=True
        )
        source_connection.execute("PRAGMA query_only=ON")
        current_snapshot_hash = _sqlite_snapshot_sha256(source_connection)
        source_connection.close()
    except sqlite3.Error:
        raise MaterializationContractError("invalid_source") from None
    if current_snapshot_hash != expected_source_snapshot_hash:
        raise MaterializationContractError("source_snapshot_mismatch")
    schema = _load_official_schema(schema_file, schema_identity)

    scope_root = _secure_directory(scopes_root)
    registry_file: Path | None = None
    registry_identity: _FileIdentity | None = None
    registry_hash: str | None = None
    if registry_path is None:
        selected_scopes = SCOPES
    else:
        registry_file = _regular_file(registry_path, "invalid_registry")
        if registry_file.parent != scope_root:
            raise MaterializationContractError("invalid_registry")
        registry_identity = _file_identity(registry_file, "invalid_registry")
        registry_hash = sha256_file(registry_file)
        try:
            registry = load_registry(registry_file)
        except RegistryError:
            raise MaterializationContractError("invalid_registry") from None
        selected_scopes = tuple(vault.id for vault in registry.active)
        if not selected_scopes or registry_hash is None:
            raise MaterializationContractError("invalid_registry")
    manifests = _secure_directory(scope_root / "manifests")
    builds = _secure_directory(scope_root / ".builds")
    manifest_path = manifests / f"{selected_version}.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise MaterializationContractError("version_collision")
    final_directories = {
        scope: scope_root / scope / selected_version for scope in selected_scopes
    }
    for scope, final in final_directories.items():
        scope_directory = _secure_directory(scope_root / scope)
        if final.parent != scope_directory or final.exists() or final.is_symlink():
            raise MaterializationContractError("version_collision")

    build_root = builds / f"{selected_version}.{secrets.token_hex(8)}"
    if build_root.exists() or build_root.is_symlink():
        raise MaterializationContractError("version_collision")
    build_root.mkdir(mode=0o700)

    connection: sqlite3.Connection | None = None
    outputs: dict[str, sqlite3.Connection] = {}
    stores: list[StoreSummary] = []
    try:
        connection = sqlite3.connect(f"{staging.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        staging_data_version = connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]
        connection.execute("BEGIN")
        routes, model, integrity_key = _validate_and_route(
            connection,
            staging,
            expected_source_hash,
            expected_source_snapshot_hash,
            source_identity,
            frozenset(selected_scopes) if registry_file is not None else None,
        )
        for scope in selected_scopes:
            scope_build = build_root / scope / selected_version
            scope_build.mkdir(parents=True, mode=0o700)
            os.chmod(scope_build.parent, 0o700)
            database = scope_build / "graph.db"
            target = _apply_schema(database, schema)
            outputs[scope] = target
            entities, relationships, properties = _copy_scope(
                connection,
                target,
                routes,
                scope,
                opaque=registry_file is not None,
            )
            target.close()
            del outputs[scope]
            os.chmod(database, 0o600, follow_symlinks=False)
            database_hash = sha256_file(database)
            if database_hash is None:
                raise MaterializationContractError("output_hash_failed")
            stores.append(
                StoreSummary(
                    scope=scope,
                    sha256=database_hash,
                    entities=entities,
                    relationships=relationships,
                    properties=properties,
                )
            )

        _verify_file_state(
            source, source_identity, expected_source_hash, "source_changed"
        )
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro", uri=True
        )
        source_connection.execute("PRAGMA query_only=ON")
        final_snapshot_hash = _sqlite_snapshot_sha256(source_connection)
        source_connection.close()
        if final_snapshot_hash != expected_source_snapshot_hash:
            raise MaterializationContractError("source_changed")
        try:
            _validate_checkpoint(
                connection,
                integrity_key,
                expected_source_snapshot_hash,
                dict(connection.execute("SELECT key,value FROM agam_classifier_meta"))[
                    "run_id"
                ],
            )
            _validate_run_integrity(connection, integrity_key, required=True)
        except Exception:
            raise MaterializationContractError("invalid_integrity") from None
        if (
            connection.execute("PRAGMA data_version").fetchone()[0]
            != staging_data_version
        ):
            raise MaterializationContractError("staging_changed")
        if _sqlite_wal_state(staging) != staging_wal_state:
            raise MaterializationContractError("staging_changed")
        _verify_file_state(
            staging, staging_identity, expected_staging_hash, "staging_changed"
        )
        if registry_file is not None and registry_identity is not None:
            _verify_file_state(
                registry_file,
                registry_identity,
                registry_hash,
                "registry_changed",
            )

        for scope in selected_scopes:
            built_version = build_root / scope / selected_version
            final_version = final_directories[scope]
            _fsync_directory(built_version)
            os.replace(built_version, final_version)
            _fsync_directory(final_version.parent)
            _fsync_directory(built_version.parent)

        manifest = {
            "version": selected_version,
            "source_sha256": expected_source_hash,
            "source_snapshot_sha256": expected_source_snapshot_hash,
            "staging_sha256": expected_staging_hash,
            "model": model,
            "created_at": created_at,
            **(
                {"registry_sha256": registry_hash}
                if registry_hash is not None
                else {}
            ),
            "stores": {
                store.scope: {
                    "path": f"{store.scope}/{selected_version}/graph.db",
                    "sha256": store.sha256,
                    "entities": store.entities,
                    "relationships": store.relationships,
                    "properties": store.properties,
                }
                for store in stores
            },
        }
        _atomic_json(manifest_path, manifest)
        try:
            _atomic_json(
                scope_root / "active.json",
                {
                    "version": selected_version,
                    "manifest": f"manifests/{selected_version}.json",
                },
            )
        except _PostReplaceSyncError:
            # The pointer rename happened, but a power-loss-durable outcome
            # cannot be asserted. Do not misreport this as an ordinary failure
            # that preserved the previous activation.
            raise MaterializationContractError("activation_uncertain") from None
        return MaterializationSummary(
            version=selected_version,
            source_sha256=expected_source_hash,
            source_snapshot_sha256=expected_source_snapshot_hash,
            staging_sha256=expected_staging_hash,
            stores=tuple(stores),
        )
    except MaterializationContractError:
        raise
    except (OSError, sqlite3.Error):
        raise MaterializationContractError("materialization_failed") from None
    finally:
        if connection is not None:
            connection.close()
        for output in outputs.values():
            output.close()

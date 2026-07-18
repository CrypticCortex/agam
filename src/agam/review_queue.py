"""Integrity-preserving operations for one sealed classifier review queue."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agam.knowledge_classifier import (
    ClassificationRunSummary,
    classify_graph,
    sha256_file,
)
from agam.knowledge_scopes import load_active_manifest
from agam.vault_registry import RegistryError, load_registry


class ReviewQueueError(RuntimeError):
    """A stable, content-free review workflow failure."""


@dataclass(frozen=True, slots=True)
class ReviewItem:
    opaque_id: str
    reason_code: str
    failed: bool
    classified_at: str


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    opaque_id: str
    name: str = field(repr=False)
    kind: str
    description: str = field(repr=False)
    reason_code: str
    failed: bool


class _ValidationRunner:
    model = "haiku"

    def __call__(self, _prompt: str) -> str:
        raise ReviewQueueError("unexpected_unclassified_item")


class _DecisionRunner:
    model = "haiku"

    def __init__(self, opaque_id: str, privacy: str, kind: str) -> None:
        self.opaque_id = opaque_id
        self.privacy = privacy
        self.kind = kind

    def __call__(self, _prompt: str) -> str:
        return json.dumps(
            {
                "items": [
                    {
                        "id": self.opaque_id,
                        "privacy": self.privacy,
                        "kind": self.kind,
                        "confidence": 1.0,
                        "reason_code": "user_confirmed",
                    }
                ]
            }
        )


class _VaultDecisionRunner:
    model = "haiku"

    def __init__(self, opaque_id: str, vault_id: str) -> None:
        self.opaque_id = opaque_id
        self.vault_id = vault_id

    def __call__(self, _prompt: str) -> str:
        return json.dumps(
            {
                "items": [
                    {
                        "id": self.opaque_id,
                        "vault_id": self.vault_id,
                        "confidence": 1.0,
                        "reason_code": "user_confirmed",
                    }
                ]
            }
        )


_MANUAL_ROUTES = frozenset(
    {
        ("PORTABLE", "GUIDANCE"),
        ("PORTABLE", "SOLUTION"),
        ("RESTRICTED", "CUSTOM"),
    }
)


class ReviewQueue:
    """Read and resolve review rows without bypassing classifier seals."""

    def __init__(
        self,
        source: str | Path,
        staging: str | Path,
        *,
        pointer_path: str | Path | None = None,
        data_home: str | Path | None = None,
        registry_path: str | Path | None = None,
    ) -> None:
        self.source = Path(source)
        self.staging = Path(staging)
        self.pointer_path = Path(pointer_path) if pointer_path is not None else None
        self.data_home = Path(data_home) if data_home is not None else None
        self.registry_path = (
            Path(registry_path) if registry_path is not None else None
        )

    @classmethod
    def discover(cls, data_home: str | Path) -> "ReviewQueue":
        """Find the active sealed run without exposing paths in UI output."""
        home = Path(data_home).resolve(strict=True)
        knowledge = home / "knowledge"
        sealed = knowledge / "sealed"
        pointer = sealed / "review-active.json"

        if pointer.exists() and not pointer.is_symlink():
            try:
                if pointer.stat().st_size > 4096:
                    raise ValueError
                data = json.loads(pointer.read_text(encoding="utf-8"))
                if set(data) != {"source", "staging", "source_sha256", "staging_sha256"}:
                    raise ValueError
                source = (home / data["source"]).resolve(strict=True)
                staging = (home / data["staging"]).resolve(strict=True)
                source.relative_to(sealed / "sources")
                staging.relative_to(sealed / "staging")
                if (
                    sha256_file(source) != data["source_sha256"]
                    or sha256_file(staging) != data["staging_sha256"]
                ):
                    raise ValueError
                queue = cls(
                    source,
                    staging,
                    pointer_path=pointer,
                    data_home=home,
                    registry_path=home / "knowledge" / "scopes" / "registry.json",
                )
                queue._validate()
                return queue
            except (OSError, RuntimeError, TypeError, ValueError, KeyError):
                raise ReviewQueueError("review_pointer_invalid") from None

        scopes = knowledge / "scopes"
        manifest = load_active_manifest(scopes / "active.json", scope_root=scopes)
        if manifest is None:
            raise ReviewQueueError("active_manifest_unavailable")
        source_hash = manifest.get("source_sha256")
        staging_hash = manifest.get("staging_sha256")
        if not isinstance(source_hash, str) or not isinstance(staging_hash, str):
            raise ReviewQueueError("review_provenance_unavailable")

        sources = [
            path
            for path in sorted((sealed / "sources").glob("*.db"))
            if not path.is_symlink() and sha256_file(path) == source_hash
        ]
        staging_runs = [
            path
            for path in sorted((sealed / "staging").glob("*.db"))
            if not path.is_symlink() and sha256_file(path) == staging_hash
        ]
        for staging in staging_runs:
            for source in sources:
                queue = cls(
                    source,
                    staging,
                    pointer_path=pointer,
                    data_home=home,
                    registry_path=scopes / "registry.json",
                )
                try:
                    queue._validate()
                except Exception:
                    continue
                return queue
        raise ReviewQueueError("sealed_review_run_unavailable")

    def _save_pointer(self, summary: ClassificationRunSummary) -> None:
        if self.pointer_path is None or self.data_home is None:
            return
        try:
            sealed = self.data_home / "knowledge" / "sealed"
            source = self.source.resolve(strict=True)
            staging = self.staging.resolve(strict=True)
            source.relative_to(sealed / "sources")
            staging.relative_to(sealed / "staging")
            source_rel = source.relative_to(self.data_home)
            staging_rel = staging.relative_to(self.data_home)
        except (OSError, ValueError):
            raise ReviewQueueError("review_pointer_invalid") from None
        payload = {
            "source": str(source_rel),
            "staging": str(staging_rel),
            "source_sha256": summary.source_sha256,
            "staging_sha256": summary.staging_sha256,
        }
        self.pointer_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.pointer_path.parent,
            prefix=f".{self.pointer_path.name}.",
            delete=False,
        ) as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.pointer_path)

    def _validate(self) -> ClassificationRunSummary:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            pending = connection.execute(
                "SELECT 1 FROM agam_classifications WHERE privacy IS NULL LIMIT 1"
            ).fetchone()
        except (OSError, sqlite3.Error):
            raise ReviewQueueError("sealed_review_run_unavailable") from None
        finally:
            if connection is not None:
                connection.close()
        if pending is not None:
            raise ReviewQueueError("review_run_incomplete")
        return classify_graph(
            self.source,
            self.staging,
            _ValidationRunner(),
            model="haiku",
            registry_path=self.registry_path,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.staging.resolve(strict=True).as_uri()}?mode=ro", uri=True
        )
        connection.execute("PRAGMA query_only=ON")
        return connection

    def items(self) -> tuple[ReviewItem, ...]:
        self._validate()
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT opaque_id,reason_code,failed,classified_at "
                "FROM agam_classifications WHERE privacy='REVIEW' "
                "AND kind='REVIEW' ORDER BY classified_at,opaque_id"
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            ReviewItem(row[0], row[1], bool(row[2]), row[3]) for row in rows
        )

    def detail(self, opaque_id: str) -> ReviewDetail:
        self._validate()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT c.opaque_id,e.name,e.type,e.description,c.reason_code,c.failed "
                "FROM agam_classifications c JOIN entities e ON e.id=c.entity_id "
                "WHERE c.opaque_id=? AND c.privacy='REVIEW' AND c.kind='REVIEW'",
                (opaque_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ReviewQueueError("review_item_unavailable")
        return ReviewDetail(row[0], row[1], row[2], row[3], row[4], bool(row[5]))

    def retry(
        self, opaque_id: str, runner: Callable[[str], str]
    ) -> ClassificationRunSummary:
        summary = classify_graph(
            self.source,
            self.staging,
            runner,
            model="haiku",
            retry_review_id=opaque_id,
            registry_path=self.registry_path,
        )
        self._save_pointer(summary)
        return summary

    def resolve(
        self,
        opaque_id: str,
        *,
        vault_id: str | None = None,
        privacy: str | None = None,
        kind: str | None = None,
    ) -> ClassificationRunSummary:
        if vault_id is not None:
            if self.registry_path is None:
                raise ReviewQueueError("registry_unavailable")
            try:
                registry = load_registry(self.registry_path)
                target = registry.by_id(vault_id)
            except RegistryError:
                raise ReviewQueueError("invalid_review_route") from None
            if not target.active:
                raise ReviewQueueError("invalid_review_route")
            summary = classify_graph(
                self.source,
                self.staging,
                _VaultDecisionRunner(opaque_id, vault_id),
                model="haiku",
                retry_review_id=opaque_id,
                registry_path=self.registry_path,
            )
            self._save_pointer(summary)
            return summary
        if privacy is None or kind is None:
            raise ReviewQueueError("invalid_review_route")
        route = (privacy, kind)
        if route not in _MANUAL_ROUTES:
            raise ReviewQueueError("invalid_review_route")
        summary = classify_graph(
            self.source,
            self.staging,
            _DecisionRunner(opaque_id, privacy, kind),
            model="haiku",
            retry_review_id=opaque_id,
            registry_path=self.registry_path,
        )
        self._save_pointer(summary)
        return summary

    def publish(
        self,
        scopes_root: str | Path,
        schema_path: str | Path,
        *,
        version: str | None = None,
    ):
        """Materialize the current sealed decisions as one immutable version."""
        from agam.knowledge_materializer import materialize_graph

        summary = self._validate()
        return materialize_graph(
            self.source,
            self.staging,
            scopes_root,
            schema_path,
            source_sha256=summary.source_sha256,
            source_snapshot_sha256=summary.source_snapshot_sha256,
            staging_sha256=summary.staging_sha256,
            registry_path=self.registry_path,
            version=version,
        )

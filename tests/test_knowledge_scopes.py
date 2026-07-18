"""Fail-closed policy tests for physically separated knowledge scopes."""

from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
from pathlib import Path

from agam import paths
from agam.knowledge_scopes import (
    load_active_manifest as _load_active_manifest,
    resolve_agent_capabilities,
    resolve_effective_scopes as _resolve_effective_scopes,
    resolve_scope_db as _resolve_scope_db,
)


FIRST = "vault_111111111111111111111111"
SECOND = "vault_222222222222222222222222"
THIRD = "vault_333333333333333333333333"
FOURTH = "vault_444444444444444444444444"
SCOPES = (FIRST, SECOND, THIRD, FOURTH)


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def load_active_manifest(active: Path):
    return _load_active_manifest(active, scope_root=active.parent)


def resolve_scope_db(scope: str, manifest: object, active: Path):
    return _resolve_scope_db(scope, manifest, active, scope_root=active.parent)


def resolve_effective_scopes(*args, **kwargs):
    active = kwargs.get("active_path")
    if active is not None:
        kwargs["scope_root"] = Path(active).parent
    return _resolve_effective_scopes(*args, **kwargs)


def _policy(data_home: Path) -> Path:
    root = data_home / "knowledge" / "scopes"
    _registry(root)
    return _write_json(
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
            },
        },
    )


def _registry(root: Path) -> Path:
    path = root / "registry.json"
    if path.exists():
        return path
    records = [
        (FIRST, "Build Notes", "guidance", "portable"),
        (SECOND, "Solution Notes", "solutions", "portable"),
        (THIRD, "Project North", "custom", "restricted"),
        (FOURTH, "Project South", "custom", "restricted"),
    ]
    return _write_json(
        path,
        {
            "schema_version": 1,
            "vaults": [
                {
                    "id": vault_id,
                    "name": name,
                    "role": role,
                    "access": access,
                    "state": "active",
                    "routing_hint": "",
                }
                for vault_id, name, role, access in records
            ],
            "agents": {
                "codex": [FIRST, SECOND],
                "claude": list(SCOPES),
            },
        },
    )


def _activation(data_home: Path, scopes: tuple[str, ...] = SCOPES) -> Path:
    root = data_home / "knowledge" / "scopes"
    _registry(root)
    version = "v-test"
    stores: dict[str, object] = {}
    for scope in scopes:
        db = root / scope / version / "graph.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.touch()
        stores[scope] = {
            "path": f"{scope}/{version}/graph.db",
            "sha256": hashlib.sha256(b"").hexdigest(),
            "entities": 0,
            "relationships": 0,
            "properties": 0,
        }

    manifest = _write_json(
        root / "manifests" / f"{version}.json",
        {"version": version, "stores": stores},
    )
    return _write_json(
        root / "active.json",
        {
            "version": version,
            "manifest": str(manifest.relative_to(root)),
        },
    )


def test_synthetic_vault_ids_are_opaque():
    assert all(scope.startswith("vault_") and len(scope) == 30 for scope in SCOPES)


def test_new_path_helpers_follow_data_home(monkeypatch, tmp_path):
    home = tmp_path / "agam"
    monkeypatch.setenv("AGAM_DATA_HOME", str(home))

    assert paths.sealed_knowledge_dir() == home / "knowledge" / "sealed"
    assert paths.staging_knowledge_dir() == home / "knowledge" / "sealed" / "staging"
    assert paths.scopes_dir() == home / "knowledge" / "scopes"
    assert paths.scope_dir(FIRST) == home / "knowledge" / "scopes" / FIRST
    assert paths.manifests_dir() == home / "knowledge" / "scopes" / "manifests"
    assert paths.active_manifest_path() == home / "knowledge" / "scopes" / "active.json"


def test_named_profiles_fail_closed(tmp_path):
    config = _policy(tmp_path)
    active = _activation(tmp_path)

    assert resolve_effective_scopes(
        "codex", profile="expanded", config_path=config, active_path=active, env={}
    ) == ()
    assert resolve_effective_scopes(
        "codex", profile="private", config_path=config, active_path=active, env={}
    ) == ()


def test_env_scope_override_can_narrow_but_not_expand_codex(tmp_path):
    config = _policy(tmp_path)
    active = _activation(tmp_path)

    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        env={"AGAM_KNOWLEDGE_SCOPES": FIRST},
    ) == (FIRST,)
    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        env={"AGAM_KNOWLEDGE_SCOPES": f"{FIRST},{THIRD},{FOURTH}"},
    ) == (FIRST,)


def test_environment_profile_cannot_expand_registry_selection(tmp_path):
    config = _policy(tmp_path)
    active = _activation(tmp_path)

    assert resolve_effective_scopes(
        "codex",
        config_path=config,
        active_path=active,
        env={"AGAM_KNOWLEDGE_PROFILE": "expanded"},
    ) == ()


def test_manifest_availability_also_narrows_effective_scopes(tmp_path):
    config = _policy(tmp_path)
    active = _activation(tmp_path, scopes=(FIRST,))

    assert resolve_effective_scopes(
        "codex", config_path=config, active_path=active, env={}
    ) == (FIRST,)


def test_unknown_or_invalid_inputs_fail_closed(tmp_path):
    config = _policy(tmp_path)
    active = _activation(tmp_path)
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    cases = (
        {"agent": "unknown", "config_path": config, "active_path": active},
        {
            "agent": "codex",
            "profile": "unknown",
            "config_path": config,
            "active_path": active,
        },
        {
            "agent": "codex",
            "config_path": tmp_path / "missing.json",
            "active_path": active,
        },
        {"agent": "codex", "config_path": malformed, "active_path": active},
        {
            "agent": "codex",
            "config_path": config,
            "active_path": tmp_path / "missing-active.json",
        },
        {"agent": "codex", "config_path": config, "active_path": malformed},
    )
    for kwargs in cases:
        assert resolve_effective_scopes(env={}, **kwargs) == ()


def test_policy_fifo_fails_closed_without_blocking(tmp_path):
    config = tmp_path / "knowledge" / "scopes" / "config.json"
    config.parent.mkdir(parents=True)
    os.mkfifo(config)
    active = _activation(tmp_path)
    script = """
import sys
from agam.knowledge_scopes import resolve_effective_scopes

result = resolve_effective_scopes(
    "codex",
    config_path=sys.argv[1],
    active_path=sys.argv[2],
    scope_root=sys.argv[3],
    env={},
)
raise SystemExit(0 if result == () else 1)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(config), str(active), str(active.parent)],
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0


def test_active_pointer_fifo_fails_closed_without_blocking(tmp_path):
    root = tmp_path / "knowledge" / "scopes"
    root.mkdir(parents=True)
    active = root / "active.json"
    os.mkfifo(active)
    script = """
import sys
from agam.knowledge_scopes import load_active_manifest

result = load_active_manifest(sys.argv[1], scope_root=sys.argv[2])
raise SystemExit(0 if result is None else 1)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(active), str(root)],
        check=False,
        timeout=2,
    )

    assert completed.returncode == 0


def test_unknown_policy_key_fails_closed(tmp_path):
    config = _policy(tmp_path)
    policy = json.loads(config.read_text(encoding="utf-8"))
    policy["unexpected"] = {}
    _write_json(config, policy)

    assert resolve_effective_scopes(
        "codex", config_path=config, active_path=_activation(tmp_path), env={}
    ) == ()


def test_active_manifest_is_strict_and_content_free(tmp_path):
    active = _activation(tmp_path)
    manifest = load_active_manifest(active)

    assert manifest is not None
    assert manifest["version"] == "v-test"
    assert set(manifest["stores"]) == set(SCOPES)

    pointer = json.loads(active.read_text(encoding="utf-8"))
    pointer["unexpected"] = "raw content must not be accepted"
    _write_json(active, pointer)
    assert load_active_manifest(active) is None


def test_scope_db_rejects_traversal_and_wrong_scope(tmp_path):
    active = _activation(tmp_path)
    manifest = load_active_manifest(active)
    assert manifest is not None

    assert resolve_scope_db(FIRST, manifest, active) == (
        tmp_path / "knowledge" / "scopes" / FIRST / "v-test" / "graph.db"
    )

    manifest["stores"][FIRST]["path"] = "../sealed/source.graph.db"
    assert resolve_scope_db(FIRST, manifest, active) is None

    manifest["stores"][FIRST]["path"] = f"{FOURTH}/v-test/graph.db"
    assert resolve_scope_db(FIRST, manifest, active) is None


def test_scope_db_rejects_symlink_escape(tmp_path):
    active = _activation(tmp_path, scopes=(FIRST,))
    manifest = load_active_manifest(active)
    assert manifest is not None
    outside = tmp_path / "outside.db"
    outside.touch()
    declared = tmp_path / "knowledge" / "scopes" / FIRST / "v-test" / "graph.db"
    declared.unlink()
    declared.symlink_to(outside)

    assert resolve_scope_db(FIRST, manifest, active) is None


def test_scope_db_rejects_symlinked_version_directory_escape(tmp_path):
    active = _activation(tmp_path, scopes=(FIRST,))
    manifest = load_active_manifest(active)
    assert manifest is not None
    declared_dir = tmp_path / "knowledge" / "scopes" / FIRST / "v-test"
    (declared_dir / "graph.db").unlink()
    declared_dir.rmdir()
    outside_dir = tmp_path / "outside-version"
    outside_dir.mkdir()
    (outside_dir / "graph.db").touch()
    declared_dir.symlink_to(outside_dir, target_is_directory=True)

    assert resolve_scope_db(FIRST, manifest, active) is None


def test_manifest_metadata_types_are_strict(tmp_path):
    active = _activation(tmp_path, scopes=(FIRST,))
    pointer = json.loads(active.read_text(encoding="utf-8"))
    manifest_path = active.parent / pointer["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_sha256"] = {"raw": "content must not fit metadata keys"}
    _write_json(manifest_path, manifest)

    assert load_active_manifest(active) is None

    manifest["source_sha256"] = "0" * 64
    manifest["stores"][FIRST]["entities"] = "zero"
    _write_json(manifest_path, manifest)
    assert load_active_manifest(active) is None


def test_optional_agent_capabilities_must_be_boolean(tmp_path):
    config = _policy(tmp_path)
    policy = json.loads(config.read_text(encoding="utf-8"))
    policy["agents"]["codex"]["capture"] = "false"
    _write_json(config, policy)

    assert resolve_effective_scopes(
        "codex", config_path=config, active_path=_activation(tmp_path), env={}
    ) == ()


def test_recall_must_be_explicitly_true(tmp_path):
    config = _policy(tmp_path)
    active = _activation(tmp_path)
    policy = json.loads(config.read_text(encoding="utf-8"))
    policy["agents"]["codex"]["recall"] = False
    _write_json(config, policy)
    assert resolve_effective_scopes(
        "codex", config_path=config, active_path=active, env={}
    ) == ()

    del policy["agents"]["codex"]["recall"]
    _write_json(config, policy)
    assert resolve_effective_scopes(
        "codex", config_path=config, active_path=active, env={}
    ) == ()


def test_capabilities_default_false_and_require_explicit_true(tmp_path):
    root = tmp_path / "knowledge" / "scopes"
    disabled = {"recall": False, "boot-injection": False, "capture": False}
    assert resolve_agent_capabilities("codex", scope_root=root) == disabled

    config = _policy(tmp_path)
    assert resolve_agent_capabilities(
        "codex", config_path=config, scope_root=root
    ) == {"recall": True, "boot-injection": False, "capture": False}


def test_environment_cannot_redirect_policy_or_activation_root(monkeypatch, tmp_path):
    canonical_home = tmp_path / "canonical"
    attacker_home = tmp_path / "attacker"
    config = _policy(attacker_home)
    active = _activation(attacker_home)
    monkeypatch.setenv("AGAM_DATA_HOME", str(canonical_home))

    assert resolve_effective_scopes(
        "codex",
        env={
            "AGAM_KNOWLEDGE_CONFIG": str(config),
            "AGAM_ACTIVE_MANIFEST": str(active),
        },
    ) == ()


def test_scope_db_rejects_digest_mismatch(tmp_path):
    active = _activation(tmp_path, scopes=(FIRST,))
    manifest = load_active_manifest(active)
    assert manifest is not None
    db = tmp_path / "knowledge" / "scopes" / FIRST / "v-test" / "graph.db"
    db.write_bytes(b"replaced after activation")

    assert resolve_scope_db(FIRST, manifest, active) is None


def test_agam_knowledge_environment_is_removed_by_autouse_fixture(monkeypatch):
    # The autouse fixture runs before this test. Ambient values must not survive.
    assert "AGAM_KNOWLEDGE_PROFILE" not in os.environ
    assert "AGAM_KNOWLEDGE_SCOPES" not in os.environ
    monkeypatch.setenv("AGAM_KNOWLEDGE_PROFILE", "safe")
    monkeypatch.setenv("AGAM_KNOWLEDGE_SCOPES", FIRST)

"""Synthetic tests for Codex's scoped-knowledge filesystem boundary."""

from __future__ import annotations

import tomllib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agam.codex_permissions_merger import (
    PERMISSION_PROFILE,
    PermissionProfileError,
    inspect_permission_profile,
    merge_permission_profile,
)
from agam.vault_registry import add_vault, initialize_registry, set_agent_access


def test_profile_reopens_only_registry_selected_opaque_vaults(tmp_path: Path):
    config = tmp_path / ".codex" / "config.toml"
    knowledge = tmp_path / ".agam" / "knowledge"
    registry_path = knowledge / "scopes" / "registry.json"
    registry = initialize_registry(
        registry_path,
        guidance_name="My Craft",
        solutions_name="Repair Library",
        agents=("codex",),
    )
    custom = add_vault(registry_path, name="Project North")

    merge_permission_profile(config, knowledge, agent="codex")
    first_rules = tomllib.loads(config.read_text())["permissions"][
        PERMISSION_PROFILE
    ]["filesystem"]
    assert all(
        first_rules[str((knowledge / "scopes" / vault.id).resolve())] == "read"
        for vault in registry.vaults
    )
    assert str((knowledge / "scopes" / custom.id).resolve()) not in first_rules
    assert "My Craft" not in config.read_text()
    assert "Project North" not in config.read_text()

    set_agent_access(
        registry_path,
        "codex",
        [vault.id for vault in registry.vaults] + [custom.id],
    )
    merge_permission_profile(config, knowledge, agent="codex")
    refreshed = tomllib.loads(config.read_text())["permissions"][
        PERMISSION_PROFILE
    ]["filesystem"]
    assert refreshed[str((knowledge / "scopes" / custom.id).resolve())] == "read"


def test_creates_selectable_profile_that_fails_closed_without_registry(
    tmp_path: Path,
):
    config = tmp_path / ".codex" / "config.toml"
    knowledge = tmp_path / ".agam" / "knowledge"

    status = merge_permission_profile(config, knowledge)
    parsed = tomllib.loads(config.read_text())
    profile = parsed["permissions"][PERMISSION_PROFILE]
    filesystem = profile["filesystem"]

    assert status.configured is True
    assert status.select_required is True
    assert status.legacy_sandbox_conflict is False
    assert profile["extends"] == ":workspace"
    assert filesystem[str(knowledge.resolve())] == "deny"
    assert filesystem[str((knowledge / "scopes" / "config.json").resolve())] == "read"
    assert filesystem[str((knowledge / "scopes" / "registry.json").resolve())] == "read"
    assert filesystem[str((knowledge / "scopes" / "active.json").resolve())] == "read"
    assert filesystem[str((knowledge / "scopes" / "manifests").resolve())] == "read"
    assert len(filesystem) == 5
    assert config.stat().st_mode & 0o777 == 0o600


def test_preserves_user_config_and_is_idempotent(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('# user comment\nmodel = "synthetic-model"\n')
    knowledge = tmp_path / "knowledge"

    merge_permission_profile(config, knowledge)
    first = config.read_text()
    merge_permission_profile(config, knowledge)
    second = config.read_text()

    assert first == second
    assert second.startswith('# user comment\nmodel = "synthetic-model"\n')
    assert second.count("agam:scoped-knowledge:begin") == 1
    assert tomllib.loads(second)["model"] == "synthetic-model"


def test_replaces_only_owned_block_when_knowledge_root_changes(tmp_path: Path):
    config = tmp_path / "config.toml"
    first_root = tmp_path / "first" / "knowledge"
    second_root = tmp_path / "second" / "knowledge"

    merge_permission_profile(config, first_root)
    merge_permission_profile(config, second_root)
    rendered = config.read_text()

    assert str(first_root.resolve()) not in rendered
    assert str(second_root.resolve()) in rendered
    assert rendered.count("agam:scoped-knowledge:begin") == 1


def test_refuses_user_owned_profile_collision_without_modifying_file(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = (
        f"[permissions.{PERMISSION_PROFILE}]\n"
        'description = "user-owned"\n'
    )
    config.write_text(original)

    with pytest.raises(PermissionProfileError) as raised:
        merge_permission_profile(config, tmp_path / "knowledge")

    assert raised.value.code == "permission_profile_conflict"
    assert config.read_text() == original


def test_refuses_symlink_config_without_touching_target(tmp_path: Path):
    target = tmp_path / "target.toml"
    target.write_text('model = "untouched"\n')
    config = tmp_path / "config.toml"
    config.symlink_to(target)

    with pytest.raises(PermissionProfileError) as raised:
        merge_permission_profile(config, tmp_path / "knowledge")

    assert raised.value.code == "unsafe_config_path"
    assert target.read_text() == 'model = "untouched"\n'


def test_reports_legacy_sandbox_conflict_without_removing_user_setting(
    tmp_path: Path,
):
    config = tmp_path / "config.toml"
    config.write_text('sandbox_mode = "workspace-write"\n')

    status = merge_permission_profile(config, tmp_path / "knowledge")
    inspected = inspect_permission_profile(config, tmp_path / "knowledge")

    assert status.configured is True
    assert status.legacy_sandbox_conflict is True
    assert inspected == status
    assert tomllib.loads(config.read_text())["sandbox_mode"] == "workspace-write"


def test_inspection_fails_closed_for_missing_or_tampered_profile(tmp_path: Path):
    config = tmp_path / "config.toml"
    knowledge = tmp_path / "knowledge"

    assert inspect_permission_profile(config, knowledge).configured is False
    merge_permission_profile(config, knowledge)
    config.write_text(config.read_text().replace('extends = ":workspace"', 'extends = ":read-only"'))

    status = inspect_permission_profile(config, knowledge)
    assert status.configured is False
    assert status.select_required is False


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI unavailable")
def test_profile_is_enforced_by_codex_sandbox(tmp_path: Path):
    codex_home = tmp_path / ".codex"
    knowledge = tmp_path / ".agam" / "knowledge"
    registry = initialize_registry(
        knowledge / "scopes" / "registry.json",
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    custom = add_vault(knowledge / "scopes" / "registry.json", name="Project North")
    restricted = knowledge / "scopes" / custom.id / "synthetic.txt"
    allowed = knowledge / "scopes" / registry.vaults[0].id / "synthetic.txt"
    restricted.parent.mkdir(parents=True)
    allowed.parent.mkdir(parents=True)
    restricted.write_text("SYNTHETIC_RESTRICTED_MARKER")
    allowed.write_text("SYNTHETIC_ALLOWED_MARKER")
    merge_permission_profile(codex_home / "config.toml", knowledge)
    environment = {**os.environ, "CODEX_HOME": str(codex_home)}

    denied = subprocess.run(
        [
            shutil.which("codex"),
            "sandbox",
            "--permission-profile",
            PERMISSION_PROFILE,
            "--",
            "/bin/cat",
            str(restricted),
        ],
        text=True,
        capture_output=True,
        env=environment,
        timeout=20,
    )
    readable = subprocess.run(
        [
            shutil.which("codex"),
            "sandbox",
            "--permission-profile",
            PERMISSION_PROFILE,
            "--",
            "/bin/cat",
            str(allowed),
        ],
        text=True,
        capture_output=True,
        env=environment,
        timeout=20,
    )

    if (
        readable.returncode == 71
        and "sandbox_apply: Operation not permitted" in readable.stderr
    ):
        pytest.skip("nested Codex sandbox unavailable")

    assert denied.returncode != 0
    assert "SYNTHETIC_RESTRICTED_MARKER" not in denied.stdout
    assert readable.returncode == 0
    assert readable.stdout == "SYNTHETIC_ALLOWED_MARKER"

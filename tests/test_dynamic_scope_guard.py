"""Registry-derived restricted-root tests for the Codex pre-tool guard."""

from __future__ import annotations

from agam.hooks.scope_guard import _configured_roots
from agam.vault_registry import add_vault, initialize_registry


def test_guard_restricts_every_unselected_vault(tmp_path):
    data_home = tmp_path / ".agam"
    scopes = data_home / "knowledge" / "scopes"
    registry = initialize_registry(
        scopes / "registry.json",
        guidance_name="Craft",
        solutions_name="Repairs",
        agents=("codex",),
    )
    custom = add_vault(scopes / "registry.json", name="Project North")

    roots = set(
        _configured_roots(
            {
                "AGAM_DATA_HOME": str(data_home),
                "AGAM_RECALL_AGENT": "codex",
            }
        )
    )

    assert str(scopes / custom.id) in roots
    assert all(str(scopes / vault.id) not in roots for vault in registry.vaults)
    assert str(data_home / "knowledge" / "sealed") in roots


def test_guard_denies_entire_scope_root_when_registry_is_unavailable(tmp_path):
    scopes = tmp_path / "knowledge" / "scopes"

    roots = _configured_roots(
        {
            "AGAM_KNOWLEDGE_SCOPE_ROOT": str(scopes),
            "AGAM_RECALL_AGENT": "codex",
        }
    )

    assert str(scopes) in roots

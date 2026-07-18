"""Scriptable CLI contracts for user-defined vault lifecycle operations."""

from __future__ import annotations

import json

from agam.cli import main
from tests.test_vault_migration import _legacy_state


def _run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out else None
    return code, payload, captured.err


def test_cli_setup_add_rename_archive_restore_and_access(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGAM_DATA_HOME", str(tmp_path / ".agam"))

    code, setup, _ = _run(
        [
            "vault",
            "setup",
            "--guidance-name",
            "How I Build",
            "--solutions-name",
            "Repair Library",
        ],
        capsys,
    )
    assert code == 0
    assert [item["name"] for item in setup["vaults"]] == [
        "How I Build",
        "Repair Library",
    ]

    code, added, _ = _run(
        ["vault", "add", "--name", "Project North", "--hint", "north only"],
        capsys,
    )
    assert code == 0
    custom_id = added["vault"]["id"]
    assert added["vault"]["access"] == "restricted"
    assert added["rewire_required"] is False

    code, renamed, _ = _run(
        ["vault", "rename", custom_id, "--name", "North Archive"], capsys
    )
    assert code == 0
    assert renamed["vault"]["id"] == custom_id
    assert renamed["vault"]["name"] == "North Archive"

    code, access, _ = _run(
        ["vault", "access", "codex", "--vault", setup["vaults"][0]["id"], "--vault", custom_id],
        capsys,
    )
    assert code == 0
    assert access["rewire_required"] is True
    assert access["selected"] == [setup["vaults"][0]["id"], custom_id]

    code, archived, _ = _run(["vault", "archive", custom_id], capsys)
    assert code == 0
    assert archived["vault"]["state"] == "archived"
    assert archived["data_retained"] is True

    code, restored, _ = _run(["vault", "restore", custom_id], capsys)
    assert code == 0
    assert restored["vault"]["state"] == "active"

    code, listing, _ = _run(["vault", "list"], capsys)
    assert code == 0
    assert [item["name"] for item in listing["vaults"]] == [
        "How I Build",
        "Repair Library",
        "North Archive",
    ]


def test_cli_migration_is_a_dry_run_until_apply(monkeypatch, tmp_path, capsys):
    root = _legacy_state(tmp_path / "knowledge")
    monkeypatch.setenv("AGAM_DATA_HOME", str(tmp_path))

    code, preview, _ = _run(["vault", "migrate"], capsys)
    assert code == 0
    assert preview["status"] == "dry-run"
    assert preview["copied_stores"] == 0
    assert preview["data_retained"] is True
    assert not (root / "registry.json").exists()

    code, applied, _ = _run(["vault", "migrate", "--apply"], capsys)
    assert code == 0
    assert applied["status"] == "migrated"
    assert applied["copied_stores"] == 3
    assert (root / "registry.json").exists()

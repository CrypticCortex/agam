"""Tests for the Agam installer wizard.

The installer is routed through a ``home`` parameter in every test so we
never touch any ``~/.claude/``. Each test works against
a pytest ``tmp_path`` tempdir.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest


def _default_answers(tmp_path: Path) -> dict:
    return {
        "name": "Alice",
        "primary_goal": "learn",
        "projects_dir": str(tmp_path / "projects"),
        "platform": "mac",
        "container_mode": "none",
        "bootstrap_now": False,
    }


# ---------------------------------------------------------------------------
# Two tests required by the plan
# ---------------------------------------------------------------------------


def test_installer_writes_config_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agam.installer import run_wizard

    answers = {
        "name": "Alice",
        "primary_goal": "learn",
        "projects_dir": str(tmp_path),
        "platform": "mac",
        "container_mode": "none",
        "bootstrap_now": False,
    }
    run_wizard(answers=answers, home=tmp_path)

    cfg = (tmp_path / ".claude" / "agam" / "config.yaml").read_text()
    assert "name: Alice" in cfg


def test_installer_refuses_existing_agam_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "agam").mkdir(parents=True)
    (tmp_path / ".claude" / "agam" / "AGAM.md").write_text("existing")

    from agam.installer import run_wizard

    answers = _default_answers(tmp_path)
    with pytest.raises(SystemExit):
        run_wizard(answers=answers, force=False, home=tmp_path)


# ---------------------------------------------------------------------------
# Identity + prompt + hook + tool layout
# ---------------------------------------------------------------------------


def test_installer_writes_identity_files(tmp_path):
    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    agam = tmp_path / ".claude" / "agam"
    for name in ("AGAM.md", "THISAI.md", "MUGAM.md", "config.yaml"):
        assert (agam / name).is_file(), f"missing {name}"

    # Template placeholder got stamped with today's date.
    agam_md = (agam / "AGAM.md").read_text()
    assert "Last updated: YYYY-MM-DD" not in agam_md
    assert "Last updated: 20" in agam_md  # 20xx


def test_installer_writes_prompts(tmp_path):
    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    prompts = tmp_path / ".claude" / "agam" / "prompts"
    assert (prompts / "work-log.txt").is_file()
    assert (prompts / "agam-sync.txt").is_file()
    # Prompts aren't templated at install time; placeholders persist for
    # the watchdog to substitute later.
    assert "{{SESSION_SIGNALS}}" in (prompts / "work-log.txt").read_text()


def test_installer_writes_hooks(tmp_path):
    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    hooks = tmp_path / ".claude" / "hooks"

    expected = [
        "graph_recall.py",
        "graph_update.py",
        "session_close.py",
        "lesson_activate.py",
        "lesson_activate_post.py",
        "agam_watchdog.sh",
        "agam_watchdog_inner.py",
    ]
    for name in expected:
        p = hooks / name
        assert p.is_file(), f"missing hook {name}"
        # User-executable bit set on .py and .sh files.
        mode = p.stat().st_mode
        assert mode & stat.S_IXUSR, f"{name} is not executable"


def test_installer_writes_tools(tmp_path):
    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    tools = tmp_path / ".claude" / "tools" / "agam"

    expected = [
        "knowledge_graph.py",
        "agam_context.py",
        "agam_lint.py",
        "apply_proposals.py",
        "session_patterns.py",
        "watchdog_monitor.py",
        "pending_queue.py",
    ]
    for name in expected:
        p = tools / name
        assert p.is_file(), f"missing tool {name}"

    # The executable python tools should have +x.
    assert (tools / "knowledge_graph.py").stat().st_mode & stat.S_IXUSR


def test_installer_skips_package_dunders(tmp_path):
    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    hooks = tmp_path / ".claude" / "hooks"
    tools = tmp_path / ".claude" / "tools" / "agam"
    # __init__.py is a package artifact; the installed layout doesn't
    # need dunders and including them would leak Python packaging
    # structure into a plain hooks directory.
    assert not (hooks / "__init__.py").exists()
    assert not (tools / "__init__.py").exists()


# ---------------------------------------------------------------------------
# Knowledge graph
# ---------------------------------------------------------------------------


def test_installer_creates_kg_from_schema(tmp_path):
    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    db = tmp_path / ".claude" / "knowledge" / "graph.db"
    assert db.is_file()

    conn = sqlite3.connect(db)
    try:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    # Core tables from graph-schema.sql.
    assert "entities" in rows
    assert "relationships" in rows
    assert "properties" in rows


# ---------------------------------------------------------------------------
# LaunchAgents plist
# ---------------------------------------------------------------------------


def test_installer_writes_plist_on_mac(tmp_path):
    import plistlib

    from agam.installer import run_wizard

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)
    plist = tmp_path / "Library" / "LaunchAgents" / "com.agam.watchdog.plist"
    assert plist.is_file()

    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "com.agam.watchdog"
    # Placeholders got substituted with our tmp home.
    assert data["WorkingDirectory"] == str(tmp_path)
    assert data["ProgramArguments"][1].startswith(
        str(tmp_path / ".claude" / "hooks")
    )


def test_installer_skips_plist_on_linux(tmp_path):
    from agam.installer import run_wizard

    answers = _default_answers(tmp_path)
    answers["platform"] = "linux"
    run_wizard(answers=answers, home=tmp_path)

    plist = tmp_path / "Library" / "LaunchAgents" / "com.agam.watchdog.plist"
    assert not plist.exists()


# ---------------------------------------------------------------------------
# Force + backup
# ---------------------------------------------------------------------------


def test_installer_force_backup_preserves_old(tmp_path):
    from agam.installer import run_wizard

    # Pre-existing agam dir with a sentinel file we expect to survive
    # the reinstall -- just under a different path (the backup dir).
    agam = tmp_path / ".claude" / "agam"
    agam.mkdir(parents=True)
    sentinel = agam / "SENTINEL.md"
    sentinel.write_text("old-install")

    result = run_wizard(
        answers=_default_answers(tmp_path),
        home=tmp_path,
        force=True,
    )

    # Fresh install present.
    assert (agam / "AGAM.md").is_file()
    # Backup path exists and contains the sentinel.
    assert result.backup is not None
    assert result.backup.is_dir()
    assert (result.backup / "SENTINEL.md").read_text() == "old-install"
    # Backup sits under ~/.claude/ with a timestamp suffix.
    assert result.backup.parent == tmp_path / ".claude"
    assert result.backup.name.startswith("agam.backup-")


def test_installer_allows_empty_existing_agam_dir(tmp_path):
    """An agam dir that exists but is empty is not a conflict."""
    from agam.installer import run_wizard

    (tmp_path / ".claude" / "agam").mkdir(parents=True)
    # Should not raise.
    run_wizard(answers=_default_answers(tmp_path), home=tmp_path, force=False)
    assert (tmp_path / ".claude" / "agam" / "AGAM.md").is_file()


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_installer_atomic_on_failure(tmp_path, monkeypatch):
    """If a step blows up mid-install, no partial ~/.claude/agam/."""
    from agam import installer

    # Poison the KG step.
    def boom(*args, **kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(installer, "_create_kg", boom)

    with pytest.raises(RuntimeError, match="forced failure"):
        installer.run_wizard(answers=_default_answers(tmp_path), home=tmp_path)

    # Nothing landed.
    assert not (tmp_path / ".claude" / "agam").exists()
    assert not (tmp_path / ".claude" / "knowledge").exists()
    # Staging dir got cleaned up too.
    leftovers = list((tmp_path / ".claude").glob(".agam-stage-*"))
    assert not leftovers


# ---------------------------------------------------------------------------
# Config shape
# ---------------------------------------------------------------------------


def test_installer_config_is_dash_case_yaml(tmp_path):
    from agam.installer import run_wizard

    answers = _default_answers(tmp_path)
    answers["primary_goal"] = "ship the thing"
    answers["projects_dir"] = "/home/alice/coding"
    run_wizard(answers=answers, home=tmp_path)

    cfg_text = (tmp_path / ".claude" / "agam" / "config.yaml").read_text()
    # Dash-case keys per CLAUDE.md preference.
    assert "primary-goal:" in cfg_text
    assert "projects-dir:" in cfg_text
    assert "container-mode:" in cfg_text
    assert "bootstrap-now:" in cfg_text
    # Snake_case did NOT leak through.
    assert "primary_goal" not in cfg_text
    assert "projects_dir" not in cfg_text


def test_installer_rejects_invalid_platform(tmp_path):
    from agam.installer import run_wizard

    answers = _default_answers(tmp_path)
    answers["platform"] = "windows"
    with pytest.raises(ValueError, match="platform must be"):
        run_wizard(answers=answers, home=tmp_path)


def test_installer_rejects_invalid_container_mode(tmp_path):
    from agam.installer import run_wizard

    answers = _default_answers(tmp_path)
    answers["container_mode"] = "podman"
    with pytest.raises(ValueError, match="container_mode must be"):
        run_wizard(answers=answers, home=tmp_path)


# ---------------------------------------------------------------------------
# Does not touch the real ~/.claude/
# ---------------------------------------------------------------------------


def test_installer_does_not_touch_real_home(tmp_path):
    """Running with a ``home=`` override must not read/write the real HOME."""
    from agam.installer import run_wizard

    real_home = os.environ.get("HOME")
    real_agam = Path(real_home) / ".claude" / "agam" / "AGAM.md" if real_home else None
    before = real_agam.stat().st_mtime if real_agam and real_agam.exists() else None

    run_wizard(answers=_default_answers(tmp_path), home=tmp_path)

    after = real_agam.stat().st_mtime if real_agam and real_agam.exists() else None
    assert before == after, (
        "installer mutated the real ~/.claude/agam/AGAM.md despite home= override"
    )

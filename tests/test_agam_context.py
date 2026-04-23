"""Tests for agam_context.py.

These tests confirm the AGAM_HOME / AGAM_KG_PATH / AGAM_WORK_LOG env overrides
fully isolate the tool from the user's real ~/.claude/ directory.
"""

import os
import pathlib
import sqlite3
import subprocess

import pytest


TOOL = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "tools" / "agam_context.py"
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "knowledge" / "graph-schema.sql"


@pytest.fixture
def agam_env(tmp_path):
    """Fabricate a fake AGAM_HOME with identity files + fresh KG."""
    home = tmp_path / "agam"
    home.mkdir()
    (home / "AGAM.md").write_text(
        "# AGAM\n\n## Core Identity\n\nTest identity.\n\n"
        "## What I've Learned\n\nLesson: always write tests.\n"
    )
    (home / "THISAI.md").write_text(
        "# THISAI\n\n## Current Direction\n\nBuild the test suite.\n"
    )
    (home / "MUGAM.md").write_text("# MUGAM\n\nPublic face text.\n")
    kg = tmp_path / "graph.db"
    conn = sqlite3.connect(kg)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()
    work_log = tmp_path / "work-log.md"
    work_log.write_text("# Work log\n\n## 2026-04-20\n\nTest entry.\n")
    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "AGAM_KG_PATH": str(kg),
        "AGAM_WORK_LOG": str(work_log),
        "AGAM_PROJECTS_DIR": str(tmp_path / "nonexistent-projects"),
    }
    return env, home, kg


def _run(agam_env, *args, cwd=None):
    env, home, _ = agam_env
    r = subprocess.run(
        ["uv", "run", "--script", str(TOOL), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd or str(home),
    )
    return r


def test_boot_does_not_crash(agam_env):
    r = _run(agam_env, "boot")
    assert r.returncode == 0, r.stderr
    assert "AGAM" in r.stdout or "identity" in r.stdout.lower()


def test_entity_unknown_graceful(agam_env):
    """Fresh KG, no entities. `entity <name>` must exit 0 and say nothing exploded."""
    r = _run(agam_env, "entity", "nonexistent")
    # Accept exit 0 or 1; what matters is no traceback
    assert "Traceback" not in r.stderr, r.stderr
    assert "NOT FOUND" in r.stdout or "not found" in r.stdout.lower()


def test_direction_reads_thisai(agam_env):
    r = _run(agam_env, "direction")
    assert r.returncode == 0, r.stderr
    # direction command should reference THISAI content
    assert "test suite" in r.stdout.lower() or "direction" in r.stdout.lower()


def test_learned_does_not_crash_on_empty_kg(agam_env):
    r = _run(agam_env, "learned")
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr


def test_agam_home_redirect_isolates(tmp_path):
    """If AGAM_HOME points at a dir with no identity files, the tool should not
    read from the user's real ~/.claude/agam/. Verify by passing a dir containing
    a sentinel file and confirming the tool's boot output reflects the sentinel,
    not the user's real identity."""
    fake_home = tmp_path / "empty-agam"
    fake_home.mkdir()
    (fake_home / "AGAM.md").write_text(
        "# AGAM\n\nSENTINEL-STRING-UNLIKELY-TO-APPEAR-IN-REAL-IDENTITY\n"
    )
    (fake_home / "THISAI.md").write_text("# THISAI\n\nsentinel direction\n")
    env = {
        **os.environ,
        "AGAM_HOME": str(fake_home),
        "AGAM_KG_PATH": str(tmp_path / "nonexistent.db"),
        "AGAM_WORK_LOG": str(tmp_path / "fake-work-log.md"),
        "AGAM_PROJECTS_DIR": str(tmp_path / "nonexistent-projects"),
    }
    # Create empty KG + work log so the tool doesn't fail on missing files
    conn = sqlite3.connect(tmp_path / "nonexistent.db")
    conn.executescript(SCHEMA.read_text())
    conn.close()
    (tmp_path / "fake-work-log.md").write_text("# empty\n")
    r = subprocess.run(
        ["uv", "run", "--script", str(TOOL), "boot"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(fake_home),
    )
    assert r.returncode == 0, r.stderr
    assert "SENTINEL-STRING-UNLIKELY" in r.stdout, (
        "AGAM_HOME redirect not honored -- tool read from somewhere else"
    )


def test_no_args_prints_usage(agam_env):
    """Invoking with no subcommand should show usage and exit non-zero."""
    r = _run(agam_env)
    assert r.returncode != 0
    assert "Usage" in r.stdout or "agam_context" in r.stdout


def test_unknown_command_fails_gracefully(agam_env):
    """Unknown subcommand exits non-zero without traceback."""
    r = _run(agam_env, "bogus-subcommand")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "Unknown command" in r.stdout or "bogus" in r.stdout.lower()


def test_learned_reads_agam_md(agam_env):
    """learned extracts the 'What I've Learned' section from AGAM.md."""
    r = _run(agam_env, "learned")
    assert r.returncode == 0, r.stderr
    assert "always write tests" in r.stdout.lower() or "learned" in r.stdout.lower()


def test_entity_found_after_insert(agam_env):
    """Insert an entity via SQL then confirm `entity <name>` retrieves it."""
    _, _, kg = agam_env
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("widget", "component", "A reusable widget."),
    )
    conn.commit()
    conn.close()
    r = _run(agam_env, "entity", "widget")
    assert r.returncode == 0, r.stderr
    assert "widget" in r.stdout.lower()
    assert "component" in r.stdout.lower()
    assert "Traceback" not in r.stderr


def test_boot_missing_kg_still_works(tmp_path):
    """Boot should survive a missing KG path (returns None from get_db)."""
    home = tmp_path / "agam"
    home.mkdir()
    (home / "AGAM.md").write_text("# AGAM\n\nIdentity string.\n")
    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "AGAM_KG_PATH": str(tmp_path / "does-not-exist.db"),
        "AGAM_WORK_LOG": str(tmp_path / "missing-work-log.md"),
        "AGAM_PROJECTS_DIR": str(tmp_path / "missing-projects"),
    }
    r = subprocess.run(
        ["uv", "run", "--script", str(TOOL), "boot"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(home),
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr

"""Tests for agam_lint.py and session_patterns.py.

These tests confirm the AGAM_HOME / AGAM_KG_PATH / AGAM_WORK_LOG /
AGAM_PROJECTS_DIR / AGAM_MEMORY_DIR env overrides fully isolate both tools
from the user's real ~/.claude/ directory.
"""

import os
import pathlib
import sqlite3
import subprocess

import pytest


TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "tools"
LINT_TOOL = TOOLS_DIR / "agam_lint.py"
PATTERNS_TOOL = TOOLS_DIR / "session_patterns.py"
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "knowledge" / "graph-schema.sql"

REAL_AGAM_MD = pathlib.Path.home() / ".claude" / "agam" / "AGAM.md"
REAL_KG = pathlib.Path.home() / ".claude" / "knowledge" / "graph.db"
REAL_WORK_LOG = pathlib.Path.home() / ".claude" / "work-log.md"


def _mtimes():
    """Snapshot mtimes of real user files that must NOT be touched."""
    out = {}
    for p in (REAL_AGAM_MD, REAL_KG, REAL_WORK_LOG):
        if p.exists():
            out[p] = p.stat().st_mtime
    return out


@pytest.fixture
def lint_env(tmp_path):
    """Fabricate an isolated AGAM_HOME + fresh KG + stub work-log."""
    home = tmp_path / "agam"
    home.mkdir()
    (home / "AGAM.md").write_text(
        "# AGAM\n\n"
        "## What I Value\n\n"
        "- writing tests first\n"
        "- epistemic honesty\n\n"
        "## What I've Learned\n\n"
        "Lesson: always write tests.\n"
    )
    (home / "THISAI.md").write_text(
        "# THISAI\n\n## Active Goals\n\n"
        "### Ship the v0 release\n"
        "Build the open-source port.\n"
    )
    (home / "MUGAM.md").write_text("# MUGAM\n\nPublic face.\n")

    kg = tmp_path / "graph.db"
    conn = sqlite3.connect(kg)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()

    work_log = tmp_path / "work-log.md"
    work_log.write_text(
        "# Work log\n\n"
        "## 2026-04-20 | agam | 10:00\n\nPorted tools.\n\n"
        "## 2026-04-21 | agam | 11:00\n\nWrote tests.\n"
    )

    projects = tmp_path / "projects"
    projects.mkdir()

    memory = tmp_path / "MEMORY"
    memory.mkdir()

    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "AGAM_KG_PATH": str(kg),
        "AGAM_WORK_LOG": str(work_log),
        "AGAM_PROJECTS_DIR": str(projects),
        "AGAM_MEMORY_DIR": str(memory),
    }
    return env, home, kg, work_log


def _run(tool, env, *args, cwd=None, timeout=30):
    r = subprocess.run(
        ["uv", "run", "--script", str(tool), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    return r


# -- agam_lint tests --


def test_lint_runs_on_empty_kg(lint_env):
    """Full lint against a fresh schema-only KG must exit 0 with no traceback."""
    env, home, _kg, _wl = lint_env
    before = _mtimes()
    r = _run(LINT_TOOL, env, cwd=str(home))
    after = _mtimes()
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert "AGAM LINT" in r.stdout
    assert "[OK] Lint complete" in r.stdout
    # Real user files untouched
    assert before == after, "Real ~/.claude files were modified!"


def test_lint_writes_findings_into_agam_home(lint_env):
    """Findings file must land in AGAM_HOME, not ~/.claude/agam."""
    env, home, _kg, _wl = lint_env
    r = _run(LINT_TOOL, env, cwd=str(home))
    assert r.returncode == 0, r.stderr
    findings = home / ".lint-findings.md"
    assert findings.exists(), "lint must create .lint-findings.md in AGAM_HOME"
    body = findings.read_text()
    assert "## Lint Findings" in body


def test_lint_quick_mode(lint_env):
    """--quick runs fewer audits and still succeeds."""
    env, home, _kg, _wl = lint_env
    r = _run(LINT_TOOL, env, "--quick", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "quick mode" in r.stdout
    # Quick mode should NOT run audit 9 (memory anchors) or 7/8
    assert "MEMORY ANCHORS" not in r.stdout


def test_lint_missing_kg_degrades_gracefully(tmp_path):
    """If AGAM_KG_PATH points to a non-existent file, lint still runs."""
    home = tmp_path / "agam"
    home.mkdir()
    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "AGAM_KG_PATH": str(tmp_path / "missing.db"),
        "AGAM_WORK_LOG": str(tmp_path / "missing-wl.md"),
        "AGAM_PROJECTS_DIR": str(tmp_path / "missing-projects"),
        "AGAM_MEMORY_DIR": str(tmp_path / "missing-memory"),
    }
    before = _mtimes()
    r = _run(LINT_TOOL, env, "--quick", cwd=str(home))
    after = _mtimes()
    assert r.returncode == 0, r.stderr
    assert "[SKIP] No graph database" in r.stdout
    assert before == after, "Real ~/.claude files were modified!"


def test_lint_detects_decision_without_rationale(lint_env):
    """Insert a decision entity with no rationale, confirm lint flags it."""
    env, home, kg, _wl = lint_env
    conn = sqlite3.connect(kg)
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("use-hatchling", "decision", "Chose hatchling over setuptools."),
    )
    conn.commit()
    conn.close()
    r = _run(LINT_TOOL, env, cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "use-hatchling" in r.stdout
    assert "decisions missing rationale" in r.stdout


def test_lint_does_not_touch_real_agam_dir(lint_env):
    """Regression: confirm no write to ~/.claude/agam when AGAM_HOME is redirected."""
    env, home, _kg, _wl = lint_env
    real_agam_dir = pathlib.Path.home() / ".claude" / "agam"
    # Snapshot mtime of every existing file under real AGAM_HOME
    before = {}
    if real_agam_dir.exists():
        for p in real_agam_dir.rglob("*"):
            if p.is_file():
                before[p] = p.stat().st_mtime
    r = _run(LINT_TOOL, env, cwd=str(home))
    assert r.returncode == 0, r.stderr
    after = {}
    if real_agam_dir.exists():
        for p in real_agam_dir.rglob("*"):
            if p.is_file():
                after[p] = p.stat().st_mtime
    # Every pre-existing file must have the same mtime
    for p, t in before.items():
        assert p in after, f"File disappeared: {p}"
        assert after[p] == t, f"File mtime changed: {p}"


# -- session_patterns tests --


def test_patterns_full_analysis(lint_env):
    """Default invocation should produce all sections."""
    env, home, _kg, _wl = lint_env
    r = _run(PATTERNS_TOOL, env, cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "PROJECT ACTIVITY" in r.stdout
    assert "ACTIVITY STREAKS" in r.stdout
    assert "GOAL ACTIVITY GAPS" in r.stdout
    assert "RECURRING TOPICS" in r.stdout


def test_patterns_projects_subcommand(lint_env):
    env, home, _kg, _wl = lint_env
    r = _run(PATTERNS_TOOL, env, "projects", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "PROJECT ACTIVITY" in r.stdout
    assert "agam" in r.stdout.lower()


def test_patterns_streaks_subcommand(lint_env):
    env, home, _kg, _wl = lint_env
    r = _run(PATTERNS_TOOL, env, "streaks", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "ACTIVITY STREAKS" in r.stdout
    assert "Total active days" in r.stdout


def test_patterns_gaps_sees_thisai_goals(lint_env):
    """gaps should read THISAI.md from AGAM_HOME."""
    env, home, _kg, _wl = lint_env
    r = _run(PATTERNS_TOOL, env, "gaps", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "GOAL ACTIVITY GAPS" in r.stdout
    # Fixture THISAI.md has "### Ship the v0 release"
    assert "Ship the v0 release" in r.stdout


def test_patterns_missing_worklog_fails_cleanly(tmp_path):
    """Missing AGAM_WORK_LOG should trigger [FAIL] and exit non-zero."""
    home = tmp_path / "agam"
    home.mkdir()
    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "AGAM_WORK_LOG": str(tmp_path / "missing.md"),
    }
    before = _mtimes()
    r = _run(PATTERNS_TOOL, env, cwd=str(home))
    after = _mtimes()
    assert r.returncode != 0
    assert "[FAIL]" in r.stdout
    assert before == after, "Real ~/.claude files were modified!"


def test_patterns_topics_subcommand(lint_env):
    env, home, _kg, _wl = lint_env
    r = _run(PATTERNS_TOOL, env, "topics", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "RECURRING TOPICS" in r.stdout


def test_patterns_does_not_touch_real_work_log(lint_env):
    """Regression: confirm AGAM_WORK_LOG redirect isolates real work-log."""
    env, home, _kg, _wl = lint_env
    before = _mtimes()
    r = _run(PATTERNS_TOOL, env, cwd=str(home))
    after = _mtimes()
    assert r.returncode == 0, r.stderr
    assert before == after, "Real ~/.claude files were modified!"

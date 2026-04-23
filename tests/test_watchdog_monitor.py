"""Tests for watchdog_monitor.py.

Confirm AGAM_HOME / AGAM_HOOKS_DIR / AGAM_CONTAINER_PATTERN /
AGAM_CONTAINER_NAME env overrides fully isolate the tool from the user's real
~/.claude/ directory and that the status command augments its output with
container discovery + queue dir counts + watchdog.log tail without breaking
existing behavior.

Every test that invokes subprocess stubs `docker` via a fake binary on PATH so
no real container is ever contacted.
"""

import os
import pathlib
import stat
import subprocess

import pytest


TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "agam" / "tools"
MONITOR_TOOL = TOOLS_DIR / "watchdog_monitor.py"

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


def _write_fake_docker(bin_dir: pathlib.Path, ps_stdout: str = "", rc: int = 0):
    """Install a fake `docker` shim that prints ps_stdout for `docker ps ...`
    and exits rc. Any other subcommand is a no-op returning 0."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "docker"
    # No indentation -- shebang must be at column 0 and heredoc marker must
    # match at column 0 too. textwrap.dedent leaves leading whitespace alone
    # when the interpolated content is flush-left.
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "ps" ]; then\n'
        "cat <<'DOCKEREOF'\n"
        f"{ps_stdout}\n"
        "DOCKEREOF\n"
        f"exit {rc}\n"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


@pytest.fixture
def monitor_env(tmp_path):
    """Fabricate an isolated AGAM_HOME with queue dirs + watchdog log and a
    stubbed docker binary on PATH."""
    home = tmp_path / "agam"
    home.mkdir()
    (home / "queue").mkdir()
    (home / "queue-errors").mkdir()
    (home / "logs").mkdir()

    # Drop a handful of queue files so the status line has non-zero counts.
    (home / "queue" / "a.json").write_text("{}")
    (home / "queue" / "b.json").write_text("{}")
    (home / "queue" / "not-counted.txt").write_text("ignore me")
    (home / "queue-errors" / "bad.json").write_text("{}")

    # Last-10-lines tail comes from watchdog.log
    log_lines = "\n".join(f"line-{i}" for i in range(1, 13))  # 12 lines
    (home / "logs" / "watchdog.log").write_text(log_lines + "\n")

    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-code-abc ghcr.io/anthropic/claude-code:latest",
    )

    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()

    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "AGAM_HOOKS_DIR": str(hooks_dir),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
    }
    return env, home, bin_dir


def _run(env, *args, cwd=None, timeout=30, stdin=None):
    r = subprocess.run(
        ["uv", "run", "--script", str(MONITOR_TOOL), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        input=stdin,
    )
    return r


def test_status_shows_container_and_queue_counts(monitor_env):
    """status must display discovered container, queue dir count, error queue
    count, and the last-10 watchdog.log lines -- without breaking existing
    sections."""
    env, home, _bin = monitor_env
    before = _mtimes()
    r = _run(env, "status", cwd=str(home))
    after = _mtimes()
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    # Existing sections still present
    assert "AGAM WATCHDOG MONITOR" in r.stdout
    assert "QUEUE  (idle>=" in r.stdout
    assert "RECENT EVENTS" in r.stdout
    # New container-aware additions
    assert "my-claude-code-abc" in r.stdout
    assert "queue dir" in r.stdout
    assert "2 files" in r.stdout  # a.json + b.json, not the .txt
    assert "error queue" in r.stdout
    assert "1 files" in r.stdout  # bad.json
    # Log tail: we wrote 12 lines, tail should show line-3..line-12
    assert "WATCHDOG LOG" in r.stdout
    assert "line-12" in r.stdout
    assert "line-3" in r.stdout
    assert "line-2" not in r.stdout  # earliest two must be trimmed
    # Real user files untouched
    assert before == after, "Real ~/.claude files were modified!"


def test_status_container_name_override(monitor_env):
    """AGAM_CONTAINER_NAME short-circuits discovery. Even a docker stub that
    prints nothing matching the pattern should yield the override name."""
    env, home, bin_dir = monitor_env
    # Overwrite docker shim so ps returns unrelated output
    _write_fake_docker(bin_dir, ps_stdout="random-nginx nginx:alpine")
    env = {**env, "AGAM_CONTAINER_NAME": "user-pinned-container"}
    r = _run(env, "status", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "user-pinned-container" in r.stdout


def test_status_no_container_reports_none_running(monitor_env):
    """When docker ps returns nothing matching the pattern, status shows
    `(none running)` rather than a legacy placeholder."""
    env, home, bin_dir = monitor_env
    _write_fake_docker(bin_dir, ps_stdout="unrelated-nginx nginx:alpine")
    r = _run(env, "status", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "(none running)" in r.stdout


def test_status_custom_pattern(monitor_env):
    """AGAM_CONTAINER_PATTERN controls the regex used to match container
    names/images."""
    env, home, bin_dir = monitor_env
    _write_fake_docker(
        bin_dir,
        ps_stdout="zebra-box registry.example.com/zebra:1.0",
    )
    env = {**env, "AGAM_CONTAINER_PATTERN": "zebra"}
    r = _run(env, "status", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "zebra-box" in r.stdout


def test_status_missing_queue_dirs_reports_zero(tmp_path):
    """Absent queue/ and queue-errors/ directories must not crash -- counts
    should be 0 and the tool should still exit 0."""
    home = tmp_path / "agam"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    _write_fake_docker(bin_dir, ps_stdout="")
    env = {
        **os.environ,
        "AGAM_HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
    }
    before = _mtimes()
    r = _run(env, "status", cwd=str(home))
    after = _mtimes()
    assert r.returncode == 0, r.stderr
    assert "queue dir    : 0 files" in r.stdout
    assert "error queue  : 0 files" in r.stdout
    assert "(no log at" in r.stdout
    assert before == after, "Real ~/.claude files were modified!"


def test_queue_subcommand_empty(monitor_env):
    """queue subcommand against an empty pending-closes jsonl prints (empty)."""
    env, home, _bin = monitor_env
    r = _run(env, "queue", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "(empty)" in r.stdout


def test_log_subcommand_empty(monitor_env):
    """log subcommand without a .watchdog-log file exits cleanly with no
    output."""
    env, home, _bin = monitor_env
    r = _run(env, "log", cwd=str(home))
    assert r.returncode == 0, r.stderr


def test_json_subcommand_contains_new_fields(monitor_env):
    """--json dump must include the new container-aware fields so
    downstream consumers can read them programmatically."""
    import json as _json
    env, home, _bin = monitor_env
    r = _run(env, "--json", cwd=str(home))
    assert r.returncode == 0, r.stderr
    data = _json.loads(r.stdout)
    assert data["container"] == "my-claude-code-abc"
    assert data["queue_dir_count"] == 2
    assert data["queue_errors_count"] == 1
    assert "line-12" in data["watchdog_log_tail"]


def test_sync_all_status_no_worker(monitor_env):
    """sync-all-status with no PID file reports the no-worker message."""
    env, home, _bin = monitor_env
    r = _run(env, "sync-all-status", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "no detached sync --all worker recorded" in r.stdout


def test_sync_all_stop_no_worker(monitor_env):
    """sync-all-stop with no PID file reports the no-worker message."""
    env, home, _bin = monitor_env
    r = _run(env, "sync-all-stop", cwd=str(home))
    assert r.returncode == 0, r.stderr
    assert "no detached sync --all worker to stop" in r.stdout


def test_unknown_subcommand_prints_doc(monitor_env):
    """Unknown subcommand prints the module docstring and exits 2."""
    env, home, _bin = monitor_env
    r = _run(env, "bogus-command", cwd=str(home))
    assert r.returncode == 2
    assert "watchdog_monitor.py" in r.stdout


def test_status_does_not_touch_real_agam_dir(monitor_env):
    """Regression: confirm no write under ~/.claude/agam when AGAM_HOME is
    redirected to tmpdir."""
    env, home, _bin = monitor_env
    real_agam_dir = pathlib.Path.home() / ".claude" / "agam"
    before = {}
    if real_agam_dir.exists():
        for p in real_agam_dir.rglob("*"):
            if p.is_file():
                before[p] = p.stat().st_mtime
    r = _run(env, "status", cwd=str(home))
    assert r.returncode == 0, r.stderr
    after = {}
    if real_agam_dir.exists():
        for p in real_agam_dir.rglob("*"):
            if p.is_file():
                after[p] = p.stat().st_mtime
    for p, t in before.items():
        assert p in after, f"File disappeared: {p}"
        assert after[p] == t, f"File mtime changed: {p}"

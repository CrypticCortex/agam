"""Tests for agam_watchdog.sh.

Host-side launchd job that discovers a running claude-code container and
docker-exec's the inner watchdog script inside it. Falls back to host mode
only when AGAM_WATCHDOG_MODE=host.

Every test stubs `docker` via a fake binary on PATH and fabricates a
temporary AGAM_HOME so no real container or user file is ever touched.
"""

import os
import pathlib
import stat
import subprocess

import pytest


SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src"
    / "agam"
    / "hooks"
    / "agam_watchdog.sh"
)

REAL_AGAM_MD = pathlib.Path.home() / ".claude" / "agam" / "AGAM.md"
REAL_KG = pathlib.Path.home() / ".claude" / "knowledge" / "graph.db"


def _mtime(p: pathlib.Path):
    return p.stat().st_mtime if p.exists() else None


def _write_fake_docker(
    bin_dir: pathlib.Path,
    ps_stdout: str = "",
    exec_rc: int = 0,
    log_path: pathlib.Path | None = None,
    exec_rc_map: dict | None = None,
):
    """Install a fake `docker` shim on PATH.

    - `docker ps ...`    -> prints `ps_stdout` and exits 0
    - `docker exec ...`  -> logs argv to `log_path` (if given) and exits
                            `exec_rc`. If `exec_rc_map` is given, exits with
                            the rc keyed by the queue entry basename found
                            on stdin.
    Anything else exits 0.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "docker"
    log_line = f'echo "$@" >> "{log_path}"\n' if log_path is not None else ""

    # If per-entry rc map supplied, read stdin, match a filename marker the
    # test plants inside each queue JSON, and exit accordingly. We write the
    # marker as a distinct token like "entry=name" on a single line.
    if exec_rc_map is not None:
        rc_script = (
            'input=$(cat)\n'
            f'{log_line}'
            'rc=0\n'
        )
        for marker, rc in exec_rc_map.items():
            rc_script += (
                f'if echo "$input" | grep -q "entry={marker}"; then rc={rc}; fi\n'
            )
        rc_script += 'exit $rc\n'
    else:
        rc_script = f'{log_line}cat > /dev/null\nexit {exec_rc}\n'

    # `docker inspect -f '{{.State.Running}}' <name>` returns "true" if the
    # name appears as a token in ps_stdout (covers AGAM_CONTAINER_NAME probe).
    inspect_script = (
        '# parse name (last arg after -f format)\n'
        'shift  # drop "inspect"\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -f|--format) shift; shift ;;\n'
        '    *) target="$1"; shift ;;\n'
        '  esac\n'
        'done\n'
        'if echo "' + ps_stdout + '" | grep -qw "$target"; then\n'
        '  echo true\n'
        '  exit 0\n'
        'fi\n'
        'echo false\n'
        'exit 1\n'
    )
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "ps" ]; then\n'
        "cat <<'DOCKEREOF'\n"
        f"{ps_stdout}\n"
        "DOCKEREOF\n"
        "exit 0\n"
        "fi\n"
        'if [ "$1" = "inspect" ]; then\n'
        f"{inspect_script}"
        "fi\n"
        'if [ "$1" = "exec" ]; then\n'
        f"{rc_script}"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _make_home(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "agam"
    (home / "queue").mkdir(parents=True)
    (home / "processed").mkdir()
    (home / "queue-errors").mkdir()
    (home / "logs").mkdir()
    return home


def _write_fake_cli(bin_dir: pathlib.Path, name: str) -> pathlib.Path:
    """Plant a no-op agent CLI so the shell probe can discover it."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / name
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _write_host_inner(hooks_dir: pathlib.Path, log_path: pathlib.Path) -> None:
    """Plant an inner watchdog that records the selected host CLI."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    inner = hooks_dir / "agam_watchdog_inner.py"
    inner.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "AGAM_LLM_CLI=$AGAM_LLM_CLI" >> "{log_path}"\n'
        f'echo "AGAM_LLM_CLI_PATH=$AGAM_LLM_CLI_PATH" >> "{log_path}"\n'
        f'cat >> "{log_path}"\n'
        "exit 0\n"
    )
    inner.chmod(inner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(env, script=SCRIPT, timeout=30):
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _env(home: pathlib.Path, bin_dir: pathlib.Path, **extra) -> dict:
    e = {
        **os.environ,
        "AGAM_HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(home.parent),  # sandbox $HOME so host-mode never hits real ~/
    }
    e.update(extra)
    return e


def _real_file_mtimes():
    return {
        "AGAM.md": _mtime(REAL_AGAM_MD),
        "graph.db": _mtime(REAL_KG),
    }


# ---------------------------------------------------------------------------
# 1. Container mode: docker-exec invoked with expected args.
# ---------------------------------------------------------------------------
def test_container_mode_invokes_docker_exec(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "abc.json").write_text('{"entry=abc":1}')

    docker_log = tmp_path / "docker-exec.log"
    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-abc ghcr.io/anthropic/claude-code:latest",
        exec_rc=0,
        log_path=docker_log,
    )

    env = _env(home, bin_dir)
    before = _real_file_mtimes()
    r = _run(env)
    after = _real_file_mtimes()

    assert r.returncode == 0, r.stderr
    assert before == after, "Real ~/.claude files were modified!"
    # docker exec was invoked with the expected container + inner path
    assert docker_log.exists(), "docker exec was never called"
    logged = docker_log.read_text()
    assert "exec -i my-claude-abc" in logged
    assert "AGAM_HOME=/home/node/.claude/agam" in logged
    assert "/home/node/.claude/hooks/agam_watchdog_inner.py" in logged
    # Entry moved from queue/ to processed/
    assert not (home / "queue" / "abc.json").exists()
    assert (home / "processed" / "abc.json").exists()
    # Log records ok
    tail = (home / "logs" / "watchdog.log").read_text()
    assert "drain-start invoker=container container=my-claude-abc" in tail
    assert "ok abc.json" in tail
    assert "drain-done" in tail


# ---------------------------------------------------------------------------
# 2. Host mode: docker is never called; host inner script is invoked.
# ---------------------------------------------------------------------------
def test_host_mode_skips_docker_and_invokes_host_inner(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "h.json").write_text('{"sid":"host"}')

    # Plant a fake host inner that records its env + stdin and exits 0.
    host_hooks = tmp_path / "home" / ".claude" / "hooks"
    host_hooks.mkdir(parents=True)
    inner_log = tmp_path / "inner.log"
    inner = host_hooks / "agam_watchdog_inner.py"
    inner.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "AGAM_HOME=$AGAM_HOME" >> "{inner_log}"\n'
        f'cat >> "{inner_log}"\n'
        f'echo "" >> "{inner_log}"\n'
        "exit 0\n"
    )
    inner.chmod(inner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Host invoker probe only requires `claude` on PATH (credentials.json
    # file is no longer checked; macOS host stores OAuth in Keychain so
    # that file may never exist). The fake claude binary below is enough.

    # Plant a fake `claude` binary on PATH so the host probe sees it.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_claude = bin_dir / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    docker_log = tmp_path / "docker-exec.log"
    _write_fake_docker(bin_dir, ps_stdout="", exec_rc=0, log_path=docker_log)

    env = _env(
        home,
        bin_dir,
        AGAM_WATCHDOG_MODE="host",
        HOME=str(tmp_path / "home"),
    )

    before = _real_file_mtimes()
    r = _run(env)
    after = _real_file_mtimes()

    assert r.returncode == 0, r.stderr
    assert before == after, "Real ~/.claude files were modified!"
    assert not docker_log.exists(), "docker exec was called in host mode"
    assert inner_log.exists(), "host inner was not invoked"
    body = inner_log.read_text()
    assert f"AGAM_HOME={home}" in body
    assert '"sid":"host"' in body
    assert (home / "processed" / "h.json").exists()
    tail = (home / "logs" / "watchdog.log").read_text()
    assert "drain-start invoker=host" in tail
    assert "ok h.json" in tail


def test_host_auto_falls_back_to_codex_when_other_clis_absent(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "codex.json").write_text('{"sid":"codex"}')

    bin_dir = tmp_path / "bin"
    _write_fake_docker(bin_dir, ps_stdout="")
    _write_fake_cli(bin_dir, "codex")
    inner_log = tmp_path / "inner-codex.log"
    hooks_dir = tmp_path / "hooks"
    _write_host_inner(hooks_dir, inner_log)

    env = _env(home, bin_dir, AGAM_HOOKS_DIR=str(hooks_dir))
    # Exclude the developer machine's installed agent CLIs while retaining the
    # basic POSIX commands used by the shell script.
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert "AGAM_LLM_CLI=codex" in inner_log.read_text()
    assert (home / "processed" / "codex.json").exists()


def test_host_prefers_cursor_agent_over_codex_without_pin(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "cursor.json").write_text('{"sid":"cursor"}')

    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "cursor-agent")
    _write_fake_cli(bin_dir, "codex")
    inner_log = tmp_path / "inner-cursor.log"
    hooks_dir = tmp_path / "hooks"
    _write_host_inner(hooks_dir, inner_log)

    env = _env(
        home,
        bin_dir,
        AGAM_WATCHDOG_MODE="host",
        AGAM_HOOKS_DIR=str(hooks_dir),
    )
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert "AGAM_LLM_CLI=cursor-agent" in inner_log.read_text()


def test_codex_pin_overrides_default_host_cli_preference(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "pinned.json").write_text('{"sid":"pinned"}')

    bin_dir = tmp_path / "bin"
    for cli in ("claude", "cursor-agent", "codex"):
        _write_fake_cli(bin_dir, cli)
    inner_log = tmp_path / "inner-pinned.log"
    hooks_dir = tmp_path / "hooks"
    _write_host_inner(hooks_dir, inner_log)

    env = _env(
        home,
        bin_dir,
        AGAM_WATCHDOG_MODE="host",
        AGAM_HOOKS_DIR=str(hooks_dir),
        AGAM_LLM_CLI_PIN="codex",
    )
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert "AGAM_LLM_CLI=codex" in inner_log.read_text()


def test_absolute_codex_path_works_outside_launchd_path(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "absolute.json").write_text('{"sid":"absolute"}')

    custom_bin = tmp_path / "custom-toolchain"
    codex = _write_fake_cli(custom_bin, "codex")
    bin_dir = tmp_path / "bin"
    inner_log = tmp_path / "inner-absolute.log"
    hooks_dir = tmp_path / "hooks"
    _write_host_inner(hooks_dir, inner_log)

    env = _env(
        home,
        bin_dir,
        AGAM_WATCHDOG_MODE="host",
        AGAM_HOOKS_DIR=str(hooks_dir),
        AGAM_LLM_CLI_PATH=str(codex),
    )
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    r = _run(env)

    assert r.returncode == 0, r.stderr
    logged = inner_log.read_text()
    assert "AGAM_LLM_CLI=codex" in logged
    assert f"AGAM_LLM_CLI_PATH={codex}" in logged


# ---------------------------------------------------------------------------
# 3. No running container: queue preserved, no-container logged.
# ---------------------------------------------------------------------------
def test_no_container_preserves_queue(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "keep.json").write_text('{"entry=keep":1}')

    docker_log = tmp_path / "docker-exec.log"
    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="unrelated-nginx nginx:alpine",
        exec_rc=0,
        log_path=docker_log,
    )

    # Restrict PATH so claude isn't visible. Keep /bin and /usr/bin so the
    # script's own ``bash``/``mkdir``/``date`` etc. still resolve. Without
    # the claude binary on PATH, the host probe fails and the watchdog
    # logs "no-invoker" instead of falling back to host mode.
    env = _env(home, bin_dir)
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    before = _real_file_mtimes()
    r = _run(env)
    after = _real_file_mtimes()

    assert r.returncode == 0, r.stderr
    assert before == after
    assert not docker_log.exists(), "docker exec should NOT be called when no container matches"
    # Queue entry survives
    assert (home / "queue" / "keep.json").exists()
    assert not (home / "processed" / "keep.json").exists()
    assert not (home / "queue-errors" / "keep.json").exists()
    tail = (home / "logs" / "watchdog.log").read_text()
    # New shell cascade logs "no-invoker" when neither container nor host probe
    # is healthy (host probe fails because claude isn't on the restricted PATH).
    assert "no-invoker" in tail
    assert "queue-depth=1" in tail


# ---------------------------------------------------------------------------
# 4. Empty queue: queue-empty logged, exit 0.
# ---------------------------------------------------------------------------
def test_empty_queue_exits_early(tmp_path):
    home = _make_home(tmp_path)
    # No .json files in queue/

    bin_dir = tmp_path / "bin"
    _write_fake_docker(bin_dir, ps_stdout="my-claude-x ghcr.io/anthropic/claude-code:latest")

    env = _env(home, bin_dir)
    r = _run(env)
    assert r.returncode == 0, r.stderr
    tail = (home / "logs" / "watchdog.log").read_text()
    assert "queue-empty" in tail
    assert "drain-start" not in tail


# ---------------------------------------------------------------------------
# 5. Live PID lockfile: second invocation logs already-running and exits 0.
# ---------------------------------------------------------------------------
def test_live_lockfile_blocks_concurrent_tick(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "blocked.json").write_text('{"entry=blocked":1}')

    # Parent of this pytest process is guaranteed alive while the test runs.
    live_pid = os.getppid()
    (home / ".watchdog.lock").write_text(f"{live_pid}\n")

    docker_log = tmp_path / "docker-exec.log"
    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-x ghcr.io/anthropic/claude-code:latest",
        exec_rc=0,
        log_path=docker_log,
    )

    env = _env(home, bin_dir)
    r = _run(env)
    assert r.returncode == 0, r.stderr
    # Queue entry untouched because we refused to drain.
    assert (home / "queue" / "blocked.json").exists()
    assert not docker_log.exists()
    tail = (home / "logs" / "watchdog.log").read_text()
    assert f"already-running pid={live_pid}" in tail
    assert "drain-start" not in tail
    # Lockfile must still hold the original pid -- we did NOT clobber it.
    assert (home / ".watchdog.lock").read_text().strip() == str(live_pid)


def test_stale_lockfile_does_not_block(tmp_path):
    """A lockfile with a dead PID must not prevent draining."""
    home = _make_home(tmp_path)
    (home / "queue" / "go.json").write_text('{"entry=go":1}')

    # PID 999999 is very unlikely to exist on a normal machine.
    (home / ".watchdog.lock").write_text("999999\n")

    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-x ghcr.io/anthropic/claude-code:latest",
        exec_rc=0,
    )

    env = _env(home, bin_dir)
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert (home / "processed" / "go.json").exists()
    # trap removed the lockfile on exit
    assert not (home / ".watchdog.lock").exists()


# ---------------------------------------------------------------------------
# 6. Mixed success/failure: one entry processed, one goes to queue-errors.
# ---------------------------------------------------------------------------
def test_mixed_success_and_failure(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "good.json").write_text('{"entry=good":1}')
    (home / "queue" / "bad.json").write_text('{"entry=bad":1}')

    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-x ghcr.io/anthropic/claude-code:latest",
        exec_rc_map={"good": 0, "bad": 3},
    )

    # MAX_RETRIES=1 -> a single failure dead-letters immediately (the pre-retry
    # behavior this test was written for). Retry-then-dead-letter is covered
    # separately in test_transient_failure_retries_then_dead_letters.
    env = _env(home, bin_dir, AGAM_MAX_RETRIES="1")
    r = _run(env)
    assert r.returncode == 0, r.stderr

    assert (home / "processed" / "good.json").exists()
    assert not (home / "queue" / "good.json").exists()
    assert (home / "queue-errors" / "bad.json").exists()
    assert not (home / "queue" / "bad.json").exists()

    tail = (home / "logs" / "watchdog.log").read_text()
    assert "ok good.json" in tail
    assert "err bad.json rc=3" in tail
    assert "drain-done" in tail


# ---------------------------------------------------------------------------
# 7. AGAM_CONTAINER_NAME override: exact-name match used instead of pattern.
# ---------------------------------------------------------------------------
def test_container_name_override(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "o.json").write_text('{"entry=o":1}')

    docker_log = tmp_path / "docker-exec.log"
    bin_dir = tmp_path / "bin"
    # ps output doesn't match the default pattern, but does include the pinned
    # name in the Names-only stream.
    _write_fake_docker(
        bin_dir,
        ps_stdout="pinned-box\nanother-box",
        exec_rc=0,
        log_path=docker_log,
    )

    env = _env(home, bin_dir, AGAM_CONTAINER_NAME="pinned-box")
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert docker_log.exists()
    assert "exec -i pinned-box" in docker_log.read_text()
    assert (home / "processed" / "o.json").exists()


# ---------------------------------------------------------------------------
# 8. Per-run cap drains the OLDEST entries first; the rest wait for next tick.
# ---------------------------------------------------------------------------
def test_per_run_cap_drains_oldest_first(tmp_path):
    home = _make_home(tmp_path)
    # Four entries; force ascending mtimes so a is oldest, d is newest.
    names = ["a", "b", "c", "d"]
    for i, n in enumerate(names):
        f = home / "queue" / f"{n}.json"
        f.write_text(f'{{"entry={n}":1}}')
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))

    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-x ghcr.io/anthropic/claude-code:latest",
        exec_rc=0,
    )

    env = _env(home, bin_dir, AGAM_MAX_PER_RUN="2")
    r = _run(env)
    assert r.returncode == 0, r.stderr

    # The two OLDEST drained; the two newest remain queued for the next tick.
    assert (home / "processed" / "a.json").exists()
    assert (home / "processed" / "b.json").exists()
    assert (home / "queue" / "c.json").exists()
    assert (home / "queue" / "d.json").exists()
    assert not (home / "processed" / "c.json").exists()

    tail = (home / "logs" / "watchdog.log").read_text()
    assert "per-run-cap cap=2" in tail
    assert "remaining=2" in tail


# ---------------------------------------------------------------------------
# 9. Transient failure retries in place, then dead-letters after MAX_RETRIES.
# ---------------------------------------------------------------------------
def test_transient_failure_retries_then_dead_letters(tmp_path):
    home = _make_home(tmp_path)
    (home / "queue" / "flap.json").write_text('{"entry=flap":1}')

    bin_dir = tmp_path / "bin"
    _write_fake_docker(
        bin_dir,
        ps_stdout="my-claude-x ghcr.io/anthropic/claude-code:latest",
        exec_rc_map={"flap": 5},  # always fails
    )

    env = _env(home, bin_dir, AGAM_MAX_RETRIES="2")

    # Tick 1: first failure -> stays in queue, retry counter at 1, not exiled.
    r = _run(env)
    assert r.returncode == 0, r.stderr
    queued = list((home / "queue").glob("flap.retry-claim.*.json"))
    assert len(queued) == 1
    assert not (home / "queue-errors" / "flap.json").exists()
    retry_sidecar = home / ".retries" / queued[0].name
    assert retry_sidecar.read_text().strip() == "1"
    assert "retry flap.json rc=5 attempt=1/2" in (home / "logs" / "watchdog.log").read_text()

    # Tick 2: second failure hits MAX_RETRIES -> dead-letter, counter cleared.
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert not list((home / "queue").glob("flap*.json"))
    deadletters = list((home / "queue-errors").glob("flap*.json"))
    assert len(deadletters) == 1
    assert not list((home / ".retries").glob("flap*.json"))
    assert "dead-letter" in (home / "logs" / "watchdog.log").read_text()


def test_concurrent_reenqueue_survives_successful_claim(tmp_path):
    """A new generation enqueued during processing must remain in queue/."""
    home = _make_home(tmp_path)
    (home / "queue" / "race.json").write_text('{"generation":"old"}\n')

    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "claude")
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    seen = tmp_path / "seen.json"
    inner = hooks_dir / "agam_watchdog_inner.py"
    inner.write_text(
        "#!/usr/bin/env bash\n"
        "input=$(cat)\n"
        f'printf "%s\\n" "$input" > "{seen}"\n'
        'if echo "$input" | grep -q \'"generation":"old"\'; then\n'
        '  tmp="$AGAM_HOME/queue/.race.json.tmp"\n'
        '  printf \'{"generation":"new"}\\n\' > "$tmp"\n'
        '  mv "$tmp" "$AGAM_HOME/queue/race.json"\n'
        "fi\n"
        "exit 0\n"
    )
    inner.chmod(inner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = _env(
        home,
        bin_dir,
        AGAM_WATCHDOG_MODE="host",
        AGAM_HOOKS_DIR=str(hooks_dir),
    )
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert '"generation":"old"' in seen.read_text()
    assert (home / "processed" / "race.json").read_text().strip() == '{"generation":"old"}'
    assert (home / "queue" / "race.json").read_text().strip() == '{"generation":"new"}'
    assert not list((home / "processing").glob("claim.*"))

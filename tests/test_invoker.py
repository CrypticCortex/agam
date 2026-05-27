"""Tests for the Invoker cascade.

Pure unit tests with stubbed subprocess.run and shutil.which. No real
docker / claude invocations.
"""

from __future__ import annotations

import os
import subprocess as _sp
from dataclasses import dataclass, field
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: fake subprocess result
# ---------------------------------------------------------------------------


@dataclass
class _Proc:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _patch_which(monkeypatch, available: dict[str, str | None]):
    """Patch shutil.which to return canned answers per binary name.

    ``available`` maps each cmd name to either a path string (found) or
    ``None`` (not on PATH). Unknown commands default to None.
    """
    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: available.get(cmd),
    )


def _patch_home(monkeypatch, tmp_path: Path):
    """Point ``$HOME`` at a tempdir so probes resolving ``~`` stay sandboxed.

    The old helper also planted a placebo ``.credentials.json`` file because
    HostInvoker.probe() used to check for it. That check is gone (macOS host
    stores OAuth in Keychain so the file may never exist even with valid
    auth); the helper now only handles HOME isolation. One regression test
    below (``test_host_invoker_probe_ok_without_credentials_file``) asserts
    the file-absence contract explicitly.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


def _patch_run(monkeypatch, responses: list[_Proc]):
    """Patch subprocess.run to return ``responses`` in order, FIFO.

    Each call to subprocess.run pops the next response. If responses runs
    out, subsequent calls raise RuntimeError so tests fail loudly rather
    than reusing stale data.
    """
    calls: list[tuple] = []
    rs = iter(responses)

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        try:
            return next(rs)
        except StopIteration:
            raise RuntimeError(f"subprocess.run called more times than expected: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_invoker_abc_cannot_instantiate():
    from agam.invoker import Invoker

    with pytest.raises(TypeError):
        Invoker()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# HostInvoker
# ---------------------------------------------------------------------------


def test_host_invoker_probe_ok(monkeypatch, tmp_path):
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import HostInvoker

    r = HostInvoker().probe()
    assert r.ok
    assert r.cost_hint == "fast"


def test_host_invoker_probe_fails_when_claude_missing(monkeypatch, tmp_path):
    _patch_which(monkeypatch, {})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import HostInvoker

    r = HostInvoker().probe()
    assert not r.ok
    assert "claude" in r.detail.lower()


def test_host_invoker_probe_ok_without_credentials_file(monkeypatch, tmp_path):
    """Probe must NOT require ~/.claude/.credentials.json -- macOS host stores
    OAuth in Keychain and that file may never exist even with valid auth.
    Real auth failures surface at run() time with claude's own error.
    """
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import HostInvoker

    r = HostInvoker().probe()
    assert r.ok, f"probe must pass when claude is on PATH, regardless of creds file: {r.detail}"


def test_host_invoker_run_invokes_claude(monkeypatch, tmp_path):
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    calls = _patch_run(monkeypatch, [_Proc(stdout="ok")])
    from agam.invoker import HostInvoker

    out = HostInvoker().run("hi", "haiku")
    assert out == "ok"
    assert calls[0][0][0][0] == "claude"
    assert "--model" in calls[0][0][0]
    assert calls[0][1]["input"] == "hi"


def test_host_invoker_run_raises_on_non_zero(monkeypatch, tmp_path):
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    _patch_run(monkeypatch, [_Proc(stderr="auth failed", returncode=1)])
    from agam.invoker import HostInvoker

    with pytest.raises(RuntimeError) as ei:
        HostInvoker().run("hi", "haiku")
    assert "auth failed" in str(ei.value)


# ---------------------------------------------------------------------------
# ContainerInvoker
# ---------------------------------------------------------------------------


def test_container_invoker_probe_ok_with_matching_image(monkeypatch):
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker"})
    _patch_run(monkeypatch, [_Proc(stdout="dev1 my-claude-code:latest\nnginx nginx:latest\n")])
    from agam.invoker import ContainerInvoker

    r = ContainerInvoker(pattern="claude-code").probe()
    assert r.ok
    assert "dev1" in r.detail


def test_container_invoker_probe_fails_when_no_match(monkeypatch):
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker"})
    _patch_run(monkeypatch, [_Proc(stdout="nginx nginx:latest\nredis redis:7\n")])
    from agam.invoker import ContainerInvoker

    r = ContainerInvoker(pattern="claude-code").probe()
    assert not r.ok
    assert "no running container" in r.detail.lower()


def test_container_invoker_probe_fails_when_docker_missing(monkeypatch):
    _patch_which(monkeypatch, {})
    from agam.invoker import ContainerInvoker

    r = ContainerInvoker(pattern="claude-code").probe()
    assert not r.ok
    assert "docker" in r.detail.lower()


def test_container_invoker_run_docker_execs_discovered_name(monkeypatch):
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker"})
    calls = _patch_run(
        monkeypatch,
        [
            _Proc(stdout="dev1 claude-code:latest\n"),  # probe
            _Proc(stdout="hello"),  # run
        ],
    )
    from agam.invoker import ContainerInvoker

    inv = ContainerInvoker(pattern="claude-code")
    inv.probe()
    out = inv.run("hi", "haiku")
    assert out == "hello"
    # Second call (run) used docker exec into "dev1"
    exec_args = calls[1][0][0]
    assert exec_args[:3] == ["docker", "exec", "-i"]
    assert exec_args[3] == "dev1"


# ---------------------------------------------------------------------------
# NamedContainerInvoker
# ---------------------------------------------------------------------------


def test_named_container_probe_ok_when_running(monkeypatch):
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker"})
    _patch_run(monkeypatch, [_Proc(stdout="true\n")])
    from agam.invoker import NamedContainerInvoker

    r = NamedContainerInvoker("my-dev").probe()
    assert r.ok
    assert "my-dev" in r.detail


def test_named_container_probe_fails_when_not_running(monkeypatch):
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker"})
    _patch_run(monkeypatch, [_Proc(stdout="false\n")])
    from agam.invoker import NamedContainerInvoker

    r = NamedContainerInvoker("my-dev").probe()
    assert not r.ok


# ---------------------------------------------------------------------------
# resolve_invoker cascade
# ---------------------------------------------------------------------------


def test_resolve_returns_host_when_only_host_healthy(monkeypatch, tmp_path):
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import resolve_invoker

    inv = resolve_invoker()
    assert inv.name == "host"


def test_resolve_prefers_container_over_host_when_both_available(monkeypatch, tmp_path):
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker", "claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    _patch_run(monkeypatch, [_Proc(stdout="dev1 claude-code:latest\n")])
    from agam.invoker import resolve_invoker

    inv = resolve_invoker()
    assert inv.name == "container"


def test_resolve_honors_AGAM_INVOKER_host_override(monkeypatch, tmp_path):
    """Even with a healthy container, AGAM_INVOKER=host pins to host."""
    monkeypatch.setenv("AGAM_INVOKER", "host")
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude", "docker": "/usr/local/bin/docker"})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import resolve_invoker

    inv = resolve_invoker()
    assert inv.name == "host"


def test_resolve_honors_legacy_AGAM_WATCHDOG_MODE(monkeypatch, tmp_path):
    """AGAM_WATCHDOG_MODE=host (old env var) still pins to host."""
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.setenv("AGAM_WATCHDOG_MODE", "host")
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import resolve_invoker

    inv = resolve_invoker()
    assert inv.name == "host"


def test_resolve_AGAM_INVOKER_takes_precedence_over_legacy(monkeypatch, tmp_path):
    """AGAM_INVOKER wins over AGAM_WATCHDOG_MODE when both set."""
    monkeypatch.setenv("AGAM_INVOKER", "host")
    monkeypatch.setenv("AGAM_WATCHDOG_MODE", "container")  # opposing direction
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import resolve_invoker

    assert resolve_invoker().name == "host"


def test_resolve_raises_when_nothing_healthy(monkeypatch, tmp_path):
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    _patch_which(monkeypatch, {})  # no docker, no claude
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import resolve_invoker, NoInvokerAvailable

    with pytest.raises(NoInvokerAvailable) as ei:
        resolve_invoker()
    msg = str(ei.value)
    assert "container" in msg
    assert "host" in msg


def test_resolve_named_container_first_when_AGAM_CONTAINER_NAME_set(monkeypatch, tmp_path):
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    monkeypatch.setenv("AGAM_CONTAINER_NAME", "specific-dev")
    _patch_which(monkeypatch, {"docker": "/usr/local/bin/docker", "claude": "/usr/local/bin/claude"})
    _patch_home(monkeypatch, tmp_path)
    _patch_run(monkeypatch, [_Proc(stdout="true\n")])
    from agam.invoker import resolve_invoker

    inv = resolve_invoker()
    assert inv.name == "named-container"


# ---------------------------------------------------------------------------
# probe_all -- doctor's view of every candidate
# ---------------------------------------------------------------------------


def test_probe_all_returns_every_candidate(monkeypatch, tmp_path):
    monkeypatch.delenv("AGAM_INVOKER", raising=False)
    monkeypatch.delenv("AGAM_WATCHDOG_MODE", raising=False)
    monkeypatch.delenv("AGAM_CONTAINER_NAME", raising=False)
    _patch_which(monkeypatch, {"claude": "/usr/local/bin/claude"})  # no docker
    _patch_home(monkeypatch, tmp_path)
    from agam.invoker import probe_all

    results = probe_all()
    # Default cascade has 2 candidates when no AGAM_CONTAINER_NAME: container, host
    assert len(results) == 2
    names = [inv.name for inv, _ in results]
    assert "container" in names
    assert "host" in names
    # Host should be ok, container should be no-docker
    by_name = {inv.name: result for inv, result in results}
    assert by_name["host"].ok
    assert not by_name["container"].ok

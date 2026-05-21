"""Invoker cascade -- where Agam finds and runs ``claude -p``.

Agam needs to call ``claude -p`` somewhere. That ``somewhere`` is not
binary. It depends on the user's environment: are they running Claude
Code on host? Inside a devcontainer? Both? Neither?

The Invoker abstraction handles this. Each concrete Invoker knows how
to probe whether its target is healthy and how to run claude there.
``resolve_invoker()`` walks an ordered cascade and returns the first
healthy invoker. The caller never picks a "mode" -- it asks for an
invoker and uses whatever it gets.

Cascade ordering (default):

1. Explicit override via ``AGAM_INVOKER=host|container``.
2. Legacy ``AGAM_WATCHDOG_MODE=host|container`` (honored for back-compat).
3. Named container (``AGAM_CONTAINER_NAME``).
4. Discovered container (image pattern, default ``claude-code``).
5. Host (``claude`` on PATH + ``~/.claude/.credentials.json`` present).

Container is preferred over host when both are available so Agam's
background claude calls don't lock-conflict with an interactive Claude
Code session on host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    cost_hint: str = "fast"


class NoInvokerAvailable(RuntimeError):
    """Raised when the cascade can't find any healthy way to call claude."""


class Invoker(ABC):
    """Abstract claude -p invocation target. Subclasses pick where to run it."""

    name: str = "abstract"

    @abstractmethod
    def probe(self) -> ProbeResult:
        """Cheap health check. Must not actually invoke claude."""

    @abstractmethod
    def run(self, prompt: str, model: str, *, timeout: int = 300) -> str:
        """Invoke ``claude -p --model <model>`` with ``prompt`` on stdin.

        Returns stdout. Raises RuntimeError on non-zero exit.
        """


# ---------------------------------------------------------------------------
# Host invoker -- run claude directly on the host PATH
# ---------------------------------------------------------------------------


@dataclass
class HostInvoker(Invoker):
    name: str = "host"

    def probe(self) -> ProbeResult:
        if not shutil.which("claude"):
            return ProbeResult(False, "claude CLI not on PATH", "fast")
        creds = Path(os.path.expanduser("~/.claude/.credentials.json"))
        if not creds.exists():
            return ProbeResult(
                False,
                "no ~/.claude/.credentials.json (run `claude` once to authenticate)",
                "fast",
            )
        return ProbeResult(True, "host claude ready", "fast")

    def run(self, prompt: str, model: str, *, timeout: int = 300) -> str:
        r = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "stream-json"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"host claude -p failed (rc={r.returncode}): {r.stderr[:500]}"
            )
        return r.stdout


# ---------------------------------------------------------------------------
# Container invoker -- discover by image pattern, docker exec into it
# ---------------------------------------------------------------------------


@dataclass
class ContainerInvoker(Invoker):
    """docker-exec into the first container matching ``pattern``.

    Pattern is a regex (case-insensitive) matched against
    ``<container-name> <image-name>`` lines from ``docker ps``. Default
    ``claude-code`` catches both the public Claude Code devcontainer
    image and any organisation-specific image whose name contains
    ``claude-code``.
    """

    pattern: str = "claude-code"
    name: str = field(default="container", init=False)
    _discovered: str | None = field(default=None, init=False, repr=False)

    def probe(self) -> ProbeResult:
        if not shutil.which("docker"):
            return ProbeResult(False, "docker not on PATH", "fast")
        try:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}} {{.Image}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ProbeResult(False, f"docker ps failed: {exc}", "fast")
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()[:1]
            err_msg = err[0] if err else "non-zero exit"
            return ProbeResult(False, f"docker ps rc={r.returncode}: {err_msg}", "fast")
        rx = re.compile(self.pattern, re.IGNORECASE)
        for line in r.stdout.splitlines():
            if rx.search(line):
                name = line.split()[0]
                self._discovered = name
                return ProbeResult(
                    True, f"container {name!r} matches /{self.pattern}/", "slow"
                )
        return ProbeResult(False, f"no running container matches /{self.pattern}/", "fast")

    def run(self, prompt: str, model: str, *, timeout: int = 300) -> str:
        if not self._discovered:
            res = self.probe()
            if not res.ok:
                raise RuntimeError(res.detail)
        r = subprocess.run(
            [
                "docker", "exec", "-i", self._discovered,
                "claude", "-p", "--model", model, "--output-format", "stream-json",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"docker exec claude -p failed in container {self._discovered!r} "
                f"(rc={r.returncode}): {r.stderr[:500]}"
            )
        return r.stdout


# ---------------------------------------------------------------------------
# Named container -- exact container name match, no discovery regex
# ---------------------------------------------------------------------------


class NamedContainerInvoker(ContainerInvoker):
    """Skip image-pattern discovery; target a container by exact name."""

    def __init__(self, container_name: str) -> None:
        super().__init__(pattern=re.escape(container_name))
        self.name = "named-container"
        self._container_name = container_name

    def probe(self) -> ProbeResult:
        if not shutil.which("docker"):
            return ProbeResult(False, "docker not on PATH", "fast")
        try:
            r = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ProbeResult(False, f"docker inspect failed: {exc}", "fast")
        if r.returncode != 0 or "true" not in r.stdout.lower():
            return ProbeResult(
                False, f"container {self._container_name!r} not running", "fast"
            )
        self._discovered = self._container_name
        return ProbeResult(
            True, f"named container {self._container_name!r} running", "fast"
        )


# ---------------------------------------------------------------------------
# Cascade resolver
# ---------------------------------------------------------------------------


def _candidates() -> list[Invoker]:
    """Build the cascade ordered list based on env vars."""
    explicit = os.environ.get("AGAM_INVOKER", "").strip().lower()
    legacy = os.environ.get("AGAM_WATCHDOG_MODE", "").strip().lower()
    pinned = explicit or legacy

    pattern = os.environ.get("AGAM_CONTAINER_PATTERN", "claude-code")
    named = os.environ.get("AGAM_CONTAINER_NAME", "").strip()

    if pinned == "host":
        return [HostInvoker()]
    if pinned == "container":
        return [ContainerInvoker(pattern=pattern)]

    # Default cascade. Named container first so an exact-name override beats
    # the discovery regex; then discovered container; then host.
    out: list[Invoker] = []
    if named:
        out.append(NamedContainerInvoker(named))
    out.append(ContainerInvoker(pattern=pattern))
    out.append(HostInvoker())
    return out


def probe_all() -> list[tuple[Invoker, ProbeResult]]:
    """Run every candidate's probe. For ``agam doctor`` reporting only."""
    return [(inv, inv.probe()) for inv in _candidates()]


def resolve_invoker() -> Invoker:
    """Walk the cascade. Return the first invoker that probes ok.

    Raises ``NoInvokerAvailable`` if nothing in the cascade is healthy.
    Each failure's detail string is concatenated into the exception so
    the user sees exactly why every path failed.
    """
    failures: list[str] = []
    for inv in _candidates():
        r = inv.probe()
        if r.ok:
            return inv
        failures.append(f"  {inv.name}: {r.detail}")
    raise NoInvokerAvailable(
        "No claude invoker is available. Tried:\n" + "\n".join(failures)
    )

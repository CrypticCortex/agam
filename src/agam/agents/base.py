"""AgentTarget base + detection.

An AgentTarget knows how to detect its agent's presence and install agam's
per-agent wiring (hooks, tools, hook-config registration). Shared data (the
graph, identity, prompts, queue) is NOT an agent concern -- it lives in the
neutral data home and is managed by the installer once, regardless of how many
agents are wired.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class AgentTarget(ABC):
    name: str = "abstract"

    @abstractmethod
    def is_present(self, home: Path) -> bool:
        """True if this agent appears installed for the given home."""

    @abstractmethod
    def detect_evidence(self, home: Path) -> str:
        """Human-readable reason this agent was detected (for the wizard)."""

    @abstractmethod
    def install(self, home: Path) -> None:
        """Install agam's per-agent wiring into this agent's config tree."""

    @abstractmethod
    def hook_config_path(self, home: Path) -> Path:
        """Path to the agent's hook-registration file."""


def _on_path(*binaries: str) -> str | None:
    for b in binaries:
        found = shutil.which(b)
        if found:
            return found
    return None


def detect_agents(home: Path) -> list[AgentTarget]:
    """Return the AgentTargets that appear present on this machine.

    An agent counts as present if its config dir exists OR its CLI is on PATH.
    Ordered claude-first (the historical default) then cursor.
    """
    # Imported here to avoid a circular import at module load.
    from .claude import ClaudeAgent
    from .cursor import CursorAgent

    home = Path(home)
    present: list[AgentTarget] = []
    for agent in (ClaudeAgent(), CursorAgent()):
        if agent.is_present(home):
            present.append(agent)
    return present

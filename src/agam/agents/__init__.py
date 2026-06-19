"""Agent-target adapters.

agam wires the same shared brain into multiple coding agents. Each agent has its
own config location, hook registration format, transcript shape, and recall
mechanism. An ``AgentTarget`` encapsulates one agent's wiring; ``detect_agents``
reports which agents are present so the installer can auto-suggest targets.
"""

from .base import AgentTarget, detect_agents
from .claude import ClaudeAgent
from .cursor import CursorAgent

__all__ = ["AgentTarget", "ClaudeAgent", "CursorAgent", "detect_agents"]

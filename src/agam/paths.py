"""Central path resolution for agam's shared data home.

Historically every hook and tool defaulted its storage to ``~/.claude/...``,
which couples agam to Claude Code. Cursor support means a user may never have a
``~/.claude`` directory at all, so the shared data (knowledge graph, identity
files, prompts, queue, logs) now lives under a neutral root: ``AGAM_DATA_HOME``,
default ``~/.agam``.

This module is the single source of truth for those locations. Individual env
vars (``AGAM_KG_PATH``, ``AGAM_HOME``, ``AGAM_PROMPTS_DIR``) still override
specific paths for back-compat and tests; when unset they derive from
``data_home()``.

The standalone PEP 723 hook scripts cannot import this module (they run via
``uv run --script`` with no package on the path), so they inline the same
fallback logic. Keep the two in sync: env var first, else under ``data_home()``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


_VAULT_ID_RE = re.compile(r"vault_[0-9a-f]{24}")


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def data_home() -> Path:
    """Root of agam's shared, agent-agnostic data.

    ``AGAM_DATA_HOME`` wins if set; otherwise ``~/.agam``.
    """
    env = os.environ.get("AGAM_DATA_HOME")
    return Path(env) if env else _home() / ".agam"


def kg_path() -> Path:
    """SQLite knowledge graph file."""
    env = os.environ.get("AGAM_KG_PATH")
    return Path(env) if env else data_home() / "knowledge" / "graph.db"


def knowledge_dir() -> Path:
    """Directory holding the graph + FTS sidecar caches."""
    return kg_path().parent


def sealed_knowledge_dir() -> Path:
    """Root for source/staging stores that agents must never read directly."""
    return data_home() / "knowledge" / "sealed"


def staging_knowledge_dir() -> Path:
    """Root for versioned classified working copies."""
    return sealed_knowledge_dir() / "staging"


def scopes_dir() -> Path:
    """Root for physically separated, materialized knowledge stores."""
    return data_home() / "knowledge" / "scopes"


def scope_dir(scope: str) -> Path:
    """Return one canonical vault root; reject names and path-shaped input."""
    if not isinstance(scope, str) or _VAULT_ID_RE.fullmatch(scope) is None:
        raise ValueError(f"invalid vault id: {scope!r}")
    return scopes_dir() / scope


def manifests_dir() -> Path:
    return scopes_dir() / "manifests"


def active_manifest_path() -> Path:
    return scopes_dir() / "active.json"


def scope_config_path() -> Path:
    return scopes_dir() / "config.json"


def vault_registry_path() -> Path:
    return scopes_dir() / "registry.json"


def identity_dir() -> Path:
    """Directory holding AGAM.md / THISAI.md / MUGAM.md / config.yaml.

    ``AGAM_HOME`` is the legacy override and still wins for back-compat.
    """
    env = os.environ.get("AGAM_HOME")
    return Path(env) if env else data_home()


def prompts_dir() -> Path:
    """Directory holding watchdog prompt templates."""
    env = os.environ.get("AGAM_PROMPTS_DIR")
    return Path(env) if env else data_home() / "prompts"


def queue_dir() -> Path:
    """Watchdog pending-close queue directory."""
    return identity_dir() / "queue"


def logs_dir() -> Path:
    return identity_dir() / "logs"

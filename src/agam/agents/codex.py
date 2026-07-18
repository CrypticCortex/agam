"""Codex agent target.

Wires Agam's fail-closed, read-only recall into local Codex clients. Codex gets
only the scoped recall hook and its privacy guard under the owned namespace
``~/.codex/hooks/agam``; capture hooks and transcript tools are not installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import _copy
from .base import AgentTarget


CODEX_HOOK_FILES = [
    "graph_recall.py",
    "scope_guard.py",
]
_PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[2]


class CodexAgent(AgentTarget):
    name = "codex"

    def is_present(self, home: Path) -> bool:
        if (home / ".codex").exists():
            return True
        return shutil.which("codex") is not None

    def detect_evidence(self, home: Path) -> str:
        if (home / ".codex").exists():
            return f"{home / '.codex'} exists"
        binary = shutil.which("codex")
        if binary:
            return f"{binary} on PATH"
        return "not detected"

    def hook_config_path(self, home: Path) -> Path:
        return home / ".codex" / "hooks.json"

    def install(self, home: Path):
        codex_dir = home / ".codex"
        hooks_dir = codex_dir / "hooks" / "agam"
        scope_root = home / ".agam" / "knowledge" / "scopes"

        # Do not let an existing alias redirect Agam-owned hook writes outside
        # Codex's namespace. The user's .codex root may itself be intentionally
        # relocated, but descendants we own must be real directories.
        for owned_directory in (codex_dir / "hooks", hooks_dir):
            if owned_directory.is_symlink():
                raise OSError("unsafe Codex hook directory")

        from agam.codex_permissions_merger import merge_permission_profile

        permission_status = merge_permission_profile(
            codex_dir / "config.toml", scope_root.parent, agent="codex"
        )

        _copy.copy_files(
            CODEX_HOOK_FILES, _copy.hooks_src(), hooks_dir, executable=True
        )

        from agam.codex_hooks_merger import merge_hooks_into_file

        merge_hooks_into_file(
            self.hook_config_path(home),
            hooks_dir,
            scope_root=scope_root,
            python_path=_PACKAGE_IMPORT_ROOT,
        )
        return permission_status

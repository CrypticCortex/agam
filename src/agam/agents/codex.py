"""Codex agent target.

Wires Agam into local Codex clients by installing lifecycle hooks under the
owned namespace ``~/.codex/hooks/agam``, vendored helper modules under
``~/.codex/tools/agam``, and merging the hook registrations into
``~/.codex/hooks.json``. Shared memory continues to live under ``~/.agam``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import _copy
from .base import AgentTarget


CODEX_HOOK_FILES = [
    "graph_recall.py",
    "codex_stop.py",
    "lesson_activate.py",
    "lesson_activate_post.py",
]


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

    def install(self, home: Path) -> None:
        codex_dir = home / ".codex"
        hooks_dir = codex_dir / "hooks" / "agam"
        tools_dir = codex_dir / "tools" / "agam"

        _copy.copy_files(
            CODEX_HOOK_FILES, _copy.hooks_src(), hooks_dir, executable=True
        )
        _copy.copy_tools_tree(tools_dir, extra=[_copy.transcripts_src()])

        from agam.codex_hooks_merger import merge_hooks_into_file

        merge_hooks_into_file(
            self.hook_config_path(home), hooks_dir, tools_dir=tools_dir
        )

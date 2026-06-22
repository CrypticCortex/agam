"""Cursor agent target.

Wires agam into Cursor:

- hooks: ``~/.cursor/hooks/cursor_stop.py`` (refresh recall rule) and
  ``cursor_session_end.py`` (enqueue for the watchdog), registered in
  ``~/.cursor/hooks.json``.
- tools: ``~/.cursor/tools/agam/`` gets the tool modules plus ``transcripts.py``
  (vendored by the session-end hook).

The hooks self-resolve their shared-data paths: ``AGAM_DATA_HOME`` defaults to
``~/.agam`` and the tools dir is found as a sibling of the hooks dir, so no env
wiring is required in ``hooks.json``. Recall reaches the model via the
auto-generated ``.cursor/rules/agam.mdc`` (Cursor's only reliable channel).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import _copy
from .base import AgentTarget

CURSOR_HOOK_FILES = ["cursor_stop.py", "cursor_session_end.py"]


class CursorAgent(AgentTarget):
    name = "cursor"

    def is_present(self, home: Path) -> bool:
        if (home / ".cursor").exists():
            return True
        return _copy_on_path() is not None

    def detect_evidence(self, home: Path) -> str:
        if (home / ".cursor").exists():
            return f"{home / '.cursor'} exists"
        binary = _copy_on_path()
        if binary:
            return f"{binary} on PATH"
        return "not detected"

    def hook_config_path(self, home: Path) -> Path:
        return home / ".cursor" / "hooks.json"

    def install(self, home: Path) -> None:
        cursor_dir = home / ".cursor"
        hooks_dir = cursor_dir / "hooks"
        tools_dir = cursor_dir / "tools" / "agam"

        _copy.copy_files(
            CURSOR_HOOK_FILES, _copy.hooks_src(), hooks_dir, executable=True
        )
        _copy.copy_tools_tree(tools_dir, extra=[_copy.transcripts_src()])

        # Register hooks (idempotent merge, preserves any user hooks).
        from agam.cursor_hooks_merger import merge_hooks_into_file

        merge_hooks_into_file(self.hook_config_path(home), hooks_dir)


def _copy_on_path():
    return shutil.which("cursor") or shutil.which("cursor-agent")

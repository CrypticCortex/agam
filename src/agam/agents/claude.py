"""Claude Code agent target.

Wires agam into Claude Code: copies the hook scripts into ``~/.claude/hooks``,
the tool modules into ``~/.claude/tools/agam``, registers hooks in
``~/.claude/settings.json``, and pins ``AGAM_DATA_HOME`` in the settings env so
Claude's hooks read the neutral shared data home (``~/.agam``) deterministically
rather than depending on a code default.

Shared data (identity files, knowledge graph, prompts) and the launchd watchdog
plist remain the installer's responsibility -- they are not agent-specific.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import _copy
from .base import AgentTarget

# Every hook except the cursor_* ones.
CLAUDE_HOOK_FILES = [
    "graph_recall.py",
    "graph_update.py",
    "session_close.py",
    "lesson_activate.py",
    "lesson_activate_post.py",
    "agam_watchdog.sh",
    "agam_watchdog_inner.py",
]


class ClaudeAgent(AgentTarget):
    name = "claude"

    def is_present(self, home: Path) -> bool:
        if (home / ".claude").exists():
            return True
        return shutil.which("claude") is not None

    def detect_evidence(self, home: Path) -> str:
        if (home / ".claude").exists():
            return f"{home / '.claude'} exists"
        binary = shutil.which("claude")
        if binary:
            return f"{binary} on PATH"
        return "not detected"

    def hook_config_path(self, home: Path) -> Path:
        return home / ".claude" / "settings.json"

    def install(self, home: Path) -> None:
        claude_dir = home / ".claude"
        hooks_dir = claude_dir / "hooks"
        tools_dir = claude_dir / "tools" / "agam"

        _copy.copy_files(
            CLAUDE_HOOK_FILES, _copy.hooks_src(), hooks_dir, executable=True
        )
        _copy.copy_tools_tree(tools_dir)

        from agam.settings_merger import merge_hooks_into_settings

        merge_hooks_into_settings(self.hook_config_path(home), hooks_dir)

        # Pin the shared-home env so Claude's hooks (recall, session_close,
        # graph_update) read + enqueue against ~/.agam -- the same brain + queue
        # the shared watchdog drains. Without AGAM_HOME the Stop hook would
        # enqueue to ~/.claude/agam/queue, which the watchdog never reads.
        from agam.installer import _set_settings_env

        agam_home = home / ".agam"
        settings_path = self.hook_config_path(home)
        _set_settings_env(settings_path, "AGAM_DATA_HOME", str(agam_home))
        _set_settings_env(settings_path, "AGAM_HOME", str(agam_home))
        _set_settings_env(
            settings_path, "AGAM_KG_PATH", str(agam_home / "knowledge" / "graph.db")
        )

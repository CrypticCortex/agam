"""Backup-safe migration from the legacy ``~/.claude`` layout to ``~/.agam``.

Older agam installs stored the shared data (knowledge graph + identity files +
prompts) under ``~/.claude/knowledge`` and ``~/.claude/agam``. The neutral data
home is ``~/.agam``. This module moves a user across, but it NEVER deletes or
mutates the legacy data: it copies, verifies, and leaves the originals exactly
where they were. If anything looks wrong the user can delete ``~/.agam`` and the
old install is untouched.

Contract (``migrate_if_needed``):

- ``~/.agam`` already has content        -> ("already", None)   no-op.
- ``~/.claude/knowledge/graph.db`` found -> ("migrated", dest)  copy across.
- neither                                -> ("fresh", None)     clean install.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


def _has_content(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return path.exists()
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False


def _copy_tree_into(src: Path, dst: Path) -> None:
    """Copy every entry under ``src`` into ``dst`` (dst created if needed).

    Existing files in ``dst`` are not overwritten -- migration only fills gaps,
    so a partially-created ``~/.agam`` is never clobbered. Uses copy2 to keep
    mtimes (useful for the watchdog cutoff heuristics).
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_tree_into(item, target)
        elif not target.exists():
            shutil.copy2(item, target)


def migrate_if_needed(home: Path) -> tuple[str, Path | None]:
    """Migrate legacy ``~/.claude`` data into ``~/.agam`` if appropriate.

    Returns a ``(status, dest)`` tuple where status is one of
    ``"already" | "migrated" | "fresh"``.
    """
    home = Path(home)
    agam_home = home / ".agam"
    if _has_content(agam_home):
        return "already", None

    legacy_kg = home / ".claude" / "knowledge" / "graph.db"
    if not legacy_kg.exists():
        return "fresh", None

    # Copy the knowledge dir (graph.db + FTS sidecar caches) and the identity
    # dir (AGAM.md/THISAI.md/MUGAM.md/config.yaml/prompts) into ~/.agam.
    legacy_knowledge = home / ".claude" / "knowledge"
    legacy_identity = home / ".claude" / "agam"

    _copy_tree_into(legacy_knowledge, agam_home / "knowledge")
    if _has_content(legacy_identity):
        _copy_tree_into(legacy_identity, agam_home)

    marker = agam_home / ".migrated-from"
    marker.write_text(
        f"source: {legacy_kg.parent.parent}\n"
        f"migrated-at: {datetime.now(timezone.utc).isoformat()}\n"
        f"note: originals left untouched; delete ~/.agam to revert.\n",
        encoding="utf-8",
    )
    return "migrated", agam_home

#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Maintain the per-workspace Cursor recall rule.

Cursor's only reliable model-facing channel is a rule file with
``alwaysApply: true``. The Cursor stop hook renders agam's digest (via
``agam_context.py render-rule``) and writes it to
``<workspace>/.cursor/rules/agam.mdc`` after each turn so the model always sees
fresh identity + graph context.

The file lives inside the user's repo (that is where Cursor reads project rules),
so we add it to ``.git/info/exclude`` -- never committed, never shows up as a
dirty file, never touches the user's tracked ``.gitignore``.

Importable as ``agam.tools.cursor_rule`` (tests) and vendored onto a hook's
``sys.path`` (runtime).
"""

from __future__ import annotations

from pathlib import Path

RULE_RELPATH = ".cursor/rules/agam.mdc"
EXCLUDE_LINE = "/.cursor/rules/agam.mdc"


def ensure_git_excluded(workspace_root: Path) -> bool:
    """Add the rule file to ``.git/info/exclude`` if a repo is present.

    Returns True if the line is present afterward, False if there is no git
    repo (nothing to exclude). Idempotent.
    """
    git_dir = workspace_root / ".git"
    if not git_dir.exists():
        return False
    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude = info_dir / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = {ln.strip() for ln in existing.splitlines()}
    if EXCLUDE_LINE in lines:
        return True
    sep = "" if existing == "" or existing.endswith("\n") else "\n"
    with open(exclude, "a", encoding="utf-8") as f:
        f.write(f"{sep}{EXCLUDE_LINE}\n")
    return True


def write_rule(workspace_root: str | Path, content: str) -> Path:
    """Write ``content`` to ``<workspace>/.cursor/rules/agam.mdc`` (git-excluded).

    Returns the path written.
    """
    workspace_root = Path(workspace_root)
    rule_path = workspace_root / RULE_RELPATH
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    rule_path.write_text(content, encoding="utf-8")
    ensure_git_excluded(workspace_root)
    return rule_path

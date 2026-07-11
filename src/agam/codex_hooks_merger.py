"""Safe merge of Agam hooks into ``~/.codex/hooks.json``.

Codex uses the same three-level hook layout as Claude Code: an event contains
matcher groups, and each group contains one or more handlers::

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              {"type": "command", "command": "/absolute/hook.py"}
            ]
          }
        ]
      }
    }

This module preserves user-owned configuration, deduplicates Agam handlers on
``(event, matcher, command)``, and writes through a sibling temporary file so
an interrupted install cannot truncate the user's hook configuration.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any


_AGAM_HOOKS: tuple[tuple[str, str, str | None], ...] = (
    ("UserPromptSubmit", "graph_recall.py", None),
    ("Stop", "codex_stop.py", None),
    ("PreToolUse", "lesson_activate.py", "Bash"),
    ("PreToolUse", "lesson_activate.py", "Edit|Write"),
    ("PostToolUse", "lesson_activate_post.py", "Bash"),
)


def _guarded(script: Path, *, tools_dir: Path | None = None) -> str:
    """Return a shell command that silently skips an absent hook script."""
    path = shlex.quote(str(script))
    exec_command = f"exec {path}"
    if tools_dir is not None:
        tools = shlex.quote(str(tools_dir))
        exec_command = f"AGAM_TOOLS_DIR={tools} {exec_command}"
    return f"[ -x {path} ] && {exec_command} || true"


def agam_hook_entries(
    hooks_dir: Path,
    *,
    tools_dir: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return Agam's canonical Codex hook groups for ``hooks_dir``."""
    hooks_dir = Path(hooks_dir).expanduser().resolve(strict=False)
    if tools_dir is not None:
        tools_dir = Path(tools_dir).expanduser().resolve(strict=False)
    result: dict[str, list[dict[str, Any]]] = {}
    for event, filename, matcher in _AGAM_HOOKS:
        group: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": _guarded(
                        hooks_dir / filename, tools_dir=tools_dir
                    ),
                    "timeout": 30,
                }
            ]
        }
        # UserPromptSubmit and Stop do not support matchers. Omitting the key
        # follows Codex's canonical JSON examples and matches every event.
        if matcher is not None:
            group["matcher"] = matcher
        result.setdefault(event, []).append(group)
    return result


def _group_identities(group: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(matcher, command)`` identities from one nested group."""
    matcher = group.get("matcher", "")
    if not isinstance(matcher, str):
        return []
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return []
    return [
        (matcher, handler["command"])
        for handler in handlers
        if isinstance(handler, dict) and isinstance(handler.get("command"), str)
    ]


def merge_hooks(
    existing: dict[str, Any],
    new_hooks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Return a deep-copied Codex config with ``new_hooks`` merged into it."""
    if not isinstance(existing, dict):
        raise TypeError("existing hooks config must be an object")

    result = copy.deepcopy(existing)
    section = result.setdefault("hooks", {})
    if not isinstance(section, dict):
        raise TypeError("hooks must be an object")

    for event, groups in new_hooks.items():
        if not isinstance(groups, list):
            raise TypeError(f"new_hooks[{event!r}] must be a list")

        existing_groups = section.setdefault(event, [])
        if not isinstance(existing_groups, list):
            raise TypeError(f"hooks[{event!r}] must be a list")

        identities: set[tuple[str, str]] = set()
        for group in existing_groups:
            if isinstance(group, dict):
                identities.update(_group_identities(group))

        for candidate in groups:
            if not isinstance(candidate, dict):
                raise TypeError(f"new_hooks[{event!r}] entries must be objects")
            group = copy.deepcopy(candidate)
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise TypeError(
                    f"new_hooks[{event!r}] entries must contain a hooks list"
                )

            matcher = group.get("matcher", "")
            if not isinstance(matcher, str):
                raise TypeError(f"new_hooks[{event!r}] matcher must be a string")

            novel_handlers: list[dict[str, Any]] = []
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise TypeError(
                        f"new_hooks[{event!r}] handlers must be objects"
                    )
                command = handler.get("command")
                if not isinstance(command, str):
                    raise TypeError(
                        f"new_hooks[{event!r}] command handlers need a command"
                    )
                identity = (matcher, command)
                if identity in identities:
                    continue
                novel_handlers.append(handler)
                identities.add(identity)

            if novel_handlers:
                group["hooks"] = novel_handlers
                existing_groups.append(group)

    return result


def merge_hooks_into_file(
    hooks_path: str | os.PathLike[str],
    hooks_dir: str | os.PathLike[str],
    *,
    tools_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read, merge, and atomically replace a Codex ``hooks.json`` file."""
    hooks_path = Path(hooks_path)
    hooks_dir = Path(hooks_dir)
    resolved_tools_dir = Path(tools_dir) if tools_dir is not None else None

    if hooks_path.exists():
        raw = hooks_path.read_text(encoding="utf-8")
        existing = json.loads(raw) if raw.strip() else {}
        if not isinstance(existing, dict):
            raise TypeError(f"{hooks_path} root must be a JSON object")
    else:
        existing = {}

    merged = merge_hooks(
        existing,
        agam_hook_entries(hooks_dir, tools_dir=resolved_tools_dir),
    )

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".hooks-",
        suffix=".json.tmp",
        dir=str(hooks_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, hooks_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    return merged

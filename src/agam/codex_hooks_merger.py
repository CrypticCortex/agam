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
import stat
import tempfile
from pathlib import Path
from typing import Any


_AGAM_HOOKS: tuple[tuple[str, str, str | None], ...] = (
    ("UserPromptSubmit", "graph_recall.py", None),
    ("PreToolUse", "scope_guard.py", "*"),
)
_MAX_HOOKS_CONFIG_BYTES = 5 * 1024 * 1024


def _read_existing_hooks(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise OSError("unsafe hooks config")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_HOOKS_CONFIG_BYTES
        ):
            raise OSError("unsafe hooks config")
        chunks: list[bytes] = []
        remaining = _MAX_HOOKS_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise OSError("hooks config too large")
    finally:
        os.close(descriptor)
    try:
        raw = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("hooks config is not UTF-8") from None
    existing = json.loads(raw) if raw.strip() else {}
    if not isinstance(existing, dict):
        raise TypeError("hooks config root must be an object")
    return existing


def _guarded(
    script: Path,
    *,
    scope_root: Path,
    fail_closed: bool = False,
    python_path: Path | None = None,
    tools_dir: Path | None = None,
) -> str:
    """Return a shell command that silently skips an absent hook script."""
    path = shlex.quote(str(script))
    assignments = {
        "AGAM_RECALL_AGENT": "codex",
        "AGAM_KNOWLEDGE_SCOPE_ROOT": scope_root,
        "AGAM_KNOWLEDGE_CONFIG": scope_root / "config.json",
        "AGAM_ACTIVE_MANIFEST": scope_root / "active.json",
    }
    if python_path is not None:
        assignments["PYTHONPATH"] = python_path
    if tools_dir is not None:
        assignments["AGAM_TOOLS_DIR"] = tools_dir
    environment = " ".join(
        f"{name}={shlex.quote(str(value))}" for name, value in assignments.items()
    )
    if fail_closed:
        fallback = (
            "{ printf '%s\\n' agam_scope_guard_unavailable >&2; exit 2; }"
        )
        # Keep the shell alive so a hook that starts successfully but crashes
        # still reaches the blocking fallback. ``exec`` would replace the
        # shell and turn the hook's non-zero exit into a silent bypass.
        return f"[ -x {path} ] && {environment} {path} || {fallback}"
    exec_command = f"{environment} exec {path}"
    return f"[ -x {path} ] && {exec_command} || true"


def agam_hook_entries(
    hooks_dir: Path,
    *,
    scope_root: Path,
    profile: str | None = None,
    python_path: Path | None = None,
    tools_dir: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return Agam's canonical Codex hook groups for ``hooks_dir``."""
    if profile is not None:
        raise ValueError("profile_selector_removed")
    hooks_dir = Path(hooks_dir).expanduser().resolve(strict=False)
    scope_root = Path(scope_root).expanduser().resolve(strict=False)
    if python_path is not None:
        python_path = Path(python_path).expanduser().resolve(strict=False)
    if tools_dir is not None:
        tools_dir = Path(tools_dir).expanduser().resolve(strict=False)
    result: dict[str, list[dict[str, Any]]] = {}
    for event, filename, matcher in _AGAM_HOOKS:
        group: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": _guarded(
                        hooks_dir / filename,
                        scope_root=scope_root,
                        fail_closed=filename == "scope_guard.py",
                        python_path=python_path,
                        tools_dir=tools_dir,
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


def _command_points_into_namespace(command: str, hooks_dir: Path) -> bool:
    """Whether a command references a path inside Agam's owned hook root."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        candidate = token.strip("[]();|&<>")
        if not candidate.startswith("/"):
            continue
        try:
            resolved = Path(candidate).resolve(strict=False)
            resolved.relative_to(hooks_dir)
        except (OSError, RuntimeError, ValueError):
            continue
        return True
    return False


def _prune_owned_hooks(existing: dict[str, Any], hooks_dir: Path) -> dict[str, Any]:
    """Remove stale Agam-owned handlers while preserving every user handler."""
    result = copy.deepcopy(existing)
    section = result.get("hooks")
    if not isinstance(section, dict):
        return result

    for event, groups in tuple(section.items()):
        if not isinstance(groups, list):
            continue
        retained_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                retained_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                retained_groups.append(group)
                continue
            retained_handlers = [
                handler
                for handler in handlers
                if not (
                    isinstance(handler, dict)
                    and isinstance(handler.get("command"), str)
                    and _command_points_into_namespace(
                        handler["command"], hooks_dir
                    )
                )
            ]
            if retained_handlers:
                retained_group = copy.deepcopy(group)
                retained_group["hooks"] = retained_handlers
                retained_groups.append(retained_group)
        if retained_groups:
            section[event] = retained_groups
        else:
            del section[event]
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
    scope_root: str | os.PathLike[str],
    profile: str | None = None,
    python_path: str | os.PathLike[str] | None = None,
    tools_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read, merge, and atomically replace a Codex ``hooks.json`` file."""
    hooks_path = Path(hooks_path)
    hooks_dir = Path(hooks_dir).expanduser().resolve(strict=False)
    resolved_scope_root = Path(scope_root).expanduser().resolve(strict=False)
    resolved_python_path = Path(python_path) if python_path is not None else None
    resolved_tools_dir = Path(tools_dir) if tools_dir is not None else None

    existing = _read_existing_hooks(hooks_path)

    pruned = _prune_owned_hooks(existing, hooks_dir)
    merged = merge_hooks(
        pruned,
        agam_hook_entries(
            hooks_dir,
            scope_root=resolved_scope_root,
            profile=profile,
            python_path=resolved_python_path,
            tools_dir=resolved_tools_dir,
        ),
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

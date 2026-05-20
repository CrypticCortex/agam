"""Safe merge of Agam hooks into ``~/.claude/settings.json``.

Claude Code's hook schema in ``settings.json`` has a nested shape::

    {
      "hooks": {
        "<EventName>": [
          {
            "matcher": "<pattern>",
            "hooks": [
              {"type": "command", "command": "<path-or-cmd>", "timeout": 5}
            ]
          }
        ]
      }
    }

The event name keys an ordered list of "hook blocks". Each block has an
optional ``matcher`` (interpreted by Claude Code against tool names for
PreToolUse/PostToolUse events, ignored for UserPromptSubmit/Stop/etc.)
and an inner ``hooks`` list of command entries.

This module provides two entry points:

* ``merge_hooks(existing_settings, new_hooks)`` -- pure function, returns
  a deep-copied dict with ``new_hooks`` folded into ``existing_settings``.
  Dedupes on the inner ``command`` string (matcher-aware). Does not mutate
  the input. Non-hook keys in ``existing_settings`` are preserved as-is.

* ``merge_hooks_into_settings(settings_path, hooks_dir)`` -- reads the
  settings file (or starts from ``{}`` if missing), generates the standard
  Agam hook block pointing inside ``hooks_dir``, merges, and writes the
  result back atomically (write to a sibling tempfile, then ``os.replace``).

Input shape for ``new_hooks`` in ``merge_hooks`` is flexible. Each event
list may contain either:

* The full Claude Code block shape: ``{"matcher": "...", "hooks": [...]}``
* A simplified shape ``{"command": "...", "matcher": "...", "timeout": ...}``
  that we normalize into the nested shape before merging.

Both shapes survive a roundtrip into the nested layout on write.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Normalization: flatten any supported input shape to the canonical block
# ---------------------------------------------------------------------------


def _normalize_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single hook entry into Claude Code's block shape.

    Accepts either::

        {"matcher": "...", "hooks": [{"type": "command", "command": "..."}]}

    or the simplified::

        {"command": "...", "matcher": "?", "timeout": ?, "type": "?"}

    Returns a new dict in the nested shape. Missing ``matcher`` defaults
    to the empty string (matches every tool for matcher-aware events,
    ignored by events that don't use matchers). ``type`` defaults to
    ``"command"``.
    """
    if "hooks" in entry and isinstance(entry["hooks"], list):
        # Already nested. Deep-copy so callers can't mutate our state
        # via the input reference.
        block = copy.deepcopy(entry)
        block.setdefault("matcher", "")
        # Ensure each inner hook has the required keys.
        for inner in block["hooks"]:
            inner.setdefault("type", "command")
        return block

    if "command" not in entry:
        raise ValueError(
            "hook entry must contain 'command' or a nested 'hooks' list; "
            f"got keys: {sorted(entry.keys())}"
        )

    inner: dict[str, Any] = {
        "type": entry.get("type", "command"),
        "command": entry["command"],
    }
    if "timeout" in entry:
        inner["timeout"] = entry["timeout"]
    return {
        "matcher": entry.get("matcher", ""),
        "hooks": [inner],
    }


def _iter_commands(block: dict[str, Any]) -> list[tuple[str, str]]:
    """Return a list of ``(matcher, command)`` pairs inside a block.

    A single block can carry multiple inner hooks; each counts as a
    separate identity for dedup purposes.
    """
    matcher = block.get("matcher", "")
    result: list[tuple[str, str]] = []
    inner_hooks = block.get("hooks")
    if isinstance(inner_hooks, list):
        for inner in inner_hooks:
            if isinstance(inner, dict) and "command" in inner:
                result.append((matcher, inner["command"]))
    # Flat fallback: caller handed us a non-nested dict. Normalize first
    # when producing the identity so dedup lines up with merge.
    elif "command" in block:
        result.append((matcher, block["command"]))
    return result


# ---------------------------------------------------------------------------
# Pure merge
# ---------------------------------------------------------------------------


def merge_hooks(
    existing_settings: dict[str, Any],
    new_hooks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Fold ``new_hooks`` into ``existing_settings['hooks']``.

    Parameters
    ----------
    existing_settings
        Parsed settings.json. Not mutated.
    new_hooks
        Mapping of event name -> list of hook entries (either nested
        Claude Code blocks or simplified ``{"command": ...}`` dicts).

    Returns
    -------
    dict
        A deep-copied settings dict with hooks merged. Non-hook keys are
        preserved verbatim. Dedup is matcher-aware: the same command under
        a different matcher counts as a distinct hook and is appended.
    """
    result = copy.deepcopy(existing_settings)
    hooks_section = result.setdefault("hooks", {})
    if not isinstance(hooks_section, dict):
        raise TypeError(
            f"existing_settings['hooks'] must be a dict, "
            f"got {type(hooks_section).__name__}"
        )

    for event, entries in new_hooks.items():
        if not isinstance(entries, list):
            raise TypeError(
                f"new_hooks[{event!r}] must be a list, "
                f"got {type(entries).__name__}"
            )

        existing_blocks = hooks_section.setdefault(event, [])
        if not isinstance(existing_blocks, list):
            raise TypeError(
                f"existing_settings['hooks'][{event!r}] must be a list, "
                f"got {type(existing_blocks).__name__}"
            )

        # Build the set of (matcher, command) pairs already present under
        # this event so we can skip dupes.
        existing_identities: set[tuple[str, str]] = set()
        for block in existing_blocks:
            if isinstance(block, dict):
                existing_identities.update(_iter_commands(block))

        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError(
                    f"new_hooks[{event!r}] entries must be dicts, "
                    f"got {type(entry).__name__}"
                )
            block = _normalize_block(entry)
            # A normalized block always carries exactly one inner hook
            # when built from the simplified shape, but may carry many
            # when the caller already passed a nested block. Check each
            # inner command independently; append the whole block only
            # if at least one inner command is novel, and strip any
            # duplicate inner entries before appending.
            block_identities = _iter_commands(block)
            novel_inners = [
                inner
                for inner, ident in zip(block["hooks"], block_identities)
                if ident not in existing_identities
            ]
            if not novel_inners:
                continue
            block["hooks"] = novel_inners
            existing_blocks.append(block)
            existing_identities.update(
                (block["matcher"], inner["command"]) for inner in novel_inners
            )

    return result


# ---------------------------------------------------------------------------
# Standard Agam hook set
# ---------------------------------------------------------------------------


def _agam_hook_entries(hooks_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the canonical Agam hook registration map.

    ``hooks_dir`` is the directory where the installer wrote the Agam
    hook scripts (typically ``~/.claude/hooks``). All commands are
    absolute paths -- Claude Code resolves ``~`` itself, but absolute
    paths are safer and unambiguous.
    """
    hooks_dir = Path(hooks_dir)
    return {
        "UserPromptSubmit": [
            {"command": str(hooks_dir / "graph_recall.py"), "matcher": ""},
        ],
        "Stop": [
            {"command": str(hooks_dir / "graph_update.py"), "matcher": ""},
            {"command": str(hooks_dir / "session_close.py"), "matcher": ""},
        ],
        "PreToolUse": [
            # Bash trigger -> command-pattern lessons (e.g. "pip install in conda").
            {
                "command": str(hooks_dir / "lesson_activate.py"),
                "matcher": "Bash",
            },
            # Edit/Write/MultiEdit trigger -> file-path lessons (e.g. "mirror
            # this change into the repo"). Same script, different matcher.
            {
                "command": str(hooks_dir / "lesson_activate.py"),
                "matcher": "Edit|Write|MultiEdit",
            },
        ],
        "PostToolUse": [
            {
                "command": str(hooks_dir / "lesson_activate_post.py"),
                "matcher": "Bash",
            },
        ],
    }


# ---------------------------------------------------------------------------
# High-level: read, merge, write atomically
# ---------------------------------------------------------------------------


def merge_hooks_into_settings(
    settings_path: str | os.PathLike[str],
    hooks_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Read settings.json, merge Agam hooks, write back atomically.

    If ``settings_path`` does not exist, starts from ``{}``. The write
    uses a sibling tempfile + ``os.replace`` so a crash mid-write leaves
    the original file untouched.

    Returns the merged settings dict for inspection (useful in tests).
    """
    settings_path = Path(settings_path)
    hooks_dir = Path(hooks_dir)

    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        if raw.strip() == "":
            existing: dict[str, Any] = {}
        else:
            existing = json.loads(raw)
            if not isinstance(existing, dict):
                raise TypeError(
                    f"{settings_path} must contain a JSON object at the "
                    f"root, got {type(existing).__name__}"
                )
    else:
        existing = {}

    merged = merge_hooks(existing, _agam_hook_entries(hooks_dir))

    # Atomic write: tempfile in the same directory + os.replace. Keeping
    # the tempfile on the same filesystem guarantees rename is atomic on
    # POSIX. ensure_ascii=False preserves unicode in user content.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".json.tmp",
        dir=str(settings_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, settings_path)
    except Exception:
        # Leave the original file untouched. Clean up the temp artifact.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    return merged

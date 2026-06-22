"""Safe merge of Agam hooks into ``~/.cursor/hooks.json``.

Cursor's hook config is flatter than Claude's ``settings.json``::

    {
      "version": 1,
      "hooks": {
        "<eventName>": [
          {"command": "<path>", "matcher": "<optional>", "timeout": 30}
        ]
      }
    }

Scripts in a USER hooks file run from ``~/.cursor/``, so commands are written as
absolute paths to be unambiguous (Cursor accepts absolute paths).

Like ``settings_merger`` for Claude, this:

* preserves any existing non-Agam hooks,
* dedupes Agam's own entries on ``(event, command)`` so re-running install is
  idempotent,
* writes atomically (sibling tempfile + ``os.replace``).

Event choices reflect three Cursor constraints (see the design doc):
- ``beforeSubmitPrompt`` cannot inject context, so it is NOT used for recall.
- ``preToolUse`` can only message the agent on *deny*, so it cannot inject
  advisory lessons; lessons instead ride in the always-on rule digest.
- transcripts flush lazily (at session end), so the authoritative enqueue is on
  ``sessionEnd`` (complete transcript) while ``stop`` only does the cheap rule
  refresh.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def agam_hook_entries(hooks_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Canonical Agam hook registration map for Cursor."""
    hooks_dir = Path(hooks_dir)
    return {
        # Cheap, fire-and-forget: refresh the .cursor/rules/agam.mdc digest and
        # do a best-effort heuristic graph update with whatever is on disk.
        "stop": [
            {"command": str(hooks_dir / "cursor_stop.py"), "timeout": 30},
        ],
        # Authoritative: at session end the transcript is fully flushed, so this
        # is where we gate + enqueue for the watchdog's LLM pass.
        "sessionEnd": [
            {"command": str(hooks_dir / "cursor_session_end.py"), "timeout": 30},
        ],
    }


def merge_hooks(
    existing: dict[str, Any],
    new_hooks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Fold ``new_hooks`` into ``existing`` Cursor config. Pure, deep-copied."""
    result = copy.deepcopy(existing) if existing else {}
    result.setdefault("version", SCHEMA_VERSION)
    section = result.setdefault("hooks", {})
    if not isinstance(section, dict):
        raise TypeError("hooks must be an object")

    for event, entries in new_hooks.items():
        blocks = section.setdefault(event, [])
        if not isinstance(blocks, list):
            raise TypeError(f"hooks[{event!r}] must be a list")
        existing_cmds = {
            b.get("command")
            for b in blocks
            if isinstance(b, dict) and "command" in b
        }
        for entry in entries:
            if entry.get("command") in existing_cmds:
                continue
            blocks.append(copy.deepcopy(entry))
            existing_cmds.add(entry.get("command"))
    return result


def merge_hooks_into_file(
    hooks_path: str | os.PathLike[str],
    hooks_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Read ~/.cursor/hooks.json, merge Agam hooks, write back atomically."""
    hooks_path = Path(hooks_path)
    hooks_dir = Path(hooks_dir)

    if hooks_path.exists():
        raw = hooks_path.read_text(encoding="utf-8").strip()
        existing = json.loads(raw) if raw else {}
        if not isinstance(existing, dict):
            raise TypeError(f"{hooks_path} root must be a JSON object")
    else:
        existing = {}

    merged = merge_hooks(existing, agam_hook_entries(hooks_dir))

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".hooks-", suffix=".json.tmp", dir=str(hooks_path.parent)
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

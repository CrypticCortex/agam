#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Cursor ``stop`` hook: refresh the per-workspace recall rule.

Cursor cannot inject context through hooks (``beforeSubmitPrompt`` is block-only,
``sessionStart``/``postToolUse`` injection is bug-dropped). The one reliable
model-facing channel is a rule file with ``alwaysApply: true``. So on every turn
end we regenerate ``<workspace>/.cursor/rules/agam.mdc`` from the shared graph,
keeping the model's always-on context fresh.

This hook does NOT update the graph: Cursor flushes transcripts lazily (at
session end), so per-turn transcript reads are unreliable. Graph learning happens
at ``sessionEnd`` (cursor_session_end.py) -> watchdog. This hook is read-side
only and always exits 0 (never blocks completion).

Input (stdin JSON): ``{status, loop_count}`` + common schema
``{conversation_id, transcript_path, workspace_roots, ...}``.

Environment:
    AGAM_DATA_HOME   Shared data root (default ~/.agam).
    AGAM_TOOLS_DIR   Dir holding agam_context.py + cursor_rule.py.
    CURSOR_PROJECT_DIR  Workspace root (fallback for workspace_roots).
"""

import json
import os
import pathlib
import subprocess
import sys

_HOOK_DIR = pathlib.Path(__file__).resolve().parent


def _data_home() -> pathlib.Path:
    env = os.environ.get("AGAM_DATA_HOME")
    return pathlib.Path(env) if env else pathlib.Path(os.path.expanduser("~/.agam"))


def _tools_dir() -> pathlib.Path:
    env = os.environ.get("AGAM_TOOLS_DIR")
    if env:
        return pathlib.Path(env)
    # Probe installed + source layouts (mirror session_close's vendoring).
    for cand in (_HOOK_DIR.parent / "tools" / "agam", _HOOK_DIR.parent / "tools"):
        if (cand / "agam_context.py").exists():
            return cand
    return _HOOK_DIR.parent / "tools" / "agam"


def _vendor(tools_dir: pathlib.Path) -> None:
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))


def _workspace_root(data: dict) -> str:
    roots = data.get("workspace_roots") or []
    if isinstance(roots, list) and roots:
        return roots[0]
    return os.environ.get("CURSOR_PROJECT_DIR", "")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    workspace = _workspace_root(data)
    if not workspace or not os.path.isdir(workspace):
        # Nothing to write a project rule into; harmless no-op.
        print("{}")
        return 0

    data_home = _data_home()
    tools_dir = _tools_dir()
    context_tool = tools_dir / "agam_context.py"
    if not context_tool.exists():
        print("{}")
        return 0

    # Render the digest from the shared graph.
    env = dict(os.environ)
    env.setdefault("AGAM_HOME", str(data_home))
    env.setdefault("AGAM_KG_PATH", str(data_home / "knowledge" / "graph.db"))
    # Prefer `uv run --script` so agam_context.py's own PEP 723 dep block is
    # honored (sys.executable here is this hook's ephemeral uv venv, which would
    # not resolve a future dependency of the target script). Fall back to the
    # current interpreter when uv isn't on PATH.
    import shutil
    uv = shutil.which("uv")
    cmd = (
        [uv, "run", "--script", str(context_tool), "render-rule"]
        if uv else [sys.executable, str(context_tool), "render-rule"]
    )
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        print("{}")
        return 0
    content = r.stdout if r.returncode == 0 else ""
    if not content.strip():
        print("{}")
        return 0

    # Write the rule file (vendored helper, single source of truth).
    _vendor(tools_dir)
    try:
        import cursor_rule  # type: ignore[import-not-found]
        cursor_rule.write_rule(workspace, content)
    except Exception:
        pass  # never block completion on a write failure

    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

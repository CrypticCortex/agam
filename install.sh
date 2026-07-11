#!/bin/bash
# Agam installer. Runs on macOS host.
set -u

# --- begin prereq checks ---
command -v uv >/dev/null || { echo "ERR: install uv first -- https://docs.astral.sh/uv/"; exit 1; }
command -v claude >/dev/null || command -v cursor-agent >/dev/null || command -v codex >/dev/null || echo "WARN: no supported agent CLI found on the host (claude, cursor-agent, or codex). That's fine if Claude Code runs in a devcontainer; otherwise install and authenticate at least one supported CLI before background enrichment."
command -v docker >/dev/null || echo "WARN: docker not found. This is only required for container-based Claude enrichment; host Claude, Cursor, and Codex modes do not require it."
[[ "$(uname)" == "Darwin" ]] || { echo "ERR: macOS only for v1."; exit 1; }
# Auth is NOT checked here. install.sh writes files; actual agent calls happen
# later (bootstrap/watchdog) and surface the selected CLI's own auth errors.
# Use `agam doctor` after install to verify.
# --- end prereq checks ---

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"
uv sync
uv run agam init "$@"

#!/bin/bash
# Agam installer. Runs on macOS host.
set -u

# --- begin prereq checks ---
command -v uv >/dev/null || { echo "ERR: install uv first -- https://docs.astral.sh/uv/"; exit 1; }
command -v claude >/dev/null || echo "WARN: claude CLI not on host PATH. That's fine if you run Claude Code inside a devcontainer -- the watchdog will docker-exec into it. To set the container name override later: export AGAM_CONTAINER_NAME=<your-container>. To install claude on the host too: https://claude.ai/code"
command -v docker >/dev/null || echo "WARN: docker not found. Watchdog + bootstrap require a running claude-code container. Install Docker Desktop to enable these."
[[ "$(uname)" == "Darwin" ]] || { echo "ERR: macOS only for v1."; exit 1; }
# Auth is NOT checked here. install.sh writes files; the actual claude -p
# calls happen later (agam bootstrap, watchdog) and surface real auth errors
# with claude's own message. Use `agam doctor` after install to verify.
# --- end prereq checks ---

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"
uv sync
uv run agam init "$@"

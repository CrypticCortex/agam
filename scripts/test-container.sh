#!/bin/bash
# Usage: test-container.sh {up|down|exec <cmd>|reset}
# Creates/tears down agam-oss-test isolated from user's running container.
#
# Environment overrides (defaults work for the common case):
#   AGAM_TEST_IMAGE       Container image to run (default: claude-code base image)
#   AGAM_TEST_REPO_HOST   Host path to this repo (default: derived from script location)
#   AGAM_TEST_CREDS_HOST  Host path to Claude Code OAuth credentials (default: ~/.claude/.credentials.json)

set -u
CONTAINER="agam-oss-test"
IMAGE="${AGAM_TEST_IMAGE:-python:3.11-slim}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_HOST="${AGAM_TEST_REPO_HOST:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPO_CONTAINER="/workspace/agam"
CREDS_HOST="${AGAM_TEST_CREDS_HOST:-$HOME/.claude/.credentials.json}"
CREDS_CONTAINER="/home/node/.claude/.credentials.json"

up() {
    # Refuse if name collision (not our container)
    EXISTING=$(docker ps -a --format '{{.Names}}' | grep -x "$CONTAINER" || true)
    [[ -n "$EXISTING" ]] && docker rm -f "$CONTAINER" >/dev/null

    # Bind-mount credentials only if the host file actually exists. On macOS
    # host installs, Claude Code stores OAuth in Keychain and the file may
    # never be created -- mounting a non-existent path would have Docker
    # silently auto-create a DIRECTORY at that location, corrupting the host
    # path. Skip the mount when missing; the test container falls back to
    # whatever auth its image ships with (which, for python:3.11-slim, is
    # none -- LIVE bootstrap will fail loudly, non-LIVE tests pass).
    MOUNTS=(-v "$REPO_HOST:$REPO_CONTAINER")
    if [[ -f "$CREDS_HOST" ]]; then
        MOUNTS+=(-v "$CREDS_HOST:$CREDS_CONTAINER:ro")
    else
        echo "[test-container] WARN: $CREDS_HOST not found; container starts without OAuth mount."
        echo "[test-container]       LIVE bootstrap inside this container will fail; non-LIVE tests are unaffected."
    fi

    docker run -d --name "$CONTAINER" "${MOUNTS[@]}" "$IMAGE" sleep infinity
    # Sanity: confirm user's container still running
    docker ps --format '{{.Names}}' | grep -qv '^$' || { echo "FATAL: docker ps empty after up"; exit 1; }
}

down() { docker rm -f "$CONTAINER" 2>/dev/null || true; }

exec_() { docker exec -i "$CONTAINER" "$@"; }

reset() { down; up; }

case "${1:-}" in
    up) up ;;
    down) down ;;
    exec) shift; exec_ "$@" ;;
    reset) reset ;;
    *) echo "Usage: $0 {up|down|exec <cmd>|reset}"; exit 1 ;;
esac

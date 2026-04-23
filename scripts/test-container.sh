#!/bin/bash
# Usage: test-container.sh {up|down|exec <cmd>|reset}
# Creates/tears down agam-oss-test isolated from user's running container.

set -u
CONTAINER="agam-oss-test"
IMAGE="artifactory.example.com/claude-code/claude-code:latest"
REPO_HOST="/Users/test/coding/agam"
REPO_CONTAINER="/workspace/agam"
CREDS_HOST="/Users/test/.claude/.credentials.json"
CREDS_CONTAINER="/home/node/.claude/.credentials.json"

up() {
    # Refuse if name collision (not our container)
    EXISTING=$(docker ps -a --format '{{.Names}}' | grep -x "$CONTAINER" || true)
    [[ -n "$EXISTING" ]] && docker rm -f "$CONTAINER" >/dev/null
    docker run -d --name "$CONTAINER" \
        -v "$REPO_HOST:$REPO_CONTAINER" \
        -v "$CREDS_HOST:$CREDS_CONTAINER:ro" \
        "$IMAGE" sleep infinity
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

#!/bin/bash
# Host-side launchd-fired job. Discovers user's claude-code container,
# docker-exec's the inner watchdog script inside it. Falls back to host-mode
# only when AGAM_WATCHDOG_MODE=host is set.
#
# Queue preservation contract: if we're in container mode and no matching
# container is running, the queue is LEFT INTACT for the next tick. There is
# no backoff, no retry, and no failure state -- just "not now, try again in
# a minute."
#
# Env:
#   AGAM_HOME                  default $HOME/.claude/agam
#   AGAM_WATCHDOG_MODE         container (default) | host
#   AGAM_CONTAINER_PATTERN     default claude-code|claude-code
#   AGAM_CONTAINER_NAME        optional exact-name override
set -u

AGAM_HOME="${AGAM_HOME:-$HOME/.claude/agam}"
MODE="${AGAM_WATCHDOG_MODE:-container}"
CONTAINER_PATTERN="${AGAM_CONTAINER_PATTERN:-claude-code|claude-code}"
CONTAINER_NAME_OVERRIDE="${AGAM_CONTAINER_NAME:-}"
LOG="$AGAM_HOME/logs/watchdog.log"
LOCK="$AGAM_HOME/.watchdog.lock"

mkdir -p "$AGAM_HOME/logs" "$AGAM_HOME/queue" "$AGAM_HOME/processed" "$AGAM_HOME/queue-errors"

log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

# Single-flight: refuse if a prior tick is still draining.
if [[ -f "$LOCK" ]] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    log "already-running pid=$(cat "$LOCK" 2>/dev/null)"
    exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

discover_container() {
    if [[ -n "$CONTAINER_NAME_OVERRIDE" ]]; then
        docker ps --format '{{.Names}}' | grep -x "$CONTAINER_NAME_OVERRIDE" | head -1
    else
        docker ps --format '{{.Names}} {{.Image}}' | grep -iE "$CONTAINER_PATTERN" | head -1 | awk '{print $1}'
    fi
}

run_inner_container() {
    local entry_file="$1"
    local container="$2"
    # Inner script path is stable inside the container because ~/.claude is
    # bind-mounted to /home/node/.claude.
    docker exec -i "$container" env AGAM_HOME=/home/node/.claude/agam \
        /home/node/.claude/hooks/agam_watchdog_inner.py < "$entry_file"
}

run_inner_host() {
    local entry_file="$1"
    env AGAM_HOME="$AGAM_HOME" "$HOME/.claude/hooks/agam_watchdog_inner.py" < "$entry_file"
}

# Main drain loop.
shopt -s nullglob
entries=("$AGAM_HOME"/queue/*.json)
if [[ ${#entries[@]} -eq 0 ]]; then
    log "queue-empty"
    exit 0
fi

container=""
if [[ "$MODE" == "container" ]]; then
    container=$(discover_container)
    if [[ -z "$container" ]]; then
        log "no-container pattern=$CONTAINER_PATTERN queue-depth=${#entries[@]}"
        exit 0
    fi
    log "drain-start container=$container queue-depth=${#entries[@]}"
else
    log "drain-start mode=host queue-depth=${#entries[@]}"
fi

for entry in "${entries[@]}"; do
    name=$(basename "$entry")
    rc=0
    if [[ "$MODE" == "container" ]]; then
        run_inner_container "$entry" "$container"
        rc=$?
    else
        run_inner_host "$entry"
        rc=$?
    fi
    if [[ $rc -eq 0 ]]; then
        mv "$entry" "$AGAM_HOME/processed/$name"
        log "ok $name"
    else
        mv "$entry" "$AGAM_HOME/queue-errors/$name"
        log "err $name rc=$rc"
    fi
done

log "drain-done"

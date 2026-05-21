#!/bin/bash
# Host-side launchd-fired job. Drains the watchdog queue using a cascade
# that mirrors `agam.invoker.resolve_invoker()`:
#
#   1. AGAM_INVOKER=host or AGAM_WATCHDOG_MODE=host  -> force host invoker.
#   2. AGAM_INVOKER=container or AGAM_WATCHDOG_MODE=container -> force container.
#   3. AGAM_CONTAINER_NAME exact name running -> named container.
#   4. Image matching AGAM_CONTAINER_PATTERN running -> discovered container.
#   5. `claude` on PATH and ~/.claude/.credentials.json present -> host.
#   6. None of the above -> log no-invoker and exit (queue preserved).
#
# The Python module ``agam.invoker`` is the source of truth for this cascade
# (tested in tests/test_invoker.py). This script reimplements the same
# decision logic in shell because launchd runs the watchdog without
# guaranteed access to the agam Python environment. Keep the two in sync.
#
# Env:
#   AGAM_HOME              default $HOME/.claude/agam
#   AGAM_INVOKER           host | container (overrides cascade)
#   AGAM_WATCHDOG_MODE     host | container (legacy alias)
#   AGAM_CONTAINER_PATTERN regex (default claude-code)
#   AGAM_CONTAINER_NAME    exact name override
set -u

AGAM_HOME="${AGAM_HOME:-$HOME/.claude/agam}"
LOG="$AGAM_HOME/logs/watchdog.log"
LOCK="$AGAM_HOME/.watchdog.lock"
CREDS="$HOME/.claude/.credentials.json"

mkdir -p "$AGAM_HOME/logs" "$AGAM_HOME/queue" "$AGAM_HOME/processed" "$AGAM_HOME/queue-errors"

log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

# Single-flight lock.
if [[ -f "$LOCK" ]] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    log "already-running pid=$(cat "$LOCK" 2>/dev/null)"
    exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# --- Resolve invoker (shell cascade mirror of agam.invoker.resolve_invoker)
PINNED=""
EXPLICIT="${AGAM_INVOKER:-}"
LEGACY="${AGAM_WATCHDOG_MODE:-}"
if [[ -n "$EXPLICIT" ]]; then PINNED="$EXPLICIT"; elif [[ -n "$LEGACY" ]]; then PINNED="$LEGACY"; fi

CONTAINER_PATTERN="${AGAM_CONTAINER_PATTERN:-claude-code}"
CONTAINER_NAME_OVERRIDE="${AGAM_CONTAINER_NAME:-}"

probe_host() {
    # 0 = healthy, 1 = unhealthy. Writes detail to global PROBE_DETAIL.
    if ! command -v claude >/dev/null 2>&1; then
        PROBE_DETAIL="claude CLI not on PATH"
        return 1
    fi
    if [[ ! -f "$CREDS" ]]; then
        PROBE_DETAIL="no ~/.claude/.credentials.json (run \`claude\` once to authenticate)"
        return 1
    fi
    PROBE_DETAIL="host claude ready"
    return 0
}

probe_named_container() {
    local name="$1"
    if ! command -v docker >/dev/null 2>&1; then
        PROBE_DETAIL="docker not on PATH"
        return 1
    fi
    local state
    state=$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)
    if [[ "$state" != "true" ]]; then
        PROBE_DETAIL="container '$name' not running"
        return 1
    fi
    PROBE_DETAIL="named container '$name' running"
    DISCOVERED="$name"
    return 0
}

probe_pattern_container() {
    local pattern="$1"
    if ! command -v docker >/dev/null 2>&1; then
        PROBE_DETAIL="docker not on PATH"
        return 1
    fi
    local match
    match=$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -iE "$pattern" | head -1 | awk '{print $1}')
    if [[ -z "$match" ]]; then
        PROBE_DETAIL="no running container matches /$pattern/"
        return 1
    fi
    PROBE_DETAIL="container '$match' matches /$pattern/"
    DISCOVERED="$match"
    return 0
}

INVOKER_KIND=""
INVOKER_DETAIL=""
DISCOVERED=""
declare -a FAILURES=()

try_container_pin() {
    if [[ -n "$CONTAINER_NAME_OVERRIDE" ]] && probe_named_container "$CONTAINER_NAME_OVERRIDE"; then
        INVOKER_KIND="named-container"; INVOKER_DETAIL="$DISCOVERED"; return 0
    fi
    if probe_pattern_container "$CONTAINER_PATTERN"; then
        INVOKER_KIND="container"; INVOKER_DETAIL="$DISCOVERED"; return 0
    fi
    return 1
}

try_host_pin() {
    if probe_host; then
        INVOKER_KIND="host"; INVOKER_DETAIL="$PROBE_DETAIL"; return 0
    fi
    return 1
}

if [[ "$PINNED" == "host" ]]; then
    if ! try_host_pin; then FAILURES+=("host: $PROBE_DETAIL"); fi
elif [[ "$PINNED" == "container" ]]; then
    if ! try_container_pin; then FAILURES+=("container: $PROBE_DETAIL"); fi
else
    # Default cascade: named container -> discovered container -> host.
    if [[ -n "$CONTAINER_NAME_OVERRIDE" ]]; then
        if probe_named_container "$CONTAINER_NAME_OVERRIDE"; then
            INVOKER_KIND="named-container"; INVOKER_DETAIL="$DISCOVERED"
        else
            FAILURES+=("named-container: $PROBE_DETAIL")
        fi
    fi
    if [[ -z "$INVOKER_KIND" ]] && probe_pattern_container "$CONTAINER_PATTERN"; then
        INVOKER_KIND="container"; INVOKER_DETAIL="$DISCOVERED"
    elif [[ -z "$INVOKER_KIND" ]]; then
        FAILURES+=("container: $PROBE_DETAIL")
    fi
    if [[ -z "$INVOKER_KIND" ]] && probe_host; then
        INVOKER_KIND="host"; INVOKER_DETAIL="$PROBE_DETAIL"
    elif [[ -z "$INVOKER_KIND" ]]; then
        FAILURES+=("host: $PROBE_DETAIL")
    fi
fi

shopt -s nullglob
entries=("$AGAM_HOME"/queue/*.json)

if [[ -z "$INVOKER_KIND" ]]; then
    if [[ ${#entries[@]} -gt 0 ]]; then
        log "no-invoker queue-depth=${#entries[@]} failures=$(IFS='|'; echo "${FAILURES[*]}")"
    fi
    exit 0
fi

if [[ ${#entries[@]} -eq 0 ]]; then
    log "queue-empty invoker=$INVOKER_KIND"
    exit 0
fi

case "$INVOKER_KIND" in
    host)
        log "drain-start invoker=host queue-depth=${#entries[@]}"
        ;;
    container|named-container)
        log "drain-start invoker=$INVOKER_KIND container=$INVOKER_DETAIL queue-depth=${#entries[@]}"
        ;;
esac

run_entry() {
    local entry_file="$1"
    case "$INVOKER_KIND" in
        host)
            # Inner script needs AGAM_TOOLS_DIR + AGAM_HOOKS_DIR or it falls
            # back to defaults that don't match where the installer actually
            # writes them (installer puts tools at ~/.claude/tools/agam/,
            # NOT ~/.claude/tools/). Without these the inner script raises
            # FileNotFoundError calling apply_proposals.py and every queued
            # session ends up in queue-errors/.
            env AGAM_HOME="$AGAM_HOME" \
                AGAM_TOOLS_DIR="${AGAM_TOOLS_DIR:-$HOME/.claude/tools/agam}" \
                AGAM_HOOKS_DIR="${AGAM_HOOKS_DIR:-$HOME/.claude/hooks}" \
                AGAM_PROMPTS_DIR="${AGAM_PROMPTS_DIR:-$AGAM_HOME/prompts}" \
                AGAM_KG_PATH="${AGAM_KG_PATH:-$HOME/.claude/knowledge/graph.db}" \
                AGAM_USER_ENTITY="${AGAM_USER_ENTITY:-User}" \
                "$HOME/.claude/hooks/agam_watchdog_inner.py" < "$entry_file"
            ;;
        container|named-container)
            # Inside the container, ~/.claude is bind-mounted at
            # /home/node/.claude/. Inner-script-side env vars must use the
            # container-internal paths.
            docker exec -i "$INVOKER_DETAIL" env \
                AGAM_HOME=/home/node/.claude/agam \
                AGAM_TOOLS_DIR=/home/node/.claude/tools/agam \
                AGAM_HOOKS_DIR=/home/node/.claude/hooks \
                AGAM_PROMPTS_DIR=/home/node/.claude/agam/prompts \
                AGAM_KG_PATH=/home/node/.claude/knowledge/graph.db \
                AGAM_USER_ENTITY="${AGAM_USER_ENTITY:-User}" \
                /home/node/.claude/hooks/agam_watchdog_inner.py < "$entry_file"
            ;;
    esac
}

for entry in "${entries[@]}"; do
    name=$(basename "$entry")
    run_entry "$entry"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        mv "$entry" "$AGAM_HOME/processed/$name"
        log "ok $name"
    else
        mv "$entry" "$AGAM_HOME/queue-errors/$name"
        log "err $name rc=$rc"
    fi
done

log "drain-done"

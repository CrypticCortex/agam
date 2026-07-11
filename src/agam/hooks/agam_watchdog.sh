#!/bin/bash
# Host-side launchd-fired job. Drains the watchdog queue using a cascade
# that mirrors `agam.invoker.resolve_invoker()`:
#
#   1. AGAM_INVOKER=host or AGAM_WATCHDOG_MODE=host  -> force host invoker.
#   2. AGAM_INVOKER=container or AGAM_WATCHDOG_MODE=container -> force container.
#   3. AGAM_CONTAINER_NAME exact name running -> named container.
#   4. Image matching AGAM_CONTAINER_PATTERN running -> discovered container.
#   5. An enrichment CLI on PATH -> host. Preference is claude, then
#      cursor-agent, then codex. (Each CLI owns its own authentication.)
#   6. None of the above -> log no-invoker and exit (queue preserved).
#
# The container/host resolution mirrors ``agam.invoker``. This launchd script
# also selects among supported host CLIs because it runs without guaranteed
# access to the agam Python environment.
#
# Env:
#   AGAM_HOME              default $HOME/.agam
#   AGAM_INVOKER           host | container (overrides cascade)
#   AGAM_WATCHDOG_MODE     host | container (legacy alias)
#   AGAM_CONTAINER_PATTERN regex (default claude-code)
#   AGAM_CONTAINER_NAME    exact name override
#   AGAM_LLM_CLI_PIN       claude | cursor-agent | codex (host override)
#   AGAM_LLM_CLI_PATH      absolute host CLI executable path (launchd-safe)
#   AGAM_MAX_PER_RUN       max entries drained per tick (default 25). Bounds
#                          one pass so a backlog after downtime can't fire an
#                          unbounded number of enrichment runs at once.
#   AGAM_MAX_RETRIES       attempts before an entry is dead-lettered to
#                          queue-errors/ (default 3). A transient failure
#                          (e.g. container mid-restart) is retried next tick
#                          instead of being exiled permanently.
set -u

AGAM_HOME="${AGAM_HOME:-${AGAM_DATA_HOME:-$HOME/.agam}}"
LOG="$AGAM_HOME/logs/watchdog.log"
LOCK="$AGAM_HOME/.watchdog.lock"
PROCESSING_DIR="$AGAM_HOME/processing"
RETRY_DIR="$AGAM_HOME/.retries"
mkdir -p \
    "$AGAM_HOME/logs" "$AGAM_HOME/queue" "$AGAM_HOME/processed" \
    "$AGAM_HOME/queue-errors" "$PROCESSING_DIR" "$RETRY_DIR"

log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

# Single-flight lock. A live lock holder defers us; a stale lock (dead pid) is
# reclaimed. The claim itself uses noclobber (set -C) so that two ticks racing
# through the check->write window can't both succeed -- the loser's create fails
# and it defers instead of clobbering the winner's pid.
if [[ -f "$LOCK" ]]; then
    if kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
        log "already-running pid=$(cat "$LOCK" 2>/dev/null)"
        exit 0
    fi
    rm -f "$LOCK"  # stale: holder is dead
fi
if ! ( set -C; echo $$ > "$LOCK" ) 2>/dev/null; then
    log "already-running pid=$(cat "$LOCK" 2>/dev/null)"
    exit 0
fi
trap 'rm -f "$LOCK"' EXIT

# A claimed generation may survive an abrupt watchdog termination. Requeue it
# under a generation-specific name before taking a new snapshot. The normal
# enqueue path only replaces <session>.json, so this recovered generation
# cannot overwrite (or be overwritten by) a newer session generation.
shopt -s nullglob
for claim_dir in "$PROCESSING_DIR"/claim.*; do
    [[ -d "$claim_dir" ]] || continue
    for claim in "$claim_dir"/*.json; do
        name=$(basename "$claim")
        stem="${name%.json}"
        generation=$(basename "$claim_dir")
        recovered_name="${stem}.retry-recovered-${generation}.json"
        if [[ -e "$AGAM_HOME/queue/$recovered_name" ]]; then
            recovered_name="${stem}.retry-recovered-${generation}-$$-${RANDOM}.json"
        fi
        if mv "$claim" "$AGAM_HOME/queue/$recovered_name"; then
            if [[ -f "$RETRY_DIR/$name" ]]; then
                mv "$RETRY_DIR/$name" "$RETRY_DIR/$recovered_name"
            fi
            log "recovered-claim $name as $recovered_name"
        fi
    done
    rmdir "$claim_dir" 2>/dev/null || true
done

# --- Resolve invoker (shell cascade mirror of agam.invoker.resolve_invoker)
PINNED=""
EXPLICIT="${AGAM_INVOKER:-}"
LEGACY="${AGAM_WATCHDOG_MODE:-}"
if [[ -n "$EXPLICIT" ]]; then PINNED="$EXPLICIT"; elif [[ -n "$LEGACY" ]]; then PINNED="$LEGACY"; fi

CONTAINER_PATTERN="${AGAM_CONTAINER_PATTERN:-claude-code}"
CONTAINER_NAME_OVERRIDE="${AGAM_CONTAINER_NAME:-}"

probe_host() {
    # 0 = healthy, 1 = unhealthy. Writes detail to global PROBE_DETAIL and the
    # chosen LLM CLI to AGAM_LLM_CLI. Prefer claude; fall back to cursor-agent,
    # then codex, so a host with only one supported agent can still enrich the
    # graph without changing the historical Claude/Cursor preference.
    # Auth lives in each tool's own store; real auth failures surface at run.
    #
    # AGAM_LLM_CLI_PATH is checked first. The neutral installer records an
    # absolute path because launchd's fixed PATH may not include npm/nvm/asdf
    # shims. A stale path falls through to the normal probe cascade.
    if [[ -n "${AGAM_LLM_CLI_PATH:-}" ]]; then
        local cli_path="$AGAM_LLM_CLI_PATH"
        local cli_kind="${AGAM_LLM_CLI_PIN:-$(basename "$cli_path")}"
        if [[ "$cli_path" == /* && -f "$cli_path" && -x "$cli_path" ]]; then
            case "$cli_kind" in
                claude|cursor-agent|codex)
                    AGAM_LLM_CLI="$cli_kind"
                    PROBE_DETAIL="host $cli_kind at $cli_path"
                    return 0
                    ;;
            esac
        fi
        # Never forward a stale, relative, or unrecognized override to the
        # inner runner if a PATH fallback succeeds below.
        AGAM_LLM_CLI_PATH=""
    fi

    # AGAM_LLM_CLI_PIN overrides the preference (e.g. pin codex even when
    # claude is also installed).
    if [[ -n "${AGAM_LLM_CLI_PIN:-}" ]]; then
        local resolved
        resolved=$(command -v "$AGAM_LLM_CLI_PIN" 2>/dev/null || true)
        if [[ -n "$resolved" ]]; then
            AGAM_LLM_CLI="$AGAM_LLM_CLI_PIN"
            [[ "$resolved" == /* ]] && AGAM_LLM_CLI_PATH="$resolved"
            PROBE_DETAIL="pinned $AGAM_LLM_CLI_PIN on PATH"
            return 0
        fi
        PROBE_DETAIL="pinned $AGAM_LLM_CLI_PIN not on PATH"
        return 1
    fi
    if command -v claude >/dev/null 2>&1; then
        AGAM_LLM_CLI="claude"
        AGAM_LLM_CLI_PATH=$(command -v claude)
        PROBE_DETAIL="host claude on PATH"
        return 0
    fi
    if command -v cursor-agent >/dev/null 2>&1; then
        AGAM_LLM_CLI="cursor-agent"
        AGAM_LLM_CLI_PATH=$(command -v cursor-agent)
        PROBE_DETAIL="host cursor-agent on PATH (claude absent)"
        return 0
    fi
    if command -v codex >/dev/null 2>&1; then
        AGAM_LLM_CLI="codex"
        AGAM_LLM_CLI_PATH=$(command -v codex)
        PROBE_DETAIL="host codex on PATH (claude and cursor-agent absent)"
        return 0
    fi
    PROBE_DETAIL="none of claude, cursor-agent, or codex on PATH"
    return 1
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
AGAM_LLM_CLI="${AGAM_LLM_CLI:-claude}"
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

# Snapshot the knowledge graph before mutating it. Cursor host-mode enrichment
# writes the shared graph; if a Claude container ever writes concurrently over a
# bind mount the WAL guarantee can break, so a pre-drain backup makes any damage
# recoverable. Keep the newest 5. Uses sqlite3 .backup (WAL-consistent) when
# available, else a plain copy.
if [[ ${#entries[@]} -gt 0 && -n "${AGAM_KG_PATH:-}" && -f "${AGAM_KG_PATH:-}" ]]; then
    BK_DIR="$AGAM_HOME/backups"
    mkdir -p "$BK_DIR"
    BK="$BK_DIR/graph-predrain-$(date -u +%Y%m%dT%H%M%SZ).db"
    if command -v sqlite3 >/dev/null 2>&1; then
        # Escape single quotes for the SQL string literal (paths with a quote
        # would otherwise break the .backup statement and silently fall to cp).
        BK_SQL=${BK//\'/\'\'}
        sqlite3 "$AGAM_KG_PATH" ".backup '$BK_SQL'" 2>/dev/null || cp "$AGAM_KG_PATH" "$BK" 2>/dev/null
    else
        cp "$AGAM_KG_PATH" "$BK" 2>/dev/null
    fi
    ls -1t "$BK_DIR"/graph-predrain-*.db 2>/dev/null | tail -n +6 | while IFS= read -r old; do rm -f "$old"; done
    log "pre-drain-backup $BK"
fi

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
            # Resolve dirs from env (the launchd plist sets these to the shared
            # ~/.agam location). Fall back to the legacy ~/.claude layout only
            # when nothing is set, so old installs keep working pre-upgrade.
            HOOKS_DIR_RESOLVED="${AGAM_HOOKS_DIR:-$HOME/.claude/hooks}"
            env AGAM_HOME="$AGAM_HOME" \
                AGAM_TOOLS_DIR="${AGAM_TOOLS_DIR:-$HOME/.claude/tools/agam}" \
                AGAM_HOOKS_DIR="$HOOKS_DIR_RESOLVED" \
                AGAM_PROMPTS_DIR="${AGAM_PROMPTS_DIR:-$AGAM_HOME/prompts}" \
                AGAM_KG_PATH="${AGAM_KG_PATH:-$HOME/.claude/knowledge/graph.db}" \
                AGAM_USER_ENTITY="${AGAM_USER_ENTITY:-User}" \
                AGAM_LLM_CLI="$AGAM_LLM_CLI" \
                AGAM_LLM_CLI_PATH="${AGAM_LLM_CLI_PATH:-}" \
                "$HOOKS_DIR_RESOLVED/agam_watchdog_inner.py" < "$entry_file"
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

# Drain oldest-first (FIFO by enqueue mtime), bounded by AGAM_MAX_PER_RUN.
# `ls -1tr` sorts by mtime ascending; queue filenames are <session_id>.json
# (no spaces/newlines), so reading them line-by-line is safe. Pairing
# oldest-first with the per-run cap means a backlog drains in order over
# successive ticks instead of starving old entries or blowing up one tick.
MAX_PER_RUN="${AGAM_MAX_PER_RUN:-25}"
MAX_RETRIES="${AGAM_MAX_RETRIES:-3}"

drained=0
ok_n=0
err_n=0
retry_n=0
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    if [[ $drained -ge $MAX_PER_RUN ]]; then
        # Live count of what's still queued (not the pre-drain snapshot, which
        # would mislead once entries have moved to processed/queue-errors).
        remaining=$(ls -1 "$AGAM_HOME"/queue/*.json 2>/dev/null | wc -l | tr -d ' ')
        log "per-run-cap cap=$MAX_PER_RUN drained=$drained remaining=$remaining"
        break
    fi
    name=$(basename "$entry")
    # Atomically detach this exact generation from queue/. A concurrent
    # enqueue_file() can now recreate queue/<session>.json without our success
    # path ever moving that newer generation away.
    claim_dir=$(mktemp -d "$PROCESSING_DIR/claim.XXXXXX") || {
        log "claim-dir-failed $name"
        continue
    }
    claim="$claim_dir/$name"
    if ! mv "$entry" "$claim" 2>/dev/null; then
        rmdir "$claim_dir" 2>/dev/null || true
        continue
    fi
    generation=$(basename "$claim_dir")

    run_entry "$claim"
    rc=$?
    drained=$((drained + 1))
    if [[ $rc -eq 0 ]]; then
        archive="$AGAM_HOME/processed/$name"
        if [[ -e "$archive" ]]; then
            archive="$AGAM_HOME/processed/${name%.json}.${generation}.json"
        fi
        if mv "$claim" "$archive"; then
            rm -f "$RETRY_DIR/$name"
            ok_n=$((ok_n + 1))
            log "ok $name archived=$(basename "$archive")"
        else
            retry_n=$((retry_n + 1))
            log "archive-failed $name rc=$rc claim=$claim"
        fi
    else
        # Bounded retry: every failed claimed generation gets its own queue
        # name. Never move it back to queue/<session>.json, which may now hold a
        # newer atomically-enqueued generation.
        rfile="$RETRY_DIR/$name"
        n=$(( $(cat "$rfile" 2>/dev/null || echo 0) + 1 ))
        if [[ $n -ge $MAX_RETRIES ]]; then
            deadletter="$AGAM_HOME/queue-errors/$name"
            if [[ -e "$deadletter" ]]; then
                deadletter="$AGAM_HOME/queue-errors/${name%.json}.${generation}.json"
            fi
            if mv "$claim" "$deadletter"; then
                rm -f "$rfile"
                err_n=$((err_n + 1))
                log "err $name rc=$rc attempts=$n dead-letter=$(basename "$deadletter")"
            else
                retry_n=$((retry_n + 1))
                log "dead-letter-failed $name rc=$rc claim=$claim"
            fi
        else
            retry_name="${name%.json}.retry-${generation}.json"
            if mv "$claim" "$AGAM_HOME/queue/$retry_name"; then
                echo "$n" > "$RETRY_DIR/$retry_name"
                rm -f "$rfile"
                retry_n=$((retry_n + 1))
                log "retry $name rc=$rc attempt=$n/$MAX_RETRIES as=$retry_name"
            else
                retry_n=$((retry_n + 1))
                log "requeue-failed $name rc=$rc claim=$claim"
            fi
        fi
    fi
    rmdir "$claim_dir" 2>/dev/null || true
done < <(ls -1tr "$AGAM_HOME"/queue/*.json 2>/dev/null)

log "drain-done ok=$ok_n err=$err_n retry=$retry_n"

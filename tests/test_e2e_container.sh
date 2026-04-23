#!/bin/bash
# E2E validation for Agam v0 release.
#
# Spins up the isolated `agam-oss-test` devcontainer, runs `agam init`
# non-interactively, seeds synthetic transcripts, and verifies scaffolding.
#
# The LIVE bootstrap step (which makes real `claude -p` API calls and thus
# costs real money) is SKIPPED by default. Set AGAM_E2E_LIVE=1 to opt in:
#
#     AGAM_E2E_LIVE=1 ./tests/test_e2e_container.sh
#
# Host invariance: the script hashes the host's ~/.claude/agam/AGAM.md
# before and after and fails if it changed. All writes inside the
# container target /home/node/.claude-test/, which does not touch the
# host's ~/.claude/ (only the read-only credentials file is bind-mounted).
#
# If the Artifactory image can't be pulled (e.g. token expired), the
# script exits with a clear "setup" error. The script itself is the
# Task 24 deliverable; real execution is optional.

set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

LIVE="${AGAM_E2E_LIVE:-0}"
TEST_HOME="/home/node/.claude-test"
# The installer writes scaffolding into $HOME/.claude/agam/, so with
# HOME=$TEST_HOME the agam dir lands here:
TEST_AGAM_DIR="$TEST_HOME/.claude/agam"
TEST_KG_PATH="$TEST_HOME/.claude/knowledge/graph.db"
TEST_PROJECTS_DIR="$TEST_HOME/projects"

# (0) Baseline host invariance check --------------------------------------
HOST_AGAM_HASH=""
if [[ -f "$HOME/.claude/agam/AGAM.md" ]]; then
    HOST_AGAM_HASH=$(shasum "$HOME/.claude/agam/AGAM.md" | awk '{print $1}')
    echo "[baseline] host AGAM.md sha1: $HOST_AGAM_HASH"
fi

# (1) Reset test container ------------------------------------------------
CONTAINER_NAME="agam-oss-test"
echo "[step 1] resetting $CONTAINER_NAME test container..."
if ! ./scripts/test-container.sh reset; then
    echo "[setup-error] failed to (re)start agam-oss-test container."
    echo "[setup-error] likely causes: docker not running, or Artifactory"
    echo "[setup-error] auth expired (image pull denied). Not a script bug."
    exit 2
fi

# (2) Confirm the user's devcontainer (if any) was not disturbed ----------
if docker ps --format '{{.Names}}' | grep -qE 'claude-code-dev|claude-code'; then
    echo "[ok] user devcontainer still running alongside test container"
fi

# (3) Run installer in non-interactive mode with an answers file ----------
echo "[step 3] running agam init with injected answers..."
./scripts/test-container.sh exec bash -c "
    set -eu
    cd /workspace/agam
    mkdir -p '$TEST_HOME'
    cat > /tmp/answers.yaml <<'EOF'
name: test-user
primary-goal: e2e-test-run
projects-dir: $TEST_PROJECTS_DIR
platform: linux
container-mode: none
bootstrap-now: false
EOF
    HOME='$TEST_HOME' uv run agam init --answers /tmp/answers.yaml
"

# (4) Assert scaffolding laid down ----------------------------------------
echo "[step 4] asserting scaffolding under $TEST_AGAM_DIR..."
./scripts/test-container.sh exec bash -c "
    set -eu
    for f in AGAM.md THISAI.md MUGAM.md config.yaml; do
        if [[ ! -f '$TEST_AGAM_DIR/'\$f ]]; then
            echo 'FAIL: '\$f' missing from $TEST_AGAM_DIR'
            exit 1
        fi
    done
    if [[ ! -f '$TEST_KG_PATH' ]]; then
        echo 'FAIL: KG not created at $TEST_KG_PATH'
        exit 1
    fi
    echo '[ok] scaffolding laid down (AGAM.md, THISAI.md, MUGAM.md, config.yaml, graph.db)'
"

# (5) Seed synthetic transcripts ------------------------------------------
echo "[step 5] seeding synthetic transcripts..."
./scripts/test-container.sh exec bash -c "
    set -eu
    mkdir -p '$TEST_PROJECTS_DIR/test-proj'
    cat > '$TEST_PROJECTS_DIR/test-proj/s1.jsonl' <<'JSONL'
{\"role\":\"user\",\"content\":\"Tell me about the Agam project\"}
{\"role\":\"assistant\",\"content\":\"Agam is a knowledge-graph-powered identity system.\"}
JSONL
    echo '[ok] synthetic transcript seeded at $TEST_PROJECTS_DIR/test-proj/s1.jsonl'
"

# (6) Bootstrap -- LIVE or SKIPPED ----------------------------------------
if [[ "$LIVE" == "1" ]]; then
    echo "[step 6] LIVE bootstrap -- this makes real claude -p API calls..."
    # Symlink OAuth credentials from the bind-mount into the test HOME so
    # ``claude -p`` can authenticate. This is the only resource the isolated
    # test HOME shares with /home/node/.claude/; it mirrors exactly what a
    # real user's containerized Claude Code already does.
    ./scripts/test-container.sh exec bash -c "
        set -eu
        ln -sf /home/node/.claude/.credentials.json '$TEST_HOME/.claude/.credentials.json'
        if [[ -f /home/node/.claude.json ]]; then
            ln -sf /home/node/.claude.json '$TEST_HOME/.claude.json'
        fi
    "
    ./scripts/test-container.sh exec bash -c "
        set -eu
        cd /workspace/agam
        HOME='$TEST_HOME' \
        AGAM_HOME='$TEST_AGAM_DIR' \
        AGAM_KG_PATH='$TEST_KG_PATH' \
        AGAM_BOOTSTRAP_MODE=host \
        uv run agam bootstrap --projects '$TEST_PROJECTS_DIR' --days 30 --yes
    "

    # (7) Verify KG populated
    echo "[step 7] verifying KG has at least one entity..."
    COUNT=$(./scripts/test-container.sh exec python3 -c "
import sqlite3, sys
c = sqlite3.connect('$TEST_KG_PATH')
print(c.execute('SELECT COUNT(*) FROM entities').fetchone()[0])
" | tr -d '[:space:]')
    if [[ -z "$COUNT" || "$COUNT" -lt 1 ]]; then
        echo "FAIL: KG empty after bootstrap (count=$COUNT)"
        exit 1
    fi
    echo "[ok] KG populated ($COUNT entities)"
else
    echo "[skip] LIVE bootstrap not run (set AGAM_E2E_LIVE=1 to opt in)"
fi

# (8) Teardown ------------------------------------------------------------
echo "[step 8] tearing down test container..."
./scripts/test-container.sh down

# (9) Confirm host AGAM.md untouched --------------------------------------
if [[ -n "$HOST_AGAM_HASH" ]]; then
    HOST_AGAM_AFTER=$(shasum "$HOME/.claude/agam/AGAM.md" | awk '{print $1}')
    if [[ "$HOST_AGAM_HASH" != "$HOST_AGAM_AFTER" ]]; then
        echo "FAIL: host AGAM.md mutated during E2E"
        echo "  before: $HOST_AGAM_HASH"
        echo "  after:  $HOST_AGAM_AFTER"
        exit 1
    fi
    echo "[ok] host AGAM.md untouched"
fi

echo ""
echo "E2E PASS"

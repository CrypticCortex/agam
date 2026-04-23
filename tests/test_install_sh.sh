#!/bin/bash
# Integration test for install.sh prereq logic.
# Runs install.sh with HOME pointing to an empty tempdir so the credentials
# check fails. Asserts exit 1 and correct error message.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SH="$REPO_DIR/install.sh"
TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT

# Run with clean HOME -- uv/claude/docker still resolve via PATH, but
# ~/.claude/.credentials.json will be missing.
OUTPUT="$(HOME="$TMPHOME" bash "$INSTALL_SH" 2>&1)"
RC=$?

if [[ $RC -ne 1 ]]; then
  echo "FAIL: expected exit 1, got $RC"
  echo "output: $OUTPUT"
  exit 1
fi

if [[ "$OUTPUT" != *"no OAuth credentials"* ]]; then
  echo "FAIL: expected 'no OAuth credentials' in output, got:"
  echo "$OUTPUT"
  exit 1
fi

echo "PASS: install.sh exits 1 with credentials error when HOME has no creds"

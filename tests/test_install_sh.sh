#!/bin/bash
# Integration test for install.sh prereq logic.
#
# install.sh no longer requires ~/.claude/.credentials.json. On macOS host,
# Claude Code stores OAuth in Keychain and that file may never exist even
# when auth is healthy. The HARD prereqs are: uv on PATH, Darwin platform.
# claude on PATH and docker on PATH are WARNINGS, not failures -- the
# watchdog docker-execs into the user's claude-code devcontainer, so a
# host-side claude binary is optional. Both relaxations were triggered by
# real OSS users hitting false-negative install failures.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SH="$REPO_DIR/install.sh"

# Regression guard: the prereq block must not contain a credentials.json
# check. We bound the search with explicit `--- begin prereq checks ---`
# and `--- end prereq checks ---` marker comments in install.sh so renaming
# unrelated shell variables cannot widen the search window unexpectedly.
PREREQ_LINES="$(/usr/bin/awk '/--- begin prereq checks ---/,/--- end prereq checks ---/' "$INSTALL_SH")"
if [[ -z "$PREREQ_LINES" ]]; then
  echo "FAIL: install.sh is missing the prereq-block marker comments."
  echo "      Expected '--- begin prereq checks ---' and '--- end prereq checks ---'."
  exit 1
fi
if echo "$PREREQ_LINES" | /usr/bin/grep -q "no OAuth credentials"; then
  echo "FAIL: install.sh still contains the ~/.claude/.credentials.json check."
  echo "      This check is a false negative on macOS host installs where OAuth"
  echo "      lives in Keychain. Remove the check; let agam doctor probe auth."
  exit 1
fi
echo "PASS: install.sh does not hardcode the credentials.json check"

# Regression guard 2: when a hard prereq is genuinely missing (e.g. ``claude``
# not on PATH), install.sh must still exit non-zero with a clear message.
# We can't easily strip claude out of the real PATH without breaking the host,
# so we point PATH at an empty directory and rely on uv also being absent.
EMPTY_BIN="$(mktemp -d)"
TMPHOME="$(mktemp -d)"
trap 'rm -rf "$EMPTY_BIN" "$TMPHOME"' EXIT

set +e
OUTPUT="$(HOME="$TMPHOME" PATH="$EMPTY_BIN:/usr/bin:/bin" bash "$INSTALL_SH" 2>&1)"
RC=$?
set -u
if [[ $RC -eq 0 ]]; then
  echo "FAIL: install.sh exited 0 with no uv/claude on PATH; expected non-zero."
  echo "output: $OUTPUT"
  exit 1
fi
if [[ "$OUTPUT" != *"install uv"* && "$OUTPUT" != *"install Claude Code"* ]]; then
  echo "FAIL: install.sh failed but did not surface a clear prereq message."
  echo "output: $OUTPUT"
  exit 1
fi
echo "PASS: install.sh fails loudly when a hard prereq is missing"

# Regression guard 3: when claude is missing from PATH but uv is present,
# install.sh must NOT hard-fail with an "install Claude Code" error. The
# watchdog supports devcontainer-only setups via docker-exec, so a host-
# side claude binary is optional. Triggered by a real OSS user whose
# claude lived only inside their devcontainer and the install.sh hard-
# checked for it on host. We can't fully simulate "uv yes, claude no"
# without writing a fake uv stub on PATH, but we can at least assert the
# install.sh source treats claude as a warning, not a hard fail.
PREREQ_LINES="$(/usr/bin/awk '/--- begin prereq checks ---/,/--- end prereq checks ---/' "$INSTALL_SH")"
# The claude line must contain WARN (a warning), not ERR followed by exit.
CLAUDE_LINE="$(echo "$PREREQ_LINES" | /usr/bin/grep 'command -v claude' || true)"
if [[ -z "$CLAUDE_LINE" ]]; then
  echo "FAIL: install.sh has no 'command -v claude' check at all."
  echo "      The check should remain as a warning even if claude is optional."
  exit 1
fi
if echo "$CLAUDE_LINE" | /usr/bin/grep -q 'ERR:'; then
  echo "FAIL: install.sh treats missing claude as a hard error."
  echo "      Many users run Claude Code only inside a devcontainer (no host"
  echo "      claude binary). The watchdog docker-execs in, so this should"
  echo "      be a warning, not an exit. Found:"
  echo "      $CLAUDE_LINE"
  exit 1
fi
if ! echo "$CLAUDE_LINE" | /usr/bin/grep -q 'WARN:'; then
  echo "FAIL: install.sh claude check does not surface a WARN message."
  echo "      Found: $CLAUDE_LINE"
  exit 1
fi
echo "PASS: install.sh treats missing host claude as a warning, not a hard fail"

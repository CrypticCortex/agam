# User-Defined Vaults Implementation Plan

> **For Codex:** REQUIRED SKILL: Use `executing-plans` to implement this plan task-by-task.

**Goal:** Replace fixed vault names with a validated registry containing two protected, user-named portable roles and any number of restricted custom vaults managed from the CLI and TUI.

**Architecture:** A content-free JSON registry owns stable opaque vault IDs, display names, roles, lifecycle state, routing hints, and agent selections. Classification, immutable publication, recall, permissions, and the TUI resolve vaults through that registry and fail closed on invalid state. Archival changes registry state but never deletes a database.

**Tech Stack:** Python 3.11+, SQLite, Textual, argparse, pytest, JSON, POSIX file locks and atomic replacement.

---

### Task 1: Vault registry contract

**Files:**
- Create: `src/agam/vault_registry.py`
- Create: `tests/test_vault_registry.py`
- Modify: `src/agam/paths.py`

**Step 1: Write failing registry validation tests**

Cover a valid registry, exactly one active protected record for each portable
role, opaque ID validation, unique case-folded display names, restricted custom
defaults, unknown keys, unsafe paths, invalid agent selections, and archived
selection rejection.

**Step 2: Run the tests and observe failure**

Run: `pytest tests/test_vault_registry.py -q`

Expected: collection fails because `agam.vault_registry` does not exist.

**Step 3: Implement the immutable registry model**

Add frozen `VaultRecord` and `VaultRegistry` values, enums for role, access, and
state, strict JSON loading, stable ordering, and helpers to locate protected
roles and selected vaults. Add `paths.vault_registry_path()`.

Use opaque IDs shaped as `vault_<24 lowercase hex characters>`. Display names
must be trimmed, printable, 1-64 characters, and unique under `casefold()`.

**Step 4: Run the focused tests**

Run: `pytest tests/test_vault_registry.py -q`

Expected: all tests pass.

### Task 2: Atomic lifecycle mutations

**Files:**
- Modify: `src/agam/vault_registry.py`
- Modify: `tests/test_vault_registry.py`

**Step 1: Write failing lifecycle tests**

Prove first-run creation, protected-role rename, custom add, custom rename,
custom archive, restore, protected archive rejection, selected-vault archive
cleanup, lock serialization, mode `0600`, and atomic failure retention.

**Step 2: Observe the red tests**

Run: `pytest tests/test_vault_registry.py -q`

Expected: lifecycle symbols are missing.

**Step 3: Implement registry mutations**

Implement `initialize_registry`, `add_vault`, `rename_vault`, `archive_vault`,
`restore_vault`, and `set_agent_access`. Hold an exclusive lock, reload before
mutation, write and fsync a same-directory temporary file, set restrictive
permissions, atomically replace, and fsync the directory.

**Step 4: Prove the lifecycle contract**

Run: `pytest tests/test_vault_registry.py -q`

Expected: all tests pass and no test deletes a retained artifact.

### Task 3: Dynamic policy and manifest resolution

**Files:**
- Modify: `src/agam/knowledge_scopes.py`
- Modify: `src/agam/installer.py`
- Modify: `tests/test_knowledge_scopes.py`
- Modify: `tests/test_run_install.py`

**Step 1: Replace fixed-list tests with registry-driven failures**

Test dynamic active IDs, archived rejection, selected-agent intersection,
unknown store rejection, registry/manifest mismatch, stable registry order,
custom restricted default, and fail-closed missing registry behavior.

**Step 2: Observe failures**

Run: `pytest tests/test_knowledge_scopes.py tests/test_run_install.py -q`

Expected: fixed scope constants and installer policy violate the new contract.

**Step 3: Resolve policy through the registry**

Remove fixed vault constants. Validate manifest store IDs against the registry,
resolve effective access from the agent selection and active stores, and create
the registry during installation with generic editable suggestions.

**Step 4: Run focused policy tests**

Run: `pytest tests/test_knowledge_scopes.py tests/test_run_install.py -q`

Expected: all tests pass.

### Task 4: Registry-aware classification

**Files:**
- Modify: `src/agam/knowledge_classifier.py`
- Modify: `tests/test_knowledge_classifier.py`

**Step 1: Write failing opaque-routing tests**

Require model results to contain `vault_id`, validate it against active registry
records, strip only generic classification prefixes, route unknown or archived
IDs to review, and ensure progress/errors remain content-free. Prove the prompt
uses display names only as inert choices and never as filesystem identifiers.

**Step 2: Observe failures**

Run: `pytest tests/test_knowledge_classifier.py -q`

Expected: the fixed privacy/kind response contract fails the new assertions.

**Step 3: Implement the routing contract**

Build the allowed routing choices from the verified registry. Protected role
choices carry generic semantic descriptions; custom choices carry only their
user-provided routing hint. Accept exactly one active opaque ID per item and
preserve the existing confidence, integrity, sealed-review, and Claude CLI
boundaries.

**Step 4: Prove classifier behavior**

Run: `pytest tests/test_knowledge_classifier.py -q`

Expected: all tests pass.

### Task 5: Dynamic immutable publication

**Files:**
- Modify: `src/agam/knowledge_materializer.py`
- Modify: `src/agam/review_queue.py`
- Modify: `tests/test_knowledge_materializer.py`
- Modify: `tests/test_review_queue.py`

**Step 1: Write failing publication tests**

Cover two renamed protected records, zero custom vaults, multiple custom vaults,
archived omission, same-vault edges, cross-vault edge omission, opaque paths,
review publication, and unchanged prior versions.

**Step 2: Observe failures**

Run: `pytest tests/test_knowledge_materializer.py tests/test_review_queue.py -q`

Expected: fixed routing tables fail.

**Step 3: Materialize active registry records**

Iterate active records in registry order, route by classified opaque ID, create
one immutable store per active vault, and bind the registry digest into the
manifest. Review resolution accepts only an active target ID.

**Step 4: Prove publication integrity**

Run: `pytest tests/test_knowledge_materializer.py tests/test_review_queue.py -q`

Expected: all tests pass.

### Task 6: Dynamic agent boundary

**Files:**
- Modify: `src/agam/hooks/graph_recall.py`
- Modify: `src/agam/hooks/scope_guard.py`
- Modify: `src/agam/codex_permissions_merger.py`
- Modify: `src/agam/codex_hooks_merger.py`
- Modify: `src/agam/agents/codex.py`
- Modify: `tests/test_graph_recall.py`
- Modify: `tests/test_scope_guard.py`
- Modify: `tests/test_codex_permissions_merger.py`
- Modify: `tests/test_codex_hooks_merger.py`

**Step 1: Write failing dynamic-boundary tests**

Prove recall opens only selected portable vault IDs, a renamed display label has
no effect, a new custom vault is denied, an explicitly selected custom vault is
reopened only after wiring, stale/invalid registry state denies access, and
shell/path obfuscation cannot escape the allowed roots.

**Step 2: Observe failures**

Run: `pytest tests/test_graph_recall.py tests/test_scope_guard.py tests/test_codex_permissions_merger.py tests/test_codex_hooks_merger.py -q`

Expected: hard-coded allow/deny paths fail.

**Step 3: Derive permissions from verified metadata**

Generate the agent profile from selected opaque IDs and the active manifest.
Make the guard deny the knowledge root except exact metadata and selected
immutable paths. Keep missing, changed, symlinked, or malformed state fail
closed.

**Step 4: Prove the boundary**

Run: `pytest tests/test_graph_recall.py tests/test_scope_guard.py tests/test_codex_permissions_merger.py tests/test_codex_hooks_merger.py -q`

Expected: all tests pass.

### Task 7: CLI setup and lifecycle commands

**Files:**
- Modify: `src/agam/cli.py`
- Modify: `tests/test_cli.py`

**Step 1: Write failing CLI tests**

Cover `vault setup`, `vault list`, `vault add`, `vault rename`, `vault archive`,
`vault restore`, and `vault access`. Test JSON output, non-interactive flags,
duplicate names, protected archive rejection, unknown IDs, and rewire-required
status.

**Step 2: Observe failures**

Run: `pytest tests/test_cli.py -q -k 'vault or wire'`

Expected: commands are absent or fixed choices reject dynamic IDs.

**Step 3: Implement scriptable commands**

Expose stable IDs in machine output and user names in human output. Make every
mutation report its state delta and whether affected agents require rewiring.

**Step 4: Prove CLI behavior**

Run: `pytest tests/test_cli.py -q -k 'vault or wire'`

Expected: all selected tests pass.

### Task 8: TUI vault management

**Files:**
- Modify: `src/agam/vaults.py`
- Modify: `src/agam/tui.py`
- Modify: `tests/test_vaults.py`
- Modify: `tests/test_tui.py`

**Step 1: Write failing TUI pilot tests**

Test first-run naming, renamed protected rows, add dialog, rename dialog,
archive confirmation, restore action, protected archive rejection, agent access
toggle, restricted-content masking, selection persistence, and keyboard focus.

**Step 2: Observe failures**

Run: `pytest tests/test_vaults.py tests/test_tui.py -q`

Expected: fixed rail and missing dialogs fail.

**Step 3: Implement the operator flow**

Render registry names and stable content-free state. Add compact modal dialogs
and bindings for add, rename, archive/restore, and access. Keep restricted row
contents unopened unless the current agent selection permits them. Surface
rewire-required state after access changes.

**Step 4: Prove TUI behavior**

Run: `pytest tests/test_vaults.py tests/test_tui.py -q`

Expected: all tests pass.

### Task 9: Content-free migration

**Files:**
- Create: `src/agam/vault_migration.py`
- Create: `tests/test_vault_migration.py`
- Modify: `src/agam/installer.py`
- Modify: `src/agam/cli.py`

**Step 1: Write failing migration tests**

Build synthetic fixed-layout manifests without real content. Prove dry-run
output, opaque ID allocation, portable-role mapping, generic custom naming,
retained source artifacts, idempotence, backup creation, and rollback on
invalid metadata.

**Step 2: Observe failure**

Run: `pytest tests/test_vault_migration.py -q`

Expected: migration module is missing.

**Step 3: Implement migration**

Read only content-free manifests and policy. Write a timestamped metadata backup
and a new registry/manifest mapping without opening source databases or deleting
old paths. Require explicit apply after dry-run.

**Step 4: Prove migration safety**

Run: `pytest tests/test_vault_migration.py -q`

Expected: all tests pass and source artifact hashes remain unchanged.

### Task 10: OSS documentation and terminology audit

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/codex-integration.md`
- Modify: affected source and test files reported by the audit

**Step 1: Add documentation contract tests or executable probes**

Verify every quick-start command parses, required sections exist, and a
case-insensitive repository scan finds no organization-specific identifier in
tracked source, tests, comments, or documentation.

**Step 2: Rewrite setup documentation**

Document installation, first-run naming, unattended flags, lifecycle commands,
TUI keys, agent access, rewiring, migration dry-run/apply, privacy defaults,
verification, recovery, and troubleshooting.

**Step 3: Run the documentation and terminology checks**

Run: `python -m agam.cli --help`

Run: `rg -n -i '<forbidden-organization-term>' README.md docs src tests`

Expected: help exits zero and the audit produces no matches. The actual audit
uses the private local deny term supplied at verification time; the term is not
stored in repository files.

### Task 11: End-to-end verification and publication

**Files:**
- Modify only files required by failures attributable to this change.

**Step 1: Run focused integration tests**

Run: `pytest tests/test_scoped_knowledge_e2e.py tests/test_agents.py tests/test_watchdog_shell.py -q`

Expected: all tests pass.

**Step 2: Run static checks**

Run: `python -m compileall -q src tests`

Run: `bash -n src/agam/hooks/agam_watchdog.sh`

Run: `git diff --check`

Expected: all commands exit zero.

**Step 3: Run the full suite**

Run: `pytest -q`

Expected: all tests pass apart from explicitly documented pre-existing skips.

**Step 4: Exercise the real CLI and TUI against a temporary home**

Create two user-named protected vaults, add and archive a custom vault, select
agent access, publish synthetic data, verify restricted rows stay masked, and
restore the custom vault. Compare registry, manifest, artifact counts, and
hashes before and after.

**Step 5: Cold-review the complete diff**

Confirm requested behavior, no unrelated file inclusion, no deletion, no
private content, no forbidden terminology, and no generated artifact.

**Step 6: Commit and publish**

Stage only the approved implementation and documentation. Exclude unrelated
working-tree files. Commit without attribution trailers, push the current
branch to `origin`, and report the exact commit and remote branch.

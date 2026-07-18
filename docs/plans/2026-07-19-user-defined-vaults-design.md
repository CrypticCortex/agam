# User-Defined Vaults Design

## Outcome

Agam provides two protected portable vault roles and any number of custom
vaults. Every display name is chosen by the user. Agent access is explicit,
custom vaults are restricted by default, and the terminal UI manages the vault
lifecycle without deleting retained knowledge.

## Principles

- User-facing names are configuration, never routing keys.
- Stable opaque vault IDs keep paths, manifests, hooks, and agent wiring valid
  when a vault is renamed.
- Two protected roles preserve the portable knowledge paths agents need:
  guidance and reusable solutions.
- Custom vaults are unlimited and restricted by default.
- Removing a custom vault archives it. Published databases remain immutable and
  restorable.
- Agent access is deny-by-default and selected per vault.
- Classification and publication fail closed when the registry, assignment, or
  integrity proof is invalid.
- Repository source, tests, comments, and documentation contain no
  organization-specific terminology.

## Registry

The content-free registry lives beside the published manifests. It has a
versioned schema and contains:

- an opaque stable ID for each vault;
- a user-selected display name;
- one protected role (`guidance` or `solutions`) or the `custom` role;
- `portable` or `restricted` access class;
- active or archived lifecycle state;
- an optional classification hint for custom routing; and
- explicit per-agent vault selections.

The two protected records cannot be archived, but users can rename them at any
time. Custom records can be added, renamed, archived, and restored. Registry
writes use a lock, an atomic replacement, restrictive permissions, and schema
validation.

## Classification and Publication

The classifier receives only the active registry choices needed for routing.
Its response names an opaque vault ID rather than a display name. Portable
guidance and solution records route to their protected role IDs. Records routed
to a custom vault inherit that vault's restricted access class. Unknown,
archived, low-confidence, or malformed assignments go to sealed review.

The materializer iterates the validated registry instead of a fixed scope list.
It creates one immutable database per active vault, preserves same-vault edges,
omits cross-vault edges, and publishes a content-free manifest atomically.
Archived vaults remain in prior versions but receive no new writes.

## Agent Access

Each agent has an explicit allow-list of active vault IDs. New custom vaults are
not assigned to any agent. The two portable roles are suggested during setup,
but the user confirms the selection. Policy resolution intersects the registry,
the selected profile, the active manifest, and the agent's supported
capabilities.

Codex permissions deny the knowledge root and reopen only registry metadata,
verified manifests, and the selected immutable vault paths. The pre-tool guard
derives its allowed paths from the same content-free policy and fails closed if
that policy cannot be verified.

## Terminal UI

The vault rail displays user-selected names, access class, lifecycle state,
item counts, queue depth, and publication version. It supports:

- first-run naming of the two protected roles;
- adding a custom vault with a name and optional routing hint;
- renaming any vault;
- archiving and restoring custom vaults;
- selecting per-agent access; and
- showing when agent wiring must be refreshed.

Dialogs validate input inline, explain privacy effects before access changes,
and require confirmation before archival. Empty, unavailable, archived, and
pending-publication states remain distinguishable without exposing restricted
content.

## Setup and Migration

Interactive setup asks for the two portable display names and offers clear
editable suggestions. Non-interactive setup accepts flags and otherwise uses
generic defaults that can be renamed later. Existing fixed-scope installations
receive a content-free registry migration: known portable stores map to the two
protected roles, other stores receive opaque custom IDs, and original published
artifacts are retained as evidence.

The README documents quick start, interactive and non-interactive setup,
lifecycle operations, per-agent wiring, migration, privacy defaults, TUI keys,
verification, and troubleshooting.

## Verification

Tests prove registry validation and atomicity, stable renames, archive/restore
retention, dynamic materialization, sealed-review fallbacks, deny-by-default
agent access, dynamic permission generation, TUI keyboard flows, migration, and
README command accuracy. Final verification includes targeted tests, the full
suite, runtime TUI smoke tests, diff review, and a case-insensitive repository
audit for forbidden organization-specific terms.

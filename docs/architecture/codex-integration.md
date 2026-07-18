# Codex scoped-knowledge integration

> Status: implemented. Codex reads only the active vault IDs explicitly selected
> for it in the content-free vault registry.

## Security boundary

The old shared-graph design is not the Codex architecture. Codex does not read
`~/.agam/knowledge/graph.db`, a Claude graph, identity files, transcripts, or a
caller-supplied `AGAM_KG_PATH`. Missing or invalid scoped policy fails closed;
there is no legacy fallback.

The primary boundary is the selectable Codex filesystem permission profile
`agam_scoped`. It denies `~/.agam/knowledge` as a whole, then reopens only
content-free registry, policy, and manifest metadata plus the opaque vault IDs
selected for Codex. Display names never become filesystem paths or permission
rules.

The profile does not reopen unselected vaults, sealed staging, or legacy
shared-graph paths. The `PreToolUse` scope guard is defense in depth: it
blocks direct, escaped, indirect, symlinked, URI, shell, Python, SQLite, and
local-function attempts to reach restricted knowledge. It is not a substitute
for selecting the filesystem profile.

## Classification and publication

```text
read-only source graph
        |
        | backup into sealed staging
        v
Claude CLI / Haiku classifier
  route: one active opaque vault ID | REVIEW
        |
        | sealed, resumable classifications
        v
validated materializer
        |
        +-- vault_<opaque-id>/<version>/graph.db
        +-- vault_<opaque-id>/<version>/graph.db
        +-- ... one store per active vault
        |
        v
content-free manifest + atomic active.json
```

The source is opened read-only. Classification happens on a sealed staging
copy. The Claude CLI runner is pinned to Haiku, disables tools, slash commands,
Chrome, session persistence, project setting sources, auto-memory, and prompt
history, and uses a replacement classifier system prompt. Model-facing entity
IDs are opaque. Progress, errors, and CLI output contain only stable codes,
hashes, IDs, and aggregate counts.

Every classified description starts with its opaque assignment marker, for
example `[VAULT:vault_aaaaaaaaaaaaaaaaaaaaaaaa]`. A run-level HMAC binds
metadata, every source row, all
properties and relationships, and every classification. The checkpoint seal is
updated with each resumable transaction. Materialization revalidates the seal,
source and staging fingerprints, official schema digest, and timestamps before
publishing anything.

Routing is deterministic: an accepted assignment is copied only to the matching
opaque vault store. Unknown IDs, archived destinations, malformed responses,
and low-confidence results become `REVIEW` and are omitted until resolved.
Codex readability is independent of classification: it comes only from the
registry's explicit Codex selection.

Properties follow their entity. Relationships are copied only when both ends
land in the same physical store; cross-scope edges are omitted. Publication
builds every active immutable store first and switches `active.json` only after
validation succeeds. Manifests contain relative store paths, hashes, model,
timestamps, and aggregate counts—not entity text or source paths.

## Policy and selective wiring

`~/.agam/knowledge/scopes/registry.json` is the canonical selection source. It
contains only vault metadata and explicit per-agent vault IDs. The separate
`config.json` stores capabilities. Environment scope selection may narrow an
agent's registry selection but can never expand it.

Codex capabilities are deliberately asymmetric:

```json
{
  "recall": true,
  "boot-injection": false,
  "capture": false
}
```

Use `agam wire <agent> --show` to inspect the effective version, opaque vault
IDs, and capabilities without changing wiring:

```bash
agam wire codex
agam wire codex --show
```

The Codex adapter installs only two namespaced hooks under
`~/.codex/hooks/agam`:

| Event | Handler | Behavior |
|---|---|---|
| `UserPromptSubmit` | `graph_recall.py` | Reads active, verified stores selected for Codex and returns advisory context. |
| `PreToolUse` (`*`) | `scope_guard.py` | Blocks attempts to access restricted knowledge; unavailable/crashed guard blocks the tool call. |

There is no Codex `Stop` handler, transcript normalization, enrichment queue,
session capture, lesson post-processing, identity injection, or boot context.
Recall is prior evidence, not live truth, and tells the agent to verify
load-bearing facts against current sources.

## Install, trust, and permission selection

Wiring writes configuration; it cannot silently grant trust or select a Codex
permission profile. After `agam wire codex`:

1. Open `/hooks`, review the absolute commands under
   `~/.codex/hooks/agam`, and trust them.
2. Open `/permissions` and select `agam_scoped`.
3. If wiring reports `legacy-sandbox-conflict`, resolve the legacy sandbox
   setting before selecting the profile.
4. Run `agam wire codex --show` and confirm the expected opaque vault IDs are
   active, with boot injection and capture disabled.

Until those manual steps are complete, hook trust is `review-required` and the
filesystem profile is `select-required`; installation must not report either as
active.

## Content-free operator flow

Classification and materialization deliberately require explicit provenance:

```bash
# For a live source, first create a transactionally consistent, content-blind
# snapshot. Keep it under the sealed tree and use the same immutable file for
# both commands below.
sqlite3 <live-source.db> ".backup '<sealed-source.db>'"

agam knowledge classify \
  --source <sealed-source.db> \
  --staging ~/.agam/knowledge/sealed/staging/<run>.db \
  --model haiku

agam knowledge materialize \
  --source <sealed-source.db> \
  --staging ~/.agam/knowledge/sealed/staging/<run>.db \
  --source-sha256 <classify-output> \
  --source-snapshot-sha256 <classify-output> \
  --staging-sha256 <classify-output>

agam wire codex
```

Do not classify a database that active hooks can still mutate. The classifier
checks source identity, physical bytes, and the logical SQLite snapshot before
publication; a change at any point fails closed with `source_changed`.

Successful commands print aggregate JSON: run/version IDs, hashes, model,
per-store counts, permission state, and next actions. Failures print stable
reason codes. Neither path prints graph rows, model prompts/responses, source
filenames, or staging paths.

## TUI operator boundary

`agam tui` uses the same physical separation as Codex recall. Its Vaults view
starts from the registry and `active.json`, validates every selected store path
and digest through `knowledge_scopes`, and rejects a database containing an
assignment marker for a different vault. Unselected rows in the rail are
metadata only and their databases are not opened.

The TUI has two independent queues:

- The Sessions view delegates a selected legacy row to the monitor and a
  selected file-queue generation to the shared watchdog's basename-only
  selector. It never maps a merged-table index onto the wrong queue. Bulk drain
  remains a separately confirmed action across both formats.
- The Reviews view validates the classifier checkpoint before revealing a row.
  A one-row Haiku retry and an explicit user decision both go back through the
  classifier's transaction, per-row HMAC, and run seal. The TUI never updates
  staging tables directly. Publication revalidates the sealed run, builds a new
  immutable version, and switches the active pointer atomically. Remaining
  `REVIEW` rows stay omitted.

Review content is not included in list rows, notifications, subprocess
arguments, logs, or test fixtures. Repository verification uses synthetic
graphs and fake runners; Codex does not open the production sealed staging or
restricted vaults during development.

## Verification boundary

Repository tests use synthetic SQLite graphs, fake model runners, and temporary
homes. The end-to-end proof classifies into portable and restricted user-defined
vaults, materializes opaque physical stores, installs Codex wiring, populates a
legacy shared graph, and verifies that recall returns only explicitly selected
markers while never returning restricted or legacy-only markers.

These tests do not classify a user's live graph, authenticate Claude, trust
hooks, or select the permission profile in a running Codex client. A live
rollout therefore still requires the manual trust/permission steps above and a
smoke query after an active manifest has been published.

# Agam

Knowledge-graph-powered identity and context injection for Claude Code, Cursor,
and Codex. Agam keeps one local shared brain across agents: it recalls relevant
projects, services, decisions, bugs, and lessons while you work, then distills
substantive completed sessions back into the graph in the background.

## What Agam actually does

- Identity files at `~/.agam/` (AGAM.md, THISAI.md, MUGAM.md) describe who you are and what you are working on.
- A SQLite knowledge graph at `~/.agam/knowledge/graph.db` stores entities and relationships with FTS5 search.
- Claude Code and Codex use `UserPromptSubmit` hooks for prompt-specific recall. Cursor receives a refreshed always-on rule digest.
- Agent stop/session-end hooks enqueue substantive work for a launchd-managed watchdog, with `source-agent` provenance preserved.
- `agam bootstrap` can seed the graph from existing Claude Code transcripts in `~/.claude/projects/`.

No provider API key is required by Agam. Background enrichment reuses the
authentication of an installed agent CLI. Claude Code remains the current
engine for the optional historical bootstrap pass; ongoing enrichment can run
through Claude, Cursor Agent, or Codex.

## Prerequisites

- macOS. Only platform supported in v1.
- At least one supported agent installed and authenticated on the host (Claude Code, Cursor Agent, or Codex), or Claude Code available in a devcontainer.
- [uv](https://docs.astral.sh/uv/) for Python execution.
- Python 3.11 or newer (uv will fetch one if you do not have it).
- Optional: Docker Desktop with a running Claude Code devcontainer. Docker is only needed for container-based Claude enrichment; host Claude, Cursor, and Codex modes work without it.

The installer stops when `uv` or macOS is missing and warns when no usable host
CLI or optional Docker runtime is detected. Authentication failures surface
when the selected CLI is first invoked.

## Install

```bash
git clone <repo-url> ~/coding/agam
cd ~/coding/agam
./install.sh
```

The wizard detects installed agents and lets you choose which ones to wire. To
wire one agent explicitly, or all three at once:

```bash
./install.sh --target codex
./install.sh --target claude --target cursor --target codex
```

Codex hook scripts are installed in Agam's owned namespace at
`~/.codex/hooks/agam/`; their registrations are merged into
`~/.codex/hooks.json` without replacing unrelated hooks. After the first Codex
launch, open `/hooks`, review the installed Agam commands, and trust them if
Codex reports that they are awaiting trust.

`install.sh` does the minimum:

1. Checks for `uv`, a supported agent CLI, Docker (optional), and macOS. Authentication is verified by the selected CLI when it is first invoked.
2. Runs `uv sync` to materialize the Python environment.
3. Delegates to `uv run agam init`, which is the real installer.

`agam init` is an interactive [questionary](https://github.com/tmbo/questionary) wizard. It asks a few questions (name, primary goal, projects directory, platform, and whether to bootstrap now), then:

- Renders the shared identity and configuration into `~/.agam/`.
- Merges agent-specific wiring into the selected Claude, Cursor, and/or Codex configuration without replacing unrelated hooks.
- Writes the watchdog launchd plist to `~/Library/LaunchAgents/com.agam.watchdog.plist` and loads it.
- Creates `~/.agam/knowledge/graph.db` with the FTS5 schema if it does not exist.

Re-running the installer is safe: it refreshes bundled prompts, tools, and the
selected agent wiring while preserving the identity and knowledge graph in
`~/.agam/`. After updating the source checkout, re-run it for every agent you
use:

```bash
git pull --ff-only
./install.sh --target claude --target cursor --target codex
```

Upgrades from the older Claude-specific layout are copy-migrated when
`~/.agam/knowledge/graph.db` is absent: Agam copies the legacy graph from
`~/.claude/knowledge/` and identity files from `~/.claude/agam/`, then leaves
the originals untouched. A partially created `~/.agam/` containing only
operational data such as `queue/` does not suppress this migration; those queue
entries remain in place while the legacy graph is copied.

You can also drive the wizard non-interactively by feeding it a YAML answer file:

```bash
uv run agam init --answers my-answers.yaml
```

### Set up your vaults

Agam starts with two protected portable roles and lets you choose both display
names. The names are labels, not filesystem paths; stable opaque IDs keep
renames safe.

```bash
agam vault setup \
  --guidance-name "How I Build" \
  --solutions-name "Things That Worked"
agam tui
```

You can also complete this naming step in the TUI on first launch. Add as many
restricted vaults as you need later:

```bash
agam vault add --name "Project North" --hint "knowledge only for Project North"
agam vault list
```

New custom vaults are restricted and unselected by default. Use the IDs from
`agam vault list` to grant an agent an explicit selection, then refresh its
wiring:

```bash
agam vault access codex \
  --vault vault_aaaaaaaaaaaaaaaaaaaaaaaa \
  --vault vault_bbbbbbbbbbbbbbbbbbbbbbbb
agam wire codex
agam wire codex --show
```

Replace the example IDs with your own. Renaming preserves the ID. Archiving a
custom vault removes it from active selections but retains its database; it can
be restored later.

## Bootstrap walkthrough

The bootstrap pass is optional but strongly recommended. It reads your Claude Code session transcripts and populates the knowledge graph with entities + relationships.

```bash
agam bootstrap --days 30
```

You will see a cost preview before anything bills:

```
[agam bootstrap] projects-dir: /Users/you/.claude/projects
[agam bootstrap] transcripts: 47 (days filter: 30)
[agam bootstrap] estimated tokens: ~412,000
[agam bootstrap] estimated cost: ~$0.4536
Proceed? [y/N]
```

Answer `y` to run. The pipeline has two phases:

1. **Extraction** -- Haiku reads each transcript chunk and emits candidate entities and relationships.
2. **Reconciliation** -- Sonnet merges duplicates, resolves name variants, and returns a clean payload that is written into the graph.

State is checkpointed after every transcript. If the process is interrupted (`Ctrl-C`, crash, laptop sleep), just re-run the same command; it picks up exactly where it left off:

```bash
agam bootstrap --days 30    # resumes by default
agam bootstrap --no-resume  # force a clean run
```

Useful flags:

| Flag | Default | Meaning |
|---|---|---|
| `--days N` | 30 | Only scan transcripts modified in the last N days. |
| `--all` | off | Ignore the age filter, scan everything. |
| `--projects PATH` | `~/.claude/projects` | Override the transcript root. |
| `--yes`, `-y` | off | Skip the cost confirmation prompt. |
| `--no-resume` | off | Start clean, ignore prior state. |
| `--model-haiku` | `haiku-4-5` | Extraction model slug. |
| `--model-sonnet` | `sonnet-4-6` | Reconciliation model slug. |

Cost estimation uses a 4-chars-per-token heuristic, $0.80 per 1M Haiku input tokens, and $3.00 per 1M Sonnet input tokens with ~10% of the token budget routed to reconciliation. These rates are defaults in `src/agam/bootstrap.py`; override them there if your pricing differs.

## Configuration

### `config.yaml`

Written into `~/.agam/config.yaml` by the installer:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Your preferred name, used inside identity files. |
| `primary-goal` | string | One-line direction that anchors THISAI.md. |
| `projects-dir` | path | Where your code lives. Used by boot context injection. |
| `platform` | string | `macos` for v1. |
| `container-mode` | string | `auto` for new installs. Retained for compatibility; the watchdog now resolves an invoker at run time. |

Edit this file directly and the next session picks up the changes. Re-run
`agam init` to refresh bundled prompts, hooks, and tools without replacing the
identity or graph.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGAM_INVOKER` | unset | Pin the cascade to a single invoker: `host` or `container`. Skip if you want auto-detect. |
| `AGAM_CONTAINER_PATTERN` | `claude-code` | Regex matched against `docker ps` rows to discover your claude-code container. |
| `AGAM_CONTAINER_NAME` | unset | Exact container name. Beats the regex above. |
| `AGAM_WATCHDOG_MODE` | unset | Legacy alias for `AGAM_INVOKER`. Honored for back-compat. |
| `AGAM_LLM_CLI_PIN` | unset | Pin host enrichment to `claude`, `cursor-agent`, or `codex`. |
| `AGAM_LLM_CLI_PATH` | unset | Absolute host CLI executable path. The macOS installer records this in the launchd plist so npm/nvm/asdf-installed CLIs remain discoverable under launchd's restricted `PATH`. |
| `AGAM_DATA_HOME` | `~/.agam` | Shared, agent-neutral data root. |
| `AGAM_HOME` | `~/.agam` | Legacy-compatible identity + log root override. |
| `AGAM_KG_PATH` | `~/.agam/knowledge/graph.db` | Path to the SQLite graph. |
| `AGAM_PROMPTS_DIR` | bundled | Directory holding bootstrap prompt templates. |

These overrides are mainly useful for diagnostics, tests, and non-default layouts.

## How Agam runs background enrichment

The optional historical bootstrap still invokes Claude Code. The ongoing
watchdog is agent-neutral and chooses a healthy enrichment CLI at run time:

1. A named or discovered Claude Code container, when configured and running.
2. A host CLI, preferring `claude`, then `cursor-agent`, then `codex`.
3. `AGAM_LLM_CLI_PIN=claude|cursor-agent|codex` overrides that host preference.

On macOS, the installer also stores the selected CLI's resolved absolute path in
the launchd plist. An explicit `AGAM_LLM_CLI_PATH` must be absolute and
executable; if a stored path later becomes stale, the watchdog falls back to
the normal pin/PATH probes.

Codex enrichment runs non-interactively and ephemerally with Agam hooks disabled
for the child run, preventing a background enrichment task from recursively
enqueueing itself.

When a substantive session or turn completes:

- Its agent hook writes or refreshes a file-per-session entry in `~/.agam/queue/`.
- The launchd watchdog resolves an invoker and drains the queue every few minutes.
- With no healthy invoker, the queue remains untouched and `~/.agam/logs/watchdog.log` records the failed probes for the next tick.

For Codex, each worthwhile Stop event is normalized into a distinct immutable
snapshot under `~/.agam/transcripts/codex/`. The watchdog atomically claims one
queue generation before processing it, so a newer Stop from the same session
can remain queued without replacing the transcript currently being read.
Snapshots referenced by pending, processing, retry, or dead-letter work are
protected; completed or superseded generations are pruned to the two most
recent per session while their processed audit records remain available.

To see what Agam thinks is available right now:

```bash
agam doctor
```

Use the doctor output together with `~/.agam/logs/watchdog.log` as the first stop
when automatic learning pauses. Each selected CLI owns and reports its own
authentication failures.

## Multi-vault TUI

Run the local dashboard with:

```bash
agam tui
```

The TUI is an operator console for Agam's physical vaults and queues:

- **Vaults** shows every user-named vault with its role, access class, state,
  count, and current Codex selection. Press `n` to add, `e` to rename, `A` to
  archive or restore, and `w` to toggle Codex access. Protected portable roles
  can be renamed but not archived. Every readable database is resolved through
  the active manifest and verified digest; there is no fallback to the legacy
  mixed graph.
- **Sessions** merges the legacy and file-per-session ingestion queues. Press
  `s` to sync one row, `D` twice to archive one row, or `d` twice to confirm a
  bulk drain.
- **Reviews** exposes unresolved classifier decisions as opaque metadata. Enter
  reveals one item locally, `h` retries only that item through Claude CLI/Haiku,
  `x` opens an explicit routing dialog, and `P` twice publishes a new immutable
  vault version. Unresolved reviews remain omitted.
- **Worklog**, **Activity**, and **Health** retain the existing operational
  evidence and diagnostics.

The animated brain can show one input wire each for Claude, Cursor, and Codex.
A wire appears only when Agam-specific hooks for that agent are installed;
having a CLI or editor installed is not enough.

The packaged CLI and the optional source-checkout shim both dispatch to
`agam.tui:main`; there is no separate private TUI implementation to drift from
the OSS code.

### Migrate an older fixed vault layout

Migration is copy-only and defaults to a dry run. Agam retains the original
stores and backs up the old metadata before activation.

```bash
agam vault migrate
agam vault migrate --apply
agam vault list
agam wire codex
```

The two previously portable stores become the protected roles. Any additional
stores become restricted custom vaults with neutral temporary names that you
can rename in the TUI or with `agam vault rename`.

## Troubleshooting

Start with:

```bash
agam status
```

That prints the Agam home path, knowledge graph size, queue depth, bootstrap resume state, and the detected container name (if any). It does not touch the graph or the queue, so it is always safe to run.

Common situations:

- **`Container: (none detected)`** -- your claude-code devcontainer is not running. Start it, then re-run `agam status`. If detection still fails and you have a custom name, set `AGAM_CONTAINER_NAME` to the exact name shown by `docker ps`.
- **`no-container` lines in `~/.agam/logs/watchdog.log`** -- expected whenever the container is down. A healthy host CLI can still drain the queue.
- **Queue stuck / entries in `~/.agam/queue-errors/`** -- a session failed processing. Open the error payload to see the underlying exception. Logs:
  ```bash
  tail -n 100 ~/.agam/logs/watchdog.log
  ls ~/.agam/queue-errors/
  ```
- **`ERR: no claude-code container running` from `agam bootstrap`** -- same fix as above. Start the container and re-run; bootstrap resumes automatically.
- **Recall is not injecting anything** -- confirm the selected agent's hook wiring (`~/.claude/settings.json`, `~/.codex/hooks.json` plus `~/.codex/hooks/agam/`, or Cursor's generated rule), then confirm the graph has entities: `sqlite3 ~/.agam/knowledge/graph.db 'select count(*) from entities;'`. A fresh graph is the most common cause.

## Uninstall

Back up anything you want to keep first:

```bash
cp -r ~/.agam ~/agam-backup-$(date +%Y%m%d)
```

Preview a complete uninstall:

```bash
agam uninstall
agam uninstall --target codex          # one agent only
agam uninstall --confirm               # soft uninstall; preserves data backup
agam uninstall --confirm --purge       # permanent deletion
```

The repo at `~/coding/agam` is independent; remove it separately if you no longer want the source.

## Subcommand reference

| Command | Purpose |
|---|---|
| `agam init` | Install the shared brain and selected agent wiring. Repeat `--target` for Claude, Cursor, and/or Codex. |
| `agam bootstrap` | Scan transcripts, estimate cost, extract + reconcile into the knowledge graph. Resumable. |
| `agam status` | Print install health: paths, graph size, queue depth, container detection, resume state. |
| `agam doctor` | Run deeper installation and invoker diagnostics. |
| `agam tui` | Open the multi-vault operator console for vaults, session ingestion, sealed reviews, activity, and health. |
| `agam uninstall` | Preview or remove selected agent wiring and, when no agents remain, the shared data home. |
| `agam reset` | Remove bootstrap scratch state (`~/.claude/.agam-bootstrap-state.json` and candidates). Dry-run by default; pass `--confirm` to actually delete. Never touches identity files or the graph. |

## Project layout

```
agam/
  install.sh              -- macOS host installer. Thin wrapper over agam init.
  src/agam/
    cli.py                -- argparse entrypoint for installation, operations, and maintenance.
    installer.py          -- questionary wizard + settings merge.
    bootstrap.py          -- scan, extract (Haiku), reconcile (Sonnet), durable state.
    agents/               -- Claude, Cursor, and Codex installation adapters.
    *_hooks_merger.py     -- non-destructive per-agent hook config merging.
    tools/                -- shared graph, queue, context, and maintenance helpers.
    hooks/                -- PreToolUse / PostToolUse / Stop / UserPromptSubmit hook scripts.
  templates/              -- AGAM.md / THISAI.md / MUGAM.md / plist / CLAUDE.md snippet.
  prompts/                -- Bootstrap prompt templates (work-log, agam-sync).
  knowledge/
    graph-schema.sql      -- SQLite + FTS5 schema applied on first run.
  tests/                  -- pytest suite.
  scripts/
    test-container.sh     -- end-to-end container-mode smoke test.
```

## License

MIT. See [LICENSE](LICENSE) for the full text.

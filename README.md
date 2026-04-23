# Agam

Knowledge-graph-powered identity and context injection layer on top of Claude Code. Agam auto-injects relevant entities (projects, services, decisions, bugs, lessons) into every Claude Code session via a `UserPromptSubmit` hook, so the model answers from your history instead of searching files every time. You seed the graph once via a bootstrap pass over prior session transcripts, then Agam keeps it warm in the background.

## What Agam actually does

- Identity files at `~/.claude/agam/` (AGAM.md, THISAI.md, MUGAM.md) give Claude Code a persistent sense of who you are and what you are working on.
- A SQLite knowledge graph at `~/.claude/knowledge/graph.db` stores entities + relationships with FTS5 search.
- The `graph-recall` hook (UserPromptSubmit) matches entity names in your prompts and injects their context inline, before the model answers.
- The `graph-update` hook (Stop) enqueues finished sessions for background processing by a launchd-managed watchdog.
- The `agam bootstrap` command seeds the graph from your existing Claude Code transcripts in `~/.claude/projects/`.

No Anthropic API key is needed or supported. Every LLM call Agam makes runs inside your existing claude-code devcontainer via `docker exec`, reusing the OAuth credentials you already authenticated with.

## Prerequisites

- macOS. Only platform supported in v1.
- [Claude Code](https://claude.ai/code) installed, and you have run `claude` interactively at least once so that `~/.claude/.credentials.json` exists.
- [uv](https://docs.astral.sh/uv/) for Python execution.
- Python 3.11 or newer (uv will fetch one if you do not have it).
- Optional but strongly recommended: Docker Desktop with a running claude-code devcontainer. The bootstrap pipeline and the background watchdog both shell out to `docker exec`. The identity files and the `graph-recall` hook work without Docker, so you can install Agam on a machine where Docker is not ready yet.

The installer verifies every required prerequisite and bails with a useful error if something is missing.

## Install

```bash
git clone <repo-url> ~/coding/agam
cd ~/coding/agam
./install.sh
```

`install.sh` does the minimum:

1. Checks for `uv`, `claude`, Docker (warns if absent), macOS, and `~/.claude/.credentials.json`.
2. Runs `uv sync` to materialize the Python environment.
3. Delegates to `uv run agam init`, which is the real installer.

`agam init` is an interactive [questionary](https://github.com/tmbo/questionary) wizard. It asks a few questions (name, primary goal, projects directory, container mode), then:

- Renders `templates/*.template` into `~/.claude/agam/` (AGAM.md, THISAI.md, MUGAM.md, CLAUDE.md snippet).
- Merges Agam hooks into `~/.claude/settings.json` (creating a timestamped backup of the existing file first).
- Writes the watchdog launchd plist to `~/Library/LaunchAgents/com.agam.watchdog.plist` and loads it.
- Creates `~/.claude/knowledge/graph.db` with the FTS5 schema if it does not exist.

Re-running the installer is safe. By default it refuses to overwrite an existing `~/.claude/agam/`. Pass `--force` to overwrite with a timestamped backup of the previous install:

```bash
uv run agam init --force
```

You can also drive the wizard non-interactively by feeding it a YAML answer file:

```bash
uv run agam init --answers my-answers.yaml
```

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

Written into `~/.claude/agam/config.yaml` by the installer:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Your preferred name, used inside identity files. |
| `primary-goal` | string | One-line direction that anchors THISAI.md. |
| `projects-dir` | path | Where your code lives. Used by boot context injection. |
| `platform` | string | `macos` for v1. |
| `container-mode` | string | `docker` (default) or `host`. Controls how LLM calls are executed. |

Edit this file directly and the next session picks up the changes. Re-run `agam init --force` if you want the wizard to regenerate everything from templates.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGAM_CONTAINER_PATTERN` | `claude-code\|claude-code` | Regex matched against `docker ps` rows to discover your claude-code container. |
| `AGAM_CONTAINER_NAME` | unset | Exact container name. Overrides the regex above. |
| `AGAM_WATCHDOG_MODE` | `container` | `container` runs LLM calls via `docker exec`. `host` runs them directly on macOS. See below. |
| `AGAM_HOME` | `~/.claude/agam` | Root of identity + log directories. |
| `AGAM_KG_PATH` | `~/.claude/knowledge/graph.db` | Path to the SQLite graph. |
| `AGAM_PROMPTS_DIR` | bundled | Directory holding bootstrap prompt templates. |

You will rarely need the last three. They exist for tests and for contributors running Agam out of a non-default layout.

## Runtime model

Agam assumes you already run Claude Code inside a devcontainer that bind-mounts your host `~/.claude/` directory. That is how OAuth credentials reach the container, and how Agam's `docker exec claude -p` calls pick them up for free.

Concretely:

- The watchdog launchd agent runs on the macOS host.
- When a session closes, the Stop hook writes an entry into `~/.claude/.agam-queue/`.
- The watchdog picks up queued entries, launches `docker exec <container> claude -p ...` against the running claude-code container, and processes the session (work-log entry, Agam sync, graph update).
- If no container is running, the job stays queued. `agam status` reports the situation and `~/.claude/agam/logs/watchdog.log` gets a `no-container` line.
- When you start the container again, the next watchdog tick drains the queue.

Agam never takes an Anthropic API key. If you need one, you are using the wrong tool; use the SDK directly.

## Host-mode fallback

If you do not run Claude Code in a devcontainer, you can switch the watchdog to host mode:

```bash
export AGAM_WATCHDOG_MODE=host
```

This runs `claude -p` directly on the macOS host instead of going through `docker exec`. It requires:

- `claude` on your `PATH` at the user level (not just inside the container).
- `~/.claude/.credentials.json` already set up on the host.

Host mode is documented for debugging and for contributors who cannot run a devcontainer. The supported path is container mode. Expect host mode to move slower as Agam evolves.

## Troubleshooting

Start with:

```bash
agam status
```

That prints the Agam home path, knowledge graph size, queue depth, bootstrap resume state, and the detected container name (if any). It does not touch the graph or the queue, so it is always safe to run.

Common situations:

- **`Container: (none detected)`** -- your claude-code devcontainer is not running. Start it, then re-run `agam status`. If detection still fails and you have a custom name, set `AGAM_CONTAINER_NAME` to the exact name shown by `docker ps`.
- **`no-container` lines in `~/.claude/agam/logs/watchdog.log`** -- expected whenever the container is down. The queue will drain on the next tick after you start the container.
- **Queue stuck / entries in `~/.claude/agam/queue-errors/`** -- a session failed processing. Open the error payload to see the underlying exception. Logs:
  ```bash
  tail -n 100 ~/.claude/agam/logs/watchdog.log
  ls ~/.claude/agam/queue-errors/
  ```
- **`ERR: no claude-code container running` from `agam bootstrap`** -- same fix as above. Start the container and re-run; bootstrap resumes automatically.
- **`graph-recall` is not injecting anything** -- first confirm the hook is in `~/.claude/settings.json` under `hooks.UserPromptSubmit`. Then confirm the graph has entities: `sqlite3 ~/.claude/knowledge/graph.db 'select count(*) from entities;'`. A freshly installed Agam with no bootstrap run is empty; that is the most common cause.

## Uninstall

Back up anything you want to keep first:

```bash
cp -r ~/.claude/agam ~/agam-backup-$(date +%Y%m%d)
cp ~/.claude/knowledge/graph.db ~/graph-backup-$(date +%Y%m%d).db
```

Then tear down:

```bash
# Stop + remove the watchdog
launchctl unload ~/Library/LaunchAgents/com.agam.watchdog.plist
rm ~/Library/LaunchAgents/com.agam.watchdog.plist

# Remove Agam hooks from settings.json
# Open ~/.claude/settings.json in your editor and delete the entries
# whose command paths reference agam or ~/.claude/agam/.

# Delete identity files + knowledge graph if you want a clean slate
rm -rf ~/.claude/agam
rm -f ~/.claude/knowledge/graph.db
```

The repo at `~/coding/agam` is independent; remove it separately if you no longer want the source.

## Subcommand reference

| Command | Purpose |
|---|---|
| `agam init` | Install Agam scaffolding into `~/.claude/`. Use `--force` to overwrite, `--answers FILE.yaml` to script. |
| `agam bootstrap` | Scan transcripts, estimate cost, extract + reconcile into the knowledge graph. Resumable. |
| `agam status` | Print install health: paths, graph size, queue depth, container detection, resume state. |
| `agam reset` | Remove bootstrap scratch state (`~/.claude/.agam-bootstrap-state.json` and candidates). Dry-run by default; pass `--confirm` to actually delete. Never touches identity files or the graph. |

## Project layout

```
agam/
  install.sh              -- macOS host installer. Thin wrapper over agam init.
  src/agam/
    cli.py                -- argparse entrypoint (init, bootstrap, status, reset).
    installer.py          -- questionary wizard + settings merge.
    bootstrap.py          -- scan, extract (Haiku), reconcile (Sonnet), durable state.
    settings_merger.py    -- safe merge of Agam hooks into ~/.claude/settings.json.
    tools/                -- Python helpers shipped to ~/.claude/tools/.
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

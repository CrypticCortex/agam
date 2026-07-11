# Codex integration

> Status: implemented in the local OSS runtime. This document describes the current
> local architecture and operating boundary; it does not specify an enterprise
> federation or transport layer.

## Architecture at a glance

Agam treats Codex as another edge connected to the same local brain as Claude Code and
Cursor. Agent-specific files only adapt lifecycle events; durable identity, knowledge,
queue state, prompts, and watchdog code live under the neutral data home.

```text
Claude Code hooks ----\
Cursor hooks/rules ----+--> ~/.agam/queue --> watchdog --> ~/.agam/knowledge/graph.db
Codex hooks ----------/           ^                         |
                                  +------ prompt recall <---+
```

The default shared layout is:

```text
~/.agam/
  AGAM.md, THISAI.md, MUGAM.md, config.yaml
  knowledge/graph.db
  prompts/
  hooks/
  tools/agam/
  queue/             pending enrichment generations
  processing/        atomically claimed work
  processed/         completed queue entries
  queue-errors/      entries that exhausted retries
  transcripts/codex/ normalized immutable snapshots
  logs/
```

`AGAM_DATA_HOME`, `AGAM_HOME`, `AGAM_KG_PATH`, and the other documented path variables
can override these defaults. The normal multi-agent install uses `~/.agam` so switching
agents does not fork memory.

## Per-agent wiring

The installer maintains a small adapter in each selected agent's own configuration:

| Agent | Wiring | Durable brain |
|---|---|---|
| Claude Code | Agam hook commands merged into `~/.claude/settings.json`; helpers under `~/.claude/hooks` and `~/.claude/tools/agam` | `~/.agam` |
| Cursor | Stop/session-end commands merged into `~/.cursor/hooks.json`; helpers under `~/.cursor/hooks` and `~/.cursor/tools/agam`; recall is refreshed into Cursor's rule digest | `~/.agam` |
| Codex | Lifecycle commands merged into `~/.codex/hooks.json`; Agam-owned scripts under `~/.codex/hooks/agam` and helpers under `~/.codex/tools/agam` | `~/.agam` |

For Codex, Agam uses the client's official lifecycle hook interface as follows:

| Codex event | Agam handler | Purpose |
|---|---|---|
| `UserPromptSubmit` | `graph_recall.py` | Query the graph and return relevant context through `additionalContext`. |
| `Stop` | `codex_stop.py` | Gate, normalize, snapshot, and enqueue a substantive rollout. |
| `PreToolUse` (`Bash`, `Edit|Write`) | `lesson_activate.py` | Activate lessons that match a command or edited path. |
| `PostToolUse` (`Bash`) | `lesson_activate_post.py` | Activate lessons that match command failures and output. |

The merger is idempotent and writes `hooks.json` atomically. It preserves unrelated user
configuration and deduplicates Agam handlers. The `hooks/agam` namespace is the ownership
boundary: Agam never writes generic files such as `~/.codex/hooks/graph_recall.py`, even
if a user has files with the same names. Commands are absolute, guarded for a missing
script, and carry an absolute tools directory.

## Codex Stop flow and privacy boundary

Codex's rollout JSONL is treated as an evolving private format. `codex_stop.py` receives
Codex's `session_id`, `transcript_path`, and `cwd`, but the background worker is not given
the original rollout path. A session is enqueued only when the adapter sees all three of:

- at least six canonical user submissions;
- credible edit evidence (a successful patch-completion record when available, or a
  recognized edit/apply-patch call); and
- a real-work signal near the end of the visible conversation.

For a qualifying Stop, Agam produces a compact, Claude-like JSONL generation at
`~/.agam/transcripts/codex/<session>-<timestamp>-<uuid>.jsonl`. Normalization retains
visible user/assistant text, bounded tool inputs/results, and successful edit paths. It
drops reasoning and encrypted content, images, system/developer/world-state records,
token accounting, and other internal metadata. Ambiguous Codex `response_item` records
with `role=user` are also excluded because they can represent environment setup or
delegated sub-agent prompts rather than a visible user submission.

Every snapshot filename is generation-specific, the file is written atomically, and an
older generation is never rewritten while a worker may be reading it. This is a data
minimization boundary, not content redaction or encryption: visible conversation text
and bounded tool data can still contain sensitive material. Pending, processing, retry,
and dead-letter references protect their snapshots from cleanup. Completed or superseded
generations are retained as a two-snapshot recent tail per session; older snapshot files
are removed after their processed audit records are atomically marked as pruned. Cleanup
is fail-open, so lock or filesystem uncertainty defers pruning to a later Stop instead of
risking live work.

Hook failures are fail-open for the coding session. Malformed JSON, rollout schema drift,
or local filesystem errors can skip an enrichment pass, but must not block Codex Stop.

## Queue concurrency and recovery

Pending entries use an atomic file-per-session replace in `~/.agam/queue`. Before work,
the watchdog takes a single-flight lock and atomically moves the exact pending file into a
unique `processing/claim.*` directory. A new Stop can then create a newer pending entry
for the same session without the current worker archiving or deleting it.

Successful claims move to `processed/`. Failures are retried under generation-specific
names and eventually move to `queue-errors/`; a claim left by an interrupted watchdog is
recovered to the queue on the next run. Drains are oldest-first and bounded per tick.
This design protects both the immutable transcript generation and a concurrently
re-enqueued session generation.

## Background enrichment with Codex

The host watchdog chooses an authenticated enrichment CLI in the established order
Claude, Cursor Agent, then Codex, unless `AGAM_LLM_CLI_PIN` selects one explicitly. When
Codex is selected it runs a noninteractive turn equivalent to:

```text
codex exec --ephemeral --disable hooks --sandbox read-only \
  --skip-git-repo-check --color never --cd ~/.agam \
  --output-schema <temporary-schema.json> -
```

The prompt is supplied on standard input. `--disable hooks` prevents the watchdog's own
Codex turn from recursively enqueueing itself; `--ephemeral` avoids a persistent Codex
session; and the read-only sandbox keeps model execution separate from mutation. Strict
JSON Schemas constrain the work-log body and knowledge proposals. Agam validates stdout,
materializes temporary files mechanically, and then uses its existing deterministic
append/apply pipeline to update local state.

On macOS, the installer records the CLI kind and, when it can resolve it at install time,
the absolute executable in the launchd plist. This matters because launchd's restricted
`PATH` often cannot discover CLIs installed through npm, nvm, asdf, editor extensions, or
other user toolchains. A stale absolute path falls back to the normal probe cascade rather
than being executed.

## TUI representation

The TUI detects wiring, not mere application presence. A wire appears only when Agam's
owned hook files exist or the relevant agent configuration references them. Claude,
Cursor, and Codex each get a distinct animated wire feeding the same brain; provenance
counts in the footer use the same three sources. Therefore an installed Codex CLI alone
does not create a Codex wire: run the Codex target install first and restart the TUI.

## Migration and compatibility

Older Agam versions stored identity and the graph below `~/.claude`. On install, if the
neutral graph does not yet exist, Agam copies the legacy knowledge and identity data into
`~/.agam` and leaves the legacy source untouched. A queue-only `~/.agam` directory does
not suppress this migration. Migrated untagged graph entities are backfilled with Claude
provenance.

Existing path environment variables and legacy Claude hook filenames remain readable so
an upgrade can be rolled out without losing memory. New agent activity writes to the
neutral graph with its own provenance. Re-running install refreshes code and prompts but
does not replace an existing shared graph or edited identity files.

## Install, trust, and uninstall

Install only the Codex adapter, or combine it with other targets:

```bash
agam init --target codex
agam init --target claude --target cursor --target codex
```

After installation, open `/hooks` in Codex and review/trust the Agam command hooks if the
client marks them as awaiting trust. Trust should be based on the absolute commands under
`~/.codex/hooks/agam`; installing Agam does not imply that arbitrary commands elsewhere
in `~/.codex/hooks.json` are owned or endorsed by Agam. `agam doctor` checks that the
selected wiring, shared graph, and available invoker are coherent.

Uninstall is target-aware:

```bash
agam uninstall --target codex            # dry run
agam uninstall --target codex --confirm
```

It removes Agam's namespaced Codex scripts/tools and only Agam-owned entries from
`hooks.json`. Other Codex hooks are preserved. The shared brain and launchd job remain
while another Agam-wired agent is installed; removing the last target soft-moves shared
data by default. `--purge` is the explicit destructive option.

## Verification boundary

The implementation is covered by repository tests for agent detection and idempotent
installation, non-destructive/atomic hook merging, Codex rollout parsing and privacy
filters, immutable Stop snapshots, concurrent enqueue/claim/retry behavior, structured
noninteractive Codex invocation, launchd absolute-path handling, migration, CLI
install/uninstall/doctor behavior, and TUI wiring/provenance.

Those tests use fixtures and fake CLIs. They do not guarantee compatibility with every
future Codex rollout schema or client release, authenticate a user's Codex account, test
the actual model/network service, approve local hooks, or establish OS-level isolation
beyond the requested Codex sandbox. A release check should therefore include the full
repository suite plus one manual smoke test with the installed Codex version: install,
review `/hooks`, submit a recall prompt, perform a substantive edit session, observe the
queue drain, and confirm the Codex wire and provenance in `agam tui`.

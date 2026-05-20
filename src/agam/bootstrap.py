"""Bootstrap pipeline primitives for Agam.

This module feeds Claude Code session transcripts (JSONL files under
``~/.claude/projects/<path-slug>/<session-id>.jsonl``) through an
extraction + reconciliation pipeline to seed the knowledge graph.

Task 19 primitives (pre-LLM):

- ``scan_transcripts`` -- enumerate candidate JSONL files, filtered by mtime.
- ``estimate_cost`` -- rough USD estimate for a Haiku extraction + Sonnet
  reconciliation sweep across N tokens.
- ``count_tokens_in_file`` -- ~4-chars-per-token heuristic. Intentionally
  avoids a real tokenizer dependency; we are estimating, not metering.

Task 20 adds the Haiku extraction pass:

- ``_discover_container`` -- find a running claude-code devcontainer.
- ``_run_claude`` -- shared helper that shells out to
  ``docker exec <container> claude -p``. Task 21 (Sonnet reconciliation)
  reuses this same helper -- only the ``model`` arg differs.
- ``extract_from_transcript`` -- chunk a JSONL transcript, prompt Haiku
  for entities + relationships, parse the stream-json output.
- ``extract_all`` -- thread-pool fan-out across many transcripts.

LLM calls go through ``docker exec`` intentionally. Users already
authenticate via ``~/.claude/.credentials.json`` (OAuth managed by the
Claude Code CLI); we do NOT take an Anthropic API key.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


def scan_transcripts(
    projects_dir: Path, days: int | None = 30
) -> list[Path]:
    """Walk ``projects_dir`` recursively for non-empty ``*.jsonl`` files.

    Args:
        projects_dir: Root of the Claude Code projects tree (typically
            ``~/.claude/projects/``). Each subdir is one project slug and
            contains one JSONL per session.
        days: Only return files modified within the last ``days`` days.
            ``None`` means no age filter -- include everything.

    Returns:
        Sorted list of matching paths. Sorting gives deterministic order
        for resume/state tracking downstream.

    Missing ``projects_dir`` is handled gracefully: returns an empty list
    and logs a warning to stderr. Zero-byte files are skipped. Validation
    of JSONL contents is deferred to Task 20 (the extractor).
    """
    if not projects_dir.exists() or not projects_dir.is_dir():
        print(
            f"[agam.bootstrap] scan_transcripts: {projects_dir} does not exist; "
            f"returning empty list.",
            file=sys.stderr,
        )
        return []

    cutoff = None if days is None else time.time() - days * 86400
    matches: list[Path] = []

    for path in projects_dir.rglob("*.jsonl"):
        if not path.is_file():
            continue
        try:
            if os.path.getsize(path) == 0:
                continue
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
        except OSError:
            # File vanished between rglob and stat; skip silently.
            continue
        matches.append(path)

    return sorted(matches)


def estimate_cost(
    total_tokens: int,
    haiku_rate: float = 0.80,
    sonnet_input_rate: float = 3.00,
    reconcile_fraction: float = 0.1,
) -> float:
    """Estimate USD cost of a full bootstrap sweep over ``total_tokens``.

    Two cost components:

    1. Haiku extraction across every token: ``total_tokens / 1M * haiku_rate``.
    2. Sonnet reconciliation over a sampled fraction:
       ``total_tokens * reconcile_fraction / 1M * sonnet_input_rate``.

    Rates are USD per 1M tokens -- the industry convention as of 2026.

    Args:
        total_tokens: Aggregate token count across all transcripts.
        haiku_rate: USD per 1M Haiku input tokens.
        sonnet_input_rate: USD per 1M Sonnet input tokens.
        reconcile_fraction: Fraction of tokens routed to Sonnet for
            reconciliation (0.0 - 1.0).

    Returns:
        Estimated total cost in USD.
    """
    haiku_cost = total_tokens / 1_000_000 * haiku_rate
    sonnet_cost = (
        total_tokens * reconcile_fraction / 1_000_000 * sonnet_input_rate
    )
    return haiku_cost + sonnet_cost


def count_tokens_in_file(path: Path) -> int:
    """Rough token count for ``path`` using a 4-chars-per-token heuristic.

    This deliberately avoids depending on a tokenizer. Downstream code uses
    the result only to size a budget, not to meter billing. Missing or
    unreadable files return 0.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return 0
    return len(text) // 4


# ---- LLM plumbing --------------------------------------------------------


_DEFAULT_CONTAINER_PATTERN = "claude-code"


def _discover_container() -> str | None:
    """Return the name of a running claude-code devcontainer, or ``None``.

    Two selection modes:

    1. ``AGAM_CONTAINER_NAME`` -- explicit override. We only verify the
       named container is actually running; no pattern matching.
    2. Otherwise match ``docker ps`` rows against ``AGAM_CONTAINER_PATTERN``
       (default: ``claude-code``) on ``"<name> <image>"``.

    Any ``subprocess`` failure (``docker`` not installed, daemon down)
    surfaces as ``None`` so callers can emit a single clean error.
    """
    pattern = os.environ.get("AGAM_CONTAINER_PATTERN", _DEFAULT_CONTAINER_PATTERN)
    override = os.environ.get("AGAM_CONTAINER_NAME", "")

    try:
        if override:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            return override if override in r.stdout.split() else None

        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Image}}"],
            capture_output=True,
            text=True,
        )
        for line in r.stdout.splitlines():
            if re.search(pattern, line, re.I):
                return line.split()[0]
        return None
    except (FileNotFoundError, OSError):
        return None


def _run_claude(prompt: str, model: str, *, timeout: int = 300) -> str:
    """Run ``claude -p`` and return raw stdout.

    This is the single choke point for every LLM call Agam makes. Task 21
    reuses it for Sonnet reconciliation. Anthropic SDK is deliberately NOT
    imported; the user's existing ``~/.claude/.credentials.json`` OAuth is
    what authorizes the call.

    Two execution modes, selected by ``AGAM_BOOTSTRAP_MODE``:
      - ``container`` (default): ``docker exec`` into the user's running
        claude-code devcontainer.
      - ``host``: invoke ``claude`` directly on PATH. Used when bootstrap
        itself already runs inside a container (E2E tests) or when the
        user's host has the Claude Code CLI installed directly.

    Raises:
        SystemExit: container mode and no claude-code container running,
            or host mode and ``claude`` not on PATH.
        RuntimeError: ``claude -p`` exited non-zero.
    """
    mode = os.environ.get("AGAM_BOOTSTRAP_MODE", "container")
    if mode == "host":
        argv = [
            "claude",
            "-p",
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    else:
        container = _discover_container()
        if not container:
            sys.exit(
                "ERR: no claude-code container running. Start your devcontainer "
                "and re-run `agam bootstrap`."
            )
        argv = [
            "docker",
            "exec",
            "-i",
            container,
            "claude",
            "-p",
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    r = subprocess.run(
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={r.returncode}): {r.stderr[:500]}"
        )
    return r.stdout


# ---- Extraction ----------------------------------------------------------


_EXTRACTION_PROMPT_TEMPLATE = """\
You are extracting structured knowledge from a Claude Code session transcript.

Read the transcript chunk below. Return ONLY a single JSON object with two
keys: "entities" and "relationships". No prose, no markdown fences.

Schema:
{{
  "entities":      [{{"name": "...", "type": "...", "description": "..."}}],
  "relationships": [{{"source": "...", "relation": "...", "target": "..."}}]
}}

Entity types: project, service, bug, pattern, goal, person, decision, lesson.
Relations: uses, depends-on, caused-by, relates-to, owns, fixes, affects.

If nothing is worth extracting, return {{"entities": [], "relationships": []}}.

Transcript chunk:
---
{chunk}
---
"""


def _chunk_text(text: str, chunk_tokens: int) -> list[str]:
    """Split ``text`` into chunks of ~``chunk_tokens`` tokens each.

    Uses the 4-chars-per-token heuristic from ``count_tokens_in_file``.
    A short text (single chunk) is returned as-is. This is deliberately
    simple; real tokenizer-aware splitting is a future optimization.
    """
    chunk_chars = max(1, chunk_tokens * 4)
    if len(text) <= chunk_chars:
        return [text]
    return [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]


def _load_transcript_text(path: Path) -> str:
    """Load a JSONL transcript, skipping malformed lines with a stderr warning.

    Each line is parsed to validate it's JSON, then the raw line is kept for
    inclusion in the prompt. That way the model sees the original shape,
    including tool uses and roles, rather than a lossy reconstruction.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(
            f"[agam.bootstrap] extract: cannot read {path}: {exc}",
            file=sys.stderr,
        )
        return ""

    good_lines: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            print(
                f"[agam.bootstrap] extract: skipping malformed JSONL "
                f"at {path}:{lineno}",
                file=sys.stderr,
            )
            continue
        good_lines.append(line)
    return "\n".join(good_lines)


def _parse_stream_json(stdout: str) -> dict | None:
    """Parse ``claude -p --output-format stream-json`` output.

    Strategy: walk the lines, prefer the terminal ``type == "result"`` event
    and parse its ``result`` field as JSON. If that fails, fall back to any
    line whose parsed JSON looks like ``{"entities": [...]}``.

    TODO(2026): pin the exact stream-json schema once the CLI stabilizes.
    For now we're defensive about shape drift.
    """
    result_payload: str | None = None
    entity_fallback: dict | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result":
            # Common shape: {"type":"result","result":"<json string>"}.
            # Also tolerate {"result": {...}} or {"content": "..."}.
            r = obj.get("result")
            if isinstance(r, str):
                result_payload = r
            elif isinstance(r, dict):
                return r
            else:
                c = obj.get("content")
                if isinstance(c, str):
                    result_payload = c
        elif entity_fallback is None and "entities" in obj:
            entity_fallback = obj

    if result_payload is not None:
        try:
            return json.loads(result_payload)
        except json.JSONDecodeError:
            # Try to recover an embedded JSON object.
            m = re.search(r"\{.*\}", result_payload, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
            return None

    return entity_fallback


def _candidates_from_payload(
    payload: dict | None, source: Path
) -> list[dict]:
    """Flatten a model payload into a list of candidate dicts.

    Each candidate carries ``kind`` ("entity" or "relationship"), the
    fields from the model, and a ``source`` path for provenance.
    """
    if not payload:
        return []

    out: list[dict] = []
    for ent in payload.get("entities", []) or []:
        if not isinstance(ent, dict):
            continue
        out.append({"kind": "entity", "source": str(source), **ent})
    for rel in payload.get("relationships", []) or []:
        if not isinstance(rel, dict):
            continue
        out.append({"kind": "relationship", "source_transcript": str(source), **rel})
    return out


def extract_from_transcript(
    transcript_path: Path,
    model: str = "haiku",
    chunk_tokens: int = 50_000,
) -> list[dict]:
    """Extract entity + relationship candidates from one transcript.

    Args:
        transcript_path: JSONL session transcript.
        model: Claude model slug passed to ``claude -p --model``.
        chunk_tokens: Maximum tokens per prompt. Transcripts longer than
            this are split into multiple chunks and called sequentially.

    Returns:
        Flat list of candidate dicts. Empty list is a valid result
        (model declined to extract anything).
    """
    text = _load_transcript_text(transcript_path)
    if not text:
        return []

    chunks = _chunk_text(text, chunk_tokens)
    candidates: list[dict] = []

    for chunk in chunks:
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(chunk=chunk)
        try:
            stdout = _run_claude(prompt, model)
        except RuntimeError as exc:
            print(
                f"[agam.bootstrap] extract: _run_claude failed for "
                f"{transcript_path}: {exc}",
                file=sys.stderr,
            )
            continue
        payload = _parse_stream_json(stdout)
        chunk_candidates = _candidates_from_payload(payload, transcript_path)
        if not chunk_candidates:
            print(
                f"[agam.bootstrap] extract: zero candidates from chunk of "
                f"{transcript_path}",
                file=sys.stderr,
            )
        candidates.extend(chunk_candidates)

    return candidates


def extract_all(
    transcripts: list[Path],
    model: str = "haiku",
    max_workers: int = 4,
) -> list[dict]:
    """Run ``extract_from_transcript`` across many files in parallel.

    Uses a thread pool because the bottleneck is the model server, not local
    CPU; ``docker exec`` tolerates concurrent sessions fine.
    """
    if not transcripts:
        return []

    all_candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(extract_from_transcript, t, model): t for t in transcripts
        }
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            try:
                all_candidates.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 -- surface + continue
                print(
                    f"[agam.bootstrap] extract_all: {t} raised: {exc}",
                    file=sys.stderr,
                )
    return all_candidates


# ---- Reconciliation -----------------------------------------------------


_RECONCILE_PROMPT_TEMPLATE = """\
You are reconciling entity + relationship candidates extracted from many
Claude Code session transcripts. Merge duplicates, resolve name variants
(different capitalizations, abbreviations), union properties, and return a
clean JSON object.

Return ONLY a single JSON object with two keys: "entities" and
"relationships". No prose, no markdown fences.

Schema:
{{
  "entities":      [{{"name": "...", "type": "...", "description": "..."}}],
  "relationships": [{{"source": "...", "relation": "...", "target": "..."}}]
}}

Candidates:
{candidates}
"""

_RECONCILE_STRICT_SUFFIX = (
    "\n\nRETURN ONLY VALID JSON. NO PROSE. NO CODE FENCES. "
    "NO LEADING OR TRAILING TEXT."
)


def _dedupe_entities(entities: list[dict]) -> list[dict]:
    """Collapse entities that share a case-insensitive ``name``.

    Properties are unioned across variants (later variants win on conflict for
    scalar fields, but ``props`` dicts are merged key-by-key). The first
    variant's ``name`` is preserved verbatim so casing is stable for the
    prompt.
    """
    merged: dict[str, dict] = {}
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        name = ent.get("name")
        if not isinstance(name, str) or not name:
            continue
        key = name.lower()
        if key not in merged:
            merged[key] = {k: v for k, v in ent.items()}
            continue
        existing = merged[key]
        for k, v in ent.items():
            if k == "name":
                continue
            if k == "props" and isinstance(v, dict):
                existing_props = existing.get("props") or {}
                if not isinstance(existing_props, dict):
                    existing_props = {}
                merged_props = {**existing_props, **v}
                existing["props"] = merged_props
            elif k not in existing or not existing.get(k):
                existing[k] = v
    return list(merged.values())


def _dedupe_relationships(relationships: list[dict]) -> list[dict]:
    """Collapse relationships with identical ``(source, relation, target)``."""
    seen: dict[tuple, dict] = {}
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        key = (rel.get("source"), rel.get("relation"), rel.get("target"))
        if None in key:
            continue
        if key not in seen:
            seen[key] = {k: v for k, v in rel.items()}
    return list(seen.values())


def _regroup_by_kind(candidates: list[dict]) -> dict:
    """Split a flat ``[{kind, ...}]`` list into ``{entities, relationships}``.

    Candidate tags beyond ``entity`` / ``relationship`` are silently dropped
    rather than raised. The extraction pass only ever emits those two kinds.
    """
    entities: list[dict] = []
    relationships: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        kind = c.get("kind")
        if kind == "entity":
            # Entities carry ``source`` as transcript provenance; drop it so
            # the reconciler sees only model fields.
            trimmed = {k: v for k, v in c.items() if k not in ("kind", "source")}
            entities.append(trimmed)
        elif kind == "relationship":
            # Relationships keep their own ``source`` (subject of the
            # relation); ``source_transcript`` is the provenance.
            trimmed = {k: v for k, v in c.items() if k not in ("kind", "source_transcript")}
            relationships.append(trimmed)
    return {"entities": entities, "relationships": relationships}


def _parse_reconciliation_response(raw: str) -> dict:
    """Parse ``claude -p --output-format stream-json`` output for Sonnet.

    Raises:
        json.JSONDecodeError: when no parseable JSON object can be recovered
            from the output. The retry loop in ``reconcile_candidates``
            catches this to decide whether to re-prompt.
    """
    payload = _parse_stream_json(raw)
    if isinstance(payload, dict) and (
        "entities" in payload or "relationships" in payload
    ):
        return {
            "entities": payload.get("entities") or [],
            "relationships": payload.get("relationships") or [],
        }
    raise json.JSONDecodeError(
        "no reconciliation JSON in stream-json output", raw or "", 0
    )


def _default_candidates_path() -> Path:
    return Path(os.path.expanduser("~/.claude/.agam-bootstrap-candidates.json"))


def reconcile_candidates(
    candidates: list[dict],
    model: str = "sonnet",
    candidates_path: Path | None = None,
) -> dict:
    """Merge extracted candidates into a single reconciled KG payload.

    Flow:

    1. Regroup the flat candidate list by ``kind``.
    2. Dedupe client-side to keep the Sonnet prompt small.
    3. Call ``_run_claude`` with ``model`` (default Sonnet) and a 600s timeout.
    4. On ``json.JSONDecodeError``, retry once with a stricter JSON-only
       suffix appended to the prompt.
    5. On a second failure, write the deduped candidates to
       ``candidates_path`` (default ``~/.claude/.agam-bootstrap-candidates.json``)
       and raise ``SystemExit`` with an actionable message.

    Returns:
        ``{"entities": [...], "relationships": [...]}`` with keys in that
        order regardless of the model's ordering.
    """
    grouped = _regroup_by_kind(candidates)
    deduped = {
        "entities": _dedupe_entities(grouped["entities"]),
        "relationships": _dedupe_relationships(grouped["relationships"]),
    }

    save_path = candidates_path or _default_candidates_path()
    base_prompt = _RECONCILE_PROMPT_TEMPLATE.format(
        candidates=json.dumps(deduped, ensure_ascii=False, indent=2)
    )

    attempts = [base_prompt, base_prompt + _RECONCILE_STRICT_SUFFIX]
    last_error: Exception | None = None
    for prompt in attempts:
        try:
            stdout = _run_claude(prompt, model, timeout=600)
        except RuntimeError as exc:
            last_error = exc
            continue
        try:
            return _parse_reconciliation_response(stdout)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    # Both attempts failed. Persist deduped candidates so a later pass can
    # resume without re-running Haiku extraction.
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(
            json.dumps(deduped, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"[agam.bootstrap] reconcile: could not save candidates to "
            f"{save_path}: {exc}",
            file=sys.stderr,
        )

    detail = f" ({last_error})" if last_error else ""
    sys.exit(
        f"ERR: reconciliation failed twice{detail}. Candidates saved to "
        f"{save_path}."
    )


# ---- Orchestration (Task 22) --------------------------------------------


def _default_state_path() -> Path:
    """Default location for the durable bootstrap state file.

    Computed at call time so ``monkeypatch.setenv("HOME", ...)`` works in
    tests without needing explicit injection.
    """
    return Path(os.path.expanduser("~/.claude/.agam-bootstrap-state.json"))


def _load_state(path: Path) -> dict | None:
    """Return parsed state from ``path``, or ``None`` if missing/malformed.

    A malformed state file is treated identically to an absent one: we log
    a warning to stderr and tell the caller to start fresh. Never raises.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError as exc:
        print(
            f"[agam.bootstrap] run_bootstrap: cannot read state {path}: {exc}",
            file=sys.stderr,
        )
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"[agam.bootstrap] run_bootstrap: state file {path} is malformed "
            f"({exc}); starting clean.",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save_state(path: Path, state: dict) -> None:
    """Atomically write ``state`` to ``path``.

    Writes to a tempfile in the same directory, fsyncs, then ``os.replace``.
    Same-dir tempfile guarantees the rename is atomic on POSIX (same
    filesystem). We clean up the tempfile on any error so we never leave
    stray ``.tmp`` files behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup; re-raise the original error for the caller.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _notify(message: str) -> None:
    """Fire a macOS notification via ``osascript``. Silent on failure.

    Missing ``osascript`` (e.g. running inside a Linux devcontainer) should
    not abort the bootstrap. Callers can still pass an explicit ``notify_fn``
    if they want different behavior.
    """
    safe = message.replace('"', "'")
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe}" with title "Agam"',
            ],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        # osascript not available or timed out; swallow.
        return


def _install_sigint_handler(flush_fn: Callable[[], None]):
    """Install a SIGINT handler that calls ``flush_fn`` then re-raises.

    Returns the previous handler so the caller can restore it in a
    ``finally`` block. The re-raise path uses Python's default SIGINT
    semantics -- ``KeyboardInterrupt`` -- so existing ``try/except`` logic
    stays well-behaved.
    """
    previous = signal.getsignal(signal.SIGINT)

    def handler(signum, frame):
        try:
            flush_fn()
        except Exception as exc:  # noqa: BLE001 -- best-effort flush
            print(
                f"[agam.bootstrap] SIGINT: flush_fn raised: {exc}",
                file=sys.stderr,
            )
        # Convert to KeyboardInterrupt so callers see the standard
        # Python cancellation exception.
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, handler)
    return previous


def run_bootstrap(
    projects_dir: Path,
    days: int = 30,
    *,
    model_haiku: str = "haiku",
    model_sonnet: str = "sonnet",
    state_path: Path | None = None,
    candidates_path: Path | None = None,
    notify_fn: Callable[[str], None] | None = None,
    resume: bool = True,
) -> dict:
    """Run the end-to-end bootstrap: scan + extract + reconcile, durably.

    Processes one transcript at a time so SIGINT or a mid-run crash loses
    at most one transcript's worth of work. After every transcript the
    state file is atomically rewritten, so a later ``resume=True`` run
    picks up exactly where this one left off.

    Args:
        projects_dir: Root of the Claude Code projects tree.
        days: mtime filter (see ``scan_transcripts``).
        model_haiku: Extraction model.
        model_sonnet: Reconciliation model.
        state_path: Override for the durable state file. Defaults to
            ``~/.claude/.agam-bootstrap-state.json``.
        candidates_path: Forwarded to ``reconcile_candidates`` for its
            double-failure fallback write.
        notify_fn: Callable invoked at 50% progress and on completion.
            Defaults to the macOS ``osascript`` notifier; tests inject a
            list-appender.
        resume: When true (default), consults the state file to skip
            already-processed transcripts. Set false to force a clean run.

    Returns:
        ``{"entities": [...], "relationships": [...]}`` from the Sonnet
        reconciliation pass. Zero transcripts -> empty result, no calls
        to extract / reconcile / notify.
    """
    state_path = state_path or _default_state_path()
    notifier = notify_fn if notify_fn is not None else _notify

    processed: list[str] = []
    candidates: list[dict] = []
    if resume:
        prior = _load_state(state_path)
        if prior:
            p = prior.get("processed")
            c = prior.get("candidates")
            if isinstance(p, list):
                processed = [str(x) for x in p]
            if isinstance(c, list):
                candidates = [x for x in c if isinstance(x, dict)]

    all_transcripts = scan_transcripts(projects_dir, days=days)
    processed_set = set(processed)
    pending = [t for t in all_transcripts if str(t) not in processed_set]

    if not all_transcripts:
        # Nothing to do at all. Don't touch the state file or notifier.
        return {"entities": [], "relationships": []}

    # Denominator for the 50% gate is the full scan, so resume runs that
    # finish the 2nd half still skip the (already-fired) 50% notification.
    total = len(all_transcripts)
    halfway_threshold = total // 2 if total >= 2 else None
    halfway_fired = len(processed) >= (halfway_threshold or total + 1)

    def flush():
        _save_state(
            state_path,
            {"processed": list(processed), "candidates": list(candidates)},
        )

    prior_handler = _install_sigint_handler(flush)
    try:
        for transcript in pending:
            new = extract_from_transcript(transcript, model=model_haiku)
            candidates.extend(new)
            processed.append(str(transcript))
            flush()

            if (
                not halfway_fired
                and halfway_threshold is not None
                and len(processed) >= halfway_threshold
            ):
                halfway_fired = True
                try:
                    notifier("Agam bootstrap: 50% complete")
                except Exception as exc:  # noqa: BLE001 -- notifier is advisory
                    print(
                        f"[agam.bootstrap] run_bootstrap: notify 50% failed: {exc}",
                        file=sys.stderr,
                    )

        result = reconcile_candidates(
            candidates, model=model_sonnet, candidates_path=candidates_path
        )
    finally:
        signal.signal(signal.SIGINT, prior_handler)

    # Success: clean up state + ping user.
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"[agam.bootstrap] run_bootstrap: could not delete state "
            f"{state_path}: {exc}",
            file=sys.stderr,
        )

    try:
        notifier("Agam bootstrap: done")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[agam.bootstrap] run_bootstrap: notify done failed: {exc}",
            file=sys.stderr,
        )

    return result

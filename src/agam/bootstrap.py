"""Bootstrap pipeline primitives for Agam.

This module feeds Claude Code session transcripts (JSONL files under
``~/.claude/projects/<path-slug>/<session-id>.jsonl``) through an
extraction + reconciliation pipeline to seed the knowledge graph.

Task 19 implements only the pre-LLM scan + cost-estimate primitives:

- ``scan_transcripts`` -- enumerate candidate JSONL files, filtered by mtime.
- ``estimate_cost`` -- rough USD estimate for a Haiku extraction + Sonnet
  reconciliation sweep across N tokens.
- ``count_tokens_in_file`` -- ~4-chars-per-token heuristic. Intentionally
  avoids a real tokenizer dependency; we are estimating, not metering.

Tasks 20-22 will extend this module with the actual LLM calls, resume/state
tracking, and KG writes. Those layers are out of scope here.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


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

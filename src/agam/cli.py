"""User-facing ``agam`` command.

Subcommands:

- ``agam init``       -- run the installer wizard (Task 16).
- ``agam bootstrap``  -- scan transcripts, estimate cost, extract + reconcile
                          into the knowledge graph (Task 22).
- ``agam status``     -- inspect the local install (home paths, KG, queue,
                          container).
- ``agam reset``      -- remove bootstrap state / candidates. Dry-run by
                          default; ``--confirm`` actually deletes.

The CLI is a thin layer over ``agam.installer.run_wizard`` and
``agam.bootstrap.run_bootstrap``. All heavy lifting lives in those modules so
this file stays easy to test with monkeypatched dispatch functions.

Identity files and the knowledge graph are never touched by ``reset`` -- the
reset button is for bootstrap scratch state, not for nuking your Agam
install. Use ``agam init --force`` for that.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _home() -> Path:
    """Resolve ``$HOME`` lazily so tests can monkeypatch it per call."""
    return Path(os.path.expanduser("~"))


def _default_projects_dir() -> Path:
    return _home() / ".claude" / "projects"


def _state_path() -> Path:
    return _home() / ".claude" / ".agam-bootstrap-state.json"


def _candidates_path() -> Path:
    return _home() / ".claude" / ".agam-bootstrap-candidates.json"


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _load_answers_yaml(path: Path) -> dict[str, Any]:
    """Load a pre-built answers file for non-interactive install.

    YAML is imported lazily so the CLI can run ``status`` / ``reset`` on a
    machine where pyyaml isn't installed. The wizard proper pins pyyaml in
    its own deps, so this is just defensive.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - dep is pinned
        raise SystemExit(
            "pyyaml is required to load --answers. "
            "Install with: uv pip install pyyaml"
        ) from exc
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"--answers file must be a YAML mapping: {path}")
    return data


def _cmd_init(args: argparse.Namespace) -> int:
    # Deferred import: lets tests monkeypatch ``agam.installer.run_wizard``
    # without the CLI module caching the original reference.
    from agam import installer

    answers: dict[str, Any] | None = None
    if args.answers is not None:
        answers = _load_answers_yaml(Path(args.answers))

    try:
        result = installer.run_wizard(answers=answers, force=args.force)
    except SystemExit as exc:
        # Wizard raised with a user-facing message (e.g. "refusing to
        # overwrite"). Print it, return 1 -- no stack trace noise.
        msg = str(exc)
        if msg:
            print(msg, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface + return nonzero
        print(f"[agam init] failed: {exc}", file=sys.stderr)
        return 1

    # Best-effort summary. Fall back gracefully if the result object lacks
    # the expected attrs (e.g. a test's MockResult).
    paths = getattr(result, "paths", None)
    if paths is not None:
        agam_dir = getattr(paths, "agam", None)
        if agam_dir is not None:
            print(f"[agam init] installed into {agam_dir}")
    backup = getattr(result, "backup", None)
    if backup is not None:
        print(f"[agam init] previous install backed up to {backup}")
    return 0


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _prompt_yes_no(question: str) -> bool:
    """``input()``-based y/N prompt. Defaults to 'no' on anything but y/yes."""
    try:
        answer = input(question).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from agam import bootstrap

    projects_dir = Path(args.projects).expanduser().resolve()
    days: int | None = None if args.all else args.days

    # ----- cost preview -------------------------------------------------
    transcripts = bootstrap.scan_transcripts(projects_dir, days=days)
    total_tokens = sum(bootstrap.count_tokens_in_file(p) for p in transcripts)
    est_cost = bootstrap.estimate_cost(total_tokens)

    print(f"[agam bootstrap] projects-dir: {projects_dir}")
    print(
        f"[agam bootstrap] transcripts: {len(transcripts)} "
        f"(days filter: {days if days is not None else 'all'})"
    )
    print(f"[agam bootstrap] estimated tokens: ~{total_tokens:,}")
    print(f"[agam bootstrap] estimated cost: ~${est_cost:.4f}")

    if not transcripts:
        print("[agam bootstrap] nothing to do.")
        return 0

    # ----- confirm ------------------------------------------------------
    if not args.yes:
        if not _prompt_yes_no("Proceed? [y/N] "):
            print("[agam bootstrap] aborted by user.")
            return 1

    # ----- run ----------------------------------------------------------
    try:
        result = bootstrap.run_bootstrap(
            projects_dir,
            days=days if days is not None else 36500,  # ~100y == "all"
            model_haiku=args.model_haiku,
            model_sonnet=args.model_sonnet,
            resume=args.resume,
        )
    except SystemExit as exc:
        msg = str(exc)
        if msg:
            print(msg, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[agam bootstrap] interrupted; state saved for resume.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[agam bootstrap] failed: {exc}", file=sys.stderr)
        return 1

    entities = result.get("entities") or []
    relationships = result.get("relationships") or []
    print(
        f"[agam bootstrap] done: {len(entities)} entities, "
        f"{len(relationships)} relationships reconciled."
    )
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _format_size(n_bytes: int) -> str:
    """Human-readable byte count. KB/MB/GB, one decimal."""
    size = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _cmd_status(_args: argparse.Namespace) -> int:
    from agam import bootstrap

    home = _home()
    agam_dir = home / ".claude" / "agam"
    kg_path = home / ".claude" / "knowledge" / "graph.db"
    queue_dir = home / ".claude" / ".agam-queue"
    state = _state_path()

    print(f"Agam home:    {agam_dir}")
    print(f"  exists:     {agam_dir.exists()}")

    print(f"Knowledge DB: {kg_path}")
    if kg_path.exists():
        print(f"  size:       {_format_size(kg_path.stat().st_size)}")
    else:
        print("  size:       (missing)")

    if queue_dir.exists() and queue_dir.is_dir():
        try:
            depth = sum(1 for _ in queue_dir.iterdir())
        except OSError:
            depth = -1
        print(f"Queue:        {queue_dir} (depth: {depth})")
    else:
        print(f"Queue:        {queue_dir} (missing)")

    if state.exists():
        print(f"Bootstrap state: {state} (resume available)")
    else:
        print("Bootstrap state: (clean)")

    # _discover_container() shells out to ``docker ps``; it returns None
    # on every failure mode (no docker, daemon down, no match).
    container = bootstrap._discover_container()
    if container:
        print(f"Container:    {container}")
    else:
        print("Container:    (none detected)")

    return 0


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def _cmd_reset(args: argparse.Namespace) -> int:
    targets = [_state_path(), _candidates_path()]
    existing = [p for p in targets if p.exists()]

    if not existing:
        print("[agam reset] nothing to remove (state + candidates already clean).")
        return 0

    if not args.confirm:
        print("[agam reset] dry run. Would remove:")
        for p in existing:
            print(f"  {p}")
        print("Re-run with --confirm to actually delete.")
        return 0

    for p in existing:
        try:
            p.unlink()
            print(f"[agam reset] removed {p}")
        except OSError as exc:
            print(f"[agam reset] failed to remove {p}: {exc}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agam",
        description="Persistent knowledge-graph context for Claude Code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- init
    p_init = sub.add_parser(
        "init", help="Install Agam scaffolding into ~/.claude/."
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Back up and overwrite an existing ~/.claude/agam/.",
    )
    p_init.add_argument(
        "--answers",
        type=str,
        default=None,
        help="Path to a YAML file with pre-built install answers.",
    )
    p_init.set_defaults(func=_cmd_init)

    # -- bootstrap
    p_boot = sub.add_parser(
        "bootstrap",
        help="Seed the knowledge graph from Claude Code session transcripts.",
    )
    age = p_boot.add_mutually_exclusive_group()
    age.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only scan transcripts modified in the last N days (default 30).",
    )
    age.add_argument(
        "--all",
        action="store_true",
        help="Scan every transcript regardless of age.",
    )
    p_boot.add_argument(
        "--projects",
        type=str,
        default=str(_default_projects_dir()),
        help="Override the Claude Code projects directory.",
    )
    p_boot.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the cost-confirmation prompt.",
    )
    p_boot.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Resume from prior bootstrap state (default).",
    )
    p_boot.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Ignore prior state and start clean.",
    )
    p_boot.add_argument(
        "--model-haiku",
        type=str,
        default="haiku-4-5",
        help="Extraction model slug.",
    )
    p_boot.add_argument(
        "--model-sonnet",
        type=str,
        default="sonnet-4-6",
        help="Reconciliation model slug.",
    )
    p_boot.set_defaults(func=_cmd_bootstrap)

    # -- status
    p_status = sub.add_parser("status", help="Print Agam install health.")
    p_status.set_defaults(func=_cmd_status)

    # -- reset
    p_reset = sub.add_parser(
        "reset",
        help="Remove bootstrap scratch state. Does NOT touch identity or KG.",
    )
    p_reset.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete (default is dry-run).",
    )
    p_reset.set_defaults(func=_cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

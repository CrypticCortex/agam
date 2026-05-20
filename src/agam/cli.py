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

    # Load the launchd plist on macOS so the watchdog actually starts running.
    # Without this step the plist file sits in ~/Library/LaunchAgents/ unloaded
    # and the watchdog never fires, which is a silent failure mode.
    if getattr(result, "wrote_plist", False):
        _launchctl_bootstrap(paths)

    answers = getattr(result, "answers", None)

    # Auto-chain bootstrap if the user opted in during the wizard. The wizard's
    # ``bootstrap_now?`` question used to be cosmetic (recorded but never
    # actuated). For "just works" we honour it here: scaffolding lands, then
    # bootstrap fires with sane defaults. The user still gets the cost preview
    # + confirmation inside _cmd_bootstrap, so they can bail if the scan
    # surfaces a surprisingly large number of transcripts.
    bootstrap_now = bool(getattr(answers, "bootstrap_now", False)) if answers else False
    if bootstrap_now:
        print("[agam init] you opted in to bootstrap. Running it now...")
        projects_dir = getattr(answers, "projects_dir", "~/.claude/projects")
        bootstrap_args = argparse.Namespace(
            projects=str(projects_dir),
            days=30,
            all=False,
            yes=False,  # still show cost preview + confirm -- bills can sting
            resume=False,
            model_haiku="claude-haiku-4-5",
            model_sonnet="claude-sonnet-4-6",
        )
        rc = _cmd_bootstrap(bootstrap_args)
        if rc != 0:
            print(
                "[agam init] bootstrap did not complete. You can re-run it any "
                "time with: agam bootstrap --projects <dir>",
                file=sys.stderr,
            )
        # Either way init itself succeeded -- the scaffolding is in place.

    _print_install_banner(result)
    return 0


def _launchctl_bootstrap(paths: Any) -> None:
    """``launchctl bootstrap`` the agam-watchdog plist for the current GUI user.

    No-op on non-mac platforms (the launchd plist isn't written there). On
    Mac, idempotent: if the plist is already loaded, ``launchctl bootstrap``
    returns non-zero and we silently move on. Surface other failures so the
    user knows the watchdog isn't running.
    """
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return
    launch_agents = getattr(paths, "launch_agents", None)
    if launch_agents is None:
        return
    plist_path = Path(launch_agents) / "com.agam.watchdog.plist"
    if not plist_path.exists():
        return
    uid = os.getuid()
    domain = f"gui/{uid}"
    # First call: ``bootstrap``. If already loaded this prints to stderr and
    # exits non-zero. We swallow that case but surface any other error.
    proc = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        print(f"[agam init] launchd watchdog loaded ({plist_path.name})")
        return
    err = (proc.stderr or proc.stdout or "").strip()
    if "already" in err.lower() or "service already" in err.lower():
        print(f"[agam init] launchd watchdog already loaded ({plist_path.name})")
        return
    # Other failures -- usually the user lacks Full Disk Access for launchctl
    # or the plist references a stale path. Print enough to debug without
    # blocking install completion.
    print(
        f"[agam init] launchctl bootstrap failed (exit {proc.returncode}): {err}. "
        f"To load the watchdog manually: "
        f"launchctl bootstrap {domain} {plist_path}",
        file=sys.stderr,
    )


def _print_install_banner(result: Any) -> None:
    """End-of-install summary so users know what to do next.

    Three pointers: verify, populate, observe. Tight on purpose -- a wall of
    text after install is friction. Skip the banner entirely if ``result``
    doesn't carry the expected attrs (test mocks).
    """
    paths = getattr(result, "paths", None)
    if paths is None:
        return
    print("")
    print("Agam is installed.")
    print("")
    print("  Verify:    agam doctor")
    print("  Populate:  agam bootstrap --projects ~/.claude/projects")
    print("  TUI:       agam tui")
    print("")
    print("Identity files live at:", getattr(paths, "agam", "~/.claude/agam/"))
    print("KG lives at:", getattr(paths, "knowledge", "~/.claude/knowledge/") )
    print("")
    print("Next Claude Code session will start using Agam automatically.")
    print("")


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

    # ----- persist to KG -------------------------------------------------
    ent_written, rel_written = _persist_to_kg(entities, relationships)
    print(
        f"[agam bootstrap] done: {ent_written} entities, "
        f"{rel_written} relationships persisted to KG."
    )
    return 0


def _persist_to_kg(
    entities: list[dict], relationships: list[dict]
) -> tuple[int, int]:
    """Write reconciled entities and relationships into the knowledge graph.

    Uses ``agam.tools.knowledge_graph`` (which honours ``AGAM_KG_PATH``).
    Missing endpoints are created as stub entities so relationships never
    drop on the floor. Returns a ``(entities_written, relationships_written)``
    tally for the CLI's summary line.
    """
    from agam.tools import knowledge_graph as kg

    db = kg.get_db()
    ts = kg.now()

    ent_written = 0
    for e in entities:
        name = e.get("name")
        if not name:
            continue
        etype = e.get("type") or "concept"
        desc = e.get("description") or ""
        normalized = kg.normalize_name(name)
        existing = db.execute(
            "SELECT id FROM entities WHERE name = ?", (normalized,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE entities SET type=?, description=?, updated=? WHERE id=?",
                (etype, desc, ts, existing[0]),
            )
        else:
            db.execute(
                "INSERT INTO entities (name, type, description, created, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (normalized, etype, desc, ts, ts),
            )
            ent_written += 1

    db.commit()

    def _ensure_entity(name: str) -> int | None:
        if not name:
            return None
        normalized = kg.normalize_name(name)
        row = db.execute(
            "SELECT id FROM entities WHERE name = ?", (normalized,)
        ).fetchone()
        if row:
            return row[0]
        cur = db.execute(
            "INSERT INTO entities (name, type, description, created, updated) "
            "VALUES (?, ?, ?, ?, ?)",
            (normalized, "concept", "", ts, ts),
        )
        return cur.lastrowid

    rel_written = 0
    for r in relationships:
        src = r.get("source")
        tgt = r.get("target")
        rel = r.get("relation")
        if not (src and tgt and rel):
            continue
        src_id = _ensure_entity(src)
        tgt_id = _ensure_entity(tgt)
        if src_id is None or tgt_id is None:
            continue
        try:
            db.execute(
                "INSERT INTO relationships (source_id, target_id, relation, "
                "weight, created) VALUES (?, ?, ?, ?, ?)",
                (src_id, tgt_id, rel, 1.0, ts),
            )
            rel_written += 1
        except Exception:  # noqa: BLE001 -- dedup collision is fine
            pass

    db.commit()
    db.close()
    return ent_written, rel_written


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
# doctor -- deep health check, designed for "did my install work?"
# ---------------------------------------------------------------------------


_OK = "[OK]"
_WARN = "[WARN]"
_FAIL = "[FAIL]"


def _check(label: str, ok: bool | None, detail: str = "", fix: str = "") -> bool:
    """Print a doctor-style check line. Returns True if the check passed.

    ``ok=None`` -> warn (not an outright failure, but worth surfacing).
    The ``fix`` string is shown on the next line when ok is False/None
    so users have an actionable command to copy.
    """
    if ok is True:
        tag = _OK
    elif ok is None:
        tag = _WARN
    else:
        tag = _FAIL
    line = f"{tag:6} {label}"
    if detail:
        line += f" -- {detail}"
    print(line)
    if ok is not True and fix:
        print(f"       fix: {fix}")
    return ok is True


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Run a battery of checks that diagnose common install failures.

    Returns 0 if every check passes, 1 if any FAIL is hit. WARNs do not
    fail the exit code -- they exist for things like "no claude-code
    container running" which is fine for users on host-mode Claude Code.
    """
    import json as _json
    import platform
    import subprocess

    home = _home()
    fails = 0

    # 1. Identity files
    agam_dir = home / ".claude" / "agam"
    for f in ("AGAM.md", "THISAI.md", "MUGAM.md", "config.yaml"):
        if not _check(
            f"identity file: {f}",
            (agam_dir / f).exists(),
            detail=str(agam_dir / f),
            fix="agam init",
        ):
            fails += 1

    # 2. KG present + readable
    kg_path = home / ".claude" / "knowledge" / "graph.db"
    kg_ok = False
    kg_count = 0
    if kg_path.exists():
        try:
            import sqlite3 as _sql
            conn = _sql.connect(str(kg_path), timeout=2)
            kg_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            conn.close()
            kg_ok = True
        except Exception as exc:  # noqa: BLE001
            _check("KG readable", False, str(exc), "agam init --force")
            fails += 1
    if kg_ok:
        _check(
            "KG readable",
            True,
            detail=f"{kg_count} entities at {kg_path}",
        )
        if kg_count == 0:
            _check(
                "KG populated",
                None,
                "graph is empty -- recall hook will have nothing to inject",
                fix=f"agam bootstrap --projects {home / '.claude' / 'projects'}",
            )
    elif not kg_path.exists():
        _check("KG file present", False, str(kg_path), "agam init")
        fails += 1

    # 3. Hooks registered in settings.json
    settings_path = home / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _check(
                "settings.json parseable",
                False,
                str(exc),
                "inspect ~/.claude/settings.json manually",
            )
            fails += 1
            settings = {}
        # Walk every hook entry and look for any command path containing the
        # canonical agam hook filenames. Substring match handles installer
        # path variations (~/.claude vs absolute).
        hook_section = settings.get("hooks", {})
        all_commands: list[str] = []
        if isinstance(hook_section, dict):
            for entries in hook_section.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    for inner in entry.get("hooks", []):
                        if isinstance(inner, dict):
                            cmd = inner.get("command", "")
                            if isinstance(cmd, str):
                                all_commands.append(cmd)
        required_markers = ("graph_recall", "graph_update", "session_close")
        missing = [m for m in required_markers if not any(m in c for c in all_commands)]
        if missing:
            _check(
                "Agam hooks registered in settings.json",
                False,
                detail=f"missing: {', '.join(missing)}",
                fix="agam init --force",
            )
            fails += 1
        else:
            _check(
                "Agam hooks registered in settings.json",
                True,
                detail=f"{len(all_commands)} hook commands found",
            )
        # AGAM_USER_ENTITY env var present
        user_entity = settings.get("env", {}).get("AGAM_USER_ENTITY") if isinstance(settings.get("env"), dict) else None
        if user_entity:
            _check("AGAM_USER_ENTITY set", True, detail=user_entity)
        else:
            _check(
                "AGAM_USER_ENTITY set",
                None,
                detail="hooks will tag relations with the literal 'User'",
                fix="agam init --force (re-runs the wizard with name capture)",
            )
    else:
        _check(
            "settings.json present",
            False,
            str(settings_path),
            fix="agam init (will create settings.json with Agam hooks merged in)",
        )
        fails += 1

    # 4. OAuth credentials (required for bootstrap + watchdog)
    creds = home / ".claude" / ".credentials.json"
    if creds.exists():
        _check("Claude Code OAuth credentials", True, detail=str(creds))
    else:
        _check(
            "Claude Code OAuth credentials",
            None,
            detail=str(creds),
            fix="run `claude` interactively once to authenticate",
        )

    # 5. Container discovery (informational -- container is needed only for
    # bootstrap + watchdog, not for graph_recall / graph_update).
    try:
        from agam import bootstrap as _bs
        container = _bs._discover_container()
    except Exception:  # noqa: BLE001
        container = None
    if container:
        _check("Claude Code container running", True, detail=container)
    else:
        _check(
            "Claude Code container running",
            None,
            "bootstrap + watchdog require it; recall hook does not",
            fix="start your devcontainer (or set AGAM_BOOTSTRAP_MODE=host)",
        )

    # 6. macOS launchd plist loaded
    if platform.system() == "Darwin":
        plist_name = "com.agam.watchdog"
        plist_path = home / "Library" / "LaunchAgents" / f"{plist_name}.plist"
        if plist_path.exists():
            uid = os.getuid()
            proc = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{plist_name}"],
                capture_output=True,
                text=True,
            )
            loaded = proc.returncode == 0
            if loaded:
                _check("launchd watchdog loaded", True, detail=plist_name)
            else:
                _check(
                    "launchd watchdog loaded",
                    False,
                    detail="plist exists but is not loaded",
                    fix=f"launchctl bootstrap gui/{uid} {plist_path}",
                )
                fails += 1
        else:
            _check(
                "launchd watchdog plist installed",
                None,
                detail="not installed (only relevant if you want background sync)",
                fix="agam init (with platform=mac)",
            )

    print("")
    if fails:
        print(f"{fails} check(s) failed. Address the fix lines above.")
        return 1
    print("All checks passed.")
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
        default="haiku",
        help="Extraction model slug.",
    )
    p_boot.add_argument(
        "--model-sonnet",
        type=str,
        default="sonnet",
        help="Reconciliation model slug.",
    )
    p_boot.set_defaults(func=_cmd_bootstrap)

    # -- status
    p_status = sub.add_parser("status", help="Print Agam install health.")
    p_status.set_defaults(func=_cmd_status)

    # -- doctor
    p_doc = sub.add_parser(
        "doctor",
        help="Deep diagnostic checks. Use when something feels off.",
    )
    p_doc.set_defaults(func=_cmd_doctor)

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

    # -- tui (and bare `agam` with no args)
    p_tui = sub.add_parser(
        "tui",
        help="Launch the interactive dashboard (default when no subcommand).",
    )
    p_tui.set_defaults(func=_cmd_tui)

    return parser


def _cmd_tui(_args: argparse.Namespace) -> int:
    """Launch the interactive Textual dashboard."""
    from agam.tui import main as tui_main
    return tui_main()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare `agam` with no subcommand -> launch the TUI.
        return _cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

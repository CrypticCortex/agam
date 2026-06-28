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
import shutil
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


def _dedupe_targets(targets: list[str]) -> list[str]:
    out: list[str] = []
    for target in targets:
        if target not in out:
            out.append(target)
    return out


def _resolve_targets(args: argparse.Namespace, answers: dict[str, Any] | None) -> list[str]:
    """Decide which agents to wire: explicit flag/answers, else auto-detect.

    Precedence: ``--target`` flags > ``targets:`` in the answers YAML >
    auto-detection (with an interactive confirm/refine when possible). Falls
    back to ``["claude"]`` when nothing is detected so a bare install still does
    something sensible.
    """
    from agam.agents import detect_agents

    if getattr(args, "target", None):
        return _dedupe_targets(list(args.target))
    if answers and isinstance(answers.get("targets"), list) and answers["targets"]:
        return _dedupe_targets([str(t) for t in answers["targets"]])

    home = _home()
    detected_agents = detect_agents(home)
    detected = [a.name for a in detected_agents]
    if not detected:
        print("[agam init] no supported agent detected; defaulting to Claude.")
        return ["claude"]

    # Interactive refine when a real wizard is in play (no scripted answers).
    # Make the install scope explicit when both agents are present: the old
    # checkbox preselected everything, which made Cursor opt-out by accident.
    if answers is None and sys.stdin.isatty():
        try:
            import questionary  # type: ignore[import-not-found]

            print("[agam init] detected agents:")
            for a in detected_agents:
                print(f"  - {a.name}: {a.detect_evidence(home)}")
            if set(detected) >= {"claude", "cursor"}:
                chosen = questionary.select(
                    "Install agam for:",
                    choices=[
                        questionary.Choice("Claude only", value=["claude"]),
                        questionary.Choice("Cursor only", value=["cursor"]),
                        questionary.Choice(
                            "Both Claude + Cursor (recommended)",
                            value=["claude", "cursor"],
                        ),
                    ],
                ).ask()
            else:
                print(f"[agam init] wiring detected agent: {', '.join(detected)}")
                return detected
            if chosen is None:
                return []
            if chosen:
                return _dedupe_targets(list(chosen))
        except Exception:  # noqa: BLE001 -- non-interactive / no questionary
            pass
    if len(detected) > 1:
        print(
            f"[agam init] detected {','.join(detected)}; wiring all. "
            "Use --target claude or --target cursor to limit."
        )
    else:
        print(f"[agam init] detected {detected[0]}; wiring it.")
    return detected


def _cmd_init(args: argparse.Namespace) -> int:
    # Deferred import: lets tests monkeypatch ``agam.installer.run_install``
    # without the CLI module caching the original reference.
    from agam import installer

    answers: dict[str, Any] | None = None
    if args.answers is not None:
        answers = _load_answers_yaml(Path(args.answers))

    targets = _resolve_targets(args, answers)
    if not targets:
        print("[agam init] cancelled.", file=sys.stderr)
        return 1

    try:
        result = installer.run_install(answers, targets=targets)
    except SystemExit as exc:
        msg = str(exc)
        if msg:
            print(msg, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface + return nonzero
        print(f"[agam init] failed: {exc}", file=sys.stderr)
        return 1

    agam_home = getattr(result, "agam_home", None)
    if agam_home is not None:
        print(f"[agam init] shared data home: {agam_home}")
    installed = getattr(result, "targets", None)
    if installed:
        print(f"[agam init] wired agents: {', '.join(installed)}")
    if getattr(result, "migration_status", None) == "migrated":
        print(f"[agam init] migrated legacy ~/.claude data into {agam_home}")

    # Load the launchd plist on macOS so the watchdog actually starts running.
    if getattr(result, "wrote_plist", False):
        import types
        home = getattr(result, "home", _home())
        _launchctl_bootstrap(
            types.SimpleNamespace(launch_agents=home / "Library" / "LaunchAgents")
        )

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

    # 4. Claude CLI present on PATH. We don't check for a credentials.json
    # file: on macOS host, Claude Code stores OAuth in Keychain and that
    # file may never exist. Auth correctness is verified by the invoker
    # cascade below when it actually calls ``claude -p``; this check just
    # answers "is the binary reachable?"
    claude_path = shutil.which("claude")
    if claude_path:
        _check("Claude Code CLI on PATH", True, detail=claude_path)
    else:
        _check(
            "Claude Code CLI on PATH",
            False,
            detail="`claude` not found",
            fix="install Claude Code: https://claude.ai/code",
        )
        fails += 1

    # 5. Invoker cascade -- which paths Agam can use to call claude -p.
    # At least one must be ok for bootstrap + watchdog auto-learning.
    # graph_recall / graph_update / lesson hooks work without any invoker.
    try:
        from agam.invoker import probe_all
        invoker_results = probe_all()
    except Exception as exc:  # noqa: BLE001
        invoker_results = []
        _check("Invoker cascade", False, str(exc), "report this as a bug")
        fails += 1
    any_ok = any(r.ok for _, r in invoker_results)
    for inv, r in invoker_results:
        if r.ok:
            _check(f"invoker: {inv.name}", True, detail=r.detail)
        else:
            # WARN per invoker -- only the absence of EVERY invoker is a FAIL.
            _check(f"invoker: {inv.name}", None, detail=r.detail)
    if invoker_results and not any_ok:
        _check(
            "at least one invoker healthy",
            False,
            detail="bootstrap + watchdog auto-learning will not work",
            fix="install Claude Code on host (`claude` on PATH) or start a "
                "claude-code container",
        )
        fails += 1

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
# obsolete -- mark a KG entity as no longer current
# ---------------------------------------------------------------------------


def _kg_path() -> Path:
    """Default KG path; overridable via AGAM_KG_PATH for tests."""
    env = os.environ.get("AGAM_KG_PATH")
    if env:
        return Path(env)
    return _home() / ".claude" / "knowledge" / "graph.db"


def backfill_source_agent(kg_path: Path, agent: str) -> int:
    """Stamp source-agent=<agent> on every entity that lacks the property.

    Used to attribute the pre-existing graph (built before provenance existed)
    to the incumbent agent, and idempotent thereafter -- already-tagged entities
    (e.g. source-agent=cursor) are left alone. Returns the count tagged.
    """
    import sqlite3
    from datetime import datetime, timezone

    if not kg_path.exists():
        return 0
    ts = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(kg_path), timeout=10)
    try:
        cur = conn.execute(
            """INSERT INTO properties (entity_id, key, value, updated)
               SELECT e.id, 'source-agent', ?, ?
               FROM entities e
               WHERE NOT EXISTS (
                   SELECT 1 FROM properties p
                   WHERE p.entity_id = e.id AND p.key = 'source-agent'
               )""",
            (agent, ts),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    except sqlite3.OperationalError:
        # Graph predates the expected schema (no entities/properties table).
        # Nothing to backfill; don't fail the install.
        return 0
    finally:
        conn.close()


def _cmd_tag_source(args: argparse.Namespace) -> int:
    """Backfill source-agent provenance on untagged entities."""
    kg = _kg_path()
    if not kg.exists():
        print(f"[agam tag-source] no KG at {kg}.", file=sys.stderr)
        return 1
    n = backfill_source_agent(kg, args.agent)
    print(f"[agam tag-source] tagged {n} untagged entit{'y' if n == 1 else 'ies'} as source-agent={args.agent}.")
    return 0


def _cmd_obsolete(args: argparse.Namespace) -> int:
    """Mark an entity as obsolete so graph_recall stops surfacing it.

    The entity stays in the database (forensic value) but carries
    ``status=obsolete`` + ``obsoleted-at=<iso>`` + optional ``obsolete-reason``.
    Pass ``--include-obsolete`` to graph_recall (via env) to surface it again.

    Idempotent: re-marking the same entity refreshes the timestamp and reason.
    """
    import sqlite3
    from datetime import datetime, timezone
    from agam.tools.knowledge_graph import normalize_name

    kg = _kg_path()
    if not kg.exists():
        print(f"[agam obsolete] no KG at {kg}. Run `agam init` first.", file=sys.stderr)
        return 1

    # Entities are written through normalize_name() (PascalCase / camelCase /
    # snake_case -> kebab-case). The user can pass any form -- "VoiceFNOL",
    # "voice_fnol", "voice-fnol" -- so normalize before the lookup. Without
    # this, ``agam obsolete VoiceFNOL`` looks up "voicefnol" and finds nothing.
    normalized = normalize_name(args.entity)

    conn = sqlite3.connect(str(kg), timeout=5)
    try:
        row = conn.execute(
            "SELECT id, name FROM entities WHERE name = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if not row:
            print(
                f"[agam obsolete] no entity named {args.entity!r} "
                f"(normalized: {normalized!r}).",
                file=sys.stderr,
            )
            return 1
        eid, canonical_name = row[0], row[1]

        ts = datetime.now(timezone.utc).isoformat()
        # Upsert three properties: status, obsoleted-at, obsolete-reason.
        for key, value in (
            ("status", "obsolete"),
            ("obsoleted-at", ts),
            ("obsolete-reason", args.reason or ""),
        ):
            if not value and key == "obsolete-reason":
                continue  # don't bother storing an empty reason
            conn.execute(
                "INSERT INTO properties (entity_id, key, value, updated) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(entity_id, key) DO UPDATE SET "
                "value=excluded.value, updated=excluded.updated",
                (eid, key, value, ts),
            )
        conn.commit()
    finally:
        conn.close()

    print(f"[agam obsolete] {canonical_name} marked obsolete.")
    if args.reason:
        print(f"[agam obsolete] reason: {args.reason}")
    print(
        "[agam obsolete] graph_recall will skip this entity. "
        "Set AGAM_INCLUDE_OBSOLETE=1 to surface it for forensic queries."
    )
    return 0


# ---------------------------------------------------------------------------
# repair -- SQLite integrity check + vacuum + reindex
# ---------------------------------------------------------------------------


def _cmd_repair(args: argparse.Namespace) -> int:
    """Check + repair the knowledge graph SQLite database.

    Three operations:
      1. ``PRAGMA integrity_check`` -- detects corruption.
      2. ``VACUUM`` -- reclaims unused space, rebuilds the file compactly.
      3. ``REINDEX`` -- rebuilds all indexes including FTS5 tokens.

    ``--dry-run`` runs only the integrity check.
    """
    import sqlite3

    kg = _kg_path()
    if not kg.exists():
        print(f"[agam repair] no KG at {kg}. Nothing to repair.", file=sys.stderr)
        return 1

    size_before = kg.stat().st_size

    conn = sqlite3.connect(str(kg), timeout=10)
    try:
        # Step 1: integrity check. ``ok`` is the only passing value.
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result = [r[0] for r in rows] if rows else []
        ok = len(result) == 1 and result[0] == "ok"
        if ok:
            print("[agam repair] integrity_check: OK")
        else:
            print("[agam repair] integrity_check FAILED:")
            for line in result:
                print(f"  {line}")
            print(
                "[agam repair] corruption detected. Restore from backup or "
                "rebuild via `agam bootstrap`. VACUUM may make things worse "
                "on a corrupt file, skipping.",
                file=sys.stderr,
            )
            return 1

        if args.dry_run:
            print("[agam repair] dry-run, skipping VACUUM + REINDEX.")
            return 0

        # Step 2: vacuum. Releases space; rebuilds the file. Cannot run
        # inside a transaction, so close cleanly between ops.
        conn.execute("VACUUM")
        # Step 3: reindex.
        conn.execute("REINDEX")
        conn.commit()
    finally:
        conn.close()

    size_after = kg.stat().st_size
    reclaimed = size_before - size_after
    print(
        f"[agam repair] vacuum + reindex done. "
        f"size: {_format_size(size_before)} -> {_format_size(size_after)} "
        f"(reclaimed {_format_size(max(reclaimed, 0))})"
    )
    return 0


# ---------------------------------------------------------------------------
# digest -- daily summary of what Agam has been doing
# ---------------------------------------------------------------------------


def _cmd_digest(args: argparse.Namespace) -> int:
    """Print a summary of Agam activity in the last ``--since`` days (default 1).

    Shows: new entities created, entities marked obsolete, work-log lines
    added, watchdog sessions processed, doctor health snapshot. Useful as a
    daily "what did Agam do for me" rollup.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    since_days = max(int(args.since), 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    cutoff_iso = cutoff.isoformat()
    cutoff_naive = cutoff.replace(tzinfo=None).isoformat()

    home = _home()
    kg = _kg_path()
    print(f"Agam digest (last {since_days} day{'s' if since_days != 1 else ''})")
    print("=" * 60)

    # ----- KG growth -----------------------------------------------------
    if kg.exists():
        try:
            conn = sqlite3.connect(str(kg), timeout=5)
            new_ents = conn.execute(
                "SELECT name, type FROM entities WHERE created >= ? OR created >= ? "
                "ORDER BY created DESC LIMIT 20",
                (cutoff_iso, cutoff_naive),
            ).fetchall()
            obs_ents = conn.execute(
                """SELECT e.name, p.value FROM entities e
                   JOIN properties p ON p.entity_id = e.id
                   WHERE p.key = 'obsoleted-at' AND p.value >= ?
                   ORDER BY p.value DESC LIMIT 20""",
                (cutoff_iso,),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            conn.close()

            print(f"\nKnowledge graph: {total} entities total")
            if new_ents:
                print(f"  New ({len(new_ents)}):")
                for name, etype in new_ents[:10]:
                    print(f"    + {name} ({etype})")
                if len(new_ents) > 10:
                    print(f"    ... and {len(new_ents) - 10} more")
            else:
                print("  No new entities.")
            if obs_ents:
                print(f"  Obsoleted ({len(obs_ents)}):")
                for name, ts in obs_ents[:10]:
                    print(f"    x {name} (at {ts[:10]})")
        except Exception as exc:  # noqa: BLE001
            print(f"\nKnowledge graph: error reading -- {exc}")
    else:
        print("\nKnowledge graph: not present (run `agam init`)")

    # ----- AGAM.md learnings ---------------------------------------------
    agam_md = home / ".claude" / "agam" / "AGAM.md"
    if agam_md.exists():
        try:
            text = agam_md.read_text(encoding="utf-8")
            # Heuristic: count [lesson] / [insight] / [correction] markers
            # in the file. Doesn't track when each was added but does give a
            # current snapshot count.
            n_lesson = text.count("[lesson]")
            n_insight = text.count("[insight]")
            n_correction = text.count("[correction]")
            print(
                f"\nLearnings (cumulative): "
                f"{n_lesson} lessons, {n_insight} insights, {n_correction} corrections"
            )
        except OSError:
            pass

    # ----- watchdog activity ---------------------------------------------
    processed = home / ".claude" / "agam" / ".processed-sessions.jsonl"
    if processed.exists():
        try:
            n_total = 0
            n_recent = 0
            cutoff_ts = cutoff.timestamp()
            for line in processed.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                n_total += 1
                # Each line has a ts field; cheap text-scan for the threshold
                if '"ts":' in line:
                    import re as _re
                    m = _re.search(r'"ts":\s*([\d.]+)', line)
                    if m and float(m.group(1)) >= cutoff_ts:
                        n_recent += 1
            print(
                f"\nWatchdog: {n_recent} sessions processed in window "
                f"({n_total} total all-time)"
            )
        except OSError:
            pass

    print("")
    return 0


# ---------------------------------------------------------------------------
# upgrade -- replace code, preserve identity + KG + config
# ---------------------------------------------------------------------------


def _cmd_upgrade(args: argparse.Namespace) -> int:
    """Upgrade Agam to the version shipped with this package.

    Replaces: hooks/, tools/agam/, agam/prompts/, settings.json hook
    registrations, the launchd plist.

    Preserves: agam/AGAM.md, agam/THISAI.md, agam/MUGAM.md, agam/config.yaml,
    knowledge/graph.db, agam/SUVADU.md.

    Differs from ``agam init --force`` in that init backs up the entire
    ``~/.claude/agam/`` directory and rebuilds it from templates -- which
    means the user loses their identity edits and KG. Upgrade is the safe
    in-place update path.
    """
    from agam import installer

    home = _home()
    paths = installer.InstallPaths.for_home(home)
    preserve = {
        "AGAM.md": paths.agam / "AGAM.md",
        "THISAI.md": paths.agam / "THISAI.md",
        "MUGAM.md": paths.agam / "MUGAM.md",
        "SUVADU.md": paths.agam / "SUVADU.md",
        "config.yaml": paths.agam / "config.yaml",
        "graph.db": paths.knowledge / "graph.db",
    }
    missing = [name for name, p in preserve.items() if not p.exists()]
    if "config.yaml" in missing:
        print(
            "[agam upgrade] no existing install found (no config.yaml). "
            "Run `agam init` for a fresh install.",
            file=sys.stderr,
        )
        return 1

    # The straightforward "preserve" mechanism: snapshot the protected files,
    # let run_wizard do its full force-overwrite, then restore. This is
    # heavier than a surgical merge but uses the well-tested installer code
    # path. Edge case: install can fail mid-way; the backup is on disk so
    # we can restore manually if needed.
    import tempfile
    import shutil
    snapshot_root = Path(tempfile.mkdtemp(prefix=".agam-upgrade-snap-"))
    snapshot_files: dict[str, Path] = {}
    succeeded = False
    try:
        for name, src in preserve.items():
            if src.exists():
                dst = snapshot_root / name
                shutil.copy2(src, dst)
                snapshot_files[name] = dst

        # Load existing answers to skip the wizard prompt and keep the
        # user's config as-is.
        cfg_path = paths.agam / "config.yaml"
        try:
            import yaml as _yaml  # type: ignore[import-not-found]
            existing_cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing_cfg = {}

        print("[agam upgrade] replacing code, preserving identity + KG...")
        installer.run_wizard(
            answers=existing_cfg or None,
            force=True,
            home=home,
        )

        # Restore preserved files. The installer wrote fresh templates over
        # them; put the user's content back. KG was also fresh-from-schema;
        # swap our snapshot back in.
        for name, snap in snapshot_files.items():
            dst = preserve[name]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snap, dst)
        print(
            f"[agam upgrade] restored {len(snapshot_files)} preserved file(s)."
        )
        succeeded = True
    except Exception as exc:  # noqa: BLE001 -- surface with context
        print(
            f"[agam upgrade] failed: {exc}\n"
            f"[agam upgrade] snapshot of your data is at {snapshot_root}. "
            f"Copy files back manually if needed.",
            file=sys.stderr,
        )
        return 1
    finally:
        # Only clean the snapshot when the upgrade succeeded AND the user
        # did not pass --keep-snapshot. On failure we keep it so the user
        # can recover (the previous finally always ran rmtree, deleting the
        # snapshot the user was just told to recover from).
        if succeeded and not args.keep_snapshot:
            try:
                shutil.rmtree(snapshot_root)
            except OSError:
                pass

    print("[agam upgrade] done. Run `agam doctor` to verify.")
    return 0


# ---------------------------------------------------------------------------
# uninstall -- remove Agam scaffolding cleanly
# ---------------------------------------------------------------------------


def _file_mentions_any(path: Path, markers: tuple[str, ...]) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in text for marker in markers)


def _detect_uninstall_targets(
    *,
    home: Path,
    claude: Path,
    claude_hook_files: list[Path],
    cursor_hook_files: list[Path],
) -> list[str]:
    """Best-effort detection of which agent wirings are installed."""
    cursor = home / ".cursor"
    out: list[str] = []
    if (
        (claude / "agam").exists()
        or (claude / "knowledge").exists()
        or (claude / "tools" / "agam").exists()
        or any(p.exists() for p in claude_hook_files)
        or _file_mentions_any(
            claude / "settings.json",
            (
                "graph_recall",
                "graph_update",
                "session_close",
                "lesson_activate",
                "agam_watchdog",
                "AGAM_DATA_HOME",
                "AGAM_KG_PATH",
                "AGAM_USER_ENTITY",
            ),
        )
    ):
        out.append("claude")
    if (
        (cursor / "tools" / "agam").exists()
        or any(p.exists() for p in cursor_hook_files)
        or _file_mentions_any(
            cursor / "hooks.json",
            ("cursor_stop.py", "cursor_session_end.py", "/tools/agam/"),
        )
    ):
        out.append("cursor")
    return out


def _resolve_uninstall_targets(
    args: argparse.Namespace,
    *,
    installed: list[str],
) -> list[str]:
    """Decide which agent wiring to remove."""
    explicit = getattr(args, "target", None)
    if explicit:
        return _dedupe_targets(list(explicit))
    if not installed:
        return []
    if len(installed) == 1:
        print(f"[agam uninstall] detected installed agent: {installed[0]}")
        return installed
    if not sys.stdin.isatty():
        print(
            f"[agam uninstall] detected {','.join(installed)}; uninstalling all. "
            "Use --target claude or --target cursor to limit."
        )
        return installed
    try:
        import questionary  # type: ignore[import-not-found]

        chosen = questionary.select(
            "Uninstall Agam for:",
            choices=[
                questionary.Choice("Claude only", value=["claude"]),
                questionary.Choice("Cursor only", value=["cursor"]),
                questionary.Choice(
                    "Both Claude + Cursor (recommended)",
                    value=["claude", "cursor"],
                ),
            ],
        ).ask()
        if chosen is None:
            return []
        if chosen:
            return _dedupe_targets(list(chosen))
    except Exception:  # noqa: BLE001 -- non-interactive / no questionary
        pass
    print(
        f"[agam uninstall] detected {','.join(installed)}; uninstalling all. "
        "Use --target claude or --target cursor to limit."
    )
    return installed


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """Uninstall Agam from this machine.

    Default behavior is "soft uninstall": move all Agam-managed files into
    a timestamped ``.uninstalled-<ts>/`` subdirectory rather than delete.
    The user can recover by moving them back. Add ``--purge`` to delete.

    Always unregisters hooks from settings.json (preserving any non-Agam
    hook entries the user added) and unloads the launchd plist on macOS.
    Requires ``--confirm`` to actually run; default prints a dry run.
    """
    import json as _json
    import shutil
    import subprocess
    from datetime import datetime, timezone

    home = _home()
    claude = home / ".claude"
    cursor = home / ".cursor"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # File set Agam owns. Mirror of the installer's writes. Identity files
    # + KG land in their own backup directories; hook + tool files just get
    # removed (they have no user data).
    hook_files = [
        claude / "hooks" / "agam_watchdog.sh",
        claude / "hooks" / "agam_watchdog_inner.py",
        claude / "hooks" / "graph_recall.py",
        claude / "hooks" / "graph_update.py",
        claude / "hooks" / "session_close.py",
        claude / "hooks" / "lesson_activate.py",
        claude / "hooks" / "lesson_activate_post.py",
        # Dash-named legacy filenames (the personal setup uses these); we
        # don't ship them in this repo but the uninstaller cleans both shapes.
        claude / "hooks" / "agam-watchdog.sh",
        claude / "hooks" / "agam-watchdog-inner.py",
        claude / "hooks" / "graph-recall.py",
        claude / "hooks" / "graph-update.py",
        claude / "hooks" / "session-close-hook.py",
        claude / "hooks" / "lesson-activate.py",
        claude / "hooks" / "lesson-activate-post.py",
    ]
    cursor_hook_files = [
        cursor / "hooks" / "cursor_stop.py",
        cursor / "hooks" / "cursor_session_end.py",
    ]
    tools_dir = claude / "tools" / "agam"
    cursor_tools_dir = cursor / "tools" / "agam"
    agam_dir = claude / "agam"
    kg_dir = claude / "knowledge"
    shared_data_dirs = [
        home / ".agam",
        claude / "cursor-agam",
    ]
    settings_path = claude / "settings.json"
    cursor_hooks_path = cursor / "hooks.json"
    plist_path = home / "Library" / "LaunchAgents" / "com.agam.watchdog.plist"

    installed = _detect_uninstall_targets(
        home=home,
        claude=claude,
        claude_hook_files=hook_files,
        cursor_hook_files=cursor_hook_files,
    )
    targets = _resolve_uninstall_targets(args, installed=installed)
    target_set = set(targets)
    installed_set = set(installed)
    remaining_after = installed_set - target_set
    remove_shared = bool(target_set & installed_set) and not remaining_after

    plan: list[tuple[str, Path]] = []
    if "claude" in target_set:
        if not args.purge:
            # Soft uninstall: rename legacy Claude data dirs.
            if agam_dir.exists():
                plan.append(("move", agam_dir))
            if kg_dir.exists():
                plan.append(("move", kg_dir))
        else:
            if agam_dir.exists():
                plan.append(("delete", agam_dir))
            if kg_dir.exists():
                plan.append(("delete", kg_dir))
        for h in hook_files:
            if h.exists():
                plan.append(("delete", h))
        if tools_dir.exists():
            plan.append(("delete", tools_dir))
        if _file_mentions_any(
            settings_path,
            (
                "graph_recall",
                "graph_update",
                "session_close",
                "lesson_activate",
                "agam_watchdog",
                "AGAM_DATA_HOME",
                "AGAM_KG_PATH",
                "AGAM_USER_ENTITY",
            ),
        ):
            plan.append(("clean-settings", settings_path))
    if "cursor" in target_set:
        for h in cursor_hook_files:
            if h.exists():
                plan.append(("delete", h))
        if cursor_tools_dir.exists():
            plan.append(("delete", cursor_tools_dir))
        if _file_mentions_any(
            cursor_hooks_path,
            ("cursor_stop.py", "cursor_session_end.py", "/tools/agam/"),
        ):
            plan.append(("clean-cursor-hooks", cursor_hooks_path))
    if remove_shared:
        for data_dir in shared_data_dirs:
            if not data_dir.exists():
                continue
            plan.append(("delete" if args.purge else "move", data_dir))
    if remove_shared and plist_path.exists():
        plan.append(("delete-plist", plist_path))

    if not plan:
        print("[agam uninstall] nothing to remove. Agam isn't installed here.")
        return 0

    if not args.confirm:
        print("[agam uninstall] DRY RUN. Would do:")
        for action, p in plan:
            verb = {
                "move": "move to .uninstalled-<ts>/",
                "delete": "delete",
                "delete-plist": "unload + delete launchd plist",
                "clean-settings": "remove Agam hook entries from settings.json",
                "clean-cursor-hooks": "remove Agam hook entries from hooks.json",
            }[action]
            print(f"  {verb}: {p}")
        if args.purge:
            print("[agam uninstall] --purge will permanently delete data.")
        print("[agam uninstall] re-run with --confirm to proceed.")
        return 0

    errors: list[str] = []
    for action, p in plan:
        try:
            if action == "move":
                backup = p.parent / f"{p.name}.uninstalled-{timestamp}"
                shutil.move(str(p), str(backup))
                print(f"[agam uninstall] moved: {p} -> {backup}")
            elif action == "delete":
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                print(f"[agam uninstall] deleted: {p}")
            elif action == "delete-plist":
                uid = os.getuid()
                # bootout is the launchctl 2.x way to unload. May fail if
                # not loaded; ignore that case.
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{uid}/com.agam.watchdog"],
                    capture_output=True,
                )
                p.unlink()
                print(f"[agam uninstall] unloaded + deleted: {p}")
            elif action == "clean-settings":
                settings = _json.loads(p.read_text(encoding="utf-8"))
                hooks = settings.get("hooks", {})
                if isinstance(hooks, dict):
                    for event, entries in list(hooks.items()):
                        if not isinstance(entries, list):
                            continue
                        kept = []
                        for entry in entries:
                            if not isinstance(entry, dict):
                                kept.append(entry)
                                continue
                            inner = entry.get("hooks", [])
                            if not isinstance(inner, list):
                                kept.append(entry)
                                continue
                            kept_inner = [
                                ih for ih in inner
                                if not (
                                    isinstance(ih, dict)
                                    and isinstance(ih.get("command"), str)
                                    and any(
                                        marker in ih["command"]
                                        for marker in (
                                            "graph_recall", "graph_update",
                                            "session_close", "lesson_activate",
                                            "agam_watchdog",
                                            "graph-recall", "graph-update",
                                            "session-close", "lesson-activate",
                                            "agam-watchdog",
                                        )
                                    )
                                )
                            ]
                            if kept_inner:
                                new_entry = dict(entry)
                                new_entry["hooks"] = kept_inner
                                kept.append(new_entry)
                        if kept:
                            hooks[event] = kept
                        else:
                            del hooks[event]
                    # Also remove Agam-owned env vars.
                    env_block = settings.get("env", {})
                    if isinstance(env_block, dict):
                        for key in (
                            "AGAM_DATA_HOME",
                            "AGAM_HOME",
                            "AGAM_KG_PATH",
                            "AGAM_TOOLS_DIR",
                            "AGAM_HOOKS_DIR",
                            "AGAM_USER_ENTITY",
                        ):
                            env_block.pop(key, None)
                # Atomic write.
                import tempfile as _tf
                fd, tmpname = _tf.mkstemp(
                    prefix=".settings-uninstall-",
                    suffix=".json.tmp",
                    dir=str(p.parent),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(settings, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmpname, p)
                print(f"[agam uninstall] cleaned: {p}")
            elif action == "clean-cursor-hooks":
                settings = _json.loads(p.read_text(encoding="utf-8"))
                hooks = settings.get("hooks", {})
                if isinstance(hooks, dict):
                    for event, entries in list(hooks.items()):
                        if not isinstance(entries, list):
                            continue
                        kept = [
                            entry for entry in entries
                            if not (
                                isinstance(entry, dict)
                                and isinstance(entry.get("command"), str)
                                and any(
                                    marker in entry["command"]
                                    for marker in (
                                        "cursor_stop.py",
                                        "cursor_session_end.py",
                                        "/tools/agam/",
                                    )
                                )
                            )
                        ]
                        if kept:
                            hooks[event] = kept
                        else:
                            del hooks[event]
                env_block = settings.get("env", {})
                if isinstance(env_block, dict):
                    for key in ("AGAM_DATA_HOME", "AGAM_KG_PATH", "AGAM_TOOLS_DIR"):
                        env_block.pop(key, None)
                # Atomic write.
                import tempfile as _tf
                fd, tmpname = _tf.mkstemp(
                    prefix=".hooks-uninstall-",
                    suffix=".json.tmp",
                    dir=str(p.parent),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(settings, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.replace(tmpname, p)
                print(f"[agam uninstall] cleaned: {p}")
        except Exception as exc:  # noqa: BLE001
            msg = f"  {action} {p}: {exc}"
            errors.append(msg)
            print(f"[agam uninstall] FAILED: {msg}", file=sys.stderr)

    if errors:
        print(
            f"[agam uninstall] completed with {len(errors)} error(s). "
            f"Inspect manually if you want a clean slate.",
            file=sys.stderr,
        )
        return 1

    if args.purge:
        print("[agam uninstall] done. Data permanently deleted.")
    else:
        print(
            f"[agam uninstall] done. Data preserved as .uninstalled-{timestamp}. "
            f"Re-run with --purge to delete permanently."
        )
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
        description="Persistent knowledge-graph context for Claude Code and Cursor.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- init
    p_init = sub.add_parser(
        "init", help="Install Agam shared brain and agent wiring."
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Back up and overwrite an existing Agam install.",
    )
    p_init.add_argument(
        "--answers",
        type=str,
        default=None,
        help="Path to a YAML file with pre-built install answers.",
    )
    p_init.add_argument(
        "--target",
        action="append",
        choices=["claude", "cursor"],
        default=None,
        help="Agent(s) to wire (repeatable). Omit to auto-detect + prompt.",
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

    # -- tag-source
    p_tag = sub.add_parser(
        "tag-source",
        help="Backfill source-agent provenance on entities that lack it.",
    )
    p_tag.add_argument(
        "--agent",
        required=True,
        choices=["claude", "cursor"],
        help="Agent to attribute untagged entities to.",
    )
    p_tag.set_defaults(func=_cmd_tag_source)

    # -- obsolete
    p_obs = sub.add_parser(
        "obsolete",
        help="Mark a KG entity as no longer current (filtered from recall).",
    )
    p_obs.add_argument("entity", type=str, help="Entity name to obsolete.")
    p_obs.add_argument(
        "--reason",
        type=str,
        default="",
        help="Optional reason recorded as obsolete-reason property.",
    )
    p_obs.set_defaults(func=_cmd_obsolete)

    # -- repair
    p_rep = sub.add_parser(
        "repair",
        help="SQLite integrity check + VACUUM + REINDEX on the knowledge graph.",
    )
    p_rep.add_argument(
        "--dry-run",
        action="store_true",
        help="Run only the integrity check, skip VACUUM/REINDEX.",
    )
    p_rep.set_defaults(func=_cmd_repair)

    # -- digest
    p_dig = sub.add_parser(
        "digest",
        help="Summary of Agam activity in the last N days (default 1).",
    )
    p_dig.add_argument(
        "--since",
        type=int,
        default=1,
        help="Look back this many days (default 1).",
    )
    p_dig.set_defaults(func=_cmd_digest)

    # -- upgrade
    p_up = sub.add_parser(
        "upgrade",
        help="Replace Agam code, preserve identity files + KG + config.",
    )
    p_up.add_argument(
        "--keep-snapshot",
        action="store_true",
        help="Keep the temporary backup snapshot after upgrade (default: delete on success).",
    )
    p_up.set_defaults(func=_cmd_upgrade)

    # -- uninstall
    p_un = sub.add_parser(
        "uninstall",
        help="Remove Agam scaffolding. Default soft-uninstalls (preserves data).",
    )
    p_un.add_argument(
        "--confirm",
        action="store_true",
        help="Actually run the uninstall (default is dry-run).",
    )
    p_un.add_argument(
        "--purge",
        action="store_true",
        help="Permanently delete data instead of moving to .uninstalled-<ts>/.",
    )
    p_un.add_argument(
        "--target",
        action="append",
        choices=["claude", "cursor"],
        default=None,
        help="Agent wiring to remove (repeatable). Omit to auto-detect + prompt.",
    )
    p_un.set_defaults(func=_cmd_uninstall)

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

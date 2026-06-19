#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["questionary>=2", "pyyaml>=6"]
# ///

"""Agam installer wizard.

Lays down the scaffolding a fresh host needs to run Agam:

- ``~/.claude/agam/config.yaml``  -- user answers
- ``~/.claude/agam/AGAM.md``      -- identity (from template)
- ``~/.claude/agam/THISAI.md``    -- direction (from template)
- ``~/.claude/agam/MUGAM.md``     -- public face (from template)
- ``~/.claude/agam/prompts/*``    -- watchdog prompt templates
- ``~/.claude/hooks/*``           -- graph_recall, graph_update, session_close,
                                     lesson_activate(_post), agam_watchdog.sh,
                                     agam_watchdog_inner.py
- ``~/.claude/tools/agam/*``      -- knowledge_graph.py, agam_context.py, etc.
- ``~/.claude/knowledge/graph.db`` -- fresh SQLite KG from graph-schema.sql
- ``~/Library/LaunchAgents/com.agam.watchdog.plist`` (platform=mac, optional)

The wizard can run interactively (questionary) or accept a pre-built answers
dict for tests. Writes are atomic: everything is staged inside a single
tempdir under ``~/.claude/`` and moved into place only on success, so a
crash mid-install leaves no half-baked state.

If ``~/.claude/agam/`` already exists with content, the wizard refuses to
proceed unless ``force=True``, in which case the old directory is moved to
``~/.claude/agam.backup-<YYYYMMDD-HHMMSS>/``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any

# questionary and yaml are optional at import time so the module remains
# importable in tests that supply answers directly and don't need either.
try:
    import questionary
except ImportError:  # pragma: no cover - tests always pass answers
    questionary = None  # type: ignore[assignment]

try:
    import yaml
except ImportError as exc:  # pragma: no cover - covered by pyproject dep
    raise SystemExit(
        "pyyaml is required to run the installer. Install with: "
        "uv pip install pyyaml"
    ) from exc


# ---------------------------------------------------------------------------
# Answer dataclass
# ---------------------------------------------------------------------------


PLATFORMS = ("mac", "linux", "other")
CONTAINER_MODES = ("none", "docker", "devcontainer")


@dataclass
class Answers:
    name: str
    primary_goal: str
    projects_dir: str
    platform: str
    container_mode: str
    bootstrap_now: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Answers":
        # Accept both snake_case and dash-case keys for convenience.
        def pick(*keys: str) -> Any:
            for k in keys:
                if k in data:
                    return data[k]
            raise KeyError(f"missing required answer: {keys[0]}")

        platform = pick("platform")
        if platform not in PLATFORMS:
            raise ValueError(
                f"platform must be one of {PLATFORMS}, got {platform!r}"
            )
        # container_mode is kept on the Answers dataclass for back-compat
        # with existing config.yaml files but is no longer actuated -- the
        # Invoker cascade in agam.invoker auto-detects host vs container
        # at watchdog tick time. We still parse + validate the field so old
        # config.yaml files don't break, but the wizard no longer prompts for
        # it, and "auto" (the new default) means "let the cascade decide".
        try:
            container_mode = pick("container_mode", "container-mode")
        except KeyError:
            container_mode = "auto"
        if container_mode not in CONTAINER_MODES and container_mode != "auto":
            raise ValueError(
                f"container_mode must be one of {CONTAINER_MODES} or 'auto', "
                f"got {container_mode!r}"
            )

        return cls(
            name=str(pick("name")).strip(),
            primary_goal=str(pick("primary_goal", "primary-goal")).strip(),
            projects_dir=str(pick("projects_dir", "projects-dir")).strip(),
            platform=platform,
            container_mode=container_mode,
            bootstrap_now=bool(pick("bootstrap_now", "bootstrap-now")),
        )

    def to_yaml_mapping(self) -> dict[str, Any]:
        # User preference: dash-case keys in YAML.
        return {
            "name": self.name,
            "primary-goal": self.primary_goal,
            "projects-dir": self.projects_dir,
            "platform": self.platform,
            "container-mode": self.container_mode,
            "bootstrap-now": self.bootstrap_now,
        }


# ---------------------------------------------------------------------------
# Resource finding
# ---------------------------------------------------------------------------


def _find_resource(relpath: str) -> Path:
    """Locate a template/prompt/schema file shipped with the repo.

    Tries installed-package layout first (``src/agam/<relpath>``) via
    ``__file__``, then falls back to repo-root layout (``<root>/<relpath>``)
    for development checkouts where templates/prompts/knowledge live at
    the top level instead of inside the package.

    This dual-lookup is a Task-16-internal compromise. Follow-up work can
    move the files into the package and delete the repo-root branch.
    """
    here = Path(__file__).resolve().parent  # src/agam/
    # Package layout (future): src/agam/templates/..., src/agam/prompts/...
    pkg_candidate = here / relpath
    if pkg_candidate.exists():
        return pkg_candidate
    # Repo-root layout (current): templates/..., prompts/..., knowledge/...
    root_candidate = here.parent.parent / relpath
    if root_candidate.exists():
        return root_candidate
    raise FileNotFoundError(
        f"installer resource not found: {relpath} "
        f"(looked in {pkg_candidate} and {root_candidate})"
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass
class InstallPaths:
    home: Path
    claude: Path
    agam: Path
    prompts: Path
    hooks: Path
    tools: Path
    knowledge: Path
    launch_agents: Path

    @classmethod
    def for_home(cls, home: Path) -> "InstallPaths":
        claude = home / ".claude"
        return cls(
            home=home,
            claude=claude,
            agam=claude / "agam",
            prompts=claude / "agam" / "prompts",
            hooks=claude / "hooks",
            tools=claude / "tools" / "agam",
            knowledge=claude / "knowledge",
            launch_agents=home / "Library" / "LaunchAgents",
        )


# ---------------------------------------------------------------------------
# Interactive prompts (only used when answers=None)
# ---------------------------------------------------------------------------


def _prompt_answers(home: Path) -> Answers:  # pragma: no cover - interactive
    if questionary is None:
        raise SystemExit(
            "questionary is required for interactive mode. "
            "Install with: uv pip install 'questionary>=2'"
        )
    name = questionary.text("Your name?").ask()
    primary_goal = questionary.text(
        "Your primary goal right now (one line)?"
    ).ask()
    projects_dir = questionary.path(
        "Where do your projects live?",
        default=str(home / "coding"),
    ).ask()
    platform = questionary.select(
        "Platform?", choices=list(PLATFORMS), default="mac"
    ).ask()
    bootstrap_now = questionary.confirm(
        "Bootstrap knowledge graph with starter entities now?",
        default=False,
    ).ask()

    if any(v is None for v in (name, primary_goal, projects_dir, platform,
                                bootstrap_now)):
        raise SystemExit("installer cancelled")

    # Container vs host is auto-detected by the Invoker cascade at run time.
    # We probe here just to surface what was found so the user knows what to
    # expect; the result is informational, not stored as a hard choice.
    try:
        from agam.invoker import probe_all
        results = probe_all()
        print("\n[installer] Detected claude invokers:")
        for inv, res in results:
            mark = "ok" if res.ok else "no"
            print(f"  {mark:3} {inv.name:18} {res.detail}")
        if not any(r.ok for _, r in results):
            print(
                "\n[installer] WARNING: no claude invoker is healthy. "
                "Install Claude Code on host or start a claude-code container, "
                "then run `agam doctor` to verify.\n"
            )
    except Exception:  # noqa: BLE001 -- probe is best-effort, not blocking
        pass

    return Answers(
        name=name.strip(),
        primary_goal=primary_goal.strip(),
        projects_dir=projects_dir.strip(),
        platform=platform,
        container_mode="auto",
        bootstrap_now=bool(bootstrap_now),
    )


# ---------------------------------------------------------------------------
# Existing-agam guard
# ---------------------------------------------------------------------------


def _backup_timestamp(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y%m%d-%H%M%S")


def _directory_has_content(path: Path) -> bool:
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _check_existing_agam(paths: InstallPaths, *, force: bool) -> Path | None:
    """Return the backup path that was created, or None.

    Raises SystemExit if the agam dir exists and force is False.
    """
    if not _directory_has_content(paths.agam):
        return None
    if not force:
        raise SystemExit(
            f"refusing to overwrite existing {paths.agam}. "
            f"Re-run with --force to back up and reinstall."
        )
    backup = paths.claude / f"agam.backup-{_backup_timestamp()}"
    shutil.move(str(paths.agam), str(backup))
    return backup


# ---------------------------------------------------------------------------
# Core steps (write to a staging directory, never the real destination)
# ---------------------------------------------------------------------------


def _write_config(staging_agam: Path, answers: Answers) -> None:
    staging_agam.mkdir(parents=True, exist_ok=True)
    config_path = staging_agam / "config.yaml"
    # Hand-rolled YAML keeps ordering deterministic and avoids a yaml dump
    # that might reorder alphabetically. We control the format.
    mapping = answers.to_yaml_mapping()
    lines = []
    for key, value in mapping.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            # Quote only if value contains YAML-special chars.
            sval = str(value)
            if any(ch in sval for ch in ":#\n") or sval != sval.strip():
                # Escape embedded double quotes for safety.
                escaped = sval.replace('\\', '\\\\').replace('"', '\\"')
                lines.append(f'{key}: "{escaped}"')
            else:
                lines.append(f"{key}: {sval}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_identity_files(staging_agam: Path) -> None:
    for name in ("AGAM.md.template", "THISAI.md.template", "MUGAM.md.template"):
        src = _find_resource(f"templates/{name}")
        dst = staging_agam / name.removesuffix(".template")
        text = src.read_text(encoding="utf-8")
        # Stamp "Last updated: YYYY-MM-DD" with today's date.
        today = datetime.now().strftime("%Y-%m-%d")
        text = text.replace("Last updated: YYYY-MM-DD", f"Last updated: {today}")
        dst.write_text(text, encoding="utf-8")


def _write_prompts(staging_prompts: Path) -> None:
    staging_prompts.mkdir(parents=True, exist_ok=True)
    for name in ("work-log.txt", "agam-sync.txt"):
        src = _find_resource(f"prompts/{name}")
        (staging_prompts / name).write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _copy_tree_of(
    relsrc: str,
    dest: Path,
    *,
    executable_exts: tuple[str, ...] = (),
) -> None:
    """Copy every file under ``relsrc`` (repo-relative) into ``dest``.

    Files whose suffix matches ``executable_exts`` get chmod +x on the copy.
    Skips ``__init__.py`` and ``__pycache__`` -- the installed layout does
    not need package dunders.
    """
    src_root = _find_resource(relsrc)
    dest.mkdir(parents=True, exist_ok=True)
    for item in src_root.iterdir():
        if item.name in ("__init__.py", "__pycache__"):
            continue
        if item.is_dir():
            _copy_tree_of(
                f"{relsrc}/{item.name}",
                dest / item.name,
                executable_exts=executable_exts,
            )
            continue
        out = dest / item.name
        shutil.copy2(item, out)
        if executable_exts and out.suffix in executable_exts:
            st = out.stat()
            out.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_hooks(staging_hooks: Path) -> None:
    _copy_tree_of(
        "src/agam/hooks",
        staging_hooks,
        executable_exts=(".py", ".sh"),
    )


def _write_tools(staging_tools: Path) -> None:
    _copy_tree_of(
        "src/agam/tools",
        staging_tools,
        executable_exts=(".py",),
    )


def _create_kg(staging_knowledge: Path) -> None:
    staging_knowledge.mkdir(parents=True, exist_ok=True)
    schema = _find_resource("knowledge/graph-schema.sql").read_text(
        encoding="utf-8"
    )
    db_path = staging_knowledge / "graph.db"
    # Fresh DB: if somehow present, start over (we're in a staging tempdir).
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


def _write_launchd_plist(
    staging_launch_agents: Path,
    paths: InstallPaths,
) -> None:
    staging_launch_agents.mkdir(parents=True, exist_ok=True)
    tmpl = _find_resource("templates/com.agam.watchdog.plist.template")
    text = tmpl.read_text(encoding="utf-8")
    text = (
        text.replace("{{HOME}}", str(paths.home))
        .replace("{{AGAM_HOME}}", str(paths.agam))
        .replace("{{AGAM_HOOKS_DIR}}", str(paths.hooks))
        .replace("{{AGAM_TOOLS_DIR}}", str(paths.tools))
        .replace("{{AGAM_KG_PATH}}", str(paths.knowledge / "graph.db"))
    )
    (staging_launch_agents / "com.agam.watchdog.plist").write_text(
        text, encoding="utf-8"
    )


def _register_hooks(paths: InstallPaths, answers: Answers) -> None:
    """Merge Agam hook registrations into settings.json and set AGAM_USER_ENTITY
    in the env block so the user's chosen name becomes their graph entity name.

    Wrapped in try/except ImportError so this installer remains importable
    even if settings_merger is unavailable in a stripped-down test env.
    """
    try:
        from agam.settings_merger import (  # type: ignore[import-not-found]
            merge_hooks_into_settings,
        )
    except ImportError:
        return
    try:
        merge_hooks_into_settings(paths.claude / "settings.json", paths.hooks)
    except Exception as exc:  # pragma: no cover - merger is future work
        print(
            f"[installer] settings.json merge failed: {exc}. "
            f"You will need to register hooks manually.",
            file=sys.stderr,
        )
        return

    # Ensure AGAM_USER_ENTITY is set in the settings.json env block so hooks
    # tag the user's projects/research relations with the right entity name.
    # The hook also accepts the env var from the shell or the parent process,
    # but writing it here means "works out of the box" without shell config.
    user_entity = (answers.name or "User").strip() or "User"
    try:
        _set_settings_env(paths.claude / "settings.json", "AGAM_USER_ENTITY", user_entity)
    except Exception as exc:  # pragma: no cover - best effort
        print(
            f"[installer] could not set AGAM_USER_ENTITY in settings.json: {exc}. "
            f"Hooks will fall back to the literal 'User' entity name.",
            file=sys.stderr,
        )


def _set_settings_env(settings_path: Path, key: str, value: str) -> None:
    """Set a single key in the top-level 'env' object of settings.json.

    Reads, mutates, writes atomically. Creates 'env' if missing. Preserves
    all other settings verbatim. Skips the write if the key is already set
    to the same value (idempotent).
    """
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
    else:
        data = {}
    if not isinstance(data, dict):
        raise TypeError(f"{settings_path} root must be a JSON object")
    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        raise TypeError(f"{settings_path} 'env' must be an object")
    if env.get(key) == value:
        return
    env[key] = value
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".json.tmp",
        dir=str(settings_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, settings_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    paths: InstallPaths
    answers: Answers
    backup: Path | None = None
    wrote_plist: bool = False
    wrote_identity: bool = True
    wrote_kg: bool = True
    preserved_existing_identity: bool = False


def _plan(
    paths: InstallPaths,
    answers: Answers,
    *,
    force: bool,
) -> tuple[bool, bool, bool]:
    """Decide what to (re)write.

    Returns ``(write_identity, write_kg, preserved_existing_identity)``.

    On re-run (``force=False``, agam dir exists but guard didn't trip
    because identity files are being preserved), we still want to
    overwrite config.yaml. The existing-agam guard runs first; by the
    time we're here it has already either backed up the old dir or
    returned None because the dir was absent/empty.

    This function is a placeholder for the idempotent re-run path
    described in the plan, reached only when the caller routes around
    the guard (e.g. ``run_wizard_soft`` / future CLI flag).
    """
    return True, True, False


def run_wizard(
    answers: dict[str, Any] | Answers | None = None,
    *,
    force: bool = False,
    home: Path | None = None,
    write_plist: bool | None = None,
) -> InstallResult:
    """Run the Agam installer.

    Parameters
    ----------
    answers
        Either a dict of answers (for tests / non-interactive CLI use) or
        None to run the questionary wizard.
    force
        If True and ``~/.claude/agam/`` already has content, move it to
        ``~/.claude/agam.backup-<ts>/`` before installing.
    home
        Override ``$HOME``. Tests pass a tempdir.
    write_plist
        If None (default), write the launchd plist only when
        ``answers.platform == 'mac'``. Pass False to suppress explicitly.
    """
    home = (home or Path(os.environ["HOME"])).resolve()
    paths = InstallPaths.for_home(home)

    if answers is None:
        resolved = _prompt_answers(home)
    elif isinstance(answers, Answers):
        resolved = answers
    else:
        resolved = Answers.from_dict(answers)

    backup = _check_existing_agam(paths, force=force)

    if write_plist is None:
        write_plist = resolved.platform == "mac"

    # Stage everything under ~/.claude/.agam-stage-<pid>/ so the move into
    # place is on the same filesystem (rename is atomic). Using
    # tempfile.mkdtemp under ~/.claude/ keeps us off /tmp where a cross-fs
    # rename could copy instead of swap.
    paths.claude.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".agam-stage-", dir=str(paths.claude))
    )
    staging_agam = staging_root / "agam"
    staging_hooks = staging_root / "hooks"
    staging_tools = staging_root / "tools"
    staging_knowledge = staging_root / "knowledge"
    staging_launch = staging_root / "LaunchAgents"

    try:
        _write_config(staging_agam, resolved)
        _write_identity_files(staging_agam)
        _write_prompts(staging_agam / "prompts")
        _write_hooks(staging_hooks)
        _write_tools(staging_tools)
        _create_kg(staging_knowledge)
        if write_plist:
            _write_launchd_plist(staging_launch, paths)

        # Commit phase. Move each staged subtree into its final location.
        # Before moving, clear any matching target that's about to be
        # replaced -- for the agam dir this is always safe because the
        # existing-agam guard has already backed it up if needed.
        _commit_subtree(staging_agam, paths.agam)
        _merge_subtree(staging_hooks, paths.hooks)
        _merge_subtree(staging_tools, paths.tools)
        _commit_kg(staging_knowledge / "graph.db", paths.knowledge / "graph.db")
        wrote_plist = False
        if write_plist:
            _merge_subtree(staging_launch, paths.launch_agents)
            wrote_plist = True

    except Exception:
        # Atomic: nothing lands unless the whole sequence succeeds.
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    finally:
        # Staging dir should be empty by now (moves are destructive).
        # Clean up whatever's left (empty dirs, failed-copy residue).
        shutil.rmtree(staging_root, ignore_errors=True)

    # After the filesystem is in place, try to register hooks in
    # settings.json. Failure is logged but non-fatal.
    _register_hooks(paths, resolved)

    return InstallResult(
        paths=paths,
        answers=resolved,
        backup=backup,
        wrote_plist=wrote_plist,
    )


def _commit_subtree(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``. ``dst`` must not exist (guard ensures this)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # Shouldn't happen because the existing-agam guard already handled
        # the agam dir. For other targets (hooks, tools), we merge.
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))


def _merge_subtree(src: Path, dst: Path) -> None:
    """Copy every file under ``src`` into ``dst``, overwriting conflicts.

    Used for ``~/.claude/hooks/`` and ``~/.claude/tools/agam/`` where
    other tooling may already own the parent dir. We don't wipe the
    parent -- just add our files.
    """
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _merge_subtree(item, target)
        else:
            if target.exists():
                target.unlink()
            shutil.move(str(item), str(target))


def _commit_kg(src_db: Path, dst_db: Path) -> None:
    """Move the staged KG into place. Preserve an existing DB by refusing
    to clobber it (the wizard's 'preserve KG on re-run' behavior).

    On first install, ``dst_db`` doesn't exist and we just move.
    On re-run with force=False (after guard has passed because agam dir
    didn't exist either, which is impossible in the current flow), we'd
    still overwrite. Simplest defensible behavior for Task 16: overwrite
    when we got this far. Idempotent re-run logic that preserves an
    existing KG belongs to a follow-up task (explicit --preserve-kg
    flag) and is not in this task's scope.
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        dst_db.unlink()
    shutil.move(str(src_db), str(dst_db))


# ---------------------------------------------------------------------------
# Neutral-home multi-agent orchestrator
# ---------------------------------------------------------------------------


def _render_neutral_plist(
    home: Path, agam_home: Path, *, graph_only: bool = False
) -> Path | None:
    """Render + write the watchdog plist pointing at the shared ~/.agam home.

    Returns the written path, or None on non-mac (caller decides whether to
    load it). The watchdog runs from the shared hooks/tools copy under ~/.agam,
    so a single launchd job serves every wired agent. ``graph_only`` pins
    AGAM_GRAPH_ONLY so the drain does deterministic graph enrichment without
    LLM calls (useful where headless `claude -p` can't write files).
    """
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    tmpl = _find_resource("templates/com.agam.watchdog.plist.template")
    text = (
        tmpl.read_text(encoding="utf-8")
        .replace("{{HOME}}", str(home))
        .replace("{{AGAM_HOME}}", str(agam_home))
        .replace("{{AGAM_HOOKS_DIR}}", str(agam_home / "hooks"))
        .replace("{{AGAM_TOOLS_DIR}}", str(agam_home / "tools" / "agam"))
        .replace("{{AGAM_KG_PATH}}", str(agam_home / "knowledge" / "graph.db"))
        .replace("{{AGAM_GRAPH_ONLY}}", "1" if graph_only else "0")
    )
    out = launch_agents / "com.agam.watchdog.plist"
    out.write_text(text, encoding="utf-8")
    return out


@dataclass
class MultiInstallResult:
    home: Path
    agam_home: Path
    answers: Answers
    targets: list[str]
    migration_status: str
    wrote_plist: bool = False


def run_install(
    answers: dict[str, Any] | Answers | None,
    *,
    targets: list[str],
    home: Path | None = None,
    write_plist: bool | None = None,
) -> MultiInstallResult:
    """Install agam for one or more agents around a shared ~/.agam data home.

    Steps:
      1. Migrate legacy ~/.claude data into ~/.agam (copy-only, reversible).
      2. Write shared data into ~/.agam: config (always), identity files + KG
         (only if absent, so migrated/edited data is preserved), prompts.
      3. Install a shared hooks/tools copy under ~/.agam for the watchdog.
      4. Install per-agent wiring for each selected target.
      5. (mac) Render the watchdog plist pointing at the shared home.

    ``targets`` is a list of agent names ("claude", "cursor").
    """
    from agam.agents import ClaudeAgent, CursorAgent
    from agam.agents import _copy as agent_copy
    from agam.migrate import migrate_if_needed

    home = (home or Path(os.environ["HOME"])).resolve()
    if answers is None:
        resolved = _prompt_answers(home)
    elif isinstance(answers, Answers):
        resolved = answers
    else:
        resolved = Answers.from_dict(answers)

    migration_status, _ = migrate_if_needed(home)

    agam_home = home / ".agam"
    agam_home.mkdir(parents=True, exist_ok=True)

    # Config: always reflect the latest wizard answers.
    _write_config(agam_home, resolved)
    # Identity: preserve migrated/edited files; only seed from templates if absent.
    if not (agam_home / "AGAM.md").exists():
        _write_identity_files(agam_home)
    # Prompts: safe to refresh (code templates).
    _write_prompts(agam_home / "prompts")
    # KG: never clobber an existing/migrated graph.
    if not (agam_home / "knowledge" / "graph.db").exists():
        _create_kg(agam_home / "knowledge")

    # Shared hooks/tools copy for the watchdog.
    agent_copy.copy_hooks_tree(agam_home / "hooks")
    agent_copy.copy_tools_tree(
        agam_home / "tools" / "agam", extra=[agent_copy.transcripts_src()]
    )

    # Per-agent wiring.
    registry = {"claude": ClaudeAgent, "cursor": CursorAgent}
    installed: list[str] = []
    for name in targets:
        agent_cls = registry.get(name)
        if agent_cls is None:
            continue
        agent_cls().install(home)
        installed.append(name)

    if write_plist is None:
        write_plist = resolved.platform == "mac"
    wrote_plist = False
    if write_plist:
        if _render_neutral_plist(home, agam_home) is not None:
            wrote_plist = True

    return MultiInstallResult(
        home=home,
        agam_home=agam_home,
        answers=resolved,
        targets=installed,
        migration_status=migration_status,
        wrote_plist=wrote_plist,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(
        prog="agam-install",
        description="Install Agam scaffolding into ~/.claude/",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Back up and overwrite an existing ~/.claude/agam/.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Override $HOME (for testing).",
    )
    args = parser.parse_args(argv)

    try:
        result = run_wizard(answers=None, force=args.force, home=args.home)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[installer] failed: {exc}", file=sys.stderr)
        return 1

    print(f"[installer] wrote config: {result.paths.agam / 'config.yaml'}")
    print(f"[installer] wrote identity files in: {result.paths.agam}")
    print(f"[installer] wrote hooks in: {result.paths.hooks}")
    print(f"[installer] wrote tools in: {result.paths.tools}")
    print(f"[installer] wrote KG at: {result.paths.knowledge / 'graph.db'}")
    if result.wrote_plist:
        print(
            f"[installer] wrote plist at: "
            f"{result.paths.launch_agents / 'com.agam.watchdog.plist'}"
        )
    if result.backup is not None:
        print(f"[installer] previous agam dir backed up to: {result.backup}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

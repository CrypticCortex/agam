"""Tests for the ``agam`` CLI entry point.

Every test monkeypatches away the real installer / bootstrap / subprocess
work. The CLI's job is argparse wiring, YAML loading, and a confirm prompt;
none of the heavy lifting below it should ever run in these tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Tiny mock result so CLI can read .paths / .backup without crashing
# ---------------------------------------------------------------------------


@dataclass
class _MockPaths:
    agam: Path = Path("/tmp/agam")
    hooks: Path = Path("/tmp/hooks")
    tools: Path = Path("/tmp/tools")
    knowledge: Path = Path("/tmp/knowledge")
    launch_agents: Path = Path("/tmp/LaunchAgents")


@dataclass
class _MockAnswers:
    """Minimal stand-in for installer.Answers used by CLI auto-chain logic."""
    name: str = "Alice"
    bootstrap_now: bool = False
    projects_dir: str = "/tmp/projects"


@dataclass
class MockResult:
    success: bool = True
    paths: _MockPaths = None  # type: ignore[assignment]
    backup: Path | None = None
    wrote_plist: bool = False
    answers: _MockAnswers = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.paths is None:
            self.paths = _MockPaths()
        if self.answers is None:
            self.answers = _MockAnswers()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_cli_init_dispatches_to_installer(monkeypatch):
    from agam import cli

    called: list[dict] = []

    def fake_run_wizard(**kw):
        called.append(kw)
        return MockResult()

    monkeypatch.setattr("agam.installer.run_wizard", fake_run_wizard)

    rc = cli.main(["init"])
    assert called, "run_wizard was never invoked"
    assert rc == 0


def test_cli_init_passes_force(monkeypatch):
    from agam import cli

    called: list[dict] = []
    monkeypatch.setattr(
        "agam.installer.run_wizard",
        lambda **kw: called.append(kw) or MockResult(),
    )

    rc = cli.main(["init", "--force"])
    assert rc == 0
    assert called[0]["force"] is True


def test_cli_init_with_answers_yaml(monkeypatch, tmp_path):
    from agam import cli

    answers_path = tmp_path / "a.yaml"
    answers_path.write_text(
        "name: Alice\n"
        "primary-goal: ship\n"
        "projects-dir: /tmp\n"
        "platform: mac\n"
        "container-mode: none\n"
        "bootstrap-now: false\n"
    )

    called: list[dict] = []
    monkeypatch.setattr(
        "agam.installer.run_wizard",
        lambda **kw: called.append(kw) or MockResult(),
    )

    rc = cli.main(["init", "--answers", str(answers_path)])
    assert rc == 0
    assert called[0]["answers"]["name"] == "Alice"
    assert called[0]["answers"]["projects-dir"] == "/tmp"


def test_cli_init_chains_bootstrap_when_bootstrap_now_true(monkeypatch, tmp_path):
    """If the wizard captures bootstrap_now=True, init must auto-invoke
    the bootstrap command. Without this wire-up the wizard question is
    cosmetic and users have to run a second command anyway."""
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    answers = _MockAnswers(name="Alice", bootstrap_now=True, projects_dir=str(tmp_path))
    monkeypatch.setattr(
        "agam.installer.run_wizard",
        lambda **kw: MockResult(answers=answers),
    )

    bootstrap_calls: list[object] = []
    monkeypatch.setattr("agam.cli._cmd_bootstrap", lambda ns: bootstrap_calls.append(ns) or 0)
    monkeypatch.setattr("agam.cli._launchctl_bootstrap", lambda paths: None)

    rc = cli.main(["init"])
    assert rc == 0
    assert bootstrap_calls, "bootstrap_now=True must trigger _cmd_bootstrap"
    ns = bootstrap_calls[0]
    assert getattr(ns, "projects") == str(tmp_path)
    assert getattr(ns, "days") == 30
    assert getattr(ns, "yes") is False, "must still show the cost preview to user"


def test_cli_init_does_not_chain_bootstrap_when_bootstrap_now_false(
    monkeypatch, tmp_path
):
    """The default (bootstrap_now=False) must NOT trigger bootstrap.

    Otherwise users who decline during the wizard would still get billed
    for an unwanted scan.
    """
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    answers = _MockAnswers(name="Alice", bootstrap_now=False)
    monkeypatch.setattr(
        "agam.installer.run_wizard",
        lambda **kw: MockResult(answers=answers),
    )

    bootstrap_calls: list[object] = []
    monkeypatch.setattr("agam.cli._cmd_bootstrap", lambda ns: bootstrap_calls.append(ns) or 0)
    monkeypatch.setattr("agam.cli._launchctl_bootstrap", lambda paths: None)

    rc = cli.main(["init"])
    assert rc == 0
    assert not bootstrap_calls, "bootstrap must NOT fire when user declined"


def test_cli_init_returns_1_on_systemexit(monkeypatch):
    from agam import cli

    def raiser(**kw):
        raise SystemExit("refusing to overwrite")

    monkeypatch.setattr("agam.installer.run_wizard", raiser)

    rc = cli.main(["init"])
    assert rc == 1


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _patch_bootstrap_preview(monkeypatch, tmp_path, token_count=1000):
    """Patch scan + token count + estimate so cost preview is non-interactive."""
    fake_files = [tmp_path / "s1.jsonl"]
    monkeypatch.setattr(
        "agam.bootstrap.scan_transcripts", lambda *a, **kw: fake_files
    )
    monkeypatch.setattr(
        "agam.bootstrap.count_tokens_in_file", lambda p: token_count
    )
    monkeypatch.setattr("agam.bootstrap.estimate_cost", lambda *a, **kw: 0.001)


def test_cli_bootstrap_dispatches(monkeypatch, tmp_path):
    from agam import cli

    called: list = []

    def fake_run_bootstrap(projects_dir, **kw):
        called.append((projects_dir, kw))
        return {"entities": [{"name": "X"}], "relationships": []}

    monkeypatch.setattr("agam.bootstrap.run_bootstrap", fake_run_bootstrap)
    _patch_bootstrap_preview(monkeypatch, tmp_path)

    rc = cli.main(["bootstrap", "--projects", str(tmp_path), "--yes"])
    assert rc == 0
    assert called, "run_bootstrap was never invoked"
    projects_dir, kw = called[0]
    assert Path(projects_dir) == tmp_path


def test_cli_bootstrap_all_flag_sets_days_none(monkeypatch, tmp_path):
    from agam import cli

    seen_days = []

    def fake_scan(projects_dir, days=30):
        seen_days.append(days)
        return []

    monkeypatch.setattr("agam.bootstrap.scan_transcripts", fake_scan)
    monkeypatch.setattr("agam.bootstrap.count_tokens_in_file", lambda p: 0)
    monkeypatch.setattr("agam.bootstrap.estimate_cost", lambda *a, **kw: 0.0)
    monkeypatch.setattr(
        "agam.bootstrap.run_bootstrap",
        lambda projects_dir, **kw: {"entities": [], "relationships": []},
    )

    rc = cli.main(
        ["bootstrap", "--projects", str(tmp_path), "--all", "--yes"]
    )
    assert rc == 0
    assert seen_days[0] is None


def test_cli_bootstrap_prompts_without_yes(monkeypatch, tmp_path):
    from agam import cli

    inputs = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    _patch_bootstrap_preview(monkeypatch, tmp_path)

    bootstrap_called = []
    monkeypatch.setattr(
        "agam.bootstrap.run_bootstrap",
        lambda *a, **kw: bootstrap_called.append(1)
        or {"entities": [], "relationships": []},
    )

    rc = cli.main(["bootstrap", "--projects", str(tmp_path)])
    assert rc == 1
    assert not bootstrap_called


def test_cli_bootstrap_prompts_with_yes_input(monkeypatch, tmp_path):
    from agam import cli

    inputs = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    _patch_bootstrap_preview(monkeypatch, tmp_path)

    bootstrap_called = []
    monkeypatch.setattr(
        "agam.bootstrap.run_bootstrap",
        lambda *a, **kw: bootstrap_called.append(1)
        or {"entities": [], "relationships": []},
    )

    rc = cli.main(["bootstrap", "--projects", str(tmp_path)])
    assert rc == 0
    assert bootstrap_called


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_cli_status_no_crash(monkeypatch, tmp_path, capsys):
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("agam.bootstrap._discover_container", lambda: None)

    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Agam home" in out or "Container" in out


def test_cli_status_with_container(monkeypatch, tmp_path, capsys):
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "agam.bootstrap._discover_container", lambda: "claude-code-abc"
    )

    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-code-abc" in out


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_cli_obsolete_sets_status_property(monkeypatch, tmp_path):
    """`agam obsolete <name>` sets status=obsolete and obsoleted-at on the entity."""
    import sqlite3
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))

    # Spin up a fresh KG (using the schema from installer).
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    kg = tmp_path / ".claude" / "knowledge" / "graph.db"
    conn = sqlite3.connect(str(kg))
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('stale-feature-bug', 'bug', 'Dropped from output schema.', "
        "datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["obsolete", "stale-feature-bug", "--reason", "removed entirely"])
    assert rc == 0

    conn = sqlite3.connect(str(kg))
    rows = dict(conn.execute(
        "SELECT p.key, p.value FROM properties p "
        "JOIN entities e ON p.entity_id = e.id "
        "WHERE LOWER(e.name) = 'stale-feature-bug'"
    ).fetchall())
    conn.close()
    assert rows.get("status") == "obsolete"
    assert "obsoleted-at" in rows
    assert rows.get("obsolete-reason") == "removed entirely"


def test_cli_obsolete_missing_entity_returns_1(monkeypatch, tmp_path):
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    rc = cli.main(["obsolete", "nonexistent-entity"])
    assert rc == 1


def test_cli_repair_ok_on_healthy_kg(monkeypatch, tmp_path, capsys):
    """A freshly initialized KG should pass integrity_check + repair clean."""
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    rc = cli.main(["repair"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "integrity_check: OK" in out


def test_cli_digest_runs_on_fresh_install(monkeypatch, tmp_path, capsys):
    """Digest on a fresh install: 0 new entities is a valid state, exit 0."""
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    rc = cli.main(["digest", "--since", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Knowledge graph:" in out


def test_cli_uninstall_dry_run_is_default(monkeypatch, tmp_path, capsys):
    """Default `agam uninstall` (no --confirm) must be a dry run."""
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    # Files must still exist after dry-run.
    assert (tmp_path / ".claude" / "agam" / "AGAM.md").exists()


def test_cli_uninstall_confirm_moves_data_dirs(monkeypatch, tmp_path, capsys):
    """`agam uninstall --confirm` moves agam/ + knowledge/ to .uninstalled-<ts>/."""
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    agam_dir = tmp_path / ".claude" / "agam"
    assert agam_dir.exists()

    rc = cli.main(["uninstall", "--confirm"])
    assert rc == 0
    # Original gone, .uninstalled-<ts>/ sibling appears.
    assert not agam_dir.exists()
    backups = list((tmp_path / ".claude").glob("agam.uninstalled-*"))
    assert len(backups) == 1, f"expected one backup dir, got {backups}"


def test_cli_upgrade_preserves_identity_and_kg(monkeypatch, tmp_path):
    """`agam upgrade` must not blow away the user's AGAM.md edits or KG data."""
    import sqlite3
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )

    # Simulate user edits to identity + KG.
    agam_md = tmp_path / ".claude" / "agam" / "AGAM.md"
    agam_md.write_text("MY CUSTOM IDENTITY EDIT\n")
    kg = tmp_path / ".claude" / "knowledge" / "graph.db"
    conn = sqlite3.connect(str(kg))
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('my-custom-entity', 'note', 'user-added', datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["upgrade"])
    assert rc == 0
    assert agam_md.read_text() == "MY CUSTOM IDENTITY EDIT\n", (
        "upgrade clobbered the user's AGAM.md edits"
    )
    conn = sqlite3.connect(str(kg))
    n = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE name = 'my-custom-entity'"
    ).fetchone()[0]
    conn.close()
    assert n == 1, "upgrade wiped the user's KG entities"


def test_cli_doctor_fails_on_fresh_home(monkeypatch, tmp_path, capsys):
    """Empty HOME -> doctor flags every missing thing, returns 1."""
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("agam.bootstrap._discover_container", lambda: None)

    rc = cli.main(["doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out, "doctor must flag missing identity files on fresh HOME"
    assert "fix:" in out, "doctor must suggest a fix for each failure"
    assert "agam init" in out, "the recommended fix should be agam init"


def test_cli_doctor_passes_after_init(monkeypatch, tmp_path, capsys):
    """After a successful init the critical checks should pass.

    Container + credentials may still WARN (they depend on user env), but
    no FAIL should appear and the exit code should be 0.
    """
    import platform

    if platform.system() != "Darwin":
        pytest.skip("doctor's launchd check is mac-specific; skip elsewhere")

    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("agam.bootstrap._discover_container", lambda: None)

    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path / "projects"),
            "platform": "mac",
            "container_mode": "none",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )

    # Stub out launchctl print so the doctor doesn't shell out to the host
    # launchd. Returning rc=0 simulates "plist loaded".
    import subprocess as _sp
    orig_run = _sp.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "launchctl":
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_run)

    # Mock the invoker cascade so doctor sees at least one healthy invoker
    # (otherwise it would correctly FAIL on the "no invokers" check; this
    # test is about identity/KG/hooks scaffolding, not invoker health).
    from agam.invoker import HostInvoker, ProbeResult
    monkeypatch.setattr(
        "agam.invoker.probe_all",
        lambda: [(HostInvoker(), ProbeResult(True, "host claude ready", "fast"))],
    )

    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    # Identity + KG + hooks must pass.
    assert "[FAIL]" not in out, f"doctor should not FAIL on a fresh install:\n{out}"
    assert "All checks passed" in out
    assert rc == 0


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_cli_reset_dry_run(monkeypatch, tmp_path, capsys):
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".claude" / ".agam-bootstrap-state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")

    rc = cli.main(["reset"])
    assert rc == 0
    assert state.exists(), "reset without --confirm must not remove files"
    out = capsys.readouterr().out.lower()
    assert "would remove" in out or str(state).lower() in out


def test_cli_reset_with_confirm_removes_state(monkeypatch, tmp_path):
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".claude" / ".agam-bootstrap-state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")
    candidates = tmp_path / ".claude" / ".agam-bootstrap-candidates.json"
    candidates.write_text("{}")

    rc = cli.main(["reset", "--confirm"])
    assert rc == 0
    assert not state.exists()
    assert not candidates.exists()


def test_cli_reset_preserves_identity_and_kg(monkeypatch, tmp_path):
    from agam import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    agam_dir = tmp_path / ".claude" / "agam"
    agam_dir.mkdir(parents=True)
    identity = agam_dir / "AGAM.md"
    identity.write_text("# Identity")
    kg = tmp_path / ".claude" / "knowledge" / "graph.db"
    kg.parent.mkdir(parents=True)
    kg.write_bytes(b"sqlite")

    rc = cli.main(["reset", "--confirm"])
    assert rc == 0
    assert identity.exists(), "reset must not touch identity files"
    assert kg.exists(), "reset must not touch knowledge graph"


# ---------------------------------------------------------------------------
# unknown
# ---------------------------------------------------------------------------


def test_cli_unknown_command_exits():
    from agam import cli

    with pytest.raises(SystemExit):
        cli.main(["unknown"])


def test_cli_no_args_exits():
    from agam import cli

    # No subcommand at all -- argparse should exit nonzero.
    with pytest.raises(SystemExit):
        cli.main([])


# ---------------------------------------------------------------------------
# Round-trip + recovery tests covering bugs the original suite missed
# ---------------------------------------------------------------------------


def test_cli_obsolete_matches_camelcase_input(monkeypatch, tmp_path):
    """`agam obsolete VoiceFNOL` must match the kebab-case `voice-fnol`
    entity that the normal write path stores. Without input normalization
    the old `LOWER(name) = LOWER(?)` SQL turned "VoiceFNOL" into "voicefnol"
    and missed the actual row -- silent failure for every camelCase input."""
    import sqlite3
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice",
            "primary_goal": "test",
            "projects_dir": str(tmp_path),
            "platform": "linux",
            "container_mode": "auto",
            "bootstrap_now": False,
        },
        home=tmp_path,
    )
    kg = tmp_path / ".claude" / "knowledge" / "graph.db"
    # Mimic the write path: store as kebab-case.
    conn = sqlite3.connect(str(kg))
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('voice-fnol', 'project', 'Voice FNOL.', datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["obsolete", "VoiceFNOL", "--reason", "merged"])
    assert rc == 0, "camelCase input must resolve to the kebab-case entity"

    conn = sqlite3.connect(str(kg))
    rows = dict(conn.execute(
        "SELECT p.key, p.value FROM properties p "
        "JOIN entities e ON p.entity_id = e.id WHERE e.name = 'voice-fnol'"
    ).fetchall())
    conn.close()
    assert rows.get("status") == "obsolete"


def test_cli_obsolete_matches_snake_case_input(monkeypatch, tmp_path):
    """snake_case input must also normalize to kebab-case before lookup."""
    import sqlite3
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice", "primary_goal": "test",
            "projects_dir": str(tmp_path), "platform": "linux",
            "container_mode": "auto", "bootstrap_now": False,
        },
        home=tmp_path,
    )
    kg = tmp_path / ".claude" / "knowledge" / "graph.db"
    conn = sqlite3.connect(str(kg))
    conn.execute(
        "INSERT INTO entities (name, type, description, created, updated) "
        "VALUES ('my-feature', 'feature', '', datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()
    rc = cli.main(["obsolete", "my_feature"])
    assert rc == 0


def test_cli_upgrade_keeps_snapshot_on_failure(monkeypatch, tmp_path, capsys):
    """If upgrade raises mid-way, the snapshot directory MUST survive so the
    user can recover. The original finally-block deleted it unconditionally,
    so users following the printed recovery instructions found nothing."""
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice", "primary_goal": "test",
            "projects_dir": str(tmp_path), "platform": "linux",
            "container_mode": "auto", "bootstrap_now": False,
        },
        home=tmp_path,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("simulated upgrade failure mid-install")

    monkeypatch.setattr("agam.installer.run_wizard", boom)

    rc = cli.main(["upgrade"])
    assert rc == 1
    err = capsys.readouterr().err
    # The error message points the user at the snapshot path. Parse it.
    import re
    m = re.search(r"snapshot of your data is at (\S+?)\.\s", err)
    assert m, f"expected snapshot path in stderr, got: {err}"
    snapshot_dir = Path(m.group(1))
    assert snapshot_dir.exists(), (
        f"snapshot was deleted by the finally block: {snapshot_dir}"
    )
    # And it should contain the user's preserved files.
    assert (snapshot_dir / "AGAM.md").exists()


def test_cli_upgrade_cleans_snapshot_on_success(monkeypatch, tmp_path):
    """Happy-path upgrade: snapshot is cleaned up so we don't leak tmp dirs."""
    import re
    import sys as _sys
    from agam import cli, installer

    monkeypatch.setenv("HOME", str(tmp_path))
    installer.run_wizard(
        answers={
            "name": "Alice", "primary_goal": "test",
            "projects_dir": str(tmp_path), "platform": "linux",
            "container_mode": "auto", "bootstrap_now": False,
        },
        home=tmp_path,
    )

    snapshot_seen: list[Path] = []
    orig_mkdtemp = __import__("tempfile").mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        d = orig_mkdtemp(*args, **kwargs)
        snapshot_seen.append(Path(d))
        return d

    monkeypatch.setattr("tempfile.mkdtemp", tracked_mkdtemp)

    rc = cli.main(["upgrade"])
    assert rc == 0
    assert snapshot_seen, "tempfile.mkdtemp was never called"
    # On success the snapshot MUST be cleaned up.
    for s in snapshot_seen:
        if s.name.startswith(".agam-upgrade-snap-"):
            assert not s.exists(), f"snapshot survived a successful upgrade: {s}"

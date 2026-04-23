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
class MockResult:
    success: bool = True
    paths: _MockPaths = None  # type: ignore[assignment]
    backup: Path | None = None
    wrote_plist: bool = False

    def __post_init__(self):
        if self.paths is None:
            self.paths = _MockPaths()


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

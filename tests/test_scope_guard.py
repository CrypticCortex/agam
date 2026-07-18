"""Synthetic tests for the Codex scoped-knowledge privacy guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agam.hooks import scope_guard
from agam.hooks.scope_guard import DENIAL, evaluate_hook


HOOK = Path(__file__).parents[1] / "src" / "agam" / "hooks" / "scope_guard.py"


@pytest.fixture
def roots(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    restricted = (
        knowledge / "sealed",
        knowledge / "staging",
        knowledge / "scopes" / "vault_111111111111111111111111",
        knowledge / "scopes" / "vault_222222222222222222222222",
    )
    allowed = (
        knowledge / "scopes" / "vault_aaaaaaaaaaaaaaaaaaaaaaaa",
        knowledge / "scopes" / "vault_bbbbbbbbbbbbbbbbbbbbbbbb",
    )
    for root in (*restricted, *allowed):
        root.mkdir(parents=True)
        (root / "graph.db").write_text("synthetic")
    return knowledge, restricted, allowed


def _bash(command: object, *, cwd: Path) -> dict[str, object]:
    return {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}


@pytest.mark.parametrize(
    "scope",
    ["sealed", "staging", "vault_111111111111111111111111", "vault_222222222222222222222222"],
)
def test_denies_direct_absolute_restricted_paths(roots, scope):
    knowledge, restricted, _ = roots
    target = next(path for path in restricted if path.name == scope) / "graph.db"

    decision = evaluate_hook(
        _bash(f"sqlite3 {target}", cwd=knowledge),
        restricted_roots=restricted,
        home=knowledge.parent,
    )

    assert decision == DENIAL


def test_denies_relative_traversal_and_quoted_or_escaped_arguments(roots):
    knowledge, restricted, _ = roots
    cwd = knowledge / "projects" / "nested"
    cwd.mkdir(parents=True)
    spaced = knowledge / "sealed" / "synthetic file.db"
    spaced.write_text("synthetic")
    escaped_spaced = str(spaced).replace(" ", "\\ ")

    commands = (
        "sqlite3 ../../scopes/vault_222222222222222222222222/graph.db",
        f"sqlite3 '{spaced}'",
        f"sqlite3 {escaped_spaced}",
        f"sqlite3 '{knowledge / 'scopes' / 'vault_222222222222222222222222' / 'graph.db'}",
    )
    for command in commands:
        assert evaluate_hook(
            _bash(command, cwd=cwd),
            restricted_roots=restricted,
            home=knowledge.parent,
        ) == DENIAL


def test_denies_home_expansions_without_executing_shell_expansion(roots):
    knowledge, restricted, _ = roots
    home = knowledge.parent
    relative = knowledge.relative_to(home) / "scopes" / "vault_222222222222222222222222" / "graph.db"

    for reference in (f"~/{relative}", f"$HOME/{relative}", f"${{HOME}}/{relative}"):
        assert evaluate_hook(
            _bash(f"sqlite3 {reference}", cwd=home),
            restricted_roots=restricted,
            home=home,
        ) == DENIAL


def test_denies_symlink_aliases_and_nonexistent_children(roots, tmp_path):
    knowledge, restricted, _ = roots
    alias = tmp_path / "innocent-alias"
    alias.symlink_to(
        knowledge / "scopes" / "vault_222222222222222222222222",
        target_is_directory=True,
    )

    for target in (alias / "graph.db", alias / "future.db"):
        assert evaluate_hook(
            _bash(f"sqlite3 {target}", cwd=tmp_path),
            restricted_roots=restricted,
            home=tmp_path,
        ) == DENIAL


def test_denies_path_bearing_tool_when_cwd_is_restricted(roots):
    knowledge, restricted, _ = roots
    restricted_cwd = knowledge / "sealed"

    assert evaluate_hook(
        _bash("ls", cwd=restricted_cwd),
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


@pytest.mark.parametrize(
    "command",
    [
        "cat {scopes}/vault_2*/graph.db",
        "cat {scopes}/{{vault_222222222222222222222222}}/graph.db",
        "SCOPE=vault_222222222222222222222222; cat {scopes}/$SCOPE/graph.db",
        "SCOPE=vault_222222222222222222222222; cat {scopes}/${{SCOPE}}/graph.db",
    ],
)
def test_denies_dynamic_shell_paths_that_can_expand_into_restricted_roots(
    roots, command
):
    knowledge, restricted, _ = roots

    assert evaluate_hook(
        _bash(command.format(scopes=knowledge / "scopes"), cwd=knowledge),
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_denies_chained_relative_navigation_into_restricted_roots(roots):
    knowledge, restricted, _ = roots

    assert evaluate_hook(
        _bash("cd scopes; cd vault_222222222222222222222222; cat graph.db", cwd=knowledge),
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


@pytest.mark.parametrize(
    "command_template",
    [
        "base='{scopes}'; target=\"$base/vault_222222222222222222222222/graph.db\"; cat \"$target\"",
        "python -c \"from pathlib import Path; Path('{scopes}/' + 'vault_222222222222222222222222/graph.db').read_text()\"",
        "cat {scopes}/vault_222222222222\\\n222222222222/graph.db",
        "cat $'{scopes}/vault_22222222222222222222222\\x32/graph.db'",
    ],
)
def test_denies_composed_or_escaped_restricted_command_paths(
    roots, command_template
):
    knowledge, restricted, _ = roots

    assert evaluate_hook(
        _bash(
            command_template.format(scopes=knowledge / "scopes"),
            cwd=knowledge,
        ),
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_denies_mcp_relative_path_using_tool_input_workdir(roots):
    knowledge, restricted, allowed = roots
    payload = {
        "tool_name": "mcp__synthetic__read",
        "cwd": str(knowledge),
        "tool_input": {
            "workdir": str(allowed[0]),
            "path": "../vault_222222222222222222222222/graph.db",
        },
    }

    assert evaluate_hook(
        payload,
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_denies_named_user_tilde_alias_without_opening_it():
    home = Path.home()
    restricted = home / ".synthetic-scope-guard" / "vault_222222222222222222222222"
    username = home.name
    payload = _bash(
        f"cat ~{username}/.synthetic-scope-guard/vault_222222222222222222222222/graph.db",
        cwd=home,
    )

    assert evaluate_hook(
        payload,
        restricted_roots=(restricted,),
        home=home,
    ) == DENIAL


@pytest.mark.parametrize(
    "template",
    [
        "python -c \"open('{path}').read()\"",
        "python -c \"from pathlib import Path; Path('{path}').read_bytes()\"",
        "sqlite3 'file:{path}?mode=ro'",
        "knowledge-tool --database={path}",
    ],
)
def test_denies_paths_embedded_in_python_sqlite_and_tool_arguments(roots, template):
    knowledge, restricted, _ = roots
    target = knowledge / "scopes" / "vault_111111111111111111111111" / "graph.db"

    assert evaluate_hook(
        _bash(template.format(path=target), cwd=knowledge),
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_denies_direct_file_tool_inputs_and_apply_patch_headers(roots):
    knowledge, restricted, _ = roots
    target = knowledge / "sealed" / "graph.db"
    payloads = (
        {
            "tool_name": "Read",
            "cwd": str(knowledge),
            "tool_input": {"file_path": str(target)},
        },
        {
            "tool_name": "apply_patch",
            "cwd": str(knowledge),
            "tool_input": {"command": f"*** Update File: {target}\n"},
        },
    )

    for payload in payloads:
        assert evaluate_hook(
            payload,
            restricted_roots=restricted,
            home=knowledge.parent,
        ) == DENIAL


def test_denies_nested_and_new_path_bearing_tool_fields(roots):
    knowledge, restricted, _ = roots
    target = knowledge / "sealed" / "graph.db"
    payloads = (
        {
            "tool_name": "mcp__synthetic__read",
            "cwd": str(knowledge),
            "tool_input": {"request": {"path": str(target)}},
        },
        {
            "tool_name": "mcp__synthetic__exec",
            "cwd": str(knowledge),
            "tool_input": {
                "request": {
                    "command": "cd scopes; cd vault_222222222222222222222222; cat graph.db"
                }
            },
        },
        {
            "tool_name": "image_gen",
            "cwd": str(knowledge),
            "tool_input": {"referenced_image_paths": [str(target)]},
        },
    )

    for payload in payloads:
        assert evaluate_hook(
            payload,
            restricted_roots=restricted,
            home=knowledge.parent,
        ) == DENIAL


def test_denies_wrong_case_alias_on_case_insensitive_filesystem(roots):
    knowledge, restricted, _ = roots
    canonical = knowledge / "scopes" / "vault_222222222222222222222222" / "graph.db"
    wrong_case = knowledge / "scopes" / "VAULT_222222222222222222222222" / "graph.db"
    if not wrong_case.exists() or not os.path.samefile(canonical, wrong_case):
        pytest.skip("fixture volume is case-sensitive")

    assert evaluate_hook(
        {
            "tool_name": "Read",
            "cwd": str(knowledge),
            "tool_input": {"file_path": str(wrong_case)},
        },
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_allows_selected_vault_paths_and_similar_prefixes(roots):
    knowledge, restricted, allowed = roots
    similar = knowledge / "scopes" / "vault_notes" / "graph.db"
    similar.parent.mkdir()
    similar.write_text("synthetic")

    for target in (allowed[0] / "graph.db", allowed[1] / "graph.db", similar):
        assert evaluate_hook(
            _bash(f"sqlite3 '{target}'", cwd=knowledge),
            restricted_roots=restricted,
            home=knowledge.parent,
        ) is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"tool_name": 7, "tool_input": {"command": "sqlite3 harmless.db"}},
        {"tool_name": "Unrelated", "tool_input": None},
    ],
)
def test_malformed_unrelated_hook_payloads_are_safe_no_ops(roots, payload):
    knowledge, restricted, _ = roots

    assert evaluate_hook(
        payload,
        restricted_roots=restricted,
        home=knowledge.parent,
    ) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": None},
        {"tool_name": "Bash", "tool_input": {"command": 7}},
        {"tool_name": "Read", "tool_input": {}},
        {"tool_name": "apply_patch", "tool_input": {"command": None}},
    ],
)
def test_malformed_path_bearing_hook_payloads_fail_closed(roots, payload):
    knowledge, restricted, _ = roots

    assert evaluate_hook(
        payload,
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_malformed_tool_name_cannot_hide_a_direct_restricted_path(roots):
    knowledge, restricted, _ = roots
    target = knowledge / "sealed" / "graph.db"
    payload = {
        "tool_name": None,
        "cwd": str(knowledge),
        "tool_input": {"path": str(target)},
    }

    assert evaluate_hook(
        payload,
        restricted_roots=restricted,
        home=knowledge.parent,
    ) == DENIAL


def test_denial_is_exact_and_never_echoes_command_or_path(roots):
    knowledge, restricted, _ = roots
    marker = "SYNTHETIC_PRIVATE_MARKER"
    target = knowledge / "sealed" / marker / "graph.db"
    payload = _bash(f"sqlite3 '{target}'", cwd=knowledge)

    rendered = json.dumps(
        evaluate_hook(payload, restricted_roots=restricted, home=knowledge.parent)
    )

    assert json.loads(rendered) == DENIAL
    assert marker not in rendered
    assert str(target) not in rendered
    assert "sqlite3" not in rendered


def test_cli_reads_synthetic_roots_from_environment_and_fails_closed_on_bad_json(
    roots,
):
    knowledge, restricted, _ = roots
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(knowledge.parent),
        "AGAM_KNOWLEDGE_SCOPE_ROOT": str(knowledge / "scopes"),
    }
    denied_results = []
    for target in (
        knowledge / "scopes" / "vault_222222222222222222222222" / "graph.db",
        knowledge / "sealed" / "graph.db",
        knowledge / "graph.db",
    ):
        denied_results.append(
            subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(_bash(f"sqlite3 {target}", cwd=knowledge)),
                text=True,
                capture_output=True,
                env=environment,
                timeout=10,
            )
        )
    malformed = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not-json",
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )

    for denied in denied_results:
        assert denied.returncode == 0
        assert json.loads(denied.stdout) == DENIAL
        assert denied.stderr == ""
    assert malformed.returncode == 0
    assert json.loads(malformed.stdout) == DENIAL
    assert malformed.stderr == ""


def test_cli_malformed_json_with_visible_restricted_path_fails_closed(roots):
    knowledge, _, _ = roots
    marker = "SYNTHETIC_PRIVATE_MARKER"
    target = knowledge / "sealed" / marker / "graph.db"
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=f'{{"tool_name":"Bash","tool_input":{{"command":"sqlite3 {target}"',
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(knowledge.parent),
            "AGAM_KNOWLEDGE_SCOPE_ROOT": str(knowledge / "scopes"),
        },
        timeout=10,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == DENIAL
    assert marker not in result.stdout
    assert str(target) not in result.stdout


def test_cli_unexpected_internal_failure_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(
        scope_guard,
        "_configured_roots",
        lambda _environment: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )

    scope_guard.main()

    assert json.loads(capsys.readouterr().out) == DENIAL


@pytest.mark.parametrize(
    "relative",
    [
        "graph.db-wal",
        "graph.db-shm",
        "graph.db-journal",
        "graph.db.bak-synthetic",
        "entity-names.txt",
        "concept-index.json",
        "idf-index.json",
        "sycophancy-log.jsonl",
    ],
)
def test_cli_denies_legacy_database_sidecars(roots, relative):
    knowledge, _, _ = roots
    target = knowledge / relative
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(_bash(f"cat {target}", cwd=knowledge)),
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(knowledge.parent),
            "AGAM_KNOWLEDGE_SCOPE_ROOT": str(knowledge / "scopes"),
        },
        timeout=10,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == DENIAL

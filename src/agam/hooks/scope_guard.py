#!/usr/bin/env python3
"""Block Codex tool access to physically restricted knowledge scopes.

The guard only parses hook input. It never invokes a shell, imports user code,
opens a referenced path, or includes command/path content in its response.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


_MAX_INPUT_CHARS = 1_000_000
_MAX_NESTING = 32
_MAX_CANDIDATES = 4096
_HOME_RE = re.compile(r"(?<![A-Za-z0-9_])(?:\$HOME|\$\{HOME\})(?=/|\b)")
_DYNAMIC_PATH_RE = re.compile(r"(?<!\\)[*?\[\]{}$`]")
_ABSOLUTE_RE = re.compile(r"(?:file:)?/[^\s\"'`;|&<>(){},]+")
_RELATIVE_RE = re.compile(r"(?:\.\.?/)+(?:[^\s\"'`;|&<>(){},]+)")
_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")
_PATH_KEYS = frozenset(
    {
        "file_path",
        "path",
        "paths",
        "database",
        "database_path",
        "db",
        "source",
        "destination",
        "target",
        "referenced_image_paths",
    }
)
_COMMAND_KEYS = frozenset({"command", "cmd", "script", "code", "query"})
_PATH_BEARING_TOOLS = frozenset(
    {"bash", "shell", "execcommand", "read", "edit", "write", "multiedit", "applypatch"}
)
_VAULT_ID_RE = re.compile(r"vault_[0-9a-f]{24}")

DENIAL = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "agam_restricted_knowledge",
    }
}


def _denial() -> dict[str, object]:
    return {"hookSpecificOutput": dict(DENIAL["hookSpecificOutput"])}


def _normalized_tool_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _malformed_path_bearing_input(tool_name: str, tool_input: object) -> bool:
    normalized = _normalized_tool_name(tool_name)
    if normalized not in _PATH_BEARING_TOOLS:
        return False
    if not isinstance(tool_input, dict):
        return True
    if normalized in {"bash", "shell", "execcommand", "applypatch"}:
        return not isinstance(tool_input.get("command"), str)
    return not any(
        isinstance(value, str) and bool(value)
        or isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, str) and bool(item) for item in value)
        for key, value in tool_input.items()
        if key in _PATH_KEYS
    )


def _expand_home(value: str, home: Path) -> str:
    expanded = _HOME_RE.sub(str(home), value)
    if expanded == "~":
        return str(home)
    if expanded.startswith("~/"):
        return str(home / expanded[2:])
    if expanded.startswith("~"):
        named_home = os.path.expanduser(expanded)
        if named_home != expanded:
            return named_home
    return expanded


def _normalize_static_shell_text(value: str) -> str:
    """Join shell continuations and literal concatenations without executing."""
    normalized = value.replace("\\\r\n", "").replace("\\\n", "")
    for quote in ("'", '"'):
        pattern = re.compile(
            re.escape(quote)
            + r"([^\n"
            + re.escape(quote)
            + r"]*)"
            + re.escape(quote)
            + r"\s*\+\s*"
            + re.escape(quote)
            + r"([^\n"
            + re.escape(quote)
            + r"]*)"
            + re.escape(quote)
        )
        while True:
            joined, count = pattern.subn(
                lambda match: (
                    quote + match.group(1) + match.group(2) + quote
                ),
                normalized,
            )
            normalized = joined
            if count == 0:
                break
    return normalized


def _looks_path_like(value: str) -> bool:
    return (
        value.startswith(("/", "./", "../", "~/", "file:/"))
        or "/" in value
        or value.endswith((".db", ".sqlite", ".sqlite3"))
    )


def _is_path_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = _normalized_tool_name(value)
    return (
        value.lower() in _PATH_KEYS
        or normalized.endswith("path")
        or normalized.endswith("paths")
        or normalized in {"file", "filename", "uri"}
    )


def _is_command_key(value: object) -> bool:
    return isinstance(value, str) and value.lower() in _COMMAND_KEYS


def _command_candidates(command: str, home: Path) -> set[str]:
    """Extract path-shaped arguments without performing shell evaluation."""
    expanded = _expand_home(_normalize_static_shell_text(command), home)
    candidates: set[str] = set()
    try:
        tokens = shlex.split(expanded, posix=True)
    except ValueError:
        # An unmatched quote must not hide an otherwise visible restricted path.
        tokens = expanded.split()

    for token in tokens:
        token = token.strip("\t\r\n;|&<>(){},")
        if "=" in token:
            _, assigned = token.split("=", 1)
            if assigned:
                candidates.add(assigned)
        if _looks_path_like(token):
            candidates.add(token)

    candidates.update(_ABSOLUTE_RE.findall(expanded))
    candidates.update(_RELATIVE_RE.findall(expanded))
    for quoted in _QUOTED_RE.findall(expanded):
        if _looks_path_like(quoted):
            candidates.add(quoted)
    return candidates


def _extract_candidates(
    tool_input: dict[str, object], home: Path
) -> tuple[set[str], tuple[str, ...]]:
    candidates: set[str] = set()
    commands: list[str] = []

    def visit(value: object, *, key: object = None, depth: int = 0) -> None:
        if depth > _MAX_NESTING or len(candidates) > _MAX_CANDIDATES:
            raise ValueError("candidate_limit")
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                visit(nested_value, key=nested_key, depth=depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for nested_value in value:
                visit(nested_value, key=key, depth=depth + 1)
            return
        if not isinstance(value, str):
            return
        if _is_command_key(key):
            commands.append(value)
            candidates.update(_command_candidates(value, home))
        elif _is_path_key(key) or _looks_path_like(value):
            candidates.add(value)
        if len(candidates) > _MAX_CANDIDATES:
            raise ValueError("candidate_limit")

    visit(tool_input)

    # Codex apply_patch places patch headers in ``command``. The generic
    # command parser normally sees them, but explicit extraction keeps paths
    # with spaces intact and does not interpret patch contents.
    command = tool_input.get("command")
    if isinstance(command, str):
        candidates.update(
            match.group(1).strip()
            for match in re.finditer(
                r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$",
                command,
                re.MULTILINE,
            )
        )
    return candidates, tuple(commands)


def _canonical_candidate(value: str, *, cwd: Path, home: Path) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = _expand_home(value.strip(), home)
    candidate = candidate.strip("\t\r\n\"'`;|&<>(){},")
    if candidate.startswith("file:"):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        if parsed.netloc not in ("", "localhost"):
            return None
        candidate = unquote(parsed.path)
    elif candidate.startswith("/") and "?" in candidate:
        candidate = candidate.split("?", 1)[0]
    if not candidate:
        return None

    path = Path(candidate)
    if not path.is_absolute():
        path = cwd / path
    try:
        # strict=False follows every existing symlink ancestor while still
        # canonicalizing a not-yet-created final component.
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _canonical_root(value: str | Path, home: Path) -> Path | None:
    return _canonical_candidate(str(value), cwd=home, home=home)


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        pass
    else:
        return True

    # Path.resolve() preserves the caller's spelling on case-insensitive
    # volumes. Compare each existing ancestor by filesystem identity so a
    # wrong-case alias cannot bypass lexical containment.
    current = candidate
    while True:
        try:
            if current.exists() and root.exists() and os.path.samefile(current, root):
                return True
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _same_path(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _restricted_match(candidate: Path, root: Path) -> bool:
    if _within(candidate, root):
        return True
    # The legacy source database can leave backup generations whose suffixes
    # are not enumerable. They are equally sealed evidence.
    return (
        root.name == "graph.db"
        and _same_path(candidate.parent, root.parent)
        and candidate.name.startswith("graph.db.")
    )


def _dynamic_path_can_reach_root(
    value: str, *, cwd: Path, home: Path, roots: tuple[Path, ...]
) -> bool:
    expanded = _expand_home(value.strip(), home)
    match = _DYNAMIC_PATH_RE.search(expanded)
    if match is None:
        return False
    prefix = expanded[: match.start()].rstrip("\t\r\n\"'`;|&<>(){},")
    if "/" in prefix:
        prefix = prefix.rsplit("/", 1)[0] or "/"
    else:
        prefix = "."
    base = _canonical_candidate(prefix, cwd=cwd, home=home)
    if base is None:
        return True
    return any(_within(base, root) or _within(root, base) for root in roots)


def _navigation_is_sensitive(
    command: str, *, cwd: Path, home: Path, roots: tuple[Path, ...]
) -> bool:
    """Conservatively follow explicit shell directory changes."""
    try:
        lexer = shlex.shlex(
            _expand_home(_normalize_static_shell_text(command), home),
            posix=True,
            punctuation_chars=";&|",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return True

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(token)

    current = cwd
    for segment in segments:
        if not segment:
            continue
        offset = 0
        while offset < len(segment) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[offset]
        ):
            offset += 1
        if offset >= len(segment) or segment[offset] not in {"cd", "pushd"}:
            continue
        offset += 1
        if offset < len(segment) and segment[offset] == "--":
            offset += 1
        target_value = segment[offset] if offset < len(segment) else str(home)
        target = _canonical_candidate(target_value, cwd=current, home=home)
        if target is None:
            return True
        if any(
            _within(target, root) or _same_path(target, root.parent)
            for root in roots
        ):
            return True
        current = target
    return False


def evaluate_hook(
    payload: object,
    *,
    restricted_roots: tuple[str | Path, ...] | list[str | Path],
    home: str | Path | None = None,
) -> dict[str, object] | None:
    """Return a content-free denial for a positive restricted-path match."""
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if isinstance(tool_name, str) and _malformed_path_bearing_input(
        tool_name, tool_input
    ):
        return _denial()
    if not isinstance(tool_input, dict):
        return None

    try:
        home_path = Path(home if home is not None else os.environ.get("HOME", "~"))
        home_path = home_path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError):
        return _denial()
    cwd_value = payload.get("cwd")
    try:
        cwd = (
            Path(cwd_value).resolve(strict=False)
            if isinstance(cwd_value, str) and cwd_value
            else Path.cwd().resolve(strict=False)
        )
    except (OSError, RuntimeError):
        cwd = home_path

    tool_cwd_value = next(
        (
            tool_input[key]
            for key in ("workdir", "working_directory")
            if isinstance(tool_input.get(key), str) and tool_input[key]
        ),
        None,
    )
    if tool_cwd_value is not None:
        tool_cwd = _canonical_candidate(
            tool_cwd_value, cwd=cwd, home=home_path
        )
        if tool_cwd is None:
            return _denial()
        cwd = tool_cwd

    roots = tuple(
        root
        for raw_root in restricted_roots
        if (root := _canonical_root(raw_root, home_path)) is not None
    )
    if restricted_roots and not roots:
        return _denial()
    if not roots:
        return None

    if (
        isinstance(tool_name, str)
        and _normalized_tool_name(tool_name) in _PATH_BEARING_TOOLS
        and any(
            _restricted_match(cwd, restricted_root)
            for restricted_root in roots
        )
    ):
        return _denial()

    try:
        candidates, commands = _extract_candidates(tool_input, home_path)
        for command in commands:
            if _navigation_is_sensitive(
                command, cwd=cwd, home=home_path, roots=roots
            ):
                return _denial()
        for raw_candidate in candidates:
            if _dynamic_path_can_reach_root(
                raw_candidate, cwd=cwd, home=home_path, roots=roots
            ):
                return _denial()
            candidate = _canonical_candidate(
                raw_candidate, cwd=cwd, home=home_path
            )
            if candidate is not None and any(
                _restricted_match(candidate, restricted_root)
                for restricted_root in roots
            ):
                return _denial()
    except Exception:
        return _denial()
    return None


def _configured_roots(environment: dict[str, str]) -> tuple[str, ...]:
    roots = [
        value
        for value in environment.get(
            "AGAM_RESTRICTED_KNOWLEDGE_ROOTS", ""
        ).split(os.pathsep)
        if value
    ]
    scopes_roots = {
        value
        for name in ("AGAM_KNOWLEDGE_SCOPE_ROOT", "AGAM_SCOPES_ROOT")
        if (value := environment.get(name))
    }
    data_home = environment.get("AGAM_DATA_HOME")
    if data_home:
        scopes_roots.add(str(Path(data_home) / "knowledge" / "scopes"))
    for scopes_root in scopes_roots:
        scope_path = Path(scopes_root)
        registry_path = scope_path / "registry.json"
        agent = environment.get("AGAM_RECALL_AGENT", "codex")
        try:
            descriptor = os.open(
                registry_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
                    raise ValueError
                registry = json.load(handle)
            if set(registry) != {"schema_version", "vaults", "agents"}:
                raise ValueError
            vaults = registry["vaults"]
            agents = registry["agents"]
            if not isinstance(vaults, list) or not isinstance(agents, dict):
                raise ValueError
            selected = agents.get(agent, [])
            if (
                not isinstance(selected, list)
                or not all(isinstance(item, str) for item in selected)
            ):
                raise ValueError
            vault_ids = []
            for vault in vaults:
                vault_id = vault.get("id") if isinstance(vault, dict) else None
                if (
                    not isinstance(vault_id, str)
                    or _VAULT_ID_RE.fullmatch(vault_id) is None
                ):
                    raise ValueError
                vault_ids.append(vault_id)
            if not set(selected).issubset(vault_ids):
                raise ValueError
            roots.extend(
                str(scope_path / vault_id)
                for vault_id in vault_ids
                if vault_id not in selected
            )
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            roots.append(str(scope_path))
        roots.extend(
            (
                # The canonical layout places source and every classified
                # staging copy in the sealed sibling of ``scopes``.
                str(scope_path.parent / "sealed"),
                # The pre-migration shared graph remains sealed evidence and
                # is never a Codex fallback.
                str(scope_path.parent / "graph.db"),
                str(scope_path.parent / "graph.db-wal"),
                str(scope_path.parent / "graph.db-shm"),
                str(scope_path.parent / "graph.db-journal"),
                str(scope_path.parent / "entity-names.txt"),
                str(scope_path.parent / "concept-index.json"),
                str(scope_path.parent / "idf-index.json"),
                str(scope_path.parent / "sycophancy-log.jsonl"),
            )
        )
    for name in ("AGAM_SEALED_KNOWLEDGE_ROOT", "AGAM_STAGING_ROOT"):
        if environment.get(name):
            roots.append(environment[name])
    return tuple(roots)


def main() -> None:
    try:
        environment = dict(os.environ)
        roots = _configured_roots(environment)
        home = environment.get("HOME", "~")
        raw = sys.stdin.read(_MAX_INPUT_CHARS + 1)
        if len(raw) > _MAX_INPUT_CHARS:
            print(json.dumps(_denial(), separators=(",", ":"), ensure_ascii=True))
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(
                json.dumps(_denial(), separators=(",", ":"), ensure_ascii=True)
            )
            return
        decision = evaluate_hook(
            payload,
            restricted_roots=roots,
            home=home,
        )
        if decision is not None:
            print(json.dumps(decision, separators=(",", ":"), ensure_ascii=True))
    except Exception:
        print(json.dumps(_denial(), separators=(",", ":"), ensure_ascii=True))


if __name__ == "__main__":
    main()

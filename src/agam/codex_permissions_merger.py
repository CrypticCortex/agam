"""Install a selectable Codex filesystem boundary for scoped knowledge.

The permission profile denies the entire knowledge directory and reopens only
policy metadata plus the vaults explicitly selected for Codex. This is the authorization
boundary; the PreToolUse path parser remains defense in depth.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agam.vault_registry import RegistryError, load_registry


PERMISSION_PROFILE = "agam_scoped"

_BEGIN = "# agam:scoped-knowledge:begin"
_END = "# agam:scoped-knowledge:end"
_DESCRIPTION = "Workspace access with Agam restricted knowledge denied."
_MAX_CONFIG_BYTES = 5 * 1024 * 1024


class PermissionProfileError(RuntimeError):
    """Content-free permission-profile failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PermissionProfileStatus:
    configured: bool
    select_required: bool
    legacy_sandbox_conflict: bool
    profile: str = PERMISSION_PROFILE


def _toml_string(value: str) -> str:
    # JSON basic strings are a compatible, conservative TOML subset for the
    # absolute paths emitted here.
    return json.dumps(value, ensure_ascii=True)


def _filesystem_rules(knowledge_root: Path, agent: str) -> dict[str, str]:
    knowledge = knowledge_root.expanduser().resolve(strict=False)
    scopes = knowledge / "scopes"
    rules = {
        str(knowledge): "deny",
        str(scopes / "registry.json"): "read",
        str(scopes / "config.json"): "read",
        str(scopes / "active.json"): "read",
        str(scopes / "manifests"): "read",
    }
    try:
        registry = load_registry(scopes / "registry.json")
    except RegistryError:
        return rules
    for vault in registry.selected_for(agent):
        rules[str(scopes / vault.id)] = "read"
    return rules


def _render_block(knowledge_root: Path, agent: str) -> str:
    lines = [
        _BEGIN,
        f"[permissions.{PERMISSION_PROFILE}]",
        f"description = {_toml_string(_DESCRIPTION)}",
        'extends = ":workspace"',
        "",
        f"[permissions.{PERMISSION_PROFILE}.filesystem]",
    ]
    lines.extend(
        f"{_toml_string(path)} = {_toml_string(access)}"
        for path, access in _filesystem_rules(knowledge_root, agent).items()
    )
    lines.append(_END)
    return "\n".join(lines) + "\n"


def _without_owned_block(text: str) -> tuple[str, bool]:
    begin_count = text.count(_BEGIN)
    end_count = text.count(_END)
    if begin_count == end_count == 0:
        return text, False
    if begin_count != 1 or end_count != 1:
        raise PermissionProfileError("invalid_managed_permission_block")
    start = text.index(_BEGIN)
    if text.find(_END) < start:
        raise PermissionProfileError("invalid_managed_permission_block")
    end = text.index(_END, start) + len(_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:start] + text[end:], True


def _parse_config(text: str) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        raise PermissionProfileError("invalid_codex_config") from None
    if not isinstance(parsed, dict):
        raise PermissionProfileError("invalid_codex_config")
    return parsed


def _read_regular_config(path: Path) -> str:
    if path.is_symlink():
        raise PermissionProfileError("unsafe_config_path")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _MAX_CONFIG_BYTES
            ):
                raise PermissionProfileError("unsafe_config_path")
            return handle.read()
    except FileNotFoundError:
        return ""
    except PermissionProfileError:
        raise
    except (OSError, UnicodeError):
        raise PermissionProfileError("codex_config_unavailable") from None


def _has_legacy_sandbox(parsed: dict[str, Any]) -> bool:
    return "sandbox_mode" in parsed or "sandbox_workspace_write" in parsed


def _expected_profile(knowledge_root: Path, agent: str) -> dict[str, Any]:
    return {
        "description": _DESCRIPTION,
        "extends": ":workspace",
        "filesystem": _filesystem_rules(knowledge_root, agent),
    }


def _atomic_write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".config-", suffix=".toml.tmp", dir=str(path.parent)
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    except PermissionProfileError:
        raise
    except OSError:
        raise PermissionProfileError("codex_config_unavailable") from None


def inspect_permission_profile(
    config_path: str | os.PathLike[str],
    knowledge_root: str | os.PathLike[str],
    *,
    agent: str = "codex",
) -> PermissionProfileStatus:
    path = Path(config_path)
    root = Path(knowledge_root)
    try:
        text = _read_regular_config(path)
        parsed = _parse_config(text)
    except PermissionProfileError:
        return PermissionProfileStatus(False, False, False)
    permissions = parsed.get("permissions")
    profile = (
        permissions.get(PERMISSION_PROFILE)
        if isinstance(permissions, dict)
        else None
    )
    configured = profile == _expected_profile(root, agent)
    conflict = _has_legacy_sandbox(parsed)
    return PermissionProfileStatus(
        configured=configured,
        select_required=configured and not conflict,
        legacy_sandbox_conflict=configured and conflict,
    )


def merge_permission_profile(
    config_path: str | os.PathLike[str],
    knowledge_root: str | os.PathLike[str],
    *,
    agent: str = "codex",
) -> PermissionProfileStatus:
    """Atomically add or refresh Agam's namespaced permission profile."""
    path = Path(config_path)
    root = Path(knowledge_root)
    original = _read_regular_config(path)
    base, owned = _without_owned_block(original)
    parsed_base = _parse_config(base)
    permissions = parsed_base.get("permissions")
    if (
        not owned
        and isinstance(permissions, dict)
        and PERMISSION_PROFILE in permissions
    ):
        raise PermissionProfileError("permission_profile_conflict")

    prefix = base.rstrip()
    rendered = (prefix + "\n\n" if prefix else "") + _render_block(root, agent)
    # Parse before replacing the user's file; malformed generated TOML must
    # never become their active Codex configuration.
    parsed_rendered = _parse_config(rendered)
    profile = parsed_rendered.get("permissions", {}).get(PERMISSION_PROFILE)
    if profile != _expected_profile(root, agent):
        raise PermissionProfileError("invalid_generated_permission_profile")
    if rendered != original:
        _atomic_write(path, rendered)
    conflict = _has_legacy_sandbox(parsed_rendered)
    return PermissionProfileStatus(
        configured=True,
        select_required=not conflict,
        legacy_sandbox_conflict=conflict,
    )

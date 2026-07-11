"""Transcript format adapters.

Claude Code, Cursor, and Codex store session transcripts as JSONL, but with
different shapes:

- Claude:  ``{"type":"user", "message":{...}, ...}`` per event.
- Cursor:  ``{"role":"user", "message":{"content":[...]}}`` for turns, plus bare
           ``{"type":"turn_ended", "status":...}`` event markers.
- Codex:   ``{"type":"event_msg", "payload":{"type":"user_message", ...}}``
           for UI events and ``response_item`` records for messages/tool calls.

Cursor also flushes its transcript lazily (at session end, not per turn), so the
heuristics here lean on whole-file raw-text scanning rather than precise event
walking -- robust to partial writes and to the exact tool-call JSON shape, which
is not yet pinned down.

The graph_update extraction (project paths, git branches, npm packages) already
scans raw transcript text with regexes, so it is format-agnostic; only the
user-turn counting and the "did real work happen" gate differ per agent. Those
live here.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass, field
from typing import Iterator

# Shared signal vocabulary: a session tail mentioning one of these reads as
# "real work happened" rather than a throwaway Q&A.
SIGNAL_RE = re.compile(
    r"\b(shipped|deployed|committed|decided|fixed|broke|learned|built|"
    r"resolved|debugged|implemented|merged|released)\b",
    re.IGNORECASE,
)

# Evidence that the agent edited code: a Cursor write/edit tool name, or a
# file_path key, anywhere in the raw transcript. Kept broad on purpose because
# the exact tool-call JSON shape is unconfirmed.
_EDIT_EVIDENCE_RE = re.compile(
    r'"(?:Write|StrReplace|Edit|MultiEdit|EditNotebook)"|"file_path"',
)

# Codex's rollout schema is deliberately treated as an evolving wire format.
# These are semantic names rather than an exhaustive list of payload types, so
# older/newer call envelopes can still be recognized without parsing every
# internal event Codex records.
_CODEX_EDIT_TOOL_NAMES = {
    "applypatch",
    "edit",
    "editnotebook",
    "multiedit",
    "strreplace",
    "write",
}
_CODEX_PATCH_INPUT_RE = re.compile(
    r"(?:\*\*\*\s+Begin\s+Patch|\btools\.apply_patch\s*\(|\bapply_patch\s*<<)",
    re.IGNORECASE,
)
_CODEX_TOOL_CALL_TYPES = {
    "custom_tool_call",
    "function_call",
    "local_shell_call",
    "tool_call",
}
_CODEX_TOOL_OUTPUT_TYPES = {
    "custom_tool_call_output",
    "function_call_output",
    "local_shell_call_output",
    "tool_call_output",
}
_CODEX_TEXT_CAP = 20_000
_CODEX_TOOL_INPUT_CAP = 8_000
_CODEX_TOOL_OUTPUT_CAP = 2_000


def _jsonl_objects(path) -> Iterator[dict]:
    """Yield dictionary records from a possibly partial/malformed JSONL file."""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    yield value
    except (OSError, TypeError, ValueError):
        return


def _codex_text_parts(value) -> list[str]:
    """Extract visible text from known and plausible future content envelopes."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_codex_text_parts(item))
        return parts
    if not isinstance(value, dict):
        return []

    # Do not persist opaque reasoning or image data. Text blocks currently use
    # input_text/output_text, but accepting plain "text" is a safe fallback.
    kind = str(value.get("type", ""))
    if kind in {"encrypted_content", "image", "input_image", "reasoning"}:
        return []
    text = value.get("text")
    if isinstance(text, str):
        return [text]
    message = value.get("message")
    if isinstance(message, str):
        return [message]
    if "content" in value:
        return _codex_text_parts(value.get("content"))
    return []


def _codex_payload(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _codex_call_name(payload: dict) -> str:
    for key in ("name", "tool_name", "tool"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("name")
            if isinstance(nested, str) and nested:
                return nested
    return ""


def _codex_call_input(payload: dict):
    for key in ("arguments", "input", "command", "params"):
        if key in payload:
            return payload.get(key)
    return None


def _codex_stringify(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _codex_is_edit_event(event: dict) -> bool:
    payload = _codex_payload(event)
    payload_type = str(payload.get("type", "")).lower()

    # Current Codex emits this after apply_patch, including a map keyed by the
    # changed absolute paths. It is the strongest edit signal in a rollout.
    if event.get("type") == "event_msg" and payload_type in {
        "patch_apply_end",
        "file_edit_end",
        "file_write_end",
    }:
        changes = payload.get("changes")
        if payload.get("success") is False:
            return False
        return bool(changes) or payload.get("status") in {"completed", "success"}

    if event.get("type") != "response_item" or payload_type not in _CODEX_TOOL_CALL_TYPES:
        return False
    normalized_name = re.sub(r"[^a-z0-9]", "", _codex_call_name(payload).lower())
    if normalized_name in _CODEX_EDIT_TOOL_NAMES:
        return True

    # Some Codex builds expose a unified exec call whose input dispatches the
    # actual apply_patch tool. Only match invocation/patch markers, not a casual
    # mention of the tool name in user or assistant prose.
    if normalized_name in {"exec", "execcommand", "shell"}:
        return bool(_CODEX_PATCH_INPUT_RE.search(_codex_stringify(_codex_call_input(payload))))
    return False


@dataclass
class _CodexScan:
    event_user_turns: int = 0
    event_roles: set[str] = field(default_factory=set)
    text: list[tuple[str, str, str]] = field(default_factory=list)
    has_edit: bool = False

    @property
    def user_turns(self) -> int:
        # event_msg is generated from the actual UI submission. response_item
        # can additionally contain environment setup, developer instructions,
        # and delegated sub-agent tasks under role=user. Without explicit
        # user-visible provenance those records are neither counted nor stored.
        return self.event_user_turns

    def extracted_text(self) -> str:
        parts = [
            text
            for source, role, text in self.text
            if source == "event" or role not in self.event_roles
        ]
        return "\n".join(parts)


def _scan_codex(path) -> _CodexScan:
    scan = _CodexScan()
    for event in _jsonl_objects(path):
        payload = _codex_payload(event)
        outer_type = event.get("type")
        payload_type = payload.get("type")

        if outer_type == "event_msg" and payload_type in {"user_message", "agent_message"}:
            role = "user" if payload_type == "user_message" else "assistant"
            scan.event_roles.add(role)
            if role == "user":
                scan.event_user_turns += 1
            value = payload.get("message", payload.get("content"))
            for part in _codex_text_parts(value):
                if part:
                    scan.text.append(("event", role, part))

        elif outer_type == "response_item" and payload_type == "message":
            role = payload.get("role")
            # Assistant output is a safe compatibility fallback. role=user is
            # ambiguous in Codex rollouts and may be internal orchestration.
            if role == "assistant":
                for part in _codex_text_parts(payload.get("content")):
                    if part:
                        scan.text.append(("response", role, part))

        if not scan.has_edit and _codex_is_edit_event(event):
            scan.has_edit = True
    return scan


def codex_user_turns(path) -> int:
    """Count real user submissions in a Codex rollout without double-counting."""
    return _scan_codex(path).user_turns


def codex_extract_text(path) -> str:
    """Return visible user/assistant text, excluding reasoning and duplicates."""
    return _scan_codex(path).extracted_text()


def codex_has_edit(path) -> bool:
    """Return whether a Codex rollout contains credible file-edit evidence."""
    return _scan_codex(path).has_edit


def codex_should_enqueue(path, *, min_turns: int = 6) -> bool:
    """Apply Agam's real-work gate to a Codex rollout."""
    scan = _scan_codex(path)
    if scan.user_turns < min_turns or not scan.has_edit:
        return False
    return bool(SIGNAL_RE.search(scan.extracted_text()[-20000:]))


def _clip(value: str, cap: int) -> str:
    if len(value) <= cap:
        return value
    return value[:cap] + f"...[+{len(value) - cap}]"


def _normalized_message(role: str, text: str, timestamp) -> dict:
    event = {
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": _clip(text, _CODEX_TEXT_CAP)}]},
    }
    if isinstance(timestamp, str) and timestamp:
        event["timestamp"] = timestamp
    return event


def _normalized_tool_use(payload: dict, timestamp) -> dict:
    name = _codex_call_name(payload) or str(payload.get("type") or "tool")
    value = _codex_call_input(payload)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            decoded = _clip(value, _CODEX_TOOL_INPUT_CAP)
        else:
            value_text = _codex_stringify(decoded)
            decoded = decoded if len(value_text) <= _CODEX_TOOL_INPUT_CAP else _clip(value_text, _CODEX_TOOL_INPUT_CAP)
    else:
        value_text = _codex_stringify(value)
        decoded = value if len(value_text) <= _CODEX_TOOL_INPUT_CAP else _clip(value_text, _CODEX_TOOL_INPUT_CAP)
    event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": decoded}],
        },
    }
    if isinstance(timestamp, str) and timestamp:
        event["timestamp"] = timestamp
    return event


def _normalized_tool_result(payload: dict, timestamp) -> dict:
    value = payload.get("output", payload.get("result", ""))
    content = _clip(_codex_stringify(value), _CODEX_TOOL_OUTPUT_CAP)
    event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": content}],
        },
    }
    if isinstance(timestamp, str) and timestamp:
        event["timestamp"] = timestamp
    return event


def _normalized_patch_event(payload: dict, timestamp) -> dict | None:
    changes = payload.get("changes")
    if isinstance(changes, dict):
        paths = [str(path) for path in changes]
    elif isinstance(changes, list):
        paths = [str(path) for path in changes if isinstance(path, (str, pathlib.Path))]
    else:
        paths = []
    if not paths and payload.get("success") is False:
        return None
    event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "name": "apply_patch",
                "input": {"file_paths": paths[:200], "status": payload.get("status", "")},
            }],
        },
    }
    if isinstance(timestamp, str) and timestamp:
        event["timestamp"] = timestamp
    return event


def _codex_message_sources(path) -> set[str]:
    roles: set[str] = set()
    for event in _jsonl_objects(path):
        payload = _codex_payload(event)
        if event.get("type") != "event_msg":
            continue
        if payload.get("type") == "user_message":
            roles.add("user")
        elif payload.get("type") == "agent_message":
            roles.add("assistant")
    return roles


def codex_normalized_events(path) -> Iterator[dict]:
    """Yield a compact Claude-like view of useful Codex rollout events.

    Unknown/internal records are intentionally dropped. This both insulates the
    watchdog from Codex schema churn and avoids persisting encrypted reasoning,
    model instructions, token accounting, or world-state snapshots.
    """
    event_roles = _codex_message_sources(path)
    for event in _jsonl_objects(path):
        payload = _codex_payload(event)
        outer_type = event.get("type")
        payload_type = str(payload.get("type", ""))
        timestamp = event.get("timestamp")

        if outer_type == "event_msg" and payload_type in {"user_message", "agent_message"}:
            role = "user" if payload_type == "user_message" else "assistant"
            value = payload.get("message", payload.get("content"))
            text = "\n".join(part for part in _codex_text_parts(value) if part)
            if text:
                yield _normalized_message(role, text, timestamp)
            continue

        if outer_type == "event_msg" and payload_type in {
            "patch_apply_end", "file_edit_end", "file_write_end",
        }:
            normalized = _normalized_patch_event(payload, timestamp)
            if normalized is not None:
                yield normalized
            continue

        if outer_type != "response_item":
            continue
        if payload_type == "message":
            role = payload.get("role")
            # Never persist response_item role=user. Unlike canonical
            # event_msg/user_message, it has no reliable user-visible origin
            # and is also used for developer/environment/sub-agent material.
            if role != "assistant" or role in event_roles:
                continue
            text = "\n".join(part for part in _codex_text_parts(payload.get("content")) if part)
            if text:
                yield _normalized_message(role, text, timestamp)
        elif payload_type in _CODEX_TOOL_CALL_TYPES:
            yield _normalized_tool_use(payload, timestamp)
        elif payload_type in _CODEX_TOOL_OUTPUT_TYPES:
            yield _normalized_tool_result(payload, timestamp)


def codex_write_snapshot(path, destination) -> int:
    """Atomically write a compact Codex transcript snapshot; return row count."""
    destination = pathlib.Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent),
    )
    count = 0
    try:
        with os.fdopen(fd, "w") as f:
            for event in codex_normalized_events(path):
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                count += 1
        os.replace(tmp_name, destination)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return count


# A descriptive alias for callers that think in terms of format conversion.
codex_normalize_transcript = codex_write_snapshot


def cursor_user_turns(path) -> int:
    """Count user turns in a Cursor transcript (``role == "user"`` lines)."""
    n = 0
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("role") == "user":
                n += 1
    return n


def cursor_extract_text(path) -> str:
    """Concatenate every text block from user/assistant messages.

    Used where we want only the human-readable content (e.g. signal scanning on
    the tail) rather than the full JSON.
    """
    parts = []
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = o.get("message")
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text", ""))
    return "\n".join(parts)


def cursor_should_enqueue(path, *, min_turns: int = 6) -> bool:
    """Decide whether a Cursor session is worth the watchdog's LLM pass.

    Mirrors ``session_close.should_enqueue`` for Claude: enough human turns,
    evidence of an edit, and a signal keyword near the end. Raw-text based so it
    tolerates Cursor's lazy flushing and unknown tool JSON.
    """
    if cursor_user_turns(path) < min_turns:
        return False
    try:
        with open(path, errors="replace") as f:
            raw = f.read()
    except OSError:
        return False
    if not _EDIT_EVIDENCE_RE.search(raw):
        return False
    if not SIGNAL_RE.search(raw[-20000:]):
        return False
    return True

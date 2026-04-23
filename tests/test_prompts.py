"""Tests for top-level prompts/ directory.

These prompts are templates consumed by agam_watchdog_inner.py at runtime.
They must exist, contain all the placeholders the watchdog substitutes, be
ASCII-only, and carry no personal content from this environment.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"

WORK_LOG = PROMPTS / "work-log.txt"
AGAM_SYNC = PROMPTS / "agam-sync.txt"

ALL_PROMPTS = [WORK_LOG, AGAM_SYNC]

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")
BRACE_CLUSTER_RE = re.compile(r"\{\{[^{}]*\}\}")


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=lambda p: p.name)
def test_prompt_exists(path: pathlib.Path) -> None:
    assert path.is_file(), f"missing prompt: {path}"
    assert path.stat().st_size > 0, f"empty prompt: {path}"


# ---------------------------------------------------------------------------
# Placeholders the watchdog substitutes must all be present
# ---------------------------------------------------------------------------


WORK_LOG_REQUIRED = (
    "{{SESSION_SIGNALS}}",
    "{{CONTEXT_SUMMARY}}",
    "{{PROJECT_NAME}}",
    "{{DATE}}",
    "{{TIME}}",
    "{{OUTPUT_PATH}}",
    "{{SINCE_ISO}}",
    "{{JSONL_PATH}}",
)

AGAM_SYNC_REQUIRED = (
    "{{SESSION_SIGNALS}}",
    "{{CONTEXT_SUMMARY}}",
    "{{JSONL_PATH}}",
)


@pytest.mark.parametrize("token", WORK_LOG_REQUIRED)
def test_work_log_has_placeholder(token: str) -> None:
    text = WORK_LOG.read_text(encoding="utf-8")
    assert token in text, f"work-log.txt missing required placeholder {token}"


@pytest.mark.parametrize("token", AGAM_SYNC_REQUIRED)
def test_agam_sync_has_placeholder(token: str) -> None:
    text = AGAM_SYNC.read_text(encoding="utf-8")
    assert token in text, f"agam-sync.txt missing required placeholder {token}"


# ---------------------------------------------------------------------------
# Placeholder well-formedness -- any {{...}} cluster must be {{UPPER_SNAKE}}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=lambda p: p.name)
def test_placeholders_well_formed(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    for match in BRACE_CLUSTER_RE.finditer(text):
        tok = match.group(0)
        assert PLACEHOLDER_RE.fullmatch(tok), (
            f"malformed placeholder {tok!r} in {path.name}"
        )


# ---------------------------------------------------------------------------
# ASCII only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=lambda p: p.name)
def test_prompt_is_ascii(path: pathlib.Path) -> None:
    data = path.read_bytes()
    try:
        data.decode("ascii")
    except UnicodeDecodeError as exc:
        bad = data[max(0, exc.start - 20) : exc.end + 20]
        pytest.fail(f"non-ASCII bytes in {path.name}: ...{bad!r}...")


# ---------------------------------------------------------------------------
# No hardcoded personal paths or identifiers
# ---------------------------------------------------------------------------


PERSONAL_SENTINELS = (
    "/Users/km",
    "Kalyan",
    "kalyanguru18@gmail",
    "Example",
    "kalyanguru18",
)


@pytest.mark.parametrize("path", ALL_PROMPTS, ids=lambda p: p.name)
def test_no_personal_content(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    leaks = [s for s in PERSONAL_SENTINELS if s in text]
    assert not leaks, f"{path.name} contains personal content (count={len(leaks)})"

"""Tests for Agam identity + config templates.

Verifies the templates directory ships empty scaffolding only (no personal
content), uses well-formed jinja-style placeholders, and the plist template
parses as valid XML once placeholders are substituted.
"""
from __future__ import annotations

import pathlib
import plistlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"

IDENTITY_TEMPLATES = [
    TEMPLATES / "AGAM.md.template",
    TEMPLATES / "THISAI.md.template",
    TEMPLATES / "MUGAM.md.template",
]
CLAUDE_MD_TEMPLATE = TEMPLATES / "CLAUDE.md.template"
PLIST_TEMPLATE = TEMPLATES / "com.agam.watchdog.plist.template"

ALL_TEMPLATES = IDENTITY_TEMPLATES + [CLAUDE_MD_TEMPLATE, PLIST_TEMPLATE]

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")
# Any curly-brace cluster looks like a placeholder attempt; catches malformed ones.
BRACE_CLUSTER_RE = re.compile(r"\{\{[^{}]*\}\}")


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.name)
def test_template_exists(path: pathlib.Path) -> None:
    assert path.is_file(), f"missing template: {path}"
    assert path.stat().st_size > 0, f"empty template: {path}"


# ---------------------------------------------------------------------------
# Placeholder well-formedness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.name)
def test_placeholders_well_formed(path: pathlib.Path) -> None:
    """Any {{...}} token must match {{[A-Z_]+}} exactly."""
    text = path.read_text(encoding="utf-8")
    for match in BRACE_CLUSTER_RE.finditer(text):
        tok = match.group(0)
        assert PLACEHOLDER_RE.fullmatch(tok), (
            f"malformed placeholder {tok!r} in {path.name}"
        )


def test_plist_required_placeholders_present() -> None:
    text = PLIST_TEMPLATE.read_text(encoding="utf-8")
    for required in ("{{HOME}}", "{{AGAM_HOME}}", "{{AGAM_HOOKS_DIR}}"):
        assert required in text, f"plist template missing {required}"


# ---------------------------------------------------------------------------
# ASCII only (CLAUDE.md hard rule; identity templates should follow too)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [CLAUDE_MD_TEMPLATE, *IDENTITY_TEMPLATES, PLIST_TEMPLATE],
    ids=lambda p: p.name,
)
def test_template_is_ascii(path: pathlib.Path) -> None:
    data = path.read_bytes()
    try:
        data.decode("ascii")
    except UnicodeDecodeError as exc:
        # Surface the offending byte for a readable failure.
        bad = data[max(0, exc.start - 20) : exc.end + 20]
        pytest.fail(f"non-ASCII bytes in {path.name}: ...{bad!r}...")


def test_claude_md_template_has_no_emdash() -> None:
    # Belt-and-suspenders. If ASCII check passes this is redundant but cheap.
    text = CLAUDE_MD_TEMPLATE.read_text(encoding="utf-8")
    assert "\u2014" not in text, "em-dash leaked into CLAUDE.md template"
    assert "\u2013" not in text, "en-dash leaked into CLAUDE.md template"


# ---------------------------------------------------------------------------
# No personal content in identity templates
# ---------------------------------------------------------------------------


# Class-based PII guards. Each entry is (label, regex). We assert templates
# match NONE of these. The regex is shown on failure but the matched value
# is intentionally suppressed so a broken test on someone else's fork never
# echoes their personal data.
import re as _re

PERSONAL_PATTERNS = (
    ("email address", _re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("home path",     _re.compile(r"/Users/[a-z][a-z0-9_-]*/")),
    ("home path",     _re.compile(r"/home/[a-z][a-z0-9_-]*/")),
    ("phone number",  _re.compile(r"\b\+?\d{1,3}[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}\b")),
)


@pytest.mark.parametrize("path", IDENTITY_TEMPLATES, ids=lambda p: p.name)
def test_no_personal_content(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [label for label, rx in PERSONAL_PATTERNS if rx.search(text)]
    assert not hits, f"{path.name} contains personal patterns: {hits}"


@pytest.mark.parametrize("path", IDENTITY_TEMPLATES, ids=lambda p: p.name)
def test_identity_template_is_scaffolding(path: pathlib.Path) -> None:
    """Identity templates must be mostly structure: headers + HTML comments.

    Non-blank, non-heading, non-HTML-comment lines should be rare (small
    boilerplate like 'Last updated' is OK). Nothing that reads like a
    paragraph of personal narrative.
    """
    text = path.read_text(encoding="utf-8")
    content_lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):  # markdown heading
            continue
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        content_lines.append(line)

    # Allow a short "Last updated" stub and nothing else with real prose.
    for line in content_lines:
        assert len(line) < 80, (
            f"{path.name} has a long prose line -- looks like personal content"
        )


# ---------------------------------------------------------------------------
# Plist validity
# ---------------------------------------------------------------------------


def _substitute_plist(raw: bytes) -> bytes:
    return (
        raw.replace(b"{{HOME}}", b"/tmp/home")
        .replace(b"{{AGAM_HOME}}", b"/tmp/home/.claude/agam")
        .replace(b"{{AGAM_HOOKS_DIR}}", b"/tmp/home/.claude/hooks")
        .replace(b"{{AGAM_TOOLS_DIR}}", b"/tmp/home/.claude/tools/agam")
        .replace(b"{{AGAM_KG_PATH}}", b"/tmp/home/.claude/knowledge/graph.db")
    )


def test_plist_parses_after_substitution() -> None:
    raw = PLIST_TEMPLATE.read_bytes()
    # No placeholders should remain after substitution.
    substituted = _substitute_plist(raw)
    assert b"{{" not in substituted, "unsubstituted placeholder in plist template"

    data = plistlib.loads(substituted)
    assert data["Label"] == "com.agam.watchdog"
    assert data["ProgramArguments"][0] == "/bin/bash"
    assert data["ProgramArguments"][1].endswith("/agam_watchdog.sh")
    assert data["StartInterval"] == 300
    assert data["RunAtLoad"] is False
    assert data["KeepAlive"] is False
    assert data["StandardOutPath"].endswith("/watchdog.stdout.log")
    assert data["StandardErrorPath"].endswith("/watchdog.stderr.log")
    assert data["WorkingDirectory"] == "/tmp/home"


# ---------------------------------------------------------------------------
# CLAUDE.md template shape
# ---------------------------------------------------------------------------


def test_claude_md_mentions_agam_core() -> None:
    text = CLAUDE_MD_TEMPLATE.read_text(encoding="utf-8")
    for needle in (
        "## Agam Integration",
        "~/.claude/agam/",
        "AGAM.md",
        "THISAI.md",
        "MUGAM.md",
        "graph.db",
        "graph-recall",
        "agam-watchdog",
    ):
        assert needle in text, f"CLAUDE.md template missing {needle!r}"

#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""Mechanical merger for Agam sync proposals. Writes .bak before every edit.
Refuses on structural mismatch (missing expected section headers)."""

import json
import shutil
import sys
import os
import pathlib
import re
from datetime import date


AGAM_HOME = pathlib.Path(
    os.environ.get("AGAM_HOME", os.path.expanduser("~/.claude/agam"))
)
# Memory dir lives under the projects directory's per-CWD slug, e.g.
# ``~/.claude/projects/-home-alice-coding-foo/memory/``. Claude Code creates
# the slug itself per active CWD; if not yet present we fall back to a
# generic ``$AGAM_HOME/memory`` so apply-proposals never tries to write into
# a path that hard-codes the username.
AGAM_MEMORY_DIR = pathlib.Path(
    os.environ.get(
        "AGAM_MEMORY_DIR",
        str(AGAM_HOME / "memory"),
    )
)


class ApplyError(Exception):
    pass


def _backup(path: pathlib.Path) -> pathlib.Path:
    bak = pathlib.Path(str(path) + ".bak")
    shutil.copy2(path, bak)
    return bak


def _restore(path: pathlib.Path) -> None:
    bak = pathlib.Path(str(path) + ".bak")
    if bak.exists():
        shutil.copy2(bak, path)


def _today_iso() -> str:
    return date.today().isoformat()


def _append_thisai_project(thisai_text: str, project_name: str, note: str, today: str) -> str:
    # Try exact "### {name}" header first, then fall back to a substring match so sonnet
    # proposing "Cognitive Infrastructure" can find "### Build Cognitive Infrastructure"
    # (sonnet doesn't always spell the heading exactly). Both patterns match the body
    # until the next `### ` or `## `.
    exact = re.compile(
        rf"(^### {re.escape(project_name)}\b[^\n]*\n)(.*?)(?=^### |^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = exact.search(thisai_text)
    if not m:
        fuzzy = re.compile(
            rf"(^### [^\n]*{re.escape(project_name)}[^\n]*\n)(.*?)(?=^### |^## |\Z)",
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        m = fuzzy.search(thisai_text)
    if not m:
        raise ApplyError(f"project section not found in THISAI.md: {project_name}")
    header, body = m.group(1), m.group(2)
    new_bullet = f"- {today}: {note}"
    if any(line.strip() == new_bullet for line in body.splitlines()):
        return thisai_text
    body_stripped = body.rstrip("\n")
    new_body = body_stripped + "\n" + new_bullet + "\n\n"
    return thisai_text[: m.start()] + header + new_body + thisai_text[m.end():]


def _append_lesson(agam_text: str, lesson_body: str) -> str:
    if "## What I've Learned" not in agam_text:
        raise ApplyError("AGAM.md missing '## What I've Learned' section")
    # Append under the last existing lesson subsection if present,
    # otherwise directly under "## What I've Learned".
    subsection_re = re.compile(r"^### [^\n]*Lessons[^\n]*\n(.*?)(?=^### |^## |\Z)", re.MULTILINE | re.DOTALL)
    matches = list(subsection_re.finditer(agam_text))
    insertion = lesson_body.strip() + "\n\n"
    if matches:
        m = matches[-1]
        before = agam_text[: m.end()].rstrip("\n")
        after = agam_text[m.end():]
        return before + "\n\n" + insertion + after
    # No subsection: append after "## What I've Learned" header block
    idx = agam_text.index("## What I've Learned")
    # insert at the end of the file
    return agam_text.rstrip("\n") + "\n\n" + insertion


def _append_insight_or_correction(agam_text: str, body: str) -> str:
    if "## What I've Learned" not in agam_text:
        raise ApplyError("AGAM.md missing '## What I've Learned' section")
    return agam_text.rstrip("\n") + "\n\n" + body.strip() + "\n"


def _write_memory_file(memory_dir: pathlib.Path, filename: str, mem_type: str, description: str, content: str) -> pathlib.Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    name = filename.removesuffix(".md")
    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {mem_type}\n"
        "---\n\n"
        f"{content.strip()}\n"
    )
    target = memory_dir / filename
    target.write_text(frontmatter)
    return target


def _append_suvadu(suvadu_path: pathlib.Path, lines: list[str]) -> None:
    if not lines:
        return
    with open(suvadu_path, "a") as f:
        for line in lines:
            f.write(line.rstrip() + "\n")


def _apply_obsolete(kg_path: pathlib.Path, name: str, reason: str = "") -> bool:
    """Mark a KG entity ``status=obsolete`` if it exists.

    Returns True when the entity was found + updated, False otherwise.
    Idempotent; safe to call multiple times on the same entity.
    """
    if not kg_path.exists():
        return False
    import sqlite3
    from datetime import datetime, timezone

    # Entities are written through normalize_name (PascalCase -> kebab-case),
    # so any input form (Camel, snake, kebab) must be normalized before lookup.
    # We re-implement the normalization inline to avoid a hard dep from this
    # PEP 723 script on the agam.tools.knowledge_graph module.
    s = re.sub(r"([a-z])([A-Z])", r"\1-\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", s)
    s = s.replace("_", "-").lower()
    normalized = re.sub(r"-+", "-", s).strip("-")

    conn = sqlite3.connect(str(kg_path), timeout=5)
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE name = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if not row:
            return False
        eid = row[0]
        ts = datetime.now(timezone.utc).isoformat()
        for key, value in (
            ("status", "obsolete"),
            ("obsoleted-at", ts),
            ("obsolete-reason", reason or ""),
        ):
            if not value and key == "obsolete-reason":
                continue
            conn.execute(
                "INSERT INTO properties (entity_id, key, value, updated) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(entity_id, key) DO UPDATE SET "
                "value=excluded.value, updated=excluded.updated",
                (eid, key, value, ts),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def apply_proposals(proposals: dict, *, agam_md: pathlib.Path, thisai_md: pathlib.Path,
                    suvadu_md: pathlib.Path, memory_dir: pathlib.Path,
                    kg_path: pathlib.Path | None = None,
                    today: str | None = None) -> dict:
    today = today or _today_iso()
    applied = {"projects": 0, "goals": 0, "memory": 0, "lessons": 0,
               "corrections": 0, "insights": 0, "obsoleted": 0}
    suvadu_lines: list[str] = []

    needs_agam = bool(proposals.get("lesson") or proposals.get("insight") or proposals.get("correction"))
    needs_thisai = bool(proposals.get("thisai_projects") or proposals.get("thisai_goals"))

    if needs_agam:
        agam_text_pre = agam_md.read_text()
        if "## What I've Learned" not in agam_text_pre:
            raise ApplyError("AGAM.md missing '## What I've Learned' section")

    touched: list[pathlib.Path] = []
    if needs_thisai:
        _backup(thisai_md); touched.append(thisai_md)
    if needs_agam:
        _backup(agam_md); touched.append(agam_md)

    errors = []
    try:
        if needs_thisai:
            text = thisai_md.read_text()
            for p in proposals.get("thisai_projects", []):
                try:
                    before = text
                    text = _append_thisai_project(text, p["name"], p["note"], today)
                    if text != before:
                        applied["projects"] += 1
                        suvadu_lines.append(f"{today} | THISAI.md | {p['name']} -- {p['note']}")
                except ApplyError as e:
                    errors.append(f"thisai_project '{p.get('name')}': {e}")
            for g in proposals.get("thisai_goals", []):
                try:
                    before = text
                    text = _append_thisai_project(text, g["name"], g["note"], today)
                    if text != before:
                        applied["goals"] += 1
                        suvadu_lines.append(f"{today} | THISAI.md | {g['name']} -- {g['note']}")
                except ApplyError as e:
                    errors.append(f"thisai_goal '{g.get('name')}': {e}")
            thisai_md.write_text(text)

        for m in proposals.get("memory", []):
            try:
                _write_memory_file(memory_dir, m["filename"], m["type"], m.get("description", ""), m["content"])
                applied["memory"] += 1
                suvadu_lines.append(f"{today} | memory/{m['filename']} | {m.get('description', '')}")
            except (OSError, KeyError) as e:
                errors.append(f"memory '{m.get('filename')}': {e}")

        if needs_agam:
            text = agam_md.read_text()
            for l in proposals.get("lesson", []):
                try:
                    text = _append_lesson(text, l["body"])
                    applied["lessons"] += 1
                    suvadu_lines.append(f"{today} | AGAM.md | Added lesson: {l.get('title', '')}")
                except ApplyError as e:
                    errors.append(f"lesson '{l.get('title')}': {e}")
            for ins in proposals.get("insight", []):
                try:
                    text = _append_insight_or_correction(text, ins["body"])
                    applied["insights"] += 1
                    suvadu_lines.append(f"{today} | AGAM.md | Added insight: {ins.get('title', '')}")
                except ApplyError as e:
                    errors.append(f"insight '{ins.get('title')}': {e}")
            for c in proposals.get("correction", []):
                try:
                    text = _append_insight_or_correction(text, c["body"])
                    applied["corrections"] += 1
                    suvadu_lines.append(f"{today} | AGAM.md | Added correction: {c.get('title', '')}")
                except ApplyError as e:
                    errors.append(f"correction '{c.get('title')}': {e}")
            agam_md.write_text(text)

        # Obsoletion proposals operate on the KG, not on markdown files. The
        # ``obsolete`` proposal list is a flat list of ``{"name": ..., "reason": ...}``
        # dicts. Each entry that matches an existing entity gets ``status=obsolete``
        # written as a property. Missing entities are silently skipped.
        #
        # Resolving the KG path: caller can pass it explicitly. Otherwise we use
        # AGAM_KG_PATH env (the canonical override) or the default $HOME location.
        # No filesystem-relative derivation (e.g. agam_md.parent.parent) because
        # that can accidentally land on a test fixture's parent dir and corrupt
        # unrelated tempdirs.
        obsoletes = proposals.get("obsolete", []) if isinstance(proposals.get("obsolete", []), list) else []
        if obsoletes:
            target_kg = kg_path
            if target_kg is None:
                target_kg = pathlib.Path(
                    os.environ.get(
                        "AGAM_KG_PATH",
                        os.path.expanduser("~/.claude/knowledge/graph.db"),
                    )
                )
            for ob in obsoletes:
                if not isinstance(ob, dict):
                    continue
                name = ob.get("name")
                if not name:
                    continue
                reason = ob.get("reason", "")
                try:
                    if _apply_obsolete(target_kg, name, reason):
                        applied["obsoleted"] += 1
                        suvadu_lines.append(
                            f"{today} | KG | Obsoleted entity: {name}"
                            + (f" -- {reason}" if reason else "")
                        )
                except Exception as e:  # noqa: BLE001 -- KG ops are best-effort
                    errors.append(f"obsolete '{name}': {e}")

        _append_suvadu(suvadu_md, suvadu_lines)
    except Exception:
        for t in touched:
            _restore(t)
        raise

    if errors:
        applied["errors"] = errors
    return applied


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: apply_proposals.py <proposals.json>", file=sys.stderr)
        return 2
    props_path = pathlib.Path(argv[1])
    if not props_path.exists():
        print(f"proposals file not found: {props_path}", file=sys.stderr)
        return 2
    proposals = json.loads(props_path.read_text())
    try:
        applied = apply_proposals(
            proposals,
            agam_md=AGAM_HOME / "AGAM.md",
            thisai_md=AGAM_HOME / "THISAI.md",
            suvadu_md=AGAM_HOME / "SUVADU.md",
            memory_dir=AGAM_MEMORY_DIR,
        )
    except ApplyError as e:
        print(f"apply refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

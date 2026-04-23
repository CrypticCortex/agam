"""Tests for the ported apply_proposals.py tool.

All tests operate on tempdir-backed identity files. The real files at
~/.claude/agam/ must never be touched.
"""

import pathlib
import sys
import tempfile

# Make src/ importable so `from agam.tools import apply_proposals` resolves
# without requiring an editable install in the test runner.
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agam.tools import apply_proposals as mod  # noqa: E402


def _seed_agam(root: pathlib.Path):
    agam = root / "AGAM.md"
    thisai = root / "THISAI.md"
    suvadu = root / "SUVADU.md"
    memdir = root / "memory"
    memdir.mkdir(parents=True, exist_ok=True)

    agam.write_text(
        "# Agam\n\n"
        "## Who I Am\n\nKalyan.\n\n"
        "## What I've Learned\n\n"
        "Each entry cost time or energy.\n\n"
        "### Process Lessons\n\n"
        "[lesson] **Prior lesson.** Body.\n\n"
    )
    thisai.write_text(
        "# Thisai\n\n"
        "## Active Goals\n\n"
        "### Build Cognitive Infrastructure\n"
        "Meta-project description.\n"
        "- 2026-04-13: Prior progress note.\n\n"
        "Active projects: Agam system\n\n"
        "## Other Active Projects (no explicit goal)\n\n"
        "### Foo\n"
        "Foo description.\n"
        "- 2026-04-10: Earlier note.\n\n"
    )
    suvadu.write_text("# Suvadu\n\n## 2026-03-15 -- Bootstrap\n\n")
    return {"agam": agam, "thisai": thisai, "suvadu": suvadu, "memdir": memdir}


def test_applies_thisai_project_bullet():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        props = {"thisai_projects": [{"name": "Foo", "note": "added frobnicator"}]}
        mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        text = paths["thisai"].read_text()
        assert "- 2026-04-21: added frobnicator" in text
        assert "### Foo" in text
        # Prior bullet still present
        assert "- 2026-04-10: Earlier note." in text


def test_creates_memory_file():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        props = {
            "memory": [{
                "filename": "feedback_testy.md",
                "type": "feedback",
                "description": "one-line hook",
                "content": "Body of the memory.\n\n**Why:** reason\n\n**How to apply:** when x",
            }],
        }
        mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        f = paths["memdir"] / "feedback_testy.md"
        assert f.exists()
        text = f.read_text()
        assert text.startswith("---\n")
        assert "name: feedback_testy" in text
        assert "type: feedback" in text
        assert "description: one-line hook" in text
        assert "**Why:** reason" in text


def test_appends_lesson_to_agam():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        props = {"lesson": [{
            "title": "Test thing",
            "body": "[lesson] **Test thing.** Body of the lesson. Source: 2026-04-21 session.",
            "source": "2026-04-21 session",
        }]}
        mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        text = paths["agam"].read_text()
        assert "[lesson] **Test thing.**" in text
        # Prior lesson still intact
        assert "[lesson] **Prior lesson.**" in text


def test_writes_bak_before_edit():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        props = {
            "thisai_projects": [{"name": "Foo", "note": "touch"}],
            "lesson": [{"title": "L", "body": "[lesson] **L.** body."}],
        }
        mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        assert (paths["thisai"].parent / "THISAI.md.bak").exists()
        assert (paths["agam"].parent / "AGAM.md.bak").exists()


def test_appending_same_bullet_twice_same_day_is_noop():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        props = {"thisai_projects": [{"name": "Foo", "note": "same content"}]}
        first = mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        assert first["projects"] == 1
        thisai_after_first = paths["thisai"].read_text()
        suvadu_after_first = paths["suvadu"].read_text()

        second = mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        assert second["projects"] == 0
        assert paths["thisai"].read_text() == thisai_after_first
        assert paths["suvadu"].read_text() == suvadu_after_first


def test_different_note_same_day_still_appends():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        mod.apply_proposals(
            {"thisai_projects": [{"name": "Foo", "note": "first thing"}]},
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        result = mod.apply_proposals(
            {"thisai_projects": [{"name": "Foo", "note": "second thing"}]},
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-21",
        )
        assert result["projects"] == 1
        text = paths["thisai"].read_text()
        assert "- 2026-04-21: first thing" in text
        assert "- 2026-04-21: second thing" in text


def test_refuses_on_missing_section_header():
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        # Corrupt AGAM: remove "## What I've Learned"
        paths["agam"].write_text("# Agam\n\n## Who I Am\n\nKalyan.\n")
        before = paths["agam"].read_text()
        props = {"lesson": [{"title": "L", "body": "[lesson] **L.** body."}]}
        raised = False
        try:
            mod.apply_proposals(
                props,
                agam_md=paths["agam"], thisai_md=paths["thisai"],
                suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
                today="2026-04-21",
            )
        except mod.ApplyError:
            raised = True
        assert raised
        # File untouched
        assert paths["agam"].read_text() == before


def test_substring_project_name_matches_variant_heading():
    """Sonnet proposing 'Cognitive Infrastructure' must find '### Build Cognitive Infrastructure'.
    Live watchdog log showed this exact hallucination pattern -- real heading has extra words."""
    with tempfile.TemporaryDirectory() as d:
        paths = _seed_agam(pathlib.Path(d))
        props = {"thisai_projects": [{"name": "Cognitive Infrastructure", "note": "fuzzy matched"}]}
        result = mod.apply_proposals(
            props,
            agam_md=paths["agam"], thisai_md=paths["thisai"],
            suvadu_md=paths["suvadu"], memory_dir=paths["memdir"],
            today="2026-04-22",
        )
        assert result["projects"] == 1
        assert "errors" not in result
        text = paths["thisai"].read_text()
        assert "- 2026-04-22: fuzzy matched" in text
        # Must land under the correct section header
        assert "### Build Cognitive Infrastructure" in text


def test_one_bad_proposal_does_not_block_others(tmp_path):
    """A malformed THISAI proposal should not prevent legitimate AGAM lessons from applying."""
    agam = tmp_path / "AGAM.md"
    thisai = tmp_path / "THISAI.md"
    suvadu = tmp_path / "SUVADU.md"
    memdir = tmp_path / "memory"
    agam.write_text("# AGAM\n\n## What I've Learned\n\n### Existing Lessons\n\n- old lesson\n")
    thisai.write_text("# THISAI\n\n## Projects\n\n### RealProject\n\n- 2026-04-01: started\n")
    suvadu.write_text("# SUVADU\n")

    proposals = {
        "thisai_projects": [
            {"name": "NonexistentProject", "note": "should fail"},
            {"name": "RealProject", "note": "should succeed"},
        ],
        "lesson": [
            {"title": "T", "body": "[lesson] **T.** body text. Source: test."}
        ],
    }
    applied = mod.apply_proposals(
        proposals,
        agam_md=agam,
        thisai_md=thisai,
        suvadu_md=suvadu,
        memory_dir=memdir,
        today="2026-04-22",
    )
    # RealProject bullet applied despite NonexistentProject failure
    assert "2026-04-22: should succeed" in thisai.read_text()
    # Lesson applied despite THISAI failure
    assert "[lesson] **T.**" in agam.read_text()
    assert applied["projects"] == 1
    assert applied["lessons"] == 1
    # Errors surfaced
    assert "errors" in applied
    assert any("NonexistentProject" in err for err in applied["errors"])

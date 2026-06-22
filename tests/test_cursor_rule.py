"""Tests for the Cursor rule writer + git-exclude."""

from agam.tools import cursor_rule


def test_write_rule_creates_file(tmp_path):
    p = cursor_rule.write_rule(tmp_path, "---\nalwaysApply: true\n---\nhi")
    assert p == tmp_path / ".cursor" / "rules" / "agam.mdc"
    assert p.read_text().startswith("---\nalwaysApply: true")
    assert p.read_text().endswith("\n")


def test_git_exclude_added_when_repo_present(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    cursor_rule.write_rule(tmp_path, "content")
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert "/.cursor/rules/agam.mdc" in exclude


def test_git_exclude_idempotent(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    cursor_rule.write_rule(tmp_path, "a")
    cursor_rule.write_rule(tmp_path, "b")
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert exclude.count("/.cursor/rules/agam.mdc") == 1


def test_no_git_repo_is_fine(tmp_path):
    # No .git dir -> write still succeeds, exclude returns False.
    p = cursor_rule.write_rule(tmp_path, "x")
    assert p.exists()
    assert cursor_rule.ensure_git_excluded(tmp_path) is False


def test_exclude_preserves_existing_entries(tmp_path):
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("*.log\nbuild/\n")
    cursor_rule.write_rule(tmp_path, "x")
    text = (info / "exclude").read_text()
    assert "*.log" in text and "build/" in text
    assert "/.cursor/rules/agam.mdc" in text

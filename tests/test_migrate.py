"""Tests for the backup-safe legacy -> ~/.agam migration."""

import sqlite3

from agam.migrate import migrate_if_needed


def _make_legacy(home):
    """Create a minimal legacy ~/.claude layout under home."""
    kdir = home / ".claude" / "knowledge"
    kdir.mkdir(parents=True)
    db = kdir / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO entities (name) VALUES ('voice-fnol-poc')")
    conn.commit()
    conn.close()
    (kdir / "entity-names.txt").write_text("voice-fnol-poc\n")

    idir = home / ".claude" / "agam"
    (idir / "prompts").mkdir(parents=True)
    (idir / "AGAM.md").write_text("# identity\n")
    (idir / "config.yaml").write_text("name: Kalyan\n")
    (idir / "prompts" / "work-log.txt").write_text("log template\n")
    return db


def test_fresh_when_nothing_present(tmp_path):
    status, dest = migrate_if_needed(tmp_path)
    assert status == "fresh"
    assert dest is None
    assert not (tmp_path / ".agam").exists()


def test_migrates_legacy_data(tmp_path):
    legacy_db = _make_legacy(tmp_path)
    status, dest = migrate_if_needed(tmp_path)
    assert status == "migrated"
    assert dest == tmp_path / ".agam"
    # Graph copied across.
    new_db = tmp_path / ".agam" / "knowledge" / "graph.db"
    assert new_db.exists()
    conn = sqlite3.connect(str(new_db))
    names = [r[0] for r in conn.execute("SELECT name FROM entities")]
    conn.close()
    assert "voice-fnol-poc" in names
    # Sidecar + identity + prompts copied.
    assert (tmp_path / ".agam" / "knowledge" / "entity-names.txt").exists()
    assert (tmp_path / ".agam" / "AGAM.md").exists()
    assert (tmp_path / ".agam" / "config.yaml").exists()
    assert (tmp_path / ".agam" / "prompts" / "work-log.txt").exists()
    # Marker written.
    assert (tmp_path / ".agam" / ".migrated-from").exists()


def test_originals_left_untouched(tmp_path):
    legacy_db = _make_legacy(tmp_path)
    migrate_if_needed(tmp_path)
    assert legacy_db.exists()
    assert (tmp_path / ".claude" / "agam" / "AGAM.md").exists()


def test_already_when_agam_has_content(tmp_path):
    _make_legacy(tmp_path)
    (tmp_path / ".agam").mkdir()
    (tmp_path / ".agam" / "something").write_text("x")
    status, dest = migrate_if_needed(tmp_path)
    assert status == "already"
    assert dest is None
    # Did not copy over the existing agam home.
    assert not (tmp_path / ".agam" / "knowledge").exists()


def test_idempotent(tmp_path):
    _make_legacy(tmp_path)
    s1, _ = migrate_if_needed(tmp_path)
    s2, _ = migrate_if_needed(tmp_path)
    assert s1 == "migrated"
    assert s2 == "already"

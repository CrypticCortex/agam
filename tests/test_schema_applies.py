import pathlib
import re
import sqlite3

import pytest


SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "knowledge"
    / "graph-schema.sql"
)


def _fresh_db(tmp_path):
    """Create a fresh SQLite DB with schema applied and FK enforcement on."""
    db_path = tmp_path / "g.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), f"schema not found at {SCHEMA_PATH}"
    assert SCHEMA_PATH.stat().st_size > 0


def test_schema_has_no_data():
    """No DML statements. Match the keyword followed by whitespace so
    column names like 'updated' do not false-positive."""
    dml = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\s", re.IGNORECASE)
    offending = [
        line for line in SCHEMA_PATH.read_text().splitlines()
        if dml.match(line)
    ]
    assert not offending, f"schema must contain only DDL, found: {offending}"


def test_schema_is_ascii():
    raw = SCHEMA_PATH.read_bytes()
    raw.decode("ascii")  # raises UnicodeDecodeError if non-ASCII present


def test_schema_applies_to_fresh_db(tmp_path):
    conn = _fresh_db(tmp_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cur}
    finally:
        conn.close()
    assert "entities" in tables, f"entities table missing; got {tables}"
    assert "relationships" in tables, (
        f"relationships table missing; got {tables}"
    )
    assert "properties" in tables, f"properties table missing; got {tables}"


def test_schema_fts_tables_present(tmp_path):
    """FTS5 virtual tables are expected (entities_fts, entities_fts_v2)."""
    conn = _fresh_db(tmp_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cur}
    finally:
        conn.close()
    assert "entities_fts" in tables
    assert "entities_fts_v2" in tables


def test_schema_entities_insertable(tmp_path):
    """After applying schema, we can insert + query a basic entity row
    honoring the real NOT NULL constraints (type/created/updated)."""
    conn = _fresh_db(tmp_path)
    try:
        # entities columns: id, name, type, description, created, updated,
        # last_referenced. NOT NULL without default: name, type, created,
        # updated.
        conn.execute(
            "INSERT INTO entities (name, type, created, updated) "
            "VALUES (?, ?, ?, ?)",
            ("test-ent", "concept", "2026-01-01T00:00:00",
             "2026-01-01T00:00:00"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT name, type FROM entities WHERE name=?",
            ("test-ent",),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("test-ent", "concept")


def test_schema_relationship_fk_cascade(tmp_path):
    """Relationships reference entities with ON DELETE CASCADE."""
    conn = _fresh_db(tmp_path)
    try:
        now = "2026-01-01T00:00:00"
        conn.execute(
            "INSERT INTO entities (name, type, created, updated) "
            "VALUES (?, ?, ?, ?)",
            ("a", "concept", now, now),
        )
        conn.execute(
            "INSERT INTO entities (name, type, created, updated) "
            "VALUES (?, ?, ?, ?)",
            ("b", "concept", now, now),
        )
        conn.execute(
            "INSERT INTO relationships "
            "(source_id, target_id, relation, created) "
            "VALUES ((SELECT id FROM entities WHERE name='a'), "
            "(SELECT id FROM entities WHERE name='b'), 'links-to', ?)",
            (now,),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
        assert count == 1
        conn.execute("DELETE FROM entities WHERE name='a'")
        conn.commit()
        count_after = conn.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count_after == 0, "relationships should cascade on entity delete"


def test_entities_fts_v2_tokenizer_preserves_hyphen_underscore(tmp_path):
    """entities_fts_v2 uses tokenize='unicode61 tokenchars ''-_''' so names
    like 'voice-fnol_poc' stay as a single token. Guard against a future
    dump silently dropping that tokenchars config.

    Strategy: insert two rows whose tokenizations DIFFER between default
    unicode61 and our custom tokenizer. Under default, 'voice-fnol_poc'
    splits into {voice, fnol, poc}, so a search for the bare token 'voice'
    would hit BOTH rows. Under our custom tokenizer, 'voice-fnol_poc' is a
    single token, so the bare-'voice' search must hit ONLY the standalone
    'voice' row. That asymmetry is what makes this a real contract test --
    not just a phrase-match round-trip.
    """
    db = _fresh_db(tmp_path)
    try:
        for name in ("voice-fnol_poc", "voice"):
            db.execute(
                "INSERT INTO entities (name, type, created, updated) "
                "VALUES (?, ?, ?, ?)",
                (name, "project", "2026-01-01", "2026-01-01"),
            )
        # Mirror into FTS manually (the real code does this via
        # knowledge-graph.py). entities_fts_v2 columns: name, type,
        # description (content=entities, content_rowid=id).
        db.execute(
            "INSERT INTO entities_fts_v2 (rowid, name, type, description) "
            "SELECT id, name, type, COALESCE(description, '') FROM entities"
        )
        db.commit()

        # 1) Bare-token 'voice' must match ONLY the standalone 'voice' row.
        # If tokenchars were dropped, 'voice-fnol_poc' would also tokenize
        # to include 'voice' and this row would leak in.
        bare = db.execute(
            "SELECT name FROM entities_fts_v2 WHERE entities_fts_v2 MATCH ? "
            "ORDER BY name",
            ("voice",),
        ).fetchall()
        assert bare == [("voice",)], (
            f"tokenchars '-_' appears dropped: bare 'voice' leaked into "
            f"hyphenated row; got {bare}"
        )

        # 2) Full hyphenated term (quoted to bypass MATCH's '-' operator
        # syntax) must round-trip to the hyphenated row.
        full = db.execute(
            "SELECT name FROM entities_fts_v2 WHERE entities_fts_v2 MATCH ?",
            ('"voice-fnol_poc"',),
        ).fetchall()
        assert full == [("voice-fnol_poc",)], (
            f"expected exact hyphenated match, got {full}"
        )
    finally:
        db.close()


def test_entities_name_is_case_insensitive_unique(tmp_path):
    """entities.name is UNIQUE COLLATE NOCASE -- case-insensitive uniqueness
    is a real contract. Inserting 'agam' after 'Agam' must raise."""
    db = _fresh_db(tmp_path)
    try:
        db.execute(
            "INSERT INTO entities (name, type, created, updated) "
            "VALUES (?, ?, ?, ?)",
            ("Agam", "project", "2026-01-01", "2026-01-01"),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO entities (name, type, created, updated) "
                "VALUES (?, ?, ?, ?)",
                ("agam", "project", "2026-01-01", "2026-01-01"),
            )
    finally:
        db.close()

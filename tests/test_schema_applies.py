import pathlib
import sqlite3


SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "knowledge"
    / "graph-schema.sql"
)


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), f"schema not found at {SCHEMA_PATH}"
    assert SCHEMA_PATH.stat().st_size > 0


def test_schema_has_no_data():
    """No DML statements. Match the keyword followed by whitespace so
    column names like 'updated' do not false-positive."""
    import re
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
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
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
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
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
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
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
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute("PRAGMA foreign_keys = ON")
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

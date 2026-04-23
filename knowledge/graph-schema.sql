-- Agam knowledge graph schema (extracted from production DB)
-- Source: sqlite3 .schema dump of ~/.claude/knowledge/graph.db
-- Contains DDL only: CREATE TABLE / INDEX / VIRTUAL TABLE. No data rows.
-- Applies cleanly to a fresh SQLite (>=3.35) database via executescript().
CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE,
            type TEXT NOT NULL,
            description TEXT DEFAULT '',
            created TEXT NOT NULL,
            updated TEXT NOT NULL
        , last_referenced TEXT);
-- sqlite_sequence is auto-created by SQLite when AUTOINCREMENT is used;
-- it must not be CREATEd explicitly (reserved internal table).
CREATE TABLE relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation)
        );
CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated TEXT NOT NULL,
            UNIQUE(entity_id, key)
        );
CREATE INDEX idx_entities_type ON entities(type);
CREATE INDEX idx_entities_name ON entities(name);
CREATE INDEX idx_rel_source ON relationships(source_id);
CREATE INDEX idx_rel_target ON relationships(target_id);
CREATE INDEX idx_rel_relation ON relationships(relation);
CREATE INDEX idx_prop_entity ON properties(entity_id);
-- SQLite auto-creates the FTS5 shadow tables (entities_fts_data, _idx,
-- _content, _config, _docsize) when this CREATE VIRTUAL TABLE runs; the
-- raw dump's explicit CREATEs for those shadow tables were intentionally
-- removed so this schema applies cleanly to a fresh DB.
CREATE VIRTUAL TABLE entities_fts USING fts5(
                name, type, description, content=entities, content_rowid=id
            )
/* entities_fts(name,type,description) */;
-- Same shadow-table note as entities_fts above. The tokenchars '-_' config
-- is load-bearing: it keeps names like 'voice-fnol-poc' and 'voice_fnol_poc'
-- as single tokens rather than splitting on '-' / '_'.
CREATE VIRTUAL TABLE entities_fts_v2 USING fts5(
        name, type, description,
        content=entities, content_rowid=id,
        tokenize="unicode61 tokenchars '-_'"
    )
/* entities_fts_v2(name,type,description) */;

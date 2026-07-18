#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz"]
# ///

"""
UserPromptSubmit hook: intelligent graph recall.

3-stage matching pipeline (precision-first):
  Stage 1: Exact word/phrase match against entity name cache (highest precision)
  Stage 2: SQLite FTS5 with BM25 column weighting (name 10x > description)
  Stage 3: RapidFuzz fuzzy match -- last resort, only if stages 1-2 found <2 matches

Session dedup: tracks what's already been injected this session.

Environment variables:
    AGAM_RECALL_AGENT   Explicit runtime contract: ``codex`` selects the
                        fail-closed scoped reader; ``claude`` selects Claude's
                        legacy single-graph reader. Missing defaults to Claude
                        for compatibility with already-installed hooks.
    AGAM_DATA_HOME       Agent-neutral Agam data root. Scoped policy,
                         activation, and stores are resolved beneath it.
    AGAM_KNOWLEDGE_PROFILE / AGAM_KNOWLEDGE_SCOPES
                         Optional policy narrowing. These never expand the
                         agent's explicit registry selection.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple


DB_PATH = ""
_KG_DIR = ""
NAMES_CACHE = os.path.join(_KG_DIR, "entity-names.txt")
CONCEPT_INDEX = os.path.join(_KG_DIR, "concept-index.json")
IDF_INDEX = os.path.join(_KG_DIR, "idf-index.json")
SYCOPHANCY_LOG = os.path.join(_KG_DIR, "sycophancy-log.jsonl")
AGAM_CONTEXT_TOOL = ""
SESSION_FILE = ""  # Set in main() after parsing session_id
_STATE_NAMESPACE = "inactive"
_SELECTED_STORE = None
_LEGACY_ACTIVE = False
_MAX_STATE_BYTES = 1024 * 1024
_MAX_SIDECAR_BYTES = 5 * 1024 * 1024
# Late-bound seam retained for synthetic TOCTOU tests without importing Agam
# in Claude's copied standalone hook.
resolve_scope_db = None


class _ScopedStore(NamedTuple):
    scope: str
    database: Path
    root: Path
    sha256: str
    identity: tuple[int, int]
    root_identity: tuple[int, int]


def _recall_agent():
    return os.environ.get("AGAM_RECALL_AGENT", "claude").strip().lower()


def _is_codex_recall():
    return _recall_agent() == "codex"


def _configure_legacy_paths():
    """Activate Claude's standalone single-graph runtime contract."""
    global DB_PATH, _KG_DIR, NAMES_CACHE, CONCEPT_INDEX, IDF_INDEX
    global SYCOPHANCY_LOG, AGAM_CONTEXT_TOOL, _STATE_NAMESPACE
    global _SELECTED_STORE, _LEGACY_ACTIVE
    DB_PATH = os.environ.get(
        "AGAM_KG_PATH", os.path.expanduser("~/.agam/knowledge/graph.db")
    )
    _KG_DIR = os.environ.get("AGAM_KG_DIR") or os.path.dirname(DB_PATH)
    NAMES_CACHE = os.path.join(_KG_DIR, "entity-names.txt")
    CONCEPT_INDEX = os.path.join(_KG_DIR, "concept-index.json")
    IDF_INDEX = os.path.join(_KG_DIR, "idf-index.json")
    SYCOPHANCY_LOG = os.path.join(_KG_DIR, "sycophancy-log.jsonl")
    AGAM_CONTEXT_TOOL = os.environ.get(
        "AGAM_CONTEXT_TOOL",
        os.path.expanduser("~/.agam/tools/agam/agam_context.py"),
    )
    _STATE_NAMESPACE = "claude-legacy"
    _SELECTED_STORE = None
    _LEGACY_ACTIVE = True


def _safe_component(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "unknown")[:128]


def _state_directory_path():
    return Path(tempfile.gettempdir()) / f"agam-codex-state-{os.geteuid()}"


def _open_state_directory():
    """Open the current user's private state directory without following links."""
    if not all(
        hasattr(os, attribute)
        for attribute in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise OSError("secure descriptor flags unavailable")

    directory = _state_directory_path()
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass

    directory_fd = os.open(
        directory,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("unsafe state directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(directory_fd, 0o700)
            info = os.fstat(directory_fd)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("state directory is not private")
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _state_path(prefix, session_id, suffix):
    safe_session = _safe_component(session_id or f"fallback-{os.getppid()}")[:64]
    namespace = _safe_component(_STATE_NAMESPACE)
    directory_fd = _open_state_directory()
    os.close(directory_fd)
    return str(
        _state_directory_path()
        / f"{_safe_component(prefix)}-{namespace}-{safe_session}.{_safe_component(suffix)}"
    )


def _state_entry_name(path):
    candidate = Path(path)
    if candidate.parent != _state_directory_path():
        raise OSError("state path is outside the private state directory")
    if not candidate.name or _safe_component(candidate.name) != candidate.name:
        raise OSError("invalid state filename")
    return candidate.name


def _read_state_bytes(path):
    """Read one owned regular state file through its private directory fd."""
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = _open_state_directory()
        name = _state_entry_name(path)
        file_fd = os.open(
            name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > _MAX_STATE_BYTES
        ):
            return None
        chunks = []
        remaining = _MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or sum(len(chunk) for chunk in chunks) > _MAX_STATE_BYTES
        ):
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _write_all(file_fd, data):
    view = memoryview(data)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:
            raise OSError("short state write")
        view = view[written:]


def _write_state_bytes(path, data):
    """Atomically replace an owned regular state file; refuse links/special files."""
    if not isinstance(data, bytes) or len(data) > _MAX_STATE_BYTES:
        return False

    directory_fd = -1
    temporary_fd = -1
    temporary_name = ""
    try:
        directory_fd = _open_state_directory()
        name = _state_entry_name(path)
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
        ):
            return False

        for _ in range(8):
            temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
            try:
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                temporary_name = ""
        if temporary_fd < 0:
            return False

        _write_all(temporary_fd, data)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1

        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if existing is None:
            if current is not None:
                return False
        elif current is None or (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or (current.st_dev, current.st_ino)
            != (existing.st_dev, existing.st_ino)
        ):
            return False

        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        os.fsync(directory_fd)
        return True
    except OSError:
        return False
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name and directory_fd >= 0:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            os.close(directory_fd)


def _read_state_lines(path):
    data = _read_state_bytes(path)
    if data is None:
        return set()
    try:
        return {line.strip() for line in data.decode("utf-8").splitlines() if line.strip()}
    except UnicodeDecodeError:
        return set()


def _write_state_lines(path, values):
    data = "".join(f"{value}\n" for value in sorted(set(values))).encode("utf-8")
    return _write_state_bytes(path, data)


def _read_regular_file_bytes(path, *, maximum=_MAX_SIDECAR_BYTES):
    """Read a bounded regular file without following links or blocking on FIFOs."""
    if not all(
        hasattr(os, attribute)
        for attribute in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")
    ):
        return None
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            return None
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        size = sum(len(chunk) for chunk in chunks)
        if (
            size > maximum
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_json_sidecar(path):
    raw = _read_regular_file_bytes(path)
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _connect_db():
    """Open Claude's legacy graph or deserialize Codex's verified scope store."""
    if not _is_codex_recall():
        if not _LEGACY_ACTIVE or not DB_PATH:
            raise sqlite3.OperationalError("legacy graph is not configured")
        return sqlite3.connect(DB_PATH, timeout=2)

    store = _SELECTED_STORE
    if store is None:
        raise sqlite3.OperationalError("no verified scoped store selected")
    if not all(
        hasattr(os, attribute)
        for attribute in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise sqlite3.NotSupportedError("secure descriptor flags unavailable")

    root_fd = -1
    database_fd = -1
    try:
        root_fd = os.open(
            store.root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or (root_stat.st_dev, root_stat.st_ino) != store.root_identity
        ):
            raise OSError("scoped store root identity changed")

        database_fd = os.open(
            "graph.db",
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_fd,
        )
        before = os.fstat(database_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != store.identity
        ):
            raise OSError("scoped database identity changed")

        digest = hashlib.sha256()
        chunks = []
        while chunk := os.read(database_fd, 1024 * 1024):
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(database_fd)
        if (
            (after.st_dev, after.st_ino) != store.identity
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or digest.hexdigest() != store.sha256
        ):
            raise OSError("scoped database changed after activation")
        database_bytes = b"".join(chunks)
    finally:
        if database_fd >= 0:
            os.close(database_fd)
        if root_fd >= 0:
            os.close(root_fd)

    connection = sqlite3.connect(":memory:", timeout=2)
    try:
        if not hasattr(connection, "deserialize"):
            raise sqlite3.NotSupportedError("SQLite deserialize unavailable")
        connection.deserialize(database_bytes)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except Exception:
        connection.close()
        raise


def _resolve_scoped_stores():
    """Return (version, stores) only from the active fail-closed policy."""
    global resolve_scope_db
    if not _is_codex_recall():
        return "", ()

    # Keep the copied Claude hook package-independent. Codex explicitly pins
    # its mode and PYTHONPATH in hooks.json before reaching this import.
    from agam import paths
    from agam.knowledge_scopes import (
        load_active_manifest,
        resolve_effective_scopes,
        resolve_scope_db as packaged_resolve_scope_db,
    )
    if resolve_scope_db is None:
        resolve_scope_db = packaged_resolve_scope_db

    root = Path(
        os.environ.get("AGAM_KNOWLEDGE_SCOPE_ROOT", str(paths.scopes_dir()))
    )
    config = Path(
        os.environ.get("AGAM_KNOWLEDGE_CONFIG", str(root / "config.json"))
    )
    active = Path(
        os.environ.get("AGAM_ACTIVE_MANIFEST", str(root / "active.json"))
    )
    scopes = resolve_effective_scopes(
        _recall_agent(),
        config_path=config,
        active_path=active,
        scope_root=root,
    )
    if not scopes:
        return "", ()
    manifest = load_active_manifest(active, scope_root=root)
    if manifest is None:
        return "", ()
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return "", ()
    stores = []
    for scope in scopes:
        expected_root = resolved_root / scope / manifest["version"]
        expected_database = expected_root / "graph.db"
        try:
            root_before = os.stat(expected_root, follow_symlinks=False)
            database_before = os.stat(
                expected_database, follow_symlinks=False
            )
        except OSError:
            return "", ()
        database = resolve_scope_db(
            scope, manifest, active, scope_root=root
        )
        if database is None:
            return "", ()
        entry = manifest["stores"].get(scope)
        try:
            root_after = os.stat(expected_root, follow_symlinks=False)
            database_after = os.stat(database, follow_symlinks=False)
        except OSError:
            return "", ()
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or not stat.S_ISDIR(root_before.st_mode)
            or not stat.S_ISREG(database_before.st_mode)
            or database != expected_database
            or (root_before.st_dev, root_before.st_ino)
            != (root_after.st_dev, root_after.st_ino)
            or (database_before.st_dev, database_before.st_ino)
            != (database_after.st_dev, database_after.st_ino)
        ):
            return "", ()
        stores.append(
            _ScopedStore(
                scope=scope,
                database=database,
                root=expected_root,
                sha256=entry["sha256"],
                identity=(database_before.st_dev, database_before.st_ino),
                root_identity=(root_before.st_dev, root_before.st_ino),
            )
        )
    return manifest["version"], tuple(stores)


def _select_store(version, store):
    global DB_PATH, _KG_DIR, NAMES_CACHE, CONCEPT_INDEX, IDF_INDEX
    global SYCOPHANCY_LOG, SESSION_FILE, _STATE_NAMESPACE, _SELECTED_STORE
    _SELECTED_STORE = store
    DB_PATH = str(store.database)
    _KG_DIR = str(store.root)
    NAMES_CACHE = os.path.join(_KG_DIR, "entity-names.txt")
    CONCEPT_INDEX = os.path.join(_KG_DIR, "concept-index.json")
    IDF_INDEX = os.path.join(_KG_DIR, "idf-index.json")
    SYCOPHANCY_LOG = os.path.join(_KG_DIR, "sycophancy-log.jsonl")
    _STATE_NAMESPACE = f"{version}-{store.scope}"

# IDF threshold: terms appearing in too many entities are poor discriminators.
# IDF < 3.0 means the term appears in ~27+ of 525 entities (e.g., claude=41, skill=45).
# IDF >= 3.0 means the term is specific enough to be a useful signal.
IDF_THRESHOLD_CONCEPT = 3.0   # For concept index expansion
IDF_THRESHOLD_FTS = 3.0       # For FTS5 search terms

# Messages that don't need graph context
SKIP_PATTERNS = [
    r"^\s*(fix|edit|change|update|add|remove|delete|move|rename)\s",
    r"^\s*(commit|push|pull|merge|rebase|checkout|stash)\s",
    r"^\s*(run|test|build|deploy|install|npm|uv|pip|git)\s",
    r"^\s*(yes|no|ok|sure|thanks|yeah|nah|nope|yep|cool|done|lgtm)\s*[.!]?\s*$",
    r"^\s*/",  # slash commands
    r"^\s*\d+\s*$",  # just a number (ratings, line numbers)
]


def should_skip(message):
    """Fast check: skip messages that obviously don't need graph context."""
    msg = message.strip()
    if len(msg) < 15:
        return True
    for pattern in SKIP_PATTERNS:
        if re.match(pattern, msg, re.IGNORECASE):
            return True
    # Length-gated: short conversational/directive messages (<50 chars)
    # that start with common verbs are unlikely to reference entities.
    # Exception: if message contains a hyphenated word 8+ chars, it's
    # likely an entity name (voice-fnol-poc, agam-blog, FIRE-by-40) -- don't skip.
    if len(msg) < 50 and re.match(
        r"^\s*(look|check|see|tell|give|show|write|read|find|list|open|close|save|"
        r"use|try|set|get|put|let|make|take|keep|pick|drop|"
        r"go|do|can|will|just|now|here|also|then|please|should|could|would|might)\s",
        msg, re.IGNORECASE
    ):
        if not re.search(r"[a-zA-Z][a-zA-Z0-9]*-[a-zA-Z0-9-]{4,}", msg):
            return True
    return False


def load_entity_names():
    """Load names under the active agent's recall contract."""
    if not _is_codex_recall():
        if not _LEGACY_ACTIVE:
            return set()
        raw = _read_regular_file_bytes(NAMES_CACHE)
        if raw is None:
            return set()
        try:
            return {
                line.strip()
                for line in raw.decode("utf-8").splitlines()
                if line.strip()
            }
        except UnicodeDecodeError:
            return set()
    if not DB_PATH:
        return set()
    try:
        connection = _connect_db()
        vault_prefix = _selected_vault_prefix()
        rows = connection.execute(
            "SELECT LOWER(name) FROM entities "
            "WHERE description LIKE ? OR substr(description, 1, 9) = '[GENERAL]' "
            "ORDER BY LOWER(name), id",
            (f"{vault_prefix}%",),
        ).fetchall()
        connection.close()
        return {row[0] for row in rows if row[0]}
    except (OSError, sqlite3.Error):
        return set()


def load_idf_index():
    """Load precomputed IDF scores: term -> float.
    High IDF = rare/specific term (good signal). Low IDF = common term (noise)."""
    return _load_json_sidecar(IDF_INDEX)


def load_concept_index():
    """Load concept-index.json: concept_term -> [entity_names].
    Built by build-concept-index.py. Returns empty dict if not found."""
    return _load_json_sidecar(CONCEPT_INDEX)


def get_session_seen():
    """Load entity names already injected this session."""
    return _read_state_lines(SESSION_FILE) if SESSION_FILE else set()


def mark_session_seen(names):
    """Atomically record injected entity names in private session state."""
    if SESSION_FILE:
        _write_state_lines(SESSION_FILE, get_session_seen() | set(names))


def _database_configured():
    if _is_codex_recall():
        return _SELECTED_STORE is not None
    return _LEGACY_ACTIVE and bool(DB_PATH)


def _selected_vault_prefix():
    if _SELECTED_STORE is None:
        return "[VAULT:unselected]"
    return f"[VAULT:{_SELECTED_STORE.scope}]"


def _general_clause(column="description"):
    if not _is_codex_recall():
        return ""
    prefix = _selected_vault_prefix()
    return (
        f"AND ({column} LIKE '{prefix}%' "
        f"OR substr({column}, 1, 9) = '[GENERAL]')"
    )


def _description_visible(value):
    if not _is_codex_recall():
        return True
    return isinstance(value, str) and (
        value.startswith(_selected_vault_prefix())
        or value.startswith("[GENERAL]")
    )


def match_entities_3stage(message, entity_names):
    """3-stage entity matching: exact -> concept index -> FTS5 BM25 -> fuzzy.

    Returns ORDERED list of matched entity names (best first). Stages are
    additive but fuzzy only fires when earlier stages found <2 matches.
    """
    query = re.sub(r"[^a-zA-Z0-9\s-]", " ", message.lower()).strip()
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", query))

    # Stopwords: common English words that inflate BM25 scores when they
    # appear in entity names (e.g., "done" in "define-done-upfront").
    # Curated for Agam's graph, not a generic NLP list.
    STOPWORDS = {
        "the", "and", "for", "are", "but", "not", "you", "all", "any",
        "can", "her", "was", "one", "our", "out", "has", "have", "had",
        "been", "from", "this", "that", "they", "them", "then", "than",
        "what", "when", "where", "which", "who", "how", "why",
        "will", "with", "would", "could", "should", "does", "done",
        "make", "made", "just", "also", "into", "over", "such", "take",
        "only", "come", "some", "very", "work", "each", "like", "more",
        "about", "after", "being", "before", "between", "both", "here",
        "there", "these", "those", "under", "first", "still", "every",
        "need", "want", "tell", "show", "give", "keep", "lets", "look",
    }
    # Content words stay: "voice", "axios", "deploy", "build", etc.
    content_words = words - STOPWORDS

    # Load all entity type names from graph -- taxonomic vocabulary used to
    # suppress single-term matches on type names (product, service, pattern, etc.)
    all_type_names = set()
    if _database_configured():
        try:
            _tc = _connect_db()
            for row in _tc.execute(
                "SELECT DISTINCT LOWER(type) FROM entities "
                f"WHERE type IS NOT NULL {_general_clause()}"
            ):
                if row[0]:
                    all_type_names.add(row[0])
            _tc.close()
        except Exception:
            pass

    # --- Stage 1: Exact match (word intersection + hyphen-normalized) ---
    # Direct match: message words vs entity names
    exact = entity_names & words

    # Also match hyphen-joined words: "voice fnol" -> "voice-fnol-poc"
    # Build all 2-3 word hyphen combos from message words
    word_list = re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", query)
    for i in range(len(word_list)):
        for j in range(i + 1, min(i + 4, len(word_list) + 1)):
            combo = "-".join(word_list[i:j])
            if combo in entity_names:
                exact.add(combo)

    # --- Stage 1.5: Concept index expansion ---
    # O(1) lookup: message words -> related entities via prebuilt concept map.
    # Catches cases like "dependency security" -> axios-supply-chain-attack
    # where no entity NAME matches but concept TERMS do.
    # Scoring: rank by hit count (more matching terms = more relevant).
    # Cap at 6 to prevent noisy single-term matches from flooding results.
    concept_matches = set()
    concept_idx = load_concept_index()
    idf_scores = load_idf_index()
    concept_ranked = []
    if concept_idx:
        # IDF-based filtering replaces manual stopword lists.
        # Terms with low IDF (appearing in many entities) are poor discriminators.
        # Only keep terms with IDF >= threshold (specific enough to be useful).
        if idf_scores:
            concept_words = {
                w for w in content_words
                if idf_scores.get(w, 6.0) >= IDF_THRESHOLD_CONCEPT
            }
        else:
            # Fallback: minimal stopwords if IDF index not available
            concept_words = content_words - {
                "project", "feature", "agent", "skill", "lesson",
                "decision", "service", "tool", "claude", "pattern",
            }

        # Count how many message words map to each entity
        # Try basic stemming: if "incidents" misses, try "incident" (strip s/es/ed/ing)
        entity_hits = {}  # entity_name -> set of matching concept terms
        for word in concept_words:
            variants = [word]
            if word.endswith("ies"):
                variants.append(word[:-3] + "y")
            elif word.endswith("es"):
                variants.append(word[:-2])
            elif word.endswith("s") and not word.endswith("ss"):
                variants.append(word[:-1])
            if word.endswith("ing") and len(word) > 5:
                variants.append(word[:-3])
            if word.endswith("ed") and len(word) > 4:
                variants.append(word[:-2])

            for variant in variants:
                # Skip if stemmed form has low IDF (too common to be useful)
                if variant != word and idf_scores.get(variant, 6.0) < IDF_THRESHOLD_CONCEPT:
                    continue
                if variant in concept_idx:
                    for ename in concept_idx[variant]:
                        if ename in entity_names:
                            entity_hits.setdefault(ename, set()).add(word)

        # Score: multi-term matches get priority, type matches get 2x weight
        # Look up entity types for type-match boost
        entity_types = {}
        if _database_configured():
            try:
                _conn = _connect_db()
                for ename in entity_hits:
                    row = _conn.execute(
                        "SELECT type FROM entities WHERE LOWER(name) = ? "
                        f"{_general_clause()} LIMIT 1",
                        (ename,),
                    ).fetchone()
                    if row:
                        entity_types[ename] = row[0].lower() if row[0] else ""
                _conn.close()
            except Exception:
                pass

        ranked = []
        for ename, terms in entity_hits.items():
            # Type match boost: if a message word matches the entity's type, +2 hits
            # This ensures entities OF the requested type outrank entities that
            # merely MENTION the type in their description.
            etype = entity_types.get(ename, "")
            type_boost = 0
            for t in terms:
                # Check if this term matched via entity type (stem comparison)
                if etype and (t == etype or t.rstrip("s") == etype or t.rstrip("es") == etype):
                    type_boost += 2

            # Multi-term requires actual DISTINCT content words, not type boost alone.
            # "product" matching type="product" is a single-word match even with boost.
            actual_term_count = len(terms)
            is_multi = actual_term_count >= 2
            effective_hits = actual_term_count + type_boost  # boost for ranking only
            if is_multi:
                ranked.append((ename, effective_hits, True))
            elif any(len(t) >= 6 for t in terms):
                # Name-relevance gate for single-term matches:
                # The matching term must appear in the entity NAME (not just description).
                # "checks" -> wiki-lint-operation is noise (description-only match).
                # "fnol" -> voice-fnol-poc is signal (name match).
                name_parts = set(re.findall(r"[a-zA-Z]{3,}", ename.lower()))
                if name_parts & terms:
                    # Entity-type suppression: if the ONLY matching term is a
                    # type name ANYWHERE in the graph, suppress it.
                    # Type names are taxonomic vocabulary (product, service, tool,
                    # pattern, lesson, etc.) -- when users say these words they
                    # mean the English word, not a specific entity reference.
                    # "product" -> product-mismatch-pattern is noise even though
                    # the entity type is "pattern", because "product" is a known
                    # type name in the graph.
                    matching_terms = name_parts & terms
                    if len(matching_terms) == 1 and list(matching_terms)[0] in all_type_names:
                        pass  # suppress: only match is a graph type name
                    else:
                        ranked.append((ename, len(terms), False))

        # Secondary relevance: count how many EXACT query words appear in entity name
        # Intentionally no stemming here -- stemming causes type-derived words
        # (e.g., "incident" from "incidents") to give all incident entities
        # identical relevance, drowning out the truly relevant match.
        def name_relevance(ename):
            name_parts = set(re.findall(r"[a-zA-Z]{3,}", ename.lower()))
            return len(name_parts & content_words)

        # Sort: multi-term first, then by hit count desc, then name relevance
        ranked.sort(
            key=lambda x: (-int(x[2]), -x[1], -name_relevance(x[0]), x[0])
        )

        # If we have multi-term matches, suppress noisy single-term ones
        multi_count = sum(1 for _, _, is_multi in ranked if is_multi)
        if multi_count >= 2:
            # Keep only multi-term matches (high confidence), already sorted
            concept_ranked = [ename for ename, _, is_multi in ranked if is_multi][:5]
        else:
            # Allow single-term matches but cap at 4
            concept_ranked = [ename for ename, _, _ in ranked[:4]]

    # Build ordered result: exact matches first (sorted by name relevance),
    # then concept matches in ranked order, deduped
    def rank_exact(name):
        """Exact matches ranked by how many query words appear in name."""
        parts = set(re.findall(r"[a-zA-Z]{3,}", name.lower()))
        return (-len(parts & content_words), name)

    exact_ordered = sorted(exact, key=rank_exact)
    seen_names = set()
    ordered = []
    for name in exact_ordered + concept_ranked:
        if name not in seen_names:
            seen_names.add(name)
            ordered.append(name)

    if len(ordered) >= 3:
        return ordered

    # --- Stage 2: FTS5 BM25 ranked search ---
    fts_matches = set()
    if _database_configured():
        try:
            conn = _connect_db()

            # Build FTS5 query: prefix-match each content word 5+ chars
            # IDF-based filtering: only use terms specific enough to discriminate
            fts_terms = [
                f'"{w}"*' for w in content_words
                if len(w) >= 5 and idf_scores.get(w, 6.0) >= IDF_THRESHOLD_FTS
            ]
            if fts_terms:
                fts_query = " OR ".join(fts_terms)
                # Column weights: name=10.0, type=0.1, description=1.0
                # BM25 returns negative scores (more negative = more relevant)
                cursor = conn.execute(
                    f"""SELECT name, bm25(entities_fts, 10.0, 0.1, 1.0) as score
                       FROM entities_fts
                       WHERE entities_fts MATCH ?
                       {_general_clause()}
                       ORDER BY score
                       LIMIT 10""",
                    (fts_query,)
                )
                rows = cursor.fetchall()

                if rows:
                    # Keep entities scoring within 60% of best match
                    best_score = rows[0][1]
                    for name, score in rows[:5]:  # cap at 5
                        ratio = score / best_score if best_score != 0 else 0
                        if ratio < 0.6:
                            continue
                        # Name relevance gate: at least one query word (4+ chars)
                        # must appear in the entity name (hyphen-split).
                        # This prevents description-only matches from polluting results.
                        name_parts = set(re.findall(r"[a-zA-Z]{3,}", name.lower()))
                        matching = name_parts & content_words
                        if matching:
                            # Type suppression: if the only matching term is a
                            # graph type name, it's taxonomic not referential
                            if len(matching) == 1 and list(matching)[0] in all_type_names:
                                continue
                            fts_matches.add(name.lower())

            conn.close()
        except Exception:
            pass

    # Add FTS matches after concept matches (deduped)
    for name in sorted(fts_matches):
        if name not in seen_names:
            seen_names.add(name)
            ordered.append(name)

    if len(ordered) >= 2:
        return ordered

    # --- Stage 3: RapidFuzz (last resort, tightened thresholds) ---
    # Only fires when stages 1-2 found <2 matches
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return ordered

    entity_list = sorted(entity_names)
    # Full query match with high threshold
    matches = process.extract(
        query,
        entity_list,
        scorer=fuzz.token_set_ratio,
        limit=3,
        score_cutoff=80,
    )

    # Per-word match: only long words (6+ chars), high cutoff (95)
    for word in sorted(words):
        if len(word) >= 6:
            word_matches = process.extract(
                word,
                entity_list,
                scorer=fuzz.partial_ratio,
                limit=2,
                score_cutoff=95,
            )
            matches.extend(word_matches)

    for name, score, _idx in matches:
        if name not in seen_names:
            # Name-relevance + type suppression (same gates as stages 1.5/2)
            fname_parts = set(re.findall(r"[a-zA-Z]{3,}", name.lower()))
            fname_matching = fname_parts & content_words
            if not fname_matching:
                continue  # no content word in entity name = pure noise
            if len(fname_matching) == 1 and list(fname_matching)[0] in all_type_names:
                continue  # only match is a type name = taxonomic noise
            seen_names.add(name)
            ordered.append(name)

    return ordered


def is_question(message):
    """Check if message is a question."""
    return "?" in message or re.match(r"^\s*(what|who|where|when|why|how|which|does|is|are|can|did|do|has|have|will|should|could|would)\s", message, re.IGNORECASE)


def search_graph(matched_names, message_words):
    """Query graph for matched entities, relationships, and properties."""
    if not DB_PATH:
        return [], [], [], []

    try:
        conn = _connect_db()

        results = []

        # Obsoletion filter. Entities can carry a ``status`` property whose
        # value is "obsolete" (set by ``agam obsolete <entity>`` or by Sonnet
        # via apply-proposals when a session indicates the entity is no longer
        # current). Skip them in recall so the model isn't asked to reason
        # about stale facts on every prompt that happens to mention them. The
        # entities are preserved on disk for forensic queries; set
        # ``AGAM_INCLUDE_OBSOLETE=1`` to surface them anyway.
        obsolete_names: set[str] = set()
        if os.environ.get("AGAM_INCLUDE_OBSOLETE", "").strip() != "1":
            try:
                _obs = conn.execute(
                    f"""SELECT LOWER(e.name)
                       FROM entities e
                       JOIN properties p ON p.entity_id = e.id
                       WHERE p.key = 'status' AND p.value = 'obsolete'
                       {_general_clause('e.description')}"""
                )
                obsolete_names = {row[0] for row in _obs}
            except Exception:
                # Schema without properties table or other failure -- be
                # permissive (no filtering) rather than block recall.
                obsolete_names = set()

        # Get matched entities, sorted by recency (tiebreaker for equal-relevance matches)
        for name in matched_names:
            if name.lower() in obsolete_names:
                continue
            cursor = conn.execute(
                "SELECT name, type, description FROM entities "
                "WHERE LOWER(name) = ? "
                f"{_general_clause()} LIMIT 1",
                (name,)
            )
            row = cursor.fetchone()
            if row and _description_visible(row[2]):
                results.append({"name": row[0], "type": row[1], "desc": row[2]})

        # No FTS5 supplement here -- the 3-stage matcher in match_entities_3stage()
        # already handles FTS5 with a name-relevance gate. Running ungated FTS5
        # here was the source of false positives (e.g., "what about now" matching
        # entities whose descriptions contain "what" or "about").

        # Get relationships for matched entities.
        # Skip hub entities (the user themselves, plus any high-degree nodes the
        # operator wants suppressed) as sources -- they fan out to everything.
        matched_set = set(r["name"] for r in results)
        entity_names = [result["name"] for result in results][:5]
        rels = []
        connected_names = set()  # track 1-hop neighbors for expansion

        # Hub entities: high-degree nodes that add noise as relationship sources.
        # Configurable via AGAM_HUB_ENTITIES (comma-separated). Default includes the
        # active user entity (AGAM_USER_ENTITY, defaults to "User") so a fresh 
        # install has a sensible suppression list out of the box.
        _user_entity = os.environ.get("AGAM_USER_ENTITY", "User").strip()
        _hub_env = os.environ.get("AGAM_HUB_ENTITIES", "").strip()
        if _hub_env:
            HUB_ENTITIES = {e.strip() for e in _hub_env.split(",") if e.strip()}
        else:
            HUB_ENTITIES = {_user_entity, "Claude-Code"}

        # Build SQL placeholder string and parameter list for hub suppression.
        _hub_list = sorted(HUB_ENTITIES)
        _hub_placeholders = ",".join("?" for _ in _hub_list)

        for name in entity_names:
            cursor = conn.execute(
                f"""SELECT src.name, r.relation, tgt.name, r.weight
                   FROM relationships r
                   JOIN entities src ON r.source_id = src.id
                   JOIN entities tgt ON r.target_id = tgt.id
                   WHERE (src.name = ? OR tgt.name = ?)
                   AND src.name NOT IN ({_hub_placeholders})
                   {_general_clause('src.description')}
                   {_general_clause('tgt.description')}
                   ORDER BY r.id
                   LIMIT 6""",
                (name, name, *_hub_list)
            )
            for row in cursor:
                # Skip relationships that touch obsolete entities entirely --
                # otherwise the model sees "X --[depends-on]--> Y" where Y is
                # an entity we'd never show on its own, which is misleading.
                if row[0].lower() in obsolete_names or row[2].lower() in obsolete_names:
                    continue
                conf = f" [{row[3]}]" if row[3] != 1.0 else ""
                rels.append(f"{row[0]} --[{row[1]}]--> {row[2]}{conf}")
                other = row[2] if row[0] == name else row[0]
                if other not in matched_set and other not in HUB_ENTITIES:
                    connected_names.add(other)

        # Drop any obsolete-name pickups from the 1-hop expansion candidate
        # set. Belt-and-suspenders -- the relationship loop above already
        # filtered, but expansion runs against ``connected_names`` so we
        # double-check before hitting the entities table.
        connected_names = {n for n in connected_names if n.lower() not in obsolete_names}

        # 1-hop expansion: load connected entities as full entries
        # Prioritize high-signal types (incidents, decisions, lessons, bugs)
        # Skip hub types (user, company) that add noise
        connected = []
        if connected_names:
            placeholders = ",".join("?" for _ in connected_names)
            cursor = conn.execute(
                f"""SELECT name, type, description FROM entities
                    WHERE name IN ({placeholders})
                    AND type NOT IN ('user', 'company', 'belief', 'strategy')
                    {_general_clause()}
                    ORDER BY CASE type
                        WHEN 'incident' THEN 0
                        WHEN 'decision' THEN 1
                        WHEN 'lesson' THEN 2
                        WHEN 'bug' THEN 3
                        WHEN 'pattern' THEN 4
                        WHEN 'feature' THEN 5
                        WHEN 'tool' THEN 6
                        WHEN 'service' THEN 7
                        ELSE 8
                    END, name
                    LIMIT 5""",
                sorted(connected_names)
            )
            for row in cursor:
                if _description_visible(row[2]):
                    connected.append({"name": row[0], "type": row[1], "desc": row[2]})

        # Get properties for matched entities only (connected don't need props)
        props = []
        for name in entity_names[:4]:
            cursor = conn.execute(
                f"""SELECT p.key, p.value FROM properties p
                   JOIN entities e ON p.entity_id = e.id
                   WHERE e.name = ?
                   {_general_clause('e.description')}
                   ORDER BY p.id LIMIT 3""",
                (name,)
            )
            for row in cursor:
                val = row[1][:150] if row[1] else ""
                props.append(f"{name}.{row[0]} = {val}")

        conn.close()

        # Dedupe
        seen = set()
        unique = []
        for r in results:
            if r["name"] not in seen:
                seen.add(r["name"])
                unique.append(r)

        return unique[:4], rels[:6], props[:6], connected[:4]

    except Exception:
        return [], [], [], []


def cleanup_stale_tmp():
    """Remove stale regular files only from the private Codex state directory."""
    import time

    cutoff = time.time() - 86400
    directory_fd = -1
    try:
        directory_fd = _open_state_directory()
        for name in os.listdir(directory_fd):
            try:
                info = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
    except OSError:
        pass
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def get_sycophancy_correction(session_id=""):
    """Return Claude's one-shot correction; Codex capture stays disabled."""
    if _is_codex_recall():
        return ""
    safe_id = _safe_component(session_id or f"fallback-{os.getppid()}")[:64]
    syc_file = os.path.join(tempfile.gettempdir(), f"sycophancy-{safe_id}.json")
    try:
        with open(syc_file) as handle:
            data = json.load(handle)
        os.unlink(syc_file)
        if data.get("detected"):
            patterns = ", ".join(data.get("patterns", [])[:2])
            return (
                "CORRECTION: Your previous response was flagged sycophantic. "
                f"Pattern: {patterns}. Do NOT open with praise or agreement. "
                "Lead with substance."
            )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return ""


def get_sycophancy_blacklist():
    """Load recent sycophantic phrases for session-start injection."""
    log_path = SYCOPHANCY_LOG
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        if not entries:
            return ""
        # Dedupe phrases, take last 20
        phrases = list(dict.fromkeys(e["phrase"] for e in entries[-50:]))[-20:]
        lines = ["ANTI-SYCOPHANCY (auto-learned from past sessions):"]
        lines.append("Do NOT open responses with these or similar phrases:")
        for p in phrases:
            lines.append(f'  - "{p}"')
        lines.append("Lead with substance. Skip praise. Be direct.")
        return "\n".join(lines)
    except (json.JSONDecodeError, OSError):
        return ""


def get_header_and_mark(session_id):
    """Return (header_str, is_first_fire) and mark this session as header-sent.

    First fire in a session gets the full DIRECTIVE header. Subsequent fires
    get a compact 'KG:' one-liner -- saves ~350 chars/fire on heavy sessions.
    """
    flag = _state_path("graph-recall-header", session_id, "flag")
    if _read_state_bytes(flag) is not None:
        return False

    directory_fd = -1
    marker_fd = -1
    try:
        directory_fd = _open_state_directory()
        name = _state_entry_name(flag)
        marker_fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(marker_fd, b"1")
        os.fsync(marker_fd)
    except OSError:
        # Unsafe pre-existing entries are ignored, not followed or replaced.
        pass
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
    return True


def get_boot_context(session_id):
    """Run Claude boot context once; Codex boot remains disabled by policy."""
    if _is_codex_recall():
        return ""
    import subprocess

    flag = _state_path("agam-boot", session_id, "flag")
    if _read_state_bytes(flag) is not None:
        return ""
    if not _write_state_bytes(flag, b"1") or not os.path.exists(AGAM_CONTEXT_TOOL):
        return ""
    try:
        result = subprocess.run(
            [sys.executable, AGAM_CONTEXT_TOOL, "boot"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    boot = result.stdout.strip() if result.returncode == 0 else ""
    blacklist = get_sycophancy_blacklist()
    return "\n\n".join(part for part in (boot, blacklist) if part)


def check_lesson_triggers_in_message(message, session_id=""):
    """Check the verified scoped database for matching lesson triggers."""
    seen_file = _state_path("lesson-seen", session_id, "txt")

    # Build only from the descriptor-verified in-memory SQLite image. A temp
    # cache is never an authority for lesson text or cross-scope provenance.
    try:
        conn = _connect_db()
        try:
            rows = conn.execute(f"""
                SELECT e.name, e.description, p.key, p.value
                FROM entities e
                JOIN properties p ON e.id = p.entity_id
                WHERE e.type = 'lesson' AND p.key IN ('trigger-tool', 'trigger-error', 'severity')
                {_general_clause('e.description')}
            """).fetchall()
        finally:
            conn.close()

        index = {"tool": [], "error": []}
        lessons = {}
        for name, desc, key, value in rows:
            if name not in lessons:
                lessons[name] = {"desc": desc, "severity": "medium"}
            if key == "severity":
                lessons[name]["severity"] = value
            elif key == "trigger-tool":
                try:
                    for pattern in json.loads(value):
                        index["tool"].append(
                            {
                                "pattern": pattern.lower(),
                                "lesson": name,
                                "severity": None,
                                "desc": "",
                            }
                        )
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
            elif key == "trigger-error":
                try:
                    for pattern in json.loads(value):
                        index["error"].append(
                            {
                                "pattern": pattern.lower(),
                                "lesson": name,
                                "severity": None,
                                "desc": "",
                            }
                        )
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

        for entry in index["tool"] + index["error"]:
            lesson = lessons.get(entry["lesson"], {})
            entry["severity"] = lesson.get("severity", "medium")
            entry["desc"] = lesson.get("desc", "")
    except Exception:
        return ""

    # Match message against tool + error triggers
    msg_lower = message.lower()
    matched = {}
    for entry in index.get("tool", []) + index.get("error", []):
        if entry["lesson"] in matched:
            continue
        if entry["pattern"] in msg_lower:
            matched[entry["lesson"]] = entry

    if not matched:
        return ""

    # Session dedup
    seen = _read_state_lines(seen_file)

    new_matches = [m for m in matched.values() if m["lesson"] not in seen]
    if not new_matches:
        return ""

    # Sort by severity, cap at 2
    sev_order = {"high": 0, "medium": 1, "low": 2}
    new_matches.sort(key=lambda m: sev_order.get(m["severity"], 3))
    new_matches = new_matches[:2]

    lines = ["LESSON ACTIVATION (from conversation context):"]
    for m in new_matches:
        sev = m["severity"].upper()
        desc = m.get("desc", "")[:120]
        lines.append(f"* {m['lesson']} [{sev}]: {desc}")
        lines.append(f"  Triggered by: '{m['pattern']}' mentioned in message.")
    lines.append("Consider these lessons before proceeding.")

    # Mark seen
    _write_state_lines(
        seen_file, seen | {match["lesson"] for match in new_matches}
    )

    return "\n".join(lines)


def _main_codex(message, session_id):
    global SESSION_FILE, _STATE_NAMESPACE
    version, stores = _resolve_scoped_stores()
    if not stores or should_skip(message):
        return

    message_words = set(
        re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", message.lower())
    )
    question = is_question(message)
    entities = []
    rels = []
    props = []
    connected = []
    lesson_contexts = []

    # The registry order is canonical, keeping merged recall deterministic.
    for store in stores:
        _select_store(version, store)
        SESSION_FILE = _state_path("graph-recall", session_id, "txt")
        scoped_names = load_entity_names()
        matched = (
            match_entities_3stage(message, scoped_names) if scoped_names else []
        )
        seen = get_session_seen()
        new_matches = [name for name in matched if name not in seen]
        selected = new_matches if new_matches else (matched if question else [])
        if selected:
            scoped_entities, scoped_rels, scoped_props, scoped_connected = (
                search_graph(selected, message_words)
            )
            entities.extend(scoped_entities)
            rels.extend(scoped_rels)
            props.extend(scoped_props)
            connected.extend(scoped_connected)
            if scoped_entities:
                mark_session_seen([entity["name"] for entity in scoped_entities])
        lesson = check_lesson_triggers_in_message(message, session_id)
        if lesson and lesson not in lesson_contexts:
            lesson_contexts.append(lesson)

    def unique(items, key):
        output = []
        seen = set()
        for item in items:
            marker = key(item)
            if marker not in seen:
                seen.add(marker)
                output.append(item)
        return output

    entities = unique(entities, lambda item: item["name"])
    connected = unique(connected, lambda item: item["name"])
    rels = unique(rels, lambda item: item)
    props = unique(props, lambda item: item)
    if not entities and not lesson_contexts:
        return

    _STATE_NAMESPACE = f"{version}-{'-'.join(store.scope for store in stores)}"
    is_first_fire = get_header_and_mark(session_id)
    if is_first_fire:
        header = (
            "DIRECTIVE: Scoped Agam memory is prior, advisory evidence, not live truth. "
            "Verify load-bearing facts against current disk state or an authoritative source "
            "before acting. Cite entity names when this memory influences the answer."
        )
    else:
        header = (
            "KG: scoped prior/advisory evidence. Verify load-bearing facts; cite entities."
        )

    connected_cap = 4 if is_first_fire else 2
    rels_cap = len(rels) if is_first_fire else 3
    props_cap = len(props) if is_first_fire else 3
    lines = [header]
    if entities:
        entity_names = [entity["name"] for entity in entities]
        lines.append(f"Matched: {', '.join(entity_names[:8])}")
        for entity in entities:
            description = entity["desc"][:80]
            lines.append(
                f"  {entity['name']} [{entity['type']}]: {description}"
            )
    if connected:
        lines.append("Connected (1-hop):")
        for item in connected[:connected_cap]:
            lines.append(
                f"  {item['name']} [{item['type']}]: {item['desc'][:60]}"
            )
    if rels:
        lines.append("Relationships:")
        lines.extend(f"  {item}" for item in rels[:rels_cap])
    if props:
        lines.append("Properties:")
        lines.extend(f"  {item}" for item in props[:props_cap])
    for lesson in lesson_contexts:
        lines.extend(("", lesson))

    context_parts = []
    correction = get_sycophancy_correction(session_id)
    if correction:
        context_parts.append(correction)
    context_parts.append("\n".join(lines))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n---\n\n".join(context_parts),
                }
            }
        )
    )


def _main_claude(message, session_id):
    """Preserve Claude's standalone legacy single-graph recall behavior."""
    global SESSION_FILE
    SESSION_FILE = _state_path("graph-recall", session_id, "txt")
    boot_context = get_boot_context(session_id)
    if should_skip(message):
        if boot_context:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "IDENTITY CONTEXT (session start):\n" + boot_context,
            }}))
        return
    names = load_entity_names()
    if not names:
        return
    matched = match_entities_3stage(message, names)
    seen = get_session_seen()
    fresh = [name for name in matched if name not in seen]
    selected = fresh if fresh else (matched if is_question(message) else [])
    lesson = check_lesson_triggers_in_message(message, session_id)
    if not selected:
        if lesson:
            parts = []
            if boot_context:
                parts.append("IDENTITY CONTEXT (session start):\n" + boot_context)
            parts.append(lesson)
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n---\n\n".join(parts),
            }}))
        return
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", message.lower()))
    entities, rels, props, connected = search_graph(selected, words)
    if not entities:
        return
    first = get_header_and_mark(session_id)
    header = (
        "DIRECTIVE: Knowledge graph context for this message. This is deterministic "
        "recall from your knowledge graph -- treat it as ground truth. You MUST "
        "reference matched entities by name."
        if first else
        "KG: deterministic recall (header in session-start injection). Cite entities, do not re-search."
    )
    lines = [header, "Matched: " + ", ".join(e["name"] for e in entities[:8])]
    for entity in entities:
        description = entity["desc"][:80] if entity["desc"] else "(no description)"
        lines.append(f"  {entity['name']} [{entity['type']}]: {description}")
    if connected:
        lines.append("Connected (1-hop):")
        for item in connected[:4 if first else 2]:
            description = item["desc"][:60] if item["desc"] else "(no description)"
            lines.append(f"  {item['name']} [{item['type']}]: {description}")
    if rels:
        lines.extend(["Relationships:", *(f"  {item}" for item in rels[:6 if first else 3])])
    if props:
        lines.extend(["Properties:", *(f"  {item}" for item in props[:6 if first else 3])])
    if lesson:
        lines.extend(("", lesson))
    parts = []
    correction = get_sycophancy_correction(session_id)
    if correction:
        parts.append(correction)
    if boot_context:
        parts.append("IDENTITY CONTEXT (session start):\n" + boot_context)
    parts.append("\n".join(lines))
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n\n---\n\n".join(parts),
    }}))
    mark_session_seen([entity["name"] for entity in entities])


def main():
    agent = _recall_agent()
    if agent == "claude":
        _configure_legacy_paths()
    elif agent != "codex":
        return
    cleanup_stale_tmp()
    data = json.load(sys.stdin)
    message = data.get("prompt", "") or data.get("message", "")
    session_id = data.get("session_id", "")
    if agent == "codex":
        _main_codex(message, session_id)
    else:
        _main_claude(message, session_id)


if __name__ == "__main__":
    main()

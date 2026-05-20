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
    AGAM_KG_PATH         Path to knowledge graph SQLite DB
                         (default: ~/.claude/knowledge/graph.db)
    AGAM_KG_DIR          Directory holding KG sidecar caches
                         (entity-names.txt, concept-index.json, idf-index.json,
                         sycophancy-log.jsonl). Defaults to the parent of
                         AGAM_KG_PATH.
    AGAM_CONTEXT_TOOL    Path to agam-context.py (used for boot injection).
                         (default: ~/.claude/tools/agam-context.py)
"""

import json
import sys
import re
import sqlite3
import os
import tempfile


DB_PATH = os.environ.get(
    "AGAM_KG_PATH", os.path.expanduser("~/.claude/knowledge/graph.db")
)
# Sidecar caches live alongside the KG by default; override via AGAM_KG_DIR.
_KG_DIR = os.environ.get("AGAM_KG_DIR") or os.path.dirname(DB_PATH)
NAMES_CACHE = os.path.join(_KG_DIR, "entity-names.txt")
CONCEPT_INDEX = os.path.join(_KG_DIR, "concept-index.json")
IDF_INDEX = os.path.join(_KG_DIR, "idf-index.json")
SYCOPHANCY_LOG = os.path.join(_KG_DIR, "sycophancy-log.jsonl")
AGAM_CONTEXT_TOOL = os.environ.get(
    "AGAM_CONTEXT_TOOL", os.path.expanduser("~/.claude/tools/agam-context.py")
)
SESSION_FILE = ""  # Set in main() after parsing session_id

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
    """Load cached entity names as a set for fast intersection."""
    if not os.path.exists(NAMES_CACHE):
        return set()
    with open(NAMES_CACHE) as f:
        return {line.strip() for line in f if line.strip()}


def load_idf_index():
    """Load precomputed IDF scores: term -> float.
    High IDF = rare/specific term (good signal). Low IDF = common term (noise)."""
    if not os.path.exists(IDF_INDEX):
        return {}
    try:
        with open(IDF_INDEX) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_concept_index():
    """Load concept-index.json: concept_term -> [entity_names].
    Built by build-concept-index.py. Returns empty dict if not found."""
    if not os.path.exists(CONCEPT_INDEX):
        return {}
    try:
        with open(CONCEPT_INDEX) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get_session_seen():
    """Load entity names already injected this session."""
    if not os.path.exists(SESSION_FILE):
        return set()
    with open(SESSION_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def mark_session_seen(names):
    """Append injected entity names to session dedup file."""
    with open(SESSION_FILE, "a") as f:
        for name in names:
            f.write(name + "\n")


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
    if os.path.exists(DB_PATH):
        try:
            _tc = sqlite3.connect(DB_PATH, timeout=1)
            for row in _tc.execute("SELECT DISTINCT LOWER(type) FROM entities WHERE type IS NOT NULL"):
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
        if os.path.exists(DB_PATH):
            try:
                _conn = sqlite3.connect(DB_PATH, timeout=1)
                for ename in entity_hits:
                    row = _conn.execute(
                        "SELECT type FROM entities WHERE LOWER(name) = ? LIMIT 1", (ename,)
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
        ranked.sort(key=lambda x: (-int(x[2]), -x[1], -name_relevance(x[0])))

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
        return -len(parts & content_words)

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
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=2)
            conn.execute("PRAGMA journal_mode=WAL")

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
                    """SELECT name, bm25(entities_fts, 10.0, 0.1, 1.0) as score
                       FROM entities_fts
                       WHERE entities_fts MATCH ?
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
    for name in fts_matches:
        if name not in seen_names:
            seen_names.add(name)
            ordered.append(name)

    if len(ordered) >= 2:
        return ordered

    # --- Stage 3: RapidFuzz (last resort, tightened thresholds) ---
    # Only fires when stages 1-2 found <2 matches
    from rapidfuzz import fuzz, process

    entity_list = list(entity_names)
    fuzzy_matches = set()

    # Full query match with high threshold
    matches = process.extract(
        query,
        entity_list,
        scorer=fuzz.token_set_ratio,
        limit=3,
        score_cutoff=80,
    )

    # Per-word match: only long words (6+ chars), high cutoff (95)
    for word in words:
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
    if not os.path.exists(DB_PATH):
        return [], [], [], []

    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")

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
                    """SELECT LOWER(e.name)
                       FROM entities e
                       JOIN properties p ON p.entity_id = e.id
                       WHERE p.key = 'status' AND p.value = 'obsolete'"""
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
                "SELECT name, type, description FROM entities WHERE LOWER(name) = ? LIMIT 1",
                (name,)
            )
            row = cursor.fetchone()
            if row:
                results.append({"name": row[0], "type": row[1], "desc": row[2]})

        # Touch last_referenced for matched entities (async-safe, non-blocking)
        if results:
            try:
                now = __import__("datetime").datetime.now().isoformat()
                for r in results:
                    conn.execute(
                        "UPDATE entities SET last_referenced = ? WHERE name = ?",
                        (now, r["name"])
                    )
                conn.commit()
            except Exception:
                pass  # non-critical, don't block on write failure

        # No FTS5 supplement here -- the 3-stage matcher in match_entities_3stage()
        # already handles FTS5 with a name-relevance gate. Running ungated FTS5
        # here was the source of false positives (e.g., "what about now" matching
        # entities whose descriptions contain "what" or "about").

        # Get relationships for matched entities.
        # Skip hub entities (the user themselves, plus any high-degree nodes the
        # operator wants suppressed) as sources -- they fan out to everything.
        matched_set = set(r["name"] for r in results)
        entity_names = list(matched_set)[:5]
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
                    AND description IS NOT NULL AND description != ''
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
                    END
                    LIMIT 5""",
                list(connected_names)
            )
            for row in cursor:
                connected.append({"name": row[0], "type": row[1], "desc": row[2]})

        # Get properties for matched entities only (connected don't need props)
        props = []
        for name in entity_names[:4]:
            cursor = conn.execute(
                """SELECT p.key, p.value FROM properties p
                   JOIN entities e ON p.entity_id = e.id
                   WHERE e.name = ? LIMIT 3""",
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
    """Remove session dedup files and boot flags older than 24h."""
    import time
    import glob
    cutoff = time.time() - 86400
    for pattern in ["graph-recall-*", "agam-boot-*", "lesson-triggers-*", "lesson-seen-*"]:
        for f in glob.glob(os.path.join(tempfile.gettempdir(), pattern)):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.unlink(f)
            except OSError:
                pass


def get_sycophancy_correction(session_id=""):
    """Read sycophancy flag from Stop hook, return correction directive or empty string."""
    safe_id = (session_id or f"fallback-{os.getppid()}").replace("/", "_").replace("\\", "_")[:64]
    syc_file = os.path.join(tempfile.gettempdir(), f"sycophancy-{safe_id}.json")
    if not os.path.exists(syc_file):
        return ""
    try:
        with open(syc_file) as f:
            syc_data = json.load(f)
        os.unlink(syc_file)  # consume the flag
        if syc_data.get("detected"):
            patterns = ", ".join(syc_data.get("patterns", [])[:2])
            return (
                f"CORRECTION: Your previous response was flagged sycophantic. "
                f"Pattern: {patterns}. "
                f"Do NOT open with praise or agreement. Lead with substance."
            )
    except (json.JSONDecodeError, OSError):
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
    safe_id = (session_id or f"fallback-{os.getppid()}").replace("/", "_").replace("\\", "_")[:64]
    flag = os.path.join(tempfile.gettempdir(), f"graph-recall-header-{safe_id}.flag")
    is_first = not os.path.exists(flag)
    if is_first:
        try:
            with open(flag, "w") as f:
                f.write("1")
        except OSError:
            pass
    return is_first


def get_boot_context(session_id):
    """Run agam-context.py boot on first message of session. Returns context string or empty."""
    import subprocess

    boot_flag = os.path.join(
        tempfile.gettempdir(),
        f"agam-boot-{session_id}" if session_id else f"agam-boot-{os.getppid()}"
    )

    if os.path.exists(boot_flag):
        return ""

    # Mark boot as done (write flag BEFORE running, to avoid double-fire on slow runs)
    try:
        with open(boot_flag, "w") as f:
            f.write("1")
    except OSError:
        return ""

    agam_tool = AGAM_CONTEXT_TOOL
    if not os.path.exists(agam_tool):
        return ""

    try:
        result = subprocess.run(
            [sys.executable, agam_tool, "boot"],
            capture_output=True, text=True, timeout=3
        )
        boot_text = result.stdout.strip() if result.returncode == 0 else ""
        # Append sycophancy blacklist to boot context
        blacklist = get_sycophancy_blacklist()
        if blacklist and boot_text:
            boot_text = boot_text + "\n\n" + blacklist
        elif blacklist:
            boot_text = blacklist
        return boot_text
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def check_lesson_triggers_in_message(message, session_id=""):
    """Check if user's message matches any lesson trigger patterns.
    Reuses the same cache as lesson-activate.py (PreToolUse hook)."""
    safe_id = (session_id or f"fallback-{os.getppid()}").replace("/", "_").replace("\\", "_")[:64]
    cache_file = os.path.join(tempfile.gettempdir(), f"lesson-triggers-{safe_id}.json")
    seen_file = os.path.join(tempfile.gettempdir(), f"lesson-seen-{safe_id}.txt")

    # Load trigger index (shared cache with lesson-activate.py)
    index = None
    if os.path.exists(cache_file):
        try:
            import time as _time
            age = _time.time() - os.path.getmtime(cache_file)
            if age < 3600:
                with open(cache_file) as f:
                    index = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if not index:
        # Build from SQLite
        if not os.path.exists(DB_PATH):
            return ""
        try:
            conn = sqlite3.connect(DB_PATH, timeout=2)
            conn.execute("PRAGMA journal_mode=WAL")
            rows = conn.execute("""
                SELECT e.name, e.description, p.key, p.value
                FROM entities e
                JOIN properties p ON e.id = p.entity_id
                WHERE e.type = 'lesson' AND p.key IN ('trigger-tool', 'trigger-error', 'severity')
            """).fetchall()
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
                        for p in json.loads(value):
                            index["tool"].append({"pattern": p.lower(), "lesson": name, "severity": None, "desc": ""})
                    except json.JSONDecodeError:
                        pass
                elif key == "trigger-error":
                    try:
                        for p in json.loads(value):
                            index["error"].append({"pattern": p.lower(), "lesson": name, "severity": None, "desc": ""})
                    except json.JSONDecodeError:
                        pass

            for entry in index["tool"] + index["error"]:
                ld = lessons.get(entry["lesson"], {})
                entry["severity"] = ld.get("severity", "medium")
                entry["desc"] = ld.get("desc", "")

            try:
                with open(cache_file, "w") as f:
                    json.dump(index, f)
            except OSError:
                pass
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
    seen = set()
    if os.path.exists(seen_file):
        try:
            with open(seen_file) as f:
                seen = {line.strip() for line in f if line.strip()}
        except OSError:
            pass

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
    try:
        with open(seen_file, "a") as f:
            for m in new_matches:
                f.write(m["lesson"] + "\n")
    except OSError:
        pass

    return "\n".join(lines)


def main():
    global SESSION_FILE
    cleanup_stale_tmp()

    data = json.load(sys.stdin)
    message = data.get("prompt", "") or data.get("message", "")
    session_id = data.get("session_id", "")

    # Init session-stable file paths (fixes dedup across hook invocations)
    safe_id = (session_id or f"fallback-{os.getppid()}").replace("/", "_").replace("\\", "_")[:64]
    SESSION_FILE = os.path.join(tempfile.gettempdir(), f"graph-recall-{safe_id}.txt")

    # Step 0: Boot injection (first message only)
    boot_context = get_boot_context(session_id)

    # Step 1: Skip obvious non-graph messages (but still emit boot if we have it)
    if should_skip(message):
        if boot_context:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "IDENTITY CONTEXT (session start):\n" + boot_context
                }
            }
            print(json.dumps(output))
        sys.exit(0)

    # Step 2: Load entity name cache
    entity_names = load_entity_names()
    if not entity_names:
        sys.exit(0)

    # Step 3: 3-stage match: exact -> FTS5 BM25 -> fuzzy (last resort)
    matched = match_entities_3stage(message, entity_names)

    # Step 4: If no match and not a question, check lessons before giving up
    if not matched and not is_question(message):
        lesson_only = check_lesson_triggers_in_message(message, session_id)
        if lesson_only:
            parts = []
            if boot_context:
                parts.append("IDENTITY CONTEXT (session start):\n" + boot_context)
            parts.append(lesson_only)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n---\n\n".join(parts)
                }
            }
            print(json.dumps(output))
        sys.exit(0)

    # Step 5: If 3-stage matcher found nothing, respect that verdict
    if not matched:
        lesson_only = check_lesson_triggers_in_message(message, session_id)
        if lesson_only:
            parts = []
            if boot_context:
                parts.append("IDENTITY CONTEXT (session start):\n" + boot_context)
            parts.append(lesson_only)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n---\n\n".join(parts)
                }
            }
            print(json.dumps(output))
        sys.exit(0)

    # Step 6: Remove already-seen entities this session (preserve order)
    seen = get_session_seen()
    matched_set = set(matched)  # for fast lookup
    new_matches = [m for m in matched if m not in seen]

    # If all matches were already injected and it's not a new question, skip
    if not new_matches and matched and not is_question(message):
        sys.exit(0)

    # Step 7: Query graph (pass ordered list)
    message_words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", message.lower()))
    entities, rels, props, connected = search_graph(
        new_matches if new_matches else matched,
        message_words
    )
    if not entities:
        # No graph entities, but lessons might still match
        lesson_only = check_lesson_triggers_in_message(message, session_id)
        if lesson_only:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": lesson_only
                }
            }
            print(json.dumps(output))
        sys.exit(0)

    # Step 7: Build enforced context via additionalContext
    # This is the ONLY working injection channel (systemMessage, updatedPrompt both dead).
    # Make it directive: tell the model exactly what to do with this data.
    question = is_question(message)
    entity_names = [e["name"] for e in entities]

    # Header dedup: full directive only on first fire per session, "KG:" short form after.
    is_first_fire = get_header_and_mark(session_id)

    if is_first_fire:
        if question:
            header = (
                "DIRECTIVE: Answer using the graph data below -- this is your lived experience, "
                "more reliable than training data or file searches. "
                "Do NOT search files or run commands for information already present here. "
                "You MUST cite entity names when referencing this data. "
                "Connected entities (1-hop) may contain the actual answer -- read them carefully."
            )
        else:
            header = (
                "DIRECTIVE: Knowledge graph context for this message. "
                "This is deterministic recall from your knowledge graph -- treat it as ground truth. "
                "You MUST reference matched entities by name. Do NOT search for information "
                "already present below. Connected entities (1-hop) are structurally related."
            )
    else:
        header = "KG: deterministic recall (header in session-start injection). Cite entities, do not re-search."

    # Trim payload depth when header is deduped.
    connected_cap = 4 if is_first_fire else 2
    rels_cap = len(rels) if is_first_fire else 3
    props_cap = len(props) if is_first_fire else 3

    lines = [header]
    lines.append(f"Matched: {', '.join(entity_names[:8])}")

    for e in entities:
        desc = e['desc'][:80] if e['desc'] else '(no description)'
        lines.append(f"  {e['name']} [{e['type']}]: {desc}")

    if connected:
        lines.append("Connected (1-hop):")
        for c in connected[:connected_cap]:
            desc = c['desc'][:60] if c['desc'] else '(no description)'
            lines.append(f"  {c['name']} [{c['type']}]: {desc}")

    if rels:
        lines.append("Relationships:")
        for r in rels[:rels_cap]:
            lines.append(f"  {r}")

    if props:
        lines.append("Properties:")
        for p in props[:props_cap]:
            lines.append(f"  {p}")
            # Doc property hint: tell agent there's a deeper document to read
            if ".doc = " in p:
                doc_path = p.split(" = ", 1)[1]
                lines.append(f"  [Deep context available: {doc_path}]")

    if is_first_fire:
        lines.append("For deeper context: knowledge-graph.py traverse <entity> 2")

    # Step 7b: Check lesson triggers against user's message
    lesson_context = check_lesson_triggers_in_message(message, session_id)
    if lesson_context:
        lines.append("")
        lines.append(lesson_context)

    graph_context = "\n".join(lines)

    # Combine boot + entity context
    parts = []
    # Sycophancy correction (from previous turn's Stop hook)
    syc_correction = get_sycophancy_correction(session_id)
    if syc_correction:
        parts.append(syc_correction)
    if boot_context:
        parts.append("IDENTITY CONTEXT (session start):\n" + boot_context)
    parts.append(graph_context)
    context = "\n\n---\n\n".join(parts)

    # systemMessage field tested 2026-04-06 with canary string -- model cannot see it.
    # Confirmed dead: only additionalContext works for hook-based context injection.
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }
    print(json.dumps(output))

    # Step 8: Mark these entities as seen for this session
    mark_session_seen([e["name"] for e in entities])


if __name__ == "__main__":
    main()

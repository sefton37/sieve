"""Database layer for Sieve - SQLite operations for articles and settings."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import sqlite_vec

DATABASE_PATH = Path("/home/kellogg/data/sieve.db")

# Default settings
DEFAULT_SETTINGS = {
    "ollama_model": "llama3.2",
    "ollama_num_ctx": "4096",
    "ollama_temperature": "0.3",
    "ollama_system_prompt": "Summarize the following news article in one paragraph (5-8 sentences). Cover the key facts, context, and implications. Write the summary directly without any preamble or meta-commentary.",
    "jsonl_path": "/home/kellogg/data/rssfeed.jsonl",
    "ingest_schedule": "",
    "auto_ingest": "false",
    "ollama_embed_model": "nomic-embed-text",
    "auto_digest": "false",
    "digest_schedule": "0 6 * * *",
}


def init_db():
    """Create tables if they don't exist, insert default settings."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        cursor = conn.cursor()

        # Articles table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT UNIQUE NOT NULL,
                source TEXT,
                pub_date TEXT,
                pulled_at TEXT,
                content TEXT,
                summary TEXT,
                summarized_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Add columns if they don't exist (migration for existing DBs)
        for column_def in [
            "keywords TEXT",
            "embedding BLOB",
            "embedded_at TEXT",
            "d1_attention_economy INTEGER",
            "d2_data_sovereignty INTEGER",
            "d3_power_consolidation INTEGER",
            "d4_coercion_cooperation INTEGER",
            "d5_fear_trust INTEGER",
            "d6_democratization INTEGER",
            "d7_systemic_design INTEGER",
            "composite_score INTEGER",
            "relevance_tier INTEGER",
            "convergence_flag INTEGER",
            "relevance_rationale TEXT",
            "scored_at TEXT",
        ]:
            try:
                cursor.execute(f"ALTER TABLE articles ADD COLUMN {column_def}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Chat messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Digests table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_date TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                article_count INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_pub_date ON articles(pub_date)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_summary ON articles(summary)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_embedded_at ON articles(embedded_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_composite_score ON articles(composite_score)
        """)

        # Create vector search virtual table using sqlite-vec
        # We use vec0 which supports float[768] format
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_articles USING vec0(
                article_id INTEGER PRIMARY KEY,
                embedding float[768]
            )
        """)

        # Insert default settings if not present
        for key, value in DEFAULT_SETTINGS.items():
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )

        conn.commit()


@contextmanager
def get_db():
    """Return database connection as context manager."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    # Enable loading extensions and load sqlite-vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        yield conn
    finally:
        conn.close()


def get_setting(key):
    """Get a single setting value by key."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    """Set a single setting value."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        conn.commit()


def get_all_settings():
    """Return all settings as a dictionary."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in cursor.fetchall()}


def article_exists(url):
    """Check if an article with this URL already exists."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM articles WHERE url = ?", (url,))
        return cursor.fetchone() is not None


def insert_article(article_dict):
    """Insert a single article. Returns the new article ID or None if duplicate."""
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO articles (title, url, source, pub_date, pulled_at, content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                article_dict.get("title"),
                article_dict.get("url"),
                article_dict.get("source"),
                article_dict.get("pub_date"),
                article_dict.get("pulled_at"),
                article_dict.get("content"),
            ))
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Duplicate URL
            return None


def get_articles(filters=None, page=1, per_page=20, sort="date_desc"):
    """
    Get paginated list of articles with optional filters.

    filters can include:
        - source: filter by source name
        - has_summary: True/False to filter by summary presence
        - date_from: ISO date string for start date
        - date_to: ISO date string for end date
        - search: text search in title
        - keyword: filter by keyword (partial match)
        - tier: integer 1-5 to filter by relevance tier
        - score_min: minimum composite score (0-21)
        - score_max: maximum composite score (0-21)
        - has_score: True/False to filter by score presence

    sort can be:
        - "date_desc" (default): newest first
        - "date_asc": oldest first
        - "score_desc": highest composite score first
        - "score_asc": lowest composite score first

    Returns: (list of article dicts, total count)
    """
    filters = filters or {}

    where_clauses = []
    params = []

    if filters.get("source"):
        where_clauses.append("source = ?")
        params.append(filters["source"])

    if filters.get("has_summary") is True:
        where_clauses.append("summary IS NOT NULL")
    elif filters.get("has_summary") is False:
        where_clauses.append("summary IS NULL")

    if filters.get("date_from"):
        where_clauses.append("pub_date >= ?")
        params.append(filters["date_from"])

    if filters.get("date_to"):
        where_clauses.append("pub_date <= ?")
        params.append(filters["date_to"])

    if filters.get("search"):
        where_clauses.append("title LIKE ?")
        params.append(f"%{filters['search']}%")

    if filters.get("keyword"):
        where_clauses.append("keywords LIKE ?")
        params.append(f"%{filters['keyword']}%")

    if filters.get("tier") is not None:
        where_clauses.append("relevance_tier = ?")
        params.append(filters["tier"])

    if filters.get("score_min") is not None:
        where_clauses.append("composite_score >= ?")
        params.append(filters["score_min"])

    if filters.get("score_max") is not None:
        where_clauses.append("composite_score <= ?")
        params.append(filters["score_max"])

    if filters.get("has_score") is True:
        where_clauses.append("scored_at IS NOT NULL")
    elif filters.get("has_score") is False:
        where_clauses.append("scored_at IS NULL")

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    # Determine sort order
    sort_options = {
        "date_desc": "pub_date DESC",
        "date_asc": "pub_date ASC",
        "score_desc": "composite_score DESC NULLS LAST, pub_date DESC",
        "score_asc": "composite_score ASC NULLS LAST, pub_date DESC",
    }
    order_sql = sort_options.get(sort, "pub_date DESC")

    with get_db() as conn:
        cursor = conn.cursor()

        # Get total count
        cursor.execute(f"SELECT COUNT(*) FROM articles WHERE {where_sql}", params)
        total = cursor.fetchone()[0]

        # Get paginated results
        offset = (page - 1) * per_page
        cursor.execute(f"""
            SELECT id, title, url, source, pub_date, pulled_at, content, summary,
                   keywords, summarized_at, created_at, composite_score, relevance_tier,
                   convergence_flag
            FROM articles
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """, params + [per_page, offset])

        articles = [dict(row) for row in cursor.fetchall()]

        return articles, total


def get_article(article_id):
    """Get a single article by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, url, source, pub_date, pulled_at, content, summary, keywords, summarized_at, created_at
            FROM articles
            WHERE id = ?
        """, (article_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_unsummarized_articles():
    """Get all articles where summary is NULL."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, url, source, pub_date, content
            FROM articles
            WHERE summary IS NULL
            ORDER BY pub_date DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def update_summary(article_id, summary, keywords=None):
    """Set summary, keywords, and summarized_at timestamp for an article."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Store keywords as comma-separated string
        keywords_str = ",".join(keywords) if keywords else None
        cursor.execute("""
            UPDATE articles
            SET summary = ?, keywords = ?, summarized_at = ?
            WHERE id = ?
        """, (summary, keywords_str, datetime.utcnow().isoformat(), article_id))
        conn.commit()
        return cursor.rowcount > 0


def get_article_count():
    """Get total number of articles."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM articles")
        return cursor.fetchone()[0]


def get_summarized_count():
    """Get number of articles with summaries."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM articles WHERE summary IS NOT NULL")
        return cursor.fetchone()[0]


def get_sources():
    """Get list of unique sources."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT source FROM articles WHERE source IS NOT NULL ORDER BY source")
        return [row["source"] for row in cursor.fetchall()]


def get_keywords():
    """Get list of unique keywords from all articles."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT keywords FROM articles WHERE keywords IS NOT NULL")

        # Collect all keywords and count occurrences
        keyword_counts = {}
        for row in cursor.fetchall():
            if row["keywords"]:
                for kw in row["keywords"].split(","):
                    kw = kw.strip()
                    if kw:
                        keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

        # Sort by count (descending), then alphabetically
        sorted_keywords = sorted(keyword_counts.keys(), key=lambda k: (-keyword_counts[k], k.lower()))
        return sorted_keywords


# ============================================================================
# Embedding functions
# ============================================================================

def get_unembedded_articles():
    """Get all articles with summary but no embedding."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, summary
            FROM articles
            WHERE summary IS NOT NULL AND embedded_at IS NULL
            ORDER BY pub_date DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def get_embedded_count():
    """Get number of articles with embeddings."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM articles WHERE embedded_at IS NOT NULL")
        return cursor.fetchone()[0]


# ============================================================================
# Relevance scoring functions
# ============================================================================

def get_unscored_articles():
    """Get all articles with summary but no relevance score."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content, summary, keywords
            FROM articles
            WHERE summary IS NOT NULL AND scored_at IS NULL
            ORDER BY pub_date DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def update_relevance_scores(article_id, scores, composite, tier, convergence, rationale):
    """Store relevance scores for an article.

    Args:
        article_id: Article ID
        scores: dict with keys D1-D7 (e.g. {"d1_attention_economy": 2, ...})
        composite: 0-21 sum of all dimensions
        tier: 1-5 priority tier
        convergence: 0 or 1
        rationale: 1-2 sentence explanation
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE articles
            SET d1_attention_economy = ?, d2_data_sovereignty = ?,
                d3_power_consolidation = ?, d4_coercion_cooperation = ?,
                d5_fear_trust = ?, d6_democratization = ?,
                d7_systemic_design = ?,
                composite_score = ?, relevance_tier = ?,
                convergence_flag = ?, relevance_rationale = ?, scored_at = ?
            WHERE id = ?
        """, (
            scores.get("d1_attention_economy", 0),
            scores.get("d2_data_sovereignty", 0),
            scores.get("d3_power_consolidation", 0),
            scores.get("d4_coercion_cooperation", 0),
            scores.get("d5_fear_trust", 0),
            scores.get("d6_democratization", 0),
            scores.get("d7_systemic_design", 0),
            composite, tier, convergence, rationale,
            datetime.utcnow().isoformat(), article_id,
        ))
        conn.commit()
        return cursor.rowcount > 0


def get_scored_count():
    """Get number of articles with relevance scores."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM articles WHERE scored_at IS NOT NULL")
        return cursor.fetchone()[0]


def get_score_distribution():
    """Get all scoring data for distribution analysis.

    Returns dict with:
        - composite_scores: list of all composite scores
        - dimension_scores: dict of dimension_name -> list of scores
        - convergence_count: number of articles with convergence flag
        - total: total scored articles
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT composite_score, convergence_flag,
                   d1_attention_economy, d2_data_sovereignty,
                   d3_power_consolidation, d4_coercion_cooperation,
                   d5_fear_trust, d6_democratization, d7_systemic_design
            FROM articles
            WHERE scored_at IS NOT NULL
            ORDER BY composite_score DESC
        """)

        rows = [dict(row) for row in cursor.fetchall()]

        composites = [r["composite_score"] for r in rows]
        convergence = sum(1 for r in rows if r["convergence_flag"])

        dims = {}
        for key in [
            "d1_attention_economy", "d2_data_sovereignty",
            "d3_power_consolidation", "d4_coercion_cooperation",
            "d5_fear_trust", "d6_democratization", "d7_systemic_design",
        ]:
            dims[key] = [r[key] for r in rows if r[key] is not None]

        return {
            "composite_scores": composites,
            "dimension_scores": dims,
            "convergence_count": convergence,
            "total": len(rows),
        }


def update_embedding(article_id, embedding_blob):
    """Store embedding and update vec_articles table for an article."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Update articles table with embedding blob and timestamp
        cursor.execute("""
            UPDATE articles
            SET embedding = ?, embedded_at = ?
            WHERE id = ?
        """, (embedding_blob, datetime.utcnow().isoformat(), article_id))

        # Insert/replace in vec_articles virtual table for vector search
        # Delete existing entry if present
        cursor.execute("DELETE FROM vec_articles WHERE article_id = ?", (article_id,))

        # Insert new entry
        cursor.execute("""
            INSERT INTO vec_articles (article_id, embedding)
            VALUES (?, ?)
        """, (article_id, embedding_blob))

        conn.commit()
        return cursor.rowcount > 0


def search_by_embedding(query_embedding_blob, limit=5):
    """
    Find similar articles using vector similarity search.

    Args:
        query_embedding_blob: Binary blob of query embedding
        limit: Number of results to return

    Returns:
        List of article dicts with similarity scores
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Use sqlite-vec's KNN search
        cursor.execute("""
            SELECT
                a.id, a.title, a.url, a.source, a.pub_date, a.summary, a.keywords,
                v.distance
            FROM vec_articles v
            JOIN articles a ON v.article_id = a.id
            WHERE v.embedding MATCH ?
                AND k = ?
            ORDER BY v.distance
        """, (query_embedding_blob, limit))

        results = []
        for row in cursor.fetchall():
            article = dict(row)
            # Convert distance to similarity (lower distance = higher similarity)
            article['similarity'] = 1.0 / (1.0 + article['distance'])
            results.append(article)

        return results


# ============================================================================
# Chat functions
# ============================================================================

def save_chat_message(role, content, sources=None):
    """Save a chat message to the database."""
    import json

    with get_db() as conn:
        cursor = conn.cursor()
        sources_json = json.dumps(sources) if sources else None
        cursor.execute("""
            INSERT INTO chat_messages (role, content, sources)
            VALUES (?, ?, ?)
        """, (role, content, sources_json))
        conn.commit()
        return cursor.lastrowid


def get_chat_history(limit=20):
    """Get recent chat history."""
    import json

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, role, content, sources, created_at
            FROM chat_messages
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))

        messages = []
        for row in cursor.fetchall():
            msg = dict(row)
            if msg['sources']:
                msg['sources'] = json.loads(msg['sources'])
            messages.append(msg)

        # Return in chronological order (oldest first)
        return list(reversed(messages))


def clear_chat_history():
    """Delete all chat messages."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_messages")
        conn.commit()
        return cursor.rowcount


# ============================================================================
# Digest functions
# ============================================================================

def get_articles_since(since_datetime):
    """Get articles published since a given datetime."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, url, source, pub_date, summary, keywords, content
            FROM articles
            WHERE summary IS NOT NULL
                AND pub_date >= ?
            ORDER BY pub_date DESC
        """, (since_datetime.isoformat(),))
        return [dict(row) for row in cursor.fetchall()]


def save_digest(digest_date, content, article_count):
    """Save a daily digest."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO digests (digest_date, content, article_count, created_at)
            VALUES (?, ?, ?, ?)
        """, (digest_date, content, article_count, datetime.utcnow().isoformat()))
        conn.commit()
        return cursor.lastrowid


def get_digest(digest_date):
    """Get a digest by date."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, digest_date, content, article_count, created_at
            FROM digests
            WHERE digest_date = ?
        """, (digest_date,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_recent_digests(limit=7):
    """Get recent digests."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, digest_date, content, article_count, created_at
            FROM digests
            ORDER BY digest_date DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]


def get_articles_by_ids(article_ids):
    """Get multiple articles by their IDs."""
    if not article_ids:
        return []

    with get_db() as conn:
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(article_ids))
        cursor.execute(f"""
            SELECT id, title, url, source, pub_date, summary, keywords
            FROM articles
            WHERE id IN ({placeholders})
        """, article_ids)
        return [dict(row) for row in cursor.fetchall()]

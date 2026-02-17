# Sieve

A local-first news intelligence tool that ingests RSS articles, summarizes (with related article context), embeds, scores, extracts entities, classifies topics, and detects story threads with Ollama, and provides a web interface for browsing, filtering, RAG-based chat, score-aware daily digests, and score distribution analytics.

## Overview

Sieve takes a JSONL feed of articles (from n8n), deduplicates and stores them in SQLite, generates AI summaries (with related article context), embeddings, 7-dimension relevance scores, entity extractions, and topic classifications via Ollama, detects story threads algorithmically, and serves a web UI for browsing, chatting with the corpus, and reading score-prioritized daily digests.

```
[n8n JSONL export] → [Sieve Pipeline] → [SQLite + sqlite-vec] → [Ollama Summarize + Embed + Score] → [Web UI]
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              SIEVE                                       │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   /home/kellogg/data/rssfeed.jsonl                                       │
│              │                                                           │
│              ▼                                                           │
│   ┌──────────────────────────────────────────────────────┐               │
│   │              Pipeline Orchestrator (8 stages)         │               │
│   │   1. Ingest    - Parse JSONL, dedupe by URL          │               │
│   │   2. Compress  - Deduplicate source JSONL            │               │
│   │   3. Summarize - Batch summarize via Ollama (+ctx)   │               │
│   │   4. Embed     - Batch embed via Ollama              │               │
│   │   5. Score     - 7-dimension relevance scoring       │               │
│   │   6. Entities  - Named entity extraction             │               │
│   │   7. Topics    - Topic classification                │               │
│   │   8. Threads   - Story thread detection              │               │
│   └──────────────────┬───────────────────────────────────┘               │
│                      │                                                   │
│                      ▼                                                   │
│   ┌──────────────────┐      ┌──────────────────────────┐                 │
│   │   SQLite DB      │      │   Ollama API             │                 │
│   │  + sqlite-vec    │◄────►│   localhost:11434        │                 │
│   │                  │      │   - /api/generate        │                 │
│   │  - articles      │      │   - /api/embed           │                 │
│   │  - vec_articles  │      └──────────────────────────┘                 │
│   │  - threads       │                                                   │
│   │  - article_threads│                                                  │
│   │  - settings      │                                                   │
│   │  - chat_messages │                                                   │
│   │  - digests       │                                                   │
│   └──────────────────┘                                                   │
│            │                                                             │
│            ▼                                                             │
│   ┌──────────────────────────────────────────────────────┐               │
│   │    Web UI (Flask + HTMX)      localhost:5000         │               │
│   │   - Browse & filter articles (by score, tier, etc.)  │               │
│   │   - Chat with corpus (RAG)                           │               │
│   │   - Score-aware daily digests                        │               │
│   │   - Score distribution dashboard                     │               │
│   │   - Settings & job management                        │               │
│   └──────────────────────────────────────────────────────┘               │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Backend | Python + Flask | Simple, stable, well-documented |
| Database | SQLite + sqlite-vec | Local, zero config, portable, vector search built in |
| Frontend | HTML + HTMX | No build step, progressive enhancement |
| Styling | Pico CSS | Classless, minimal, looks good by default |
| AI | Ollama HTTP API | Local, simple REST calls |
| Scheduling | APScheduler | Python-native, no external deps |

## Database Schema

```sql
-- Articles table
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    source TEXT,
    pub_date TEXT,
    pulled_at TEXT,
    content TEXT,
    summary TEXT,
    keywords TEXT,                -- comma-separated keywords from LLM
    summarized_at TEXT,
    embedding BLOB,              -- 768-dim float vector (struct-packed)
    embedded_at TEXT,
    -- Relevance scoring (7 dimensions, 0-3 each)
    d1_attention_economy INTEGER,
    d2_data_sovereignty INTEGER,
    d3_power_consolidation INTEGER,
    d4_coercion_cooperation INTEGER,
    d5_fear_trust INTEGER,
    d6_democratization INTEGER,
    d7_systemic_design INTEGER,
    composite_score INTEGER,     -- 0-21 sum of all dimensions
    relevance_tier INTEGER,      -- 1-5 priority tier
    convergence_flag INTEGER,    -- 1 if 5+ dimensions scored 2+
    relevance_rationale TEXT,    -- LLM explanation of scoring
    scored_at TEXT,
    -- Phase 3: Structure
    context_article_ids TEXT,    -- JSON array of IDs used as context during summarization
    entities TEXT,               -- JSON: {"companies":[], "people":[], "products":[], "legislation":[], "other":[]}
    entities_extracted_at TEXT,
    topics TEXT,                 -- comma-separated from fixed taxonomy
    topics_classified_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Vector search virtual table (sqlite-vec)
CREATE VIRTUAL TABLE vec_articles USING vec0(
    article_id INTEGER PRIMARY KEY,
    embedding float[768]
);

-- Settings table (key-value store)
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Chat messages table
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,           -- "user" or "assistant"
    content TEXT NOT NULL,
    sources TEXT,                 -- JSON array of article IDs
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Threads table
CREATE TABLE threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    primary_entities TEXT,       -- JSON array of top entity names
    article_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Article-thread junction table
CREATE TABLE article_threads (
    article_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (article_id, thread_id),
    FOREIGN KEY (article_id) REFERENCES articles(id),
    FOREIGN KEY (thread_id) REFERENCES threads(id)
);

-- Digests table
CREATE TABLE digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,        -- Markdown digest content
    article_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Default settings
INSERT INTO settings (key, value) VALUES
    ('ollama_model', 'llama3.2'),
    ('ollama_num_ctx', '4096'),
    ('ollama_temperature', '0.3'),
    ('ollama_system_prompt', 'Summarize the following news article...'),
    ('ollama_embed_model', 'nomic-embed-text'),
    ('jsonl_path', '/home/kellogg/data/rssfeed.jsonl'),
    ('ingest_schedule', ''),
    ('auto_ingest', 'false'),
    ('auto_digest', 'true'),
    ('digest_schedule', '0 20 * * *');
```

## File Structure

```
Sieve/
├── app.py              # Flask app, routes, job management
├── db.py               # SQLite operations, schema, migrations
├── ingest.py           # JSONL parsing and URL deduplication
├── summarize.py        # Ollama summarization with keyword extraction
├── embed.py            # Ollama embedding (nomic-embed-text, 768-dim)
├── score.py            # 7-dimension relevance scoring via Ollama
├── entities.py         # Named entity extraction via Ollama (5 categories)
├── topics.py           # Topic classification via Ollama (17-topic taxonomy)
├── threads.py          # Algorithmic story thread detection (embedding + entity overlap)
├── pipeline.py         # Orchestrator: ingest → compress → summarize → embed → score → entities → topics → threads
├── scheduler.py        # APScheduler: hourly pipeline, daily digest
├── chat.py             # RAG chat: embed query → vector search → generate
├── digest.py           # Score-aware daily digest generation in Abend voice
├── no_one_relevancy_rubric.md  # Scoring rubric (7 dimensions, tiers, convergence)
├── sieve.service       # SystemD service file for deployment
├── templates/
│   ├── base.html       # Layout with nav (Browse, Chat, Digest, Scores, Settings)
│   ├── index.html      # Article browser with filters (incl. tier, sort by score)
│   ├── article.html    # Single article view
│   ├── settings.html   # Config, stats, job triggers (incl. score)
│   ├── chat.html       # RAG chat interface
│   ├── digest.html     # Daily digest viewer
│   ├── scores.html     # Score distribution dashboard
│   └── partials/
│       ├── article_list.html      # Paginated article grid with score badges
│       ├── summary_section.html   # Summary + keywords display
│       ├── job_status.html        # Job progress/status
│       ├── chat_response.html     # Chat message rendering
│       └── stats.html             # Live statistics (incl. scored count)
├── static/
│   └── style.css       # Custom overrides for Pico CSS
├── requirements.txt
├── README.md
├── ARCHITECTURE.md
└── LICENSE
```

## UI Pages

### 1. Browse (`/`)
- Paginated article grid
- Filter by: source, keyword, summary status, date range, text search, relevance tier, topic, entity
- Sort by: date (newest/oldest), score (highest/lowest)
- Shows: title, source, date, summary preview, keyword tags, topic tags, color-coded tier badge with score, convergence flag
- Click to view full article

### 2. Article (`/article/<id>`)
- Full article content
- Summary with keywords (or "not yet summarized")
- Relevance scoring: composite score, tier badge, convergence flag, per-dimension scores, rationale (if scored)
- Topics: classified topic tags
- Entities: categorized entity tags (companies, people, products, legislation, other)
- Story threads: linked thread names with article counts
- Button: "Regenerate summary"
- Metadata: source, dates, link to original

### 3. Chat (`/chat`)
- RAG-based question answering over the article corpus
- Embeds query → vector search for top-5 similar articles → generates response
- Abend voice (analytical, observational perspective)
- Markdown rendering, source article links
- Clear history button

### 4. Digest (`/digest`)
- Score-aware AI-generated daily briefings in Abend voice
- Articles grouped by tier with proportional depth: T1 gets deep dives, T2 gets substantive coverage, T3 feeds pattern sections, T4 mentioned in passing, T5 excluded
- Dimensional profile shows which analytical themes dominate the day
- Convergence points highlighted for cross-dimensional intersection stories
- Generate on demand or via scheduled job (default: 20:00 UTC)
- Review-and-revise loop validates quotes and attribution, auto-corrects up to 3 times
- Backfills missing days and regenerates stale digests automatically
- Markdown rendering with source links
- Archive of recent digests (last 14)

### 5. Scores (`/scores`)
- Composite score histogram (0-21 distribution)
- Statistics: mean, median, standard deviation
- Tier distribution table with colored bars and percentages
- Per-dimension averages (0-3 scale) showing which dimensions the corpus scores highest on
- Convergence count and percentage

### 6. Settings (`/settings`)
- **Live stats** - Article count, summarized, embedded, scored, entities extracted, topics classified (auto-refreshing)
- **Job management** - Hourly pipeline trigger, individual action buttons (ingest, summarize, embed, score, extract entities, classify topics, detect threads, re-summarize with context), job progress display
- **Ollama config** - Model dropdown (from installed models), context window slider, temperature
- **Ingestion** - JSONL path, auto-ingest toggle, cron schedule

## API Endpoints

```
GET  /                        # Browse articles (with filter/pagination/sort params)
GET  /article/<id>            # Single article view
POST /article/<id>/summarize  # Regenerate one summary

GET  /settings                # Settings page
POST /settings                # Update settings

POST /ingest                  # Trigger JSONL ingestion
POST /summarize               # Trigger batch summarization
POST /embed                   # Trigger batch embedding
POST /score                   # Trigger batch relevance scoring
POST /entities                # Trigger batch entity extraction
POST /topics                  # Trigger batch topic classification
POST /threads                 # Trigger thread detection
POST /resummarize             # Trigger context re-summarization (backfill)
POST /pipeline                # Trigger full pipeline (8 stages)
GET  /status                  # Job status (HTMX partial or JSON)
GET  /stats                   # Live statistics (HTMX partial)

GET  /scores                  # Score distribution dashboard

GET  /chat                    # Chat interface
POST /chat                    # Send message (RAG query)
POST /chat/clear              # Clear chat history

GET  /digest                  # Digest viewer
POST /digest/generate         # Generate daily digest
```

## Ollama Integration

### Summarization (`/api/generate`)

Sends articles to Ollama with a structured prompt requesting both a summary paragraph (5-8 sentences) and 3-5 keywords. For each article, searches for up to 5 related previously-embedded articles from the past 30 days and injects their summaries as context, enabling the LLM to note connections, contradictions, and developments in ongoing stories. Parses the response to extract both. Uses the model configured in settings (default: `llama3.2`). Truncates article content to 6000 chars. 120-second timeout per article. Fail-fast on fatal errors (connection lost, model not found, OOM).

### Embedding (`/api/embed`)

Embeds `title + summary` for each article using `nomic-embed-text` (768 dimensions). Stores embeddings as struct-packed binary blobs. Used for RAG chat vector search via sqlite-vec KNN queries.

### Chat (`/api/generate` with RAG context)

Embeds user query → KNN search for top-5 similar articles → formats articles as context → generates response in Abend voice.

### Relevance Scoring (`/api/generate`)

Scores each article across 7 analytical dimensions (0-3 each) based on the No One Relevancy Rubric (`no_one_relevancy_rubric.md`):
- **D1** Attention Economy, **D2** Data Sovereignty, **D3** Power Consolidation, **D4** Coercion vs Cooperation, **D5** Fear vs Trust, **D6** Democratization, **D7** Systemic Design

LLM provides the 7 dimension scores + a rationale. Python computes composite (0-21), tier (1-5), and convergence flag deterministically. Convergence: 5+ dimensions scoring 2+ (marks ~30% of articles). Uses the same model as summarization. ~2.5 seconds per article.

**Tier boundaries:**

| Composite | Tier | Priority |
|-----------|------|----------|
| 15-21 | T1 | Critical — full analysis |
| 10-14 | T2 | High — detailed summary |
| 5-9 | T3 | Notable — brief mention |
| 1-4 | T4 | Peripheral — log only |
| 0 | T5 | Skip — excluded |

### Entity Extraction (`/api/generate`)

Extracts named entities from each article across 5 categories: companies, people, products, legislation, and other. Returns a JSON object with entity arrays. Max 10 entities per category. Same fail-fast pattern as scoring.

### Topic Classification (`/api/generate`)

Classifies each article into 1-3 topics from a fixed 17-topic taxonomy: `ai_regulation`, `ai_capabilities`, `surveillance`, `platform_dynamics`, `labor_displacement`, `consolidation`, `privacy`, `content_moderation`, `startup_funding`, `layoffs`, `acquisitions`, `open_source`, `hardware`, `infrastructure`, `cybersecurity`, `crypto`, `other`. Stored as comma-separated string. Unknown topics mapped to "other".

### Thread Detection (Algorithmic)

Detects story threads by combining embedding similarity (KNN top-5) with entity overlap (2+ shared entities) to build an article relationship graph. Finds connected components via BFS and creates/extends threads for clusters of 5+ articles. Names threads from the most frequent entity. No LLM calls — purely algorithmic.

### Digest (`/api/generate` with scored article batch)

Retrieves last 24 hours of scored articles → groups by tier with proportional content budgets (T1: 3000 chars + rationale, T2: 1500 chars, T3: summary only, T4: title only, T5: excluded) → computes dimensional profile with elevation flags → generates 1500-2500 word narrative digest in Abend voice where analysis depth scales with article tier → post-processes to ensure hyperlinks and source attribution. Uses streaming (`stream: true`) with extended timeouts (30s connect, 600s between chunks), dynamic context window sizing (minimum 32768, rounded up to fit prompt), and a 4096-token response cap.

## Setup

```bash
cd /home/kellogg/dev/Sieve

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install flask requests apscheduler python-dateutil sqlite-vec

# Run (database initializes automatically on first start at /home/kellogg/data/sieve.db)
python app.py

# Open http://localhost:5000
```

### SystemD Service

A `sieve.service` file is included for running as a system service:
```bash
sudo cp sieve.service /etc/systemd/system/
sudo systemctl enable --now sieve
```

## Usage Flow

1. **n8n runs on schedule** → writes articles to `/home/kellogg/data/rssfeed.jsonl`
2. **Hourly pipeline runs** (or manual trigger):
   - Ingests JSONL, deduplicates by URL, inserts new articles
   - Compresses JSONL file (deduplicates source file)
   - Batch summarizes unsummarized articles via Ollama (with keyword extraction and related article context)
   - Batch embeds unembedded articles via Ollama
   - Batch scores articles across 7 relevance dimensions via Ollama
   - Extracts named entities from summarized articles
   - Classifies articles into topics from fixed taxonomy
   - Detects and links story threads from entity overlap + embedding similarity
3. **You browse** → filter articles by tier/score, sort by relevance, read summaries
4. **You chat** → ask questions, get RAG-powered answers grounded in your articles
5. **Daily digest** → generated at 20:00 UTC (configurable), 1 hour after last scoring batch. Score-aware narrative briefing in Abend voice with tiered depth. Automatically backfills missing days and regenerates stale digests
6. **You review scores** → check distribution dashboard, see which dimensions dominate

## Scheduling

| Job | Default Schedule | Purpose |
|-----|-----------------|---------|
| Pipeline | `0 * * * *` (hourly) | Full 8-stage cycle: ingest → compress → summarize → embed → score → entities → topics → threads |
| Digest | `0 20 * * *` (20:00 UTC) | Generate/backfill daily briefings, then deploy Rogue Routine site |
| Ingest | Configurable | Legacy standalone ingestion (if auto_ingest enabled) |

## Model Recommendations

| Model | Context | Speed | Quality | VRAM |
|-------|---------|-------|---------|------|
| llama3.2 | 128K | Fast | Good | 4GB |
| llama3.1:8b | 128K | Fast | Good | 8GB |
| llama3.1:70b | 128K | Slow | Excellent | 48GB |
| mistral:7b | 32K | Fast | Good | 6GB |

Default summarization model: `llama3.2`. Default embedding model: `nomic-embed-text`.

---

Built for sovereignty. Runs on your machine. Your data stays yours.

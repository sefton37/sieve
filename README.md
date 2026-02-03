# Sieve

A local-first news intelligence tool that ingests RSS articles, summarizes and embeds them with Ollama, and provides a web interface for browsing, filtering, RAG-based chat, and daily digests.

## Overview

Sieve takes a JSONL feed of articles (from n8n), deduplicates and stores them in SQLite, generates AI summaries and embeddings via Ollama, and serves a web UI for browsing, chatting with the corpus, and reading daily digests.

```
[n8n JSONL export] → [Sieve Pipeline] → [SQLite + sqlite-vec] → [Ollama Summarize + Embed] → [Web UI]
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
│   │              Pipeline Orchestrator                    │               │
│   │   1. Ingest   - Parse JSONL, dedupe by URL           │               │
│   │   2. Compress - Deduplicate source JSONL             │               │
│   │   3. Summarize - Batch summarize via Ollama          │               │
│   │   4. Embed    - Batch embed via Ollama               │               │
│   └──────────────────┬───────────────────────────────────┘               │
│                      │                                                   │
│                      ▼                                                   │
│   ┌──────────────────┐      ┌──────────────────────────┐                 │
│   │   SQLite DB      │      │   Ollama API             │                 │
│   │  + sqlite-vec    │◄────►│   localhost:11434        │                 │
│   │                  │      │   - /api/generate        │                 │
│   │  - articles      │      │   - /api/embed           │                 │
│   │  - vec_articles  │      └──────────────────────────┘                 │
│   │  - settings      │                                                   │
│   │  - chat_messages │                                                   │
│   │  - digests       │                                                   │
│   └──────────────────┘                                                   │
│            │                                                             │
│            ▼                                                             │
│   ┌──────────────────────────────────────────────────────┐               │
│   │    Web UI (Flask + HTMX)      localhost:5000         │               │
│   │   - Browse & filter articles                         │               │
│   │   - Chat with corpus (RAG)                           │               │
│   │   - Daily digests                                    │               │
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
    ('auto_digest', 'false'),
    ('digest_schedule', '0 6 * * *');
```

## File Structure

```
Sieve/
├── app.py              # Flask app, routes, job management
├── db.py               # SQLite operations, schema, migrations
├── ingest.py           # JSONL parsing and URL deduplication
├── summarize.py        # Ollama summarization with keyword extraction
├── embed.py            # Ollama embedding (nomic-embed-text, 768-dim)
├── pipeline.py         # Orchestrator: ingest → compress → summarize → embed
├── scheduler.py        # APScheduler: hourly pipeline, daily digest
├── chat.py             # RAG chat: embed query → vector search → generate
├── digest.py           # Daily digest generation in Abend voice
├── sieve.service       # SystemD service file for deployment
├── templates/
│   ├── base.html       # Layout with nav (Browse, Chat, Digest, Settings)
│   ├── index.html      # Article browser with filters
│   ├── article.html    # Single article view
│   ├── settings.html   # Config, stats, job triggers
│   ├── chat.html       # RAG chat interface
│   ├── digest.html     # Daily digest viewer
│   └── partials/
│       ├── article_list.html      # Paginated article grid
│       ├── summary_section.html   # Summary + keywords display
│       ├── job_status.html        # Job progress/status
│       ├── chat_response.html     # Chat message rendering
│       └── stats.html             # Live statistics
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
- Filter by: source, keyword, summary status, date range, text search
- Shows: title, source, date, summary preview, keyword tags
- Click to view full article

### 2. Article (`/article/<id>`)
- Full article content
- Summary with keywords (or "not yet summarized")
- Button: "Regenerate summary"
- Metadata: source, dates, link to original

### 3. Chat (`/chat`)
- RAG-based question answering over the article corpus
- Embeds query → vector search for top-5 similar articles → generates response
- Abend voice (analytical, observational perspective)
- Markdown rendering, source article links
- Clear history button

### 4. Digest (`/digest`)
- AI-generated daily briefings in Abend voice
- Generate on demand or via scheduled job (default: 6 AM)
- Markdown rendering with source links
- Archive of recent digests (last 14)

### 5. Settings (`/settings`)
- **Live stats** - Article count, summarized, embedded, pending (auto-refreshing)
- **Job management** - Hourly pipeline trigger, individual action buttons (ingest, summarize, embed), job progress display
- **Ollama config** - Model dropdown (from installed models), context window slider, temperature, system prompt
- **Embedding config** - Embedding model name
- **Ingestion** - JSONL path, auto-ingest toggle, cron schedule
- **Digest** - Auto-digest toggle, digest cron schedule

## API Endpoints

```
GET  /                        # Browse articles (with filter/pagination params)
GET  /article/<id>            # Single article view
POST /article/<id>/summarize  # Regenerate one summary

GET  /settings                # Settings page
POST /settings                # Update settings

POST /ingest                  # Trigger JSONL ingestion
POST /summarize               # Trigger batch summarization
POST /embed                   # Trigger batch embedding
POST /pipeline                # Trigger full pipeline (ingest → compress → summarize → embed)
GET  /status                  # Job status (HTMX partial or JSON)
GET  /stats                   # Live statistics (HTMX partial)

GET  /chat                    # Chat interface
POST /chat                    # Send message (RAG query)
POST /chat/clear              # Clear chat history

GET  /digest                  # Digest viewer
POST /digest/generate         # Generate daily digest
```

## Ollama Integration

### Summarization (`/api/generate`)

Sends articles to Ollama with a structured prompt requesting both a summary paragraph (5-8 sentences) and 3-5 keywords. Parses the response to extract both. Uses the model configured in settings (default: `llama3.2`). Truncates article content to 6000 chars. 120-second timeout per article. Fail-fast on fatal errors (connection lost, model not found, OOM).

### Embedding (`/api/embed`)

Embeds `title + summary` for each article using `nomic-embed-text` (768 dimensions). Stores embeddings as struct-packed binary blobs. Used for RAG chat vector search via sqlite-vec KNN queries.

### Chat (`/api/generate` with RAG context)

Embeds user query → KNN search for top-5 similar articles → formats articles as context → generates response in Abend voice.

### Digest (`/api/generate` with article batch)

Retrieves last 24 hours of summarized articles → formats as context with content excerpts → generates 1500-2500 word narrative digest in Abend voice → post-processes to ensure hyperlinks and source attribution.

## Setup

```bash
cd /home/kellogg/dev/Sieve

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install flask requests apscheduler python-dateutil sqlite-vec

# Run (database initializes automatically on first start)
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
   - Batch summarizes unsummarized articles via Ollama (with keyword extraction)
   - Batch embeds unembedded articles via Ollama
3. **You browse** → filter articles, read summaries, explore keywords
4. **You chat** → ask questions, get RAG-powered answers grounded in your articles
5. **Daily digest** → generated at 6 AM (configurable), narrative briefing in Abend voice

## Scheduling

| Job | Default Schedule | Purpose |
|-----|-----------------|---------|
| Pipeline | `0 * * * *` (hourly) | Full ingest → compress → summarize → embed cycle |
| Digest | `0 6 * * *` (6 AM) | Generate daily briefing (if auto_digest enabled) |
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

# Sieve

A local-first news intelligence tool that ingests RSS articles, summarizes them with Ollama, and provides a clean interface for browsing and filtering.

## Overview

Sieve takes a JSONL feed of articles, deduplicates and stores them in SQLite, generates AI summaries via Ollama, and serves a web UI for browsing.

```
[n8n JSONL export] → [Sieve Ingestion] → [SQLite] → [Ollama Summarization] → [Web UI]
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                           SIEVE                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   /home/kellogg/data/rssfeed.jsonl                              │
│              │                                                   │
│              ▼                                                   │
│   ┌──────────────────┐                                          │
│   │   Ingest Service │  ← Scheduled or manual trigger           │
│   │   - Parse JSONL  │                                          │
│   │   - Dedupe by URL│                                          │
│   └────────┬─────────┘                                          │
│            │                                                     │
│            ▼                                                     │
│   ┌──────────────────┐                                          │
│   │     SQLite DB    │  /home/kellogg/data/sieve.db             │
│   │   - articles     │                                          │
│   │   - settings     │                                          │
│   └────────┬─────────┘                                          │
│            │                                                     │
│            ▼                                                     │
│   ┌──────────────────┐      ┌─────────────────┐                 │
│   │ Summary Service  │ ───► │  Ollama API     │                 │
│   │ - Batch process  │ ◄─── │  localhost:11434│                 │
│   │ - <100 word sum  │      └─────────────────┘                 │
│   └────────┬─────────┘                                          │
│            │                                                     │
│            ▼                                                     │
│   ┌──────────────────┐                                          │
│   │    Web UI        │  localhost:5000                          │
│   │   - Browse       │                                          │
│   │   - Settings     │                                          │
│   │   - Trigger jobs │                                          │
│   └──────────────────┘                                          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Backend | Python + Flask | Simple, stable, well-documented |
| Database | SQLite | Local, zero config, portable |
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
    summarized_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Settings table (key-value store)
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Default settings
INSERT INTO settings (key, value) VALUES
    ('ollama_model', 'llama3.1:8b'),
    ('ollama_url', 'http://localhost:11434'),
    ('system_prompt', 'Summarize this article in under 100 words. Focus on the key facts and implications. Be concise and direct.'),
    ('num_ctx', '8192'),
    ('temperature', '0.3'),
    ('jsonl_path', '/home/kellogg/data/rssfeed.jsonl'),
    ('auto_ingest', 'false'),
    ('ingest_schedule', '0 6 * * *');
```

## File Structure

```
sieve/
├── app.py              # Flask app, routes, main entry
├── db.py               # Database operations
├── ingest.py           # JSONL parsing and deduplication
├── summarize.py        # Ollama integration
├── scheduler.py        # APScheduler jobs
├── templates/
│   ├── base.html       # Layout with nav
│   ├── index.html      # Article list/browse
│   ├── article.html    # Single article view
│   └── settings.html   # Ollama & schedule config
├── static/
│   └── style.css       # Minimal overrides if needed
├── requirements.txt
└── README.md
```

## UI Pages

### 1. Browse (`/`)
- Paginated list of articles
- Shows: title, source, date, summary (truncated)
- Filter by: source, has summary, date range
- Sort by: date, source
- Click to expand full article + summary

### 2. Article (`/article/<id>`)
- Full article content
- Summary (or "not yet summarized")
- Button: "Regenerate summary"
- Metadata: source, dates, URL link

### 3. Settings (`/settings`)
- **Ollama Config**
  - Model name (dropdown of installed models)
  - Context window (num_ctx)
  - Temperature
  - System prompt (textarea)
- **Ingestion**
  - JSONL path
  - Manual trigger button: "Ingest Now"
  - Schedule (cron expression)
  - Auto-ingest toggle
- **Processing**
  - Manual trigger: "Summarize All Unsummarized"
  - Progress indicator

## API Endpoints

```
GET  /                      # Browse articles
GET  /article/<id>          # Single article
POST /article/<id>/summarize # Regenerate one summary

GET  /settings              # Settings page
POST /settings              # Update settings

POST /ingest                # Trigger JSONL ingestion
POST /summarize             # Trigger batch summarization
GET  /status                # Job status (for polling)
```

## Ollama Integration

```python
def summarize_article(title: str, content: str, settings: dict) -> str:
    """Call Ollama to summarize a single article."""
    
    prompt = f"""Title: {title}

Content:
{content[:6000]}  # Truncate to fit context

Provide a summary in under 100 words."""

    response = requests.post(
        f"{settings['ollama_url']}/api/generate",
        json={
            'model': settings['ollama_model'],
            'system': settings['system_prompt'],
            'prompt': prompt,
            'stream': False,
            'options': {
                'num_ctx': int(settings['num_ctx']),
                'temperature': float(settings['temperature'])
            }
        },
        timeout=120
    )
    
    return response.json()['response']
```

## Setup Instructions

```bash
# 1. Create directory
mkdir -p /home/kellogg/projects/sieve
cd /home/kellogg/projects/sieve

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install flask requests apscheduler

# 4. Initialize database
python -c "from db import init_db; init_db()"

# 5. Run
python app.py

# 6. Open http://localhost:5000
```

## Usage Flow

1. **n8n runs daily** → writes to `/home/kellogg/data/rssfeed.jsonl`
2. **Sieve ingests** → parses JSONL, dedupes by URL, inserts new articles
3. **Sieve summarizes** → sends unsummarized articles to Ollama one by one
4. **You browse** → open web UI, scroll through summaries, click for full content

## Model Recommendations

| Model | Context | Speed | Quality | VRAM |
|-------|---------|-------|---------|------|
| llama3.1:8b | 128K | Fast | Good | 8GB |
| llama3.1:70b | 128K | Slow | Excellent | 48GB |
| mistral:7b | 32K | Fast | Good | 6GB |
| mixtral:8x7b | 32K | Medium | Very Good | 32GB |

Start with `llama3.1:8b` — fast, good summaries, fits most GPUs.

## Future Enhancements (Not Now)

- [ ] Categories/tags per article
- [ ] Blog idea extraction pass
- [ ] Export summaries to markdown
- [ ] Search (FTS5)
- [ ] Multiple JSONL sources
- [ ] Email digest

---

Built for sovereignty. Runs on your machine. Your data stays yours.

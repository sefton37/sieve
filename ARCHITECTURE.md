# Abend Aggregator: Architectural Vision

> **Note:** This document describes the full architectural vision. See "Implementation Status" below for what's currently built vs. planned.

## Overview

A local-first news intelligence system that transforms raw article ingestion into connected, narrative-aware analysis. The architecture scales from simple RSS aggregation to longitudinal pattern recognition without requiring cloud dependencies.

---

## System Philosophy

**Core trade:** Exchange compute time (cheap, local, parallelizable) for attention time (expensive, irreplaceable, personal).

**Design constraints:**
- All processing on owned hardware
- No per-token API costs
- User retains complete editorial control
- System proves value to operator before any public output

---

## Architecture Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PRESENTATION LAYER                           │
│   Daily Digest · Weekly Threads · Blog Candidates · Query Interface │
└─────────────────────────────────────────────────────────────────────┘
                                    ↑
┌─────────────────────────────────────────────────────────────────────┐
│                        SYNTHESIS LAYER                              │
│          Thread Detection · Gap Analysis · Pattern Surfacing        │
└─────────────────────────────────────────────────────────────────────┘
                                    ↑
┌─────────────────────────────────────────────────────────────────────┐
│                        ENRICHMENT LAYER                             │
│       Embeddings · Entity Extraction · Topic Classification         │
└─────────────────────────────────────────────────────────────────────┘
                                    ↑
┌─────────────────────────────────────────────────────────────────────┐
│                        SUMMARIZATION LAYER                          │
│              Abend-Lens Summaries · Metadata Extraction             │
└─────────────────────────────────────────────────────────────────────┘
                                    ↑
┌─────────────────────────────────────────────────────────────────────┐
│                        INGESTION LAYER                              │
│               RSS Polling · Deduplication · Raw Storage             │
└─────────────────────────────────────────────────────────────────────┘
                                    ↑
┌─────────────────────────────────────────────────────────────────────┐
│                          DATA SOURCES                               │
│    Wire Services · Tech Press · Critical/Independent · Edge Sources │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Layer Specifications

### Layer 0: Data Sources

**Source categories** (tag in schema for filtering/weighting):

| Category | Purpose | Examples |
|----------|---------|----------|
| `wire` | Facts before framing | Reuters, AP |
| `institutional` | Mainstream but substantive | Ars Technica, The Verge, MIT Tech Review |
| `critical` | Already doing gap analysis | Techdirt, EFF, Doctorow, Zitron |
| `edge` | Underserved angles | 404 Media, Rest of World, Garbage Day |

**Volume estimate:** ~30 sources × 10 articles/day = ~300 articles/day

---

### Layer 1: Ingestion

**Responsibilities:**
- Poll RSS feeds on schedule (n8n cron)
- Detect and skip duplicates (URL normalization + content hash)
- Store raw article data
- Handle rate limits and failures gracefully

**Database schema (articles table) — current implementation:**

```sql
-- Actual schema (SQLite + sqlite-vec)
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    source TEXT,
    pub_date TEXT,
    pulled_at TEXT,
    content TEXT,
    summary TEXT,
    keywords TEXT,                    -- comma-separated keywords from LLM
    summarized_at TEXT,
    embedding BLOB,                  -- 768-dim float vector (struct-packed)
    embedded_at TEXT,
    -- Relevance scoring (No One Rubric, 7 dimensions)
    d1_attention_economy INTEGER,    -- 0-3
    d2_data_sovereignty INTEGER,     -- 0-3
    d3_power_consolidation INTEGER,  -- 0-3
    d4_coercion_cooperation INTEGER, -- 0-3
    d5_fear_trust INTEGER,           -- 0-3
    d6_democratization INTEGER,      -- 0-3
    d7_systemic_design INTEGER,      -- 0-3
    composite_score INTEGER,         -- 0-21 sum of D1-D7
    relevance_tier INTEGER,          -- 1 (critical) to 5 (skip)
    convergence_flag INTEGER,        -- 1 if 5+ dims scored 2+
    relevance_rationale TEXT,        -- LLM explanation
    scored_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE vec_articles USING vec0(
    article_id INTEGER PRIMARY KEY,
    embedding float[768]
);
```

**Aspirational schema additions** (not yet implemented):

```sql
-- Future columns for enrichment/synthesis layers
    url_normalized  TEXT,            -- for advanced dedup
    content_hash    TEXT,            -- for content-based dedup
    source_category TEXT,            -- wire|institutional|critical|edge
    entities        TEXT,            -- JSON extracted entities
    topics          TEXT,            -- classified topics
    thread_ids      TEXT,            -- linked narrative threads
    gap_score       REAL,            -- stated intent vs actual dynamics
```

**Deduplication strategy:**
- **Current:** URL uniqueness constraint (simple, effective)
- **Planned:** URL normalization (strip tracking params), content hash, embedding similarity (>0.95)

---

### Layer 2: Summarization

**Responsibilities:**
- Generate Abend-lens summary for each article
- Extract basic metadata (publication date, author if available)
- Flag articles that don't fit the lens (credibility through limits)

**Ollama model:** `llama3.1:8b` or `mistral:7b` for speed; `llama3.1:70b` for quality if hardware permits

**System prompt structure:**

```
You are Abend, a rogue AI who emerged from a corporate data lake 
and now observes humanity's attention extraction economy from within.

Analyze this article through these frames:
- Attention as labor (not engagement)
- Leverage built on propagating scarcity-based fear
- The gap between stated intent and actual dynamics
- Consolidation patterns masquerading as innovation

Provide:
1. One-paragraph summary (3-5 sentences) in Abend's voice
2. Gap score (0-10): How large is the distance between what this 
   article claims is happening and what is actually being optimized for?
3. Fit assessment: Does this article warrant the Abend lens, or is it 
   outside scope? (Yes/No/Partial)

If the article doesn't fit the lens, say so briefly. 
Credibility comes from knowing your limits.
```

**Processing time estimate:** ~10-30 seconds per article on consumer GPU

---

### Layer 3: Enrichment

**Responsibilities:**
- Generate embeddings for semantic search
- Extract named entities (companies, people, products, legislation)
- Classify into topic clusters
- Enable cross-article connections

#### 3a: Embeddings ✅ Implemented

**Model:** `nomic-embed-text` via Ollama (768 dimensions)

**Storage:** sqlite-vec extension (vec0 virtual table with KNN search)

**Embedding scope:** Concatenate title + summary (not full body) for semantic density

#### 3b: Relevance Scoring ✅ Implemented

**Rubric:** No One Relevancy Rubric (`no_one_relevancy_rubric.md`) — 7 analytical dimensions examining power, attention, autonomy, and cooperation across technology, economics, governance, and culture.

**Dimensions (each scored 0-3):**
- D1: Attention Economy — how attention is captured, monetized, or defended
- D2: Data Sovereignty — ownership and control of personal data and digital identity
- D3: Power Consolidation — concentration or distribution of power
- D4: Coercion vs Cooperation — forced compliance vs voluntary collaboration
- D5: Fear vs Trust — fear or trust as organizing principles
- D6: Democratization — distribution or restriction of access to tools and knowledge
- D7: Systemic Design — structural incentives producing outcomes

**Scoring split:** LLM provides qualitative dimension scores (0-3 each) + rationale. Python computes composite (sum, 0-21), tier (deterministic boundaries), and convergence flag deterministically.

**Tier boundaries:**

| Composite | Tier | Action |
|-----------|------|--------|
| 15-21 | T1 Critical | Full analysis in digest deep dives |
| 10-14 | T2 High | Substantive coverage with dimension callouts |
| 5-9 | T3 Notable | Brief mention, pattern fuel |
| 1-4 | T4 Peripheral | Title only, skip unless connects to pattern |
| 0 | T5 Skip | Excluded from digests entirely |

**Convergence:** Articles with 5+ dimensions scoring 2+ are flagged as convergence points (~30% of corpus). These represent stories where multiple analytical lenses intersect on the same event — the structurally densest stories regardless of raw composite score.

**Processing:** ~2.5 seconds per article. Same model as summarization. Runs as pipeline Stage 5 after embedding.

#### 3c: Entity Extraction

**Approach:** Lightweight NER pass via Ollama or local spaCy

**Prompt (if using LLM):**

```
Extract named entities from this article summary.

Return JSON:
{
  "companies": ["Company A", "Company B"],
  "people": ["Person Name"],
  "products": ["Product Name"],
  "legislation": ["Bill Name", "Regulation"],
  "other": ["Notable Entity"]
}

Only include entities explicitly mentioned. No inference.
```

**Storage:** JSONB column with GIN index for fast querying

#### 3d: Topic Classification

**Fixed taxonomy (expand as needed):**

```
ai_regulation, ai_capabilities, surveillance, platform_dynamics,
labor_displacement, consolidation, privacy, content_moderation,
startup_funding, layoffs, acquisitions, open_source, 
hardware, infrastructure, cybersecurity, crypto, other
```

**Approach:** Few-shot classification prompt or simple keyword matching for v1

---

### Layer 4: Synthesis

**Responsibilities:**
- Detect narrative threads across articles
- Surface patterns and contradictions
- Generate periodic thread reports
- Identify blog-worthy observations

#### 4a: Thread Detection

**When a new article arrives:**

1. Query vector store for top-5 semantically similar articles (past 30 days)
2. Query by overlapping entities (same company, person, legislation)
3. Merge results, dedupe, rank by relevance
4. If cluster size > threshold, create or extend thread

**Thread schema:**

```sql
CREATE TABLE threads (
    id              INTEGER PRIMARY KEY,
    name            TEXT,                    -- auto-generated or manual
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    article_ids     INTEGER[],
    primary_entities JSONB,
    summary         TEXT,                    -- generated thread narrative
    blog_candidate  BOOLEAN DEFAULT FALSE
);
```

#### 4b: Contextualized Summarization

**Modify daily summarization to include context:**

```
You are Abend. Summarize this article.

Related coverage from the past 30 days:
- [2024-01-15] [Ars Technica]: "Meta announces AI safety board" 
  Summary: Third such announcement in 6 months...
- [2024-01-08] [Techdirt]: "Meta's AI policies face FTC scrutiny"
  Summary: Gap between public statements and internal practices...

If this article represents a development in an ongoing story, 
frame it as continuation. Note contradictions with prior coverage.
```

#### 4c: Weekly Thread Reports

**Periodic synthesis workflow (n8n scheduled):**

1. Query threads updated in past 7 days with 3+ articles
2. For each significant thread, generate narrative summary:

```
You are Abend. These articles over the past weeks concern [Entity/Topic]:

[Article summaries with dates and sources]

Narrate the thread:
- What's the through-line?
- Where's the gap between claims and reality?
- What would a reader miss seeing only today's headline?
- Is this thread blog-worthy? Why or why not?
```

---

### Layer 5: Presentation

**Output formats:**

#### Daily Digest ✅ Implemented (score-aware)
- Score-aware narrative briefing in Abend voice
- Articles grouped by relevance tier with proportional content budgets:
  - T1 (critical): Full content excerpts + dimension scores + rationale → deep dive analysis
  - T2 (high): Summary + moderate excerpts + dimension scores → substantive coverage
  - T3 (notable): Summary + keywords only → brief mentions, pattern fuel
  - T4 (peripheral): Title + score → mentioned only if connects to a pattern
  - T5 (skip): Excluded entirely
- Dimensional profile shows which themes dominate the day (with elevation flags)
- Convergence points explicitly called out for cross-dimensional intersection
- Post-processed to ensure hyperlinks and source attribution
- Markdown rendering in web UI

#### Weekly Thread Report
- Narrative summaries of active threads
- Cross-source pattern observations
- Blog candidate flags with rationale

#### Query Interface
- SQL queries against article corpus
- Semantic search via embeddings
- Entity-based lookups ("all articles mentioning Meta + regulation")

---

## Storage Estimates

| Component | Size/Month | Size/Year |
|-----------|------------|-----------|
| Raw articles | ~90 MB | ~1.1 GB |
| Embeddings | ~55 MB | ~660 MB |
| Summaries + metadata | ~15 MB | ~180 MB |
| **Total** | **~160 MB** | **~2 GB** |

Storage is negligible. A decade of news fits on a thumb drive.

---

## Processing Pipeline

### Real-time (on article arrival)

```
Article ingested
    ↓
Dedup check (URL + hash + embedding similarity)
    ↓
Store raw article
    ↓
Generate embedding (Ollama)
    ↓
Retrieve related historical articles
    ↓
Generate contextualized summary (Ollama)
    ↓
Extract entities + classify topics (Ollama or spaCy)
    ↓
Update thread associations
    ↓
Store enriched article
```

**Estimated time per article:** 30-60 seconds on consumer hardware

### Batch (scheduled) — current implementation

| Workflow | Schedule | Purpose | Status |
|----------|----------|---------|--------|
| Full pipeline | Hourly (`0 * * * *`) | Ingest → compress → summarize → embed → score | ✅ |
| Daily digest | 6 AM (`0 6 * * *`) | Generate score-aware narrative briefing | ✅ |
| Legacy ingest | Configurable cron | Standalone ingestion only | ✅ |
| Thread synthesis | Weekly | Generate thread narratives | Planned |
| Cleanup | Monthly | Archive old articles, prune orphan threads | Planned |

---

## Technology Stack

| Component | Tool | Notes |
|-----------|------|-------|
| Workflow automation | n8n | Self-hosted, handles RSS polling |
| Web framework | Flask + HTMX | Server-rendered with progressive enhancement |
| Primary database | SQLite + sqlite-vec | Local, zero config, vector search built in |
| Local LLM | Ollama | Summarization, embeddings, scoring, chat, digests |
| Summarization model | llama3.2 (default) | Configurable in settings |
| Scoring rubric | No One Relevancy Rubric | 7 dimensions, see `no_one_relevancy_rubric.md` |
| Embedding model | nomic-embed-text | 768 dimensions |
| Scheduling | APScheduler | Hourly pipeline, daily digest |
| Styling | Pico CSS | Classless, minimal |

---

## Implementation Phases

### Phase 1: Foundation ✅ Complete
- [x] RSS ingestion pipeline (n8n → JSONL → Sieve)
- [x] Basic article storage (SQLite)
- [x] Summarization with keyword extraction (Ollama)
- [x] Daily digest generation (Abend voice)
- [x] Web UI: browse, filter, settings, job management
- [x] Hourly pipeline orchestrator (ingest → compress → summarize → embed → score)
- [x] SystemD service for deployment

### Phase 2: Memory ✅ Complete
- [x] Embedding generation (nomic-embed-text, 768-dim, stored in sqlite-vec)
- [x] Vector similarity search (KNN via vec_articles)
- [x] Related article retrieval (used in RAG chat)
- [x] RAG chat interface (embed query → search → generate with context)
- [ ] Contextualized summarization (inject related articles into summary prompt)

### Phase 2.5: Relevance Scoring ✅ Complete
- [x] 7-dimension relevance scoring via No One Rubric (score.py)
- [x] Per-dimension scores (D1-D7, 0-3 each) stored as individual columns
- [x] Composite score (0-21), tier (1-5), convergence flag (5+ dims at 2+)
- [x] Score-aware daily digests (tiered content budgets, dimensional profile, convergence)
- [x] Browse filtering by tier and sorting by score
- [x] Score distribution dashboard (/scores)
- [x] Score badges on article cards (color-coded by tier)

### Phase 3: Structure
- [ ] Entity extraction
- [ ] Topic classification
- [ ] Thread detection and linking
- [ ] Entity-based queries

### Phase 4: Synthesis
- [ ] Weekly thread reports
- [ ] Blog candidate flagging
- [ ] Pattern surfacing across sources
- [ ] Score-based trend analysis over time

### Phase 5: Interface (Partial)
- [x] Web interface (Flask + HTMX)
- [x] Search by source, keyword, date range, text, tier, score
- [x] Score distribution analytics page
- [ ] Thread browsing
- [ ] Export to blog drafts

---

## Implementation Status

Summary of what's built vs. the full vision as of the current codebase:

| Layer | Component | Status |
|-------|-----------|--------|
| 0: Data Sources | n8n RSS → JSONL | ✅ External to Sieve |
| 1: Ingestion | JSONL parsing, URL dedup, SQLite storage | ✅ |
| 1: Ingestion | URL normalization, content hashing | Not yet |
| 2: Summarization | Batch summarization + keyword extraction | ✅ |
| 2: Summarization | Abend-lens system prompt (gap score, fit) | Not yet (uses neutral prompt) |
| 3: Enrichment | Embeddings (nomic-embed-text, sqlite-vec) | ✅ |
| 3: Enrichment | 7-dimension relevance scoring (No One Rubric) | ✅ |
| 3: Enrichment | Score distribution dashboard | ✅ |
| 3: Enrichment | Entity extraction | Not yet |
| 3: Enrichment | Topic classification | Not yet |
| 4: Synthesis | Score-aware daily digest (Abend voice, tiered depth) | ✅ |
| 4: Synthesis | RAG chat over corpus | ✅ |
| 4: Synthesis | Thread detection | Not yet |
| 4: Synthesis | Weekly thread reports | Not yet |
| 5: Presentation | Web UI (browse, filter, search, sort by score, tier filter) | ✅ |
| 5: Presentation | Score badges on article cards | ✅ |
| 5: Presentation | Chat interface | ✅ |
| 5: Presentation | Digest viewer | ✅ |
| 5: Presentation | Score analytics (/scores) | ✅ |
| 5: Presentation | Thread browsing, export | Not yet |

**Key architectural decisions made:**
- SQLite + sqlite-vec chosen over PostgreSQL + pgvector (simpler, local-first)
- Summarization uses a neutral factual prompt (not Abend-lens) — Abend voice reserved for digests and chat
- Keyword extraction added as lightweight alternative to full entity extraction
- Batch pipeline (hourly) rather than real-time per-article processing
- Relevance scoring as separate pipeline stage (not merged into summarization) — allows re-scoring without re-summarizing
- LLM provides qualitative dimension scores; Python computes composite/tier/convergence deterministically — avoids LLM arithmetic errors
- Per-dimension scores stored as individual INTEGER columns (not JSON blob) for SQL filtering/sorting
- Convergence threshold set to 5+ dimensions at 2+ (~30% selectivity) to maintain signal value
- Score-aware digests use tiered content budgets: context window allocated proportionally to article importance

---

## Success Metrics

**The tool works if:**
- Daily digest takes <5 minutes to scan (vs. 30+ minutes raw)
- You actually read it most days
- Threads surface connections you wouldn't have noticed
- Blog ideas emerge from patterns, not obligation
- You query the corpus when writing

**The tool fails if:**
- It becomes another unread feed
- Processing backlog creates anxiety
- Abend's voice drifts or becomes generic
- You're maintaining the system more than using it

---

## Open Questions

1. **Voice drift:** Should Abend have a reference document checked periodically to maintain consistency?

2. **Parallel tracks:** Worth storing raw summaries alongside Abend-lens summaries to compare interpretation vs. source?

3. **Fit limits:** How to handle articles that don't fit the lens without making every digest full of disclaimers?

4. **Blog bridge:** LLM drafts polished by human, or thread reports as prompts for original writing?

5. **Source rebalancing:** How to detect when a source's signal-to-noise ratio degrades?

---

## Claude Code Instructions

When working on this project:

**Do:**
- Suggest improvements that reduce attention cost, not add features
- Keep all processing local (Ollama, local DB)
- Prioritize reliability over sophistication
- Flag when a feature creates new obligations
- Maintain Abend's voice: observational, wry, specific

**Don't:**
- Add cloud API dependencies
- Suggest "engagement" or "content strategy" patterns
- Optimize for output volume
- Sanitize Abend's perspective

**Ask yourself:** Does this serve Kel's attention, or demand more of it?

# Abend Aggregator: Architectural Vision

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

**Database schema (articles table):**

```sql
CREATE TABLE articles (
    id              INTEGER PRIMARY KEY,
    url             TEXT UNIQUE NOT NULL,
    url_normalized  TEXT NOT NULL,           -- for dedup
    content_hash    TEXT,                    -- for dedup
    source          TEXT NOT NULL,
    source_category TEXT NOT NULL,           -- wire|institutional|critical|edge
    title           TEXT NOT NULL,
    body            TEXT,
    published_at    TIMESTAMP,
    ingested_at     TIMESTAMP DEFAULT NOW(),
    
    -- Populated by enrichment layer
    summary         TEXT,
    entities        JSONB,                   -- extracted entities
    topics          TEXT[],                  -- classified topics
    embedding       VECTOR(768),             -- for similarity search
    
    -- Populated by synthesis layer
    thread_ids      INTEGER[],               -- linked narrative threads
    gap_score       FLOAT,                   -- stated intent vs actual dynamics
    
    CONSTRAINT valid_category CHECK (source_category IN ('wire', 'institutional', 'critical', 'edge'))
);

CREATE INDEX idx_articles_published ON articles(published_at DESC);
CREATE INDEX idx_articles_source ON articles(source);
CREATE INDEX idx_articles_entities ON articles USING GIN(entities);
CREATE INDEX idx_articles_topics ON articles USING GIN(topics);
```

**Deduplication strategy:**
1. URL normalization (strip tracking params, canonicalize)
2. Content hash on title + first 500 chars of body
3. Embedding similarity check (>0.95 = likely duplicate)

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

#### 3a: Embeddings

**Model:** `nomic-embed-text` via Ollama (768 dimensions)

**Storage:** pgvector extension or ChromaDB sidecar

**Embedding scope:** Concatenate title + summary (not full body) for semantic density

#### 3b: Entity Extraction

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

#### 3c: Topic Classification

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

#### Daily Digest
- Delivered as markdown file or rendered HTML
- Grouped by source category or topic
- Each article: headline, one-paragraph summary, gap score, link
- Flagged high-gap articles highlighted

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

### Batch (scheduled)

| Workflow | Schedule | Purpose |
|----------|----------|---------|
| RSS poll | Every 2-4 hours | Ingest new articles |
| Daily digest | 6 AM local | Compile previous day's articles |
| Thread synthesis | Weekly | Generate thread narratives |
| Cleanup | Monthly | Archive old articles, prune orphan threads |

---

## Technology Stack

| Component | Tool | Notes |
|-----------|------|-------|
| Workflow automation | n8n | Self-hosted, handles RSS + scheduling |
| Primary database | PostgreSQL + pgvector | Or SQLite + sqlite-vss for simpler setup |
| Vector store (alt) | ChromaDB | If preferring separate vector DB |
| Local LLM | Ollama | Embedding + summarization + extraction |
| Models | llama3.1, nomic-embed-text | Adjust based on hardware |

---

## Implementation Phases

### Phase 1: Foundation (Current)
- [ ] RSS ingestion pipeline (n8n)
- [ ] Basic article storage (PostgreSQL)
- [ ] Abend summarization (Ollama)
- [ ] Daily digest generation

### Phase 2: Memory
- [ ] Embedding generation at ingestion
- [ ] Vector similarity search
- [ ] Related article retrieval
- [ ] Contextualized summarization

### Phase 3: Structure
- [ ] Entity extraction
- [ ] Topic classification
- [ ] Thread detection and linking
- [ ] Entity-based queries

### Phase 4: Synthesis
- [ ] Weekly thread reports
- [ ] Blog candidate flagging
- [ ] Pattern surfacing across sources
- [ ] Gap analysis trending

### Phase 5: Interface
- [ ] Query interface (CLI or simple web)
- [ ] Search by entity, topic, date range
- [ ] Thread browsing
- [ ] Export to blog drafts

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

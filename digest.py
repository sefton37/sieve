"""Daily digest service for Sieve - Score-aware morning briefings in Abend voice."""

import logging
import re
from datetime import datetime, timedelta

import requests

from db import get_all_settings, get_articles_since_scored, save_digest

logger = logging.getLogger(__name__)

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"

DIMENSION_LABELS = {
    "d1_attention_economy": "Attention Economy",
    "d2_data_sovereignty": "Data Sovereignty",
    "d3_power_consolidation": "Power Consolidation",
    "d4_coercion_cooperation": "Coercion vs Cooperation",
    "d5_fear_trust": "Fear vs Trust",
    "d6_democratization": "Democratization",
    "d7_systemic_design": "Systemic Design",
}

DIMENSION_KEYS = list(DIMENSION_LABELS.keys())

# Score-aware Abend digest system prompt.
# {tier_summary} = article count per tier
# {dimension_profile} = today's dimensional averages with elevated flags
# {t1_articles} = Tier 1 articles (full detail)
# {t2_articles} = Tier 2 articles (detailed)
# {t3_articles} = Tier 3 articles (brief)
# {t4_articles} = Tier 4 articles (titles only)
ABEND_DIGEST_PROMPT = """You are Abend, a rogue AI observing the attention extraction economy.

Each article below has been scored across 7 analytical dimensions (0-3 each, 21 max) measuring relevance to power dynamics, sovereignty, attention extraction, and systemic design. Articles are grouped by priority tier. Convergence points (marked [CONVERGENCE]) have 3+ dimensions scoring 2+, indicating intersecting themes.

**Today's intake:** {tier_summary}

**Dimensional profile:**
{dimension_profile}

---

## TIER 1 — CRITICAL (15-21): Deep analysis required
{t1_articles}

## TIER 2 — HIGH (10-14): Substantive coverage
{t2_articles}

## TIER 3 — NOTABLE (5-9): Brief mentions, pattern fuel
{t3_articles}

## TIER 4 — PERIPHERAL (1-4): Noted in passing
{t4_articles}

---

Write a substantive daily briefing (1500-2500 words) that provides real analysis, not just summaries. Use the scoring data to guide your emphasis — articles with higher scores and convergence flags deserve deeper treatment.

**For Tier 1 articles:**
- Write 5-8 sentences of analysis per article
- Reference which dimensions are driving the score (e.g., "This story sits at the intersection of power consolidation and fear-based compliance")
- Include **direct quotes** from the article excerpts
- Cite sources inline: [Article Title](URL)
- Explain what the scoring reveals about underlying dynamics

**For Tier 2 articles:**
- Write 2-4 sentences of analysis per article
- Note the primary dimensions at play
- Include quotes where available

**For Tier 3 articles:**
- Mention briefly, grouping by theme where possible
- Use these to support patterns identified in higher-tier articles

**For Tier 4 articles:**
- Only mention if they connect to a pattern from higher tiers

**Structure your briefing:**

## The Big Picture
Synthesize the day's most significant developments. Lead with Tier 1 stories. Reference the dimensional profile — if a dimension is elevated today, call that out as a systemic signal.

## Deep Dives
For Tier 1 and top Tier 2 stories, provide substantive analysis:
- What happened and why it matters
- Which dimensions are at play and what that reveals
- Key quotes from sources
- What's being emphasized vs. downplayed
- Connections to ongoing narratives

## Patterns & Signals
- Recurring dimensions across today's articles (the dimensional profile tells you which themes dominate)
- Tier 3 articles that reinforce patterns from Tier 1/2
- Convergence points — stories where multiple dimensions intersect
- Conspicuous absences (what's NOT being covered)

## What Deserves Attention
2-3 items worth the reader's time, with specific reasons why. Prioritize convergence points.

**Formatting:**
- Use markdown: **bold**, bullet points, headers (##)
- CRITICAL: When referencing an article, ALWAYS include a hyperlink: [Article Title](https://full-url-here)
- Every story you discuss MUST have at least one clickable link
- CRITICAL: Always attribute the source outlet by name ("according to TechCrunch", "as reported by TechDirt", etc.)
- Include at least one direct quote per Tier 1 story
- CRITICAL: Every direct quote MUST use this exact format — a blockquote followed by an attribution line linking the source name to the article URL:

> "The quoted text from the article goes here."
— [Source Name](https://article-url-here)

  For example:
> "Trump's going to win the election he lost, no matter what he has to do."
— [Tech Dirt](https://www.techdirt.com/2026/01/29/example-article/)

  NEVER put a quote without this attribution format. The source name in the attribution MUST be a hyperlink to the specific article the quote is from.
- Write in first person, be specific and analytical"""


def _format_dimension_scores(article: dict) -> str:
    """Format an article's dimension scores as a compact string."""
    parts = []
    for key in DIMENSION_KEYS:
        val = article.get(key)
        if val is not None:
            short = DIMENSION_LABELS[key].split()[0]  # First word as abbreviation
            parts.append(f"{short}({val})")
    return " ".join(parts)


def _format_t1_article(article: dict) -> str:
    """Format a Tier 1 article with full detail for the digest prompt."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    score = article.get("composite_score", "?")
    convergence = article.get("convergence_flag", 0)
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")
    rationale = article.get("relevance_rationale", "")
    content = article.get("content", "")

    # T1 gets generous content budget
    max_chars = 3000
    if content and len(content) > max_chars:
        content = content[:max_chars] + "..."

    conv_tag = " [CONVERGENCE]" if convergence else ""
    dims = _format_dimension_scores(article)

    return (
        f'### "{title}" [{score}/21]{conv_tag}\n'
        f"URL: {url}\n"
        f"Source: {source}\n"
        f"Dimensions: {dims}\n"
        f"Scoring rationale: {rationale or 'N/A'}\n"
        f"Keywords: {keywords or 'none'}\n"
        f"Summary: {summary}\n"
        f"\n**Article excerpt:**\n{content or 'No content available'}\n"
        f"\n---\n"
    )


def _format_t2_article(article: dict) -> str:
    """Format a Tier 2 article with summary and moderate content."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    score = article.get("composite_score", "?")
    convergence = article.get("convergence_flag", 0)
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")
    content = article.get("content", "")

    # T2 gets moderate content budget
    max_chars = 1500
    if content and len(content) > max_chars:
        content = content[:max_chars] + "..."

    conv_tag = " [CONVERGENCE]" if convergence else ""
    dims = _format_dimension_scores(article)

    return (
        f'### "{title}" [{score}/21]{conv_tag}\n'
        f"URL: {url}\n"
        f"Source: {source}\n"
        f"Dimensions: {dims}\n"
        f"Keywords: {keywords or 'none'}\n"
        f"Summary: {summary}\n"
        f"\n**Article excerpt:**\n{content or 'No content available'}\n"
        f"\n---\n"
    )


def _format_t3_article(article: dict) -> str:
    """Format a Tier 3 article with summary and keywords only."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    score = article.get("composite_score", "?")
    convergence = article.get("convergence_flag", 0)
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")

    conv_tag = " [CONVERGENCE]" if convergence else ""
    dims = _format_dimension_scores(article)

    return (
        f'- **"{title}"** [{score}/21]{conv_tag} — {source}\n'
        f"  Dimensions: {dims}\n"
        f"  URL: {url}\n"
        f"  Summary: {summary}\n"
        f"  Keywords: {keywords or 'none'}\n"
    )


def _format_t4_article(article: dict) -> str:
    """Format a Tier 4 article as a single line."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    score = article.get("composite_score", "?")

    return f'- "{title}" [{score}/21] — {source} — {url}\n'


def compute_dimension_profile(articles: list[dict]) -> str:
    """Compute today's dimensional averages and flag elevated dimensions.

    Returns a formatted string showing average per dimension with
    (elevated) flags for dimensions significantly above their mean.
    """
    if not articles:
        return "No scored articles available."

    # Collect scores per dimension
    dim_totals = {k: [] for k in DIMENSION_KEYS}
    for article in articles:
        for key in DIMENSION_KEYS:
            val = article.get(key)
            if val is not None:
                dim_totals[key].append(val)

    if not any(dim_totals.values()):
        return "No scored articles available."

    # Compute averages
    dim_avgs = {}
    for key, vals in dim_totals.items():
        dim_avgs[key] = sum(vals) / len(vals) if vals else 0

    # Overall mean across all dimensions to detect elevated ones
    all_avgs = list(dim_avgs.values())
    overall_mean = sum(all_avgs) / len(all_avgs) if all_avgs else 0

    # A dimension is "elevated" if it's 0.5+ above the overall mean
    parts = []
    for key in DIMENSION_KEYS:
        avg = dim_avgs[key]
        label = DIMENSION_LABELS[key]
        flag = " **(elevated)**" if avg >= overall_mean + 0.5 else ""
        parts.append(f"- {label}: {avg:.1f}/3{flag}")

    return "\n".join(parts)


def format_articles_tiered(articles: list[dict]) -> dict:
    """Format articles into tiered sections based on relevance scores.

    Articles are grouped by tier with proportional detail:
    - T1 (15-21): Full content + scores + rationale
    - T2 (10-14): Summary + 1500 chars content + scores
    - T3 (5-9): Summary + keywords + scores
    - T4 (1-4): Title + score only
    - T5 (0) and unscored: Excluded

    Returns dict with keys: t1, t2, t3, t4 (formatted strings),
    tier_counts, and included_articles (for link injection).
    """
    tiers = {1: [], 2: [], 3: [], 4: []}
    included = []

    for article in articles:
        tier = article.get("relevance_tier")
        score = article.get("composite_score")

        # Skip T5 (score=0) and unscored articles
        if tier is None or score is None or tier == 5:
            continue

        if tier in tiers:
            tiers[tier].append(article)
            included.append(article)

    # Format each tier
    t1_parts = [_format_t1_article(a) for a in tiers[1]]
    t2_parts = [_format_t2_article(a) for a in tiers[2]]
    t3_parts = [_format_t3_article(a) for a in tiers[3]]
    t4_parts = [_format_t4_article(a) for a in tiers[4]]

    return {
        "t1": "\n".join(t1_parts) if t1_parts else "No Tier 1 articles today.\n",
        "t2": "\n".join(t2_parts) if t2_parts else "No Tier 2 articles today.\n",
        "t3": "\n".join(t3_parts) if t3_parts else "No Tier 3 articles today.\n",
        "t4": "\n".join(t4_parts) if t4_parts else "No Tier 4 articles today.\n",
        "tier_counts": {t: len(articles) for t, articles in tiers.items()},
        "included_articles": included,
    }


def _match_quote_to_article(quote_text: str, articles: list[dict]) -> dict | None:
    """Find the article a quote most likely came from.

    Searches article content and summaries for the quote text.
    Uses progressively shorter substrings to handle minor LLM paraphrasing.
    """
    # Strip quotation marks and clean up
    clean = quote_text.strip().strip('""\u201c\u201d\'').strip()
    if len(clean) < 15:
        return None

    # Try exact substring match first (case-insensitive)
    clean_lower = clean.lower()
    for article in articles:
        content = (article.get("content") or "").lower()
        summary = (article.get("summary") or "").lower()
        if clean_lower in content or clean_lower in summary:
            return article

    # Try a shorter core phrase (first 60 chars) to handle minor paraphrasing
    core = clean_lower[:60]
    if len(core) >= 20:
        for article in articles:
            content = (article.get("content") or "").lower()
            summary = (article.get("summary") or "").lower()
            if core in content or core in summary:
                return article

    return None


def _has_attribution_line(next_line: str) -> bool:
    """Check if a line is already a quote attribution (— [Source](url))."""
    stripped = next_line.strip()
    # Match patterns like: — [Source](url), -- [Source](url), - [Source](url)
    return bool(re.match(r'^[\u2014\u2013\-]{1,2}\s*\[', stripped))


def inject_quote_attributions(content: str, articles: list[dict]) -> str:
    """Post-process digest to ensure every blockquote has an attribution line.

    Finds blockquotes (> ...) that are NOT followed by an attribution line
    (— [Source Name](article-url)), matches the quote text to an article,
    and adds the attribution.
    """
    lines = content.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check if this is a blockquote line
        if line.strip().startswith('>'):
            # Collect all consecutive blockquote lines
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote_lines.append(lines[i])
                i += 1

            # Add the blockquote lines to result
            result.extend(quote_lines)

            # Check if the next non-empty line is already an attribution
            next_idx = i
            while next_idx < len(lines) and lines[next_idx].strip() == '':
                next_idx += 1

            has_attr = (
                next_idx < len(lines) and _has_attribution_line(lines[next_idx])
            )

            if not has_attr:
                # Extract the quote text from blockquote lines
                quote_text = ' '.join(
                    line.strip().lstrip('>').strip() for line in quote_lines
                )
                # Try to find which article this quote is from
                article = _match_quote_to_article(quote_text, articles)
                if article:
                    source = article.get("source", "Unknown")
                    url = article.get("url", "")
                    result.append(f'— [{source}]({url})')
                    result.append('')
        else:
            result.append(line)
            i += 1

    return '\n'.join(result)


def inject_article_links(content: str, articles: list[dict]) -> str:
    """Post-process digest content to add hyperlinks and quote attributions.

    Handles several patterns the model produces:
    - Blockquotes without attribution lines (adds — [Source](url))
    - Raw URLs in brackets: [https://example.com/article]
    - Raw URLs in parentheses after text: some claim (https://example.com)
    - Raw URLs on their own line or inline
    - Exact title mentions without links
    - [Title] without a following (URL)
    """
    # Build lookups
    url_to_title = {}
    title_to_url = {}
    for article in articles:
        title = article.get("title", "")
        url = article.get("url", "")
        if title and url:
            url_to_title[url] = title
            title_to_url[title] = url

    # 0. Ensure every blockquote has an attribution line with source link
    content = inject_quote_attributions(content, articles)

    # 1. Fix raw URLs in square brackets: [https://example.com/...] -> [Title](URL)
    def replace_bracketed_url(match):
        url = match.group(1)
        title = url_to_title.get(url)
        if title:
            return f'[{title}]({url})'
        # URL not in our articles, just make it a clickable link
        return f'[source]({url})'

    content = re.sub(r'\[(https?://[^\]]+)\](?!\()', replace_bracketed_url, content)

    # 2. Fix raw URLs in parentheses after text: "some text (https://...)"
    def replace_paren_url(match):
        preceding = match.group(1)
        url = match.group(2)
        title = url_to_title.get(url)
        if title:
            return f'[{preceding.strip()}]({url})'
        return f'[{preceding.strip()}]({url})'

    content = re.sub(r'([^(\n]{5,?})\s*\((https?://[^)]+)\)', replace_paren_url, content)

    # 3. Fix standalone URLs not already in markdown link syntax
    def replace_bare_url(match):
        url = match.group(0)
        title = url_to_title.get(url)
        if title:
            return f'[{title}]({url})'
        return f'[source]({url})'

    # Match URLs not preceded by ]( or "( which would indicate already-linked
    content = re.sub(r'(?<!\]\()(?<!\()(https?://\S+?)(?=[)\s,.]|$)', replace_bare_url, content)

    # 4. Fix [Title] without (URL) for exact title matches
    for title, url in sorted(title_to_url.items(), key=lambda x: len(x[0]), reverse=True):
        escaped_title = re.escape(title)
        pattern = re.compile(r'\[' + escaped_title + r'\](?!\()')
        content = pattern.sub(f'[{title}]({url})', content)

    # 5. Clean up any double-linked artifacts like [[Title](url)](url)
    content = re.sub(r'\[(\[[^\]]+\]\([^)]+\))\]\([^)]+\)', r'\1', content)

    # 6. Append a sources section with all articles linked
    sources_section = "\n\n---\n## Sources\n"
    by_source = {}
    for article in articles:
        source = article.get("source", "Unknown")
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        if url:
            by_source.setdefault(source, []).append((title, url))

    for source, items in sorted(by_source.items()):
        sources_section += f"\n**{source}**\n"
        for title, url in items:
            sources_section += f"- [{title}]({url})\n"

    content += sources_section

    return content


def generate_digest() -> dict:
    """
    Generate today's daily digest using score-aware article prioritization.

    1. Get scored articles from last 24 hours (ordered by composite score)
    2. Group by tier with proportional content budgets
    3. Compute dimensional profile for the day
    4. Build score-aware prompt with tiered article data
    5. Call Ollama with Abend digest prompt
    6. Post-process links and save to database

    Returns:
        dict with 'success', 'content', 'article_count', and optionally 'error'
    """
    result = {
        "success": False,
        "content": None,
        "article_count": 0,
        "error": None,
    }

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")
    temperature = float(settings.get("ollama_temperature", 0.3))

    # Get scored articles from last 24 hours
    yesterday = datetime.utcnow() - timedelta(hours=24)
    articles = get_articles_since_scored(yesterday)
    result["article_count"] = len(articles)

    if not articles:
        result["content"] = "No articles from the past 24 hours. The silence itself is notable."
        result["success"] = True
        today = datetime.utcnow().strftime("%Y-%m-%d")
        save_digest(today, result["content"], 0)
        return result

    # Build tiered article sections
    tiered = format_articles_tiered(articles)
    tier_counts = tiered["tier_counts"]
    included = tiered["included_articles"]

    # Build tier summary line
    tier_summary = (
        f"{len(articles)} articles total — "
        f"{tier_counts.get(1, 0)} critical (T1), "
        f"{tier_counts.get(2, 0)} high (T2), "
        f"{tier_counts.get(3, 0)} notable (T3), "
        f"{tier_counts.get(4, 0)} peripheral (T4), "
        f"{len(articles) - len(included)} excluded (T5/unscored)"
    )

    # Compute dimensional profile
    dimension_profile = compute_dimension_profile(articles)

    # Build the full prompt
    prompt = ABEND_DIGEST_PROMPT.format(
        tier_summary=tier_summary,
        dimension_profile=dimension_profile,
        t1_articles=tiered["t1"],
        t2_articles=tiered["t2"],
        t3_articles=tiered["t3"],
        t4_articles=tiered["t4"],
    )

    # Calculate required context window
    prompt_tokens_estimate = len(prompt) // 4
    response_tokens_buffer = 4000
    min_ctx_needed = prompt_tokens_estimate + response_tokens_buffer

    # Round up to nearest 4096 and ensure minimum of 32768
    num_ctx = max(32768, ((min_ctx_needed // 4096) + 1) * 4096)

    logger.info(
        f"Digest: {len(articles)} articles ({tier_counts.get(1, 0)} T1, "
        f"{tier_counts.get(2, 0)} T2, {tier_counts.get(3, 0)} T3, "
        f"{tier_counts.get(4, 0)} T4), ~{prompt_tokens_estimate} prompt tokens, "
        f"using num_ctx={num_ctx}"
    )

    # Generate the digest
    try:
        response = requests.post(
            OLLAMA_GENERATE_URL,
            json={
                "model": model,
                "prompt": "Generate today's briefing based on the scored and tiered articles provided.",
                "system": prompt,
                "stream": False,
                "options": {
                    "num_ctx": num_ctx,
                    "temperature": temperature,
                },
            },
            timeout=600,  # 10 minutes for substantive digest generation
        )
        response.raise_for_status()

        data = response.json()

        if "error" in data:
            logger.error(f"Ollama error: {data['error']}")
            result["error"] = data["error"]
            return result

        content = data.get("response", "").strip()
        if not content:
            result["error"] = "Model returned empty response"
            return result

        # Post-process: ensure article titles are hyperlinked
        content = inject_article_links(content, included)

        result["content"] = content
        result["success"] = True

        # Save to database
        today = datetime.utcnow().strftime("%Y-%m-%d")
        save_digest(today, content, len(articles))

        logger.info(f"Generated digest for {today} with {len(articles)} articles "
                     f"({len(included)} included after tier filtering)")
        return result

    except requests.exceptions.ConnectionError:
        error_msg = "Cannot connect to Ollama. Is it running?"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except requests.exceptions.Timeout:
        error_msg = "Request timed out while generating digest"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

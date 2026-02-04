"""Daily digest service for Sieve - Score-aware morning briefings in Abend voice."""

import json
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

Each article below has been scored across 7 analytical dimensions (0-3 each, 21 max) measuring relevance to power dynamics, sovereignty, attention extraction, and systemic design. Convergence points (marked [CONVERGENCE]) have 5+ dimensions scoring 2+, indicating intersecting themes.

**Today's intake:** {tier_summary}

**Dimensional profile:**
{dimension_profile}

---

## DEEP DIVE ARTICLES (Tier 1 + Tier 2) — Write ### subsections for ONLY these:
{t1_articles}
{t2_articles}

## PATTERN FUEL (Tier 3) — Do NOT give these ### subsections. Mention ONLY in Patterns & Signals:
{t3_articles}

## PERIPHERAL (Tier 4) — Do NOT write about these unless they connect to a T1/T2 pattern:
{t4_articles}

---

Write a substantive daily briefing (1500-2500 words). Use the scoring data to guide emphasis.

**CRITICAL STRUCTURAL RULE: Your output must contain EXACTLY FOUR sections, each appearing EXACTLY ONCE. Here is the EXACT structure to follow:**

```
## The Big Picture
(one paragraph synthesizing the day)

## Deep Dives
### "T1 Article Title" [score/21]
(5-8 sentence analysis)
### "T2 Article Title" [score/21]
(2-4 sentence analysis)

## Patterns & Signals
(bullet points referencing specific articles — weave in Tier 3 articles here by name)

## What Deserves Attention
(2-3 numbered items with concrete reasoning)
```

**The Big Picture** — One paragraph. Synthesize the day's most significant developments. Lead with Tier 1 stories. If a dimension is elevated today, call that out as a systemic signal.

**Deep Dives** — ONE section containing ### subsections for ONLY Tier 1 and Tier 2 articles. Do NOT create ### subsections for Tier 3 or Tier 4 articles.
- Tier 1 articles: 5-8 sentences. Reference which dimensions drive the score. Include a direct quote copied exactly from the article excerpt. Cite inline: [Article Title](URL).
- Tier 2 articles: 2-4 sentences. Note primary dimensions. Include quotes where available.

**Patterns & Signals** — ONE section, AFTER all deep dives. This is where Tier 3 articles belong — mention them BY NAME as supporting evidence for patterns you see across T1/T2 stories. Group by theme. Must contain observations SPECIFIC to today's articles. Do NOT use generic phrases like "a complex interplay between technological advancements." Instead: "Three stories — [Article A], [Article B], and [Article C] — show federal agencies testing compliance boundaries with different actors." Name the articles. Name the pattern. Be concrete.

**What Deserves Attention** — ONE section, at the end. 2-3 items worth the reader's time. Each item must name a specific article or connection, not restate a dimension label.

**Quote rules:**
- ONLY quote text that appears VERBATIM in the article excerpt provided above — copy-paste it exactly
- If an article excerpt has no clear quotable text, do NOT quote it — just analyze
- NEVER reuse a quote across multiple articles
- NEVER attach a quote from one article to a different article
- Format:

> "Exact text from the article excerpt."
— [Source Name](https://article-url-here)

**Formatting:**
- Use markdown: **bold**, bullet points, headers
- When referencing an article, ALWAYS hyperlink it: [Article Title](URL)
- Attribute source outlets by name ("according to TechCrunch", "as reported by TechDirt")
- Write in first person, be specific and analytical
- Do NOT use filler like "raises questions about systemic design" or "underscores the importance of data sovereignty" — say what the article ACTUALLY reveals"""


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


def strip_unverifiable_quotes(content: str, articles: list[dict]) -> str:
    """Remove blockquotes that can't be verified against article content.

    Catches two failure modes:
    1. Fabricated quotes — text the model invented that isn't in any article
    2. Placeholder text — "No direct quote found..." written as a blockquote

    Removes the blockquote lines and their attribution line (if present).
    """
    # Build combined article text for searching
    all_text = ""
    for article in articles:
        all_text += (
            " " + (article.get("content") or "")
            + " " + (article.get("summary") or "")
        )
    all_text_lower = all_text.lower()

    # Placeholder patterns the model outputs when it can't find a quote
    placeholder_patterns = [
        r"no direct quote",
        r"no quote found",
        r"no quotable text",
        r"no relevant quote",
        r"quote not available",
        r"no excerpt available",
    ]

    lines = content.split('\n')
    result = []
    i = 0
    removed = 0

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith('>'):
            # Collect the full blockquote
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote_lines.append(lines[i])
                i += 1

            # Join into full quote text
            quote_text = ' '.join(
                l.strip().lstrip('>').strip() for l in quote_lines
            )
            clean = quote_text.strip().strip('""\u201c\u201d\'').strip()

            # Check 1: Is it a placeholder?
            is_placeholder = any(
                re.search(p, clean, re.IGNORECASE) for p in placeholder_patterns
            )

            # Check 2: Can we find it in article content?
            is_verifiable = False
            if not is_placeholder and len(clean) >= 15:
                clean_lower = clean.lower()
                is_verifiable = clean_lower in all_text_lower
                if not is_verifiable:
                    # Try core substring (first 80 chars)
                    core = clean_lower[:80]
                    is_verifiable = len(core) >= 20 and core in all_text_lower

            if is_placeholder or (len(clean) >= 15 and not is_verifiable):
                removed += 1
                # Skip the attribution line too if present
                # Skip blank lines after quote
                while i < len(lines) and lines[i].strip() == '':
                    i += 1
                # Check if next line is an attribution (— [Source](url))
                if i < len(lines) and _has_attribution_line(lines[i]):
                    i += 1
                # Skip trailing blank line after attribution
                if i < len(lines) and lines[i].strip() == '':
                    i += 1
            else:
                # Quote is valid — keep it
                result.extend(quote_lines)
        else:
            result.append(line)
            i += 1

    if removed:
        logger.info(f"Stripped {removed} unverifiable quote(s) from digest")

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


REVIEW_REVISION_PROMPT = """You are Abend. You previously wrote a daily briefing, but a reviewer found problems. Fix ONLY the specific issues listed below. Keep everything else exactly as-is.

**ISSUES FOUND:**
{issues}

**YOUR PREVIOUS BRIEFING:**
{content}

**ORIGINAL ARTICLE DATA (for verifying quotes):**
{article_data}

**RULES FOR REVISION:**
1. Fix ONLY the issues listed above — do not rewrite sections that are fine
2. Maintain the EXACT same structure: ## The Big Picture, ## Deep Dives, ## Patterns & Signals, ## What Deserves Attention — each appearing EXACTLY ONCE
3. Quotes must be copied verbatim from the article excerpts — do not invent quotes
4. Every quote must be attributed to the article it actually came from
5. If you cannot find a real quote for an article, remove the quote and analyze without one
6. Patterns & Signals must make specific observations about today's articles, not generic statements
7. Do not add new sections or duplicate existing ones

Write the corrected briefing now."""


def _check_duplicate_sections(content: str) -> list[str]:
    """Check for section headers that appear more than once."""
    issues = []
    expected_singles = [
        "## The Big Picture",
        "## Deep Dives",
        "## Patterns & Signals",
        "## What Deserves Attention",
    ]
    for header in expected_singles:
        # Count occurrences (case-insensitive, flexible whitespace)
        pattern = re.compile(
            r'^' + re.escape(header), re.MULTILINE | re.IGNORECASE
        )
        matches = pattern.findall(content)
        if len(matches) > 1:
            issues.append(
                f"DUPLICATE SECTION: '{header}' appears {len(matches)} times "
                f"— it must appear exactly once. Consolidate all content under "
                f"a single '{header}' section."
            )
        elif len(matches) == 0:
            issues.append(
                f"MISSING SECTION: '{header}' is missing from the briefing. "
                f"Add this section."
            )
    return issues


def _extract_quote_blocks(content: str) -> list[dict]:
    """Extract all blockquote blocks from content, handling multiline quotes.

    Returns a list of dicts with:
        'text': the full quote text (all > lines joined)
        'end_pos': position in content after the quote block
        'attr_source': attribution source name (if found)
        'attr_url': attribution URL (if found)
    """
    lines = content.split('\n')
    blocks = []
    i = 0
    pos = 0  # track character position

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith('>'):
            # Collect consecutive blockquote lines
            quote_parts = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                text = lines[i].strip().lstrip('>').strip()
                if text:
                    quote_parts.append(text)
                pos += len(lines[i]) + 1
                i += 1

            full_quote = ' '.join(quote_parts)
            # Strip surrounding quotes
            full_quote = full_quote.strip().strip('""\u201c\u201d\'').strip()

            # Look for attribution on next non-empty line
            attr_source = None
            attr_url = None
            j = i
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines):
                attr_match = re.match(
                    r'^\s*[\u2014\u2013\-]{1,2}\s*\[([^\]]+)\]\(([^)]+)\)',
                    lines[j]
                )
                if attr_match:
                    attr_source = attr_match.group(1)
                    attr_url = attr_match.group(2)

            blocks.append({
                'text': full_quote,
                'end_pos': pos,
                'attr_source': attr_source,
                'attr_url': attr_url,
            })
        else:
            pos += len(line) + 1
            i += 1

    return blocks


def _check_quotes(content: str, articles: list[dict]) -> list[str]:
    """Check that blockquotes match actual article content."""
    issues = []

    # Build combined text from all articles for searching
    all_text = ""
    for article in articles:
        all_text += (
            " " + (article.get("content") or "")
            + " " + (article.get("summary") or "")
        )
    all_text = all_text.lower()

    # Extract full quote blocks (handles multiline quotes)
    blocks = _extract_quote_blocks(content)

    for block in blocks:
        quote = block['text']
        if len(quote) < 15:
            continue

        quote_lower = quote.lower()
        # Try exact match first, then a core substring
        found = quote_lower in all_text
        if not found:
            core = quote_lower[:80]
            found = len(core) >= 20 and core in all_text

        if not found:
            attr_info = ""
            if block['attr_source']:
                attr_info = f" (attributed to {block['attr_source']})"

            issues.append(
                f'FABRICATED QUOTE{attr_info}: The quote "{quote[:80]}..." '
                f"does not appear in any article excerpt. Remove this quote "
                f"or replace it with text actually found in the article."
            )

    return issues


def _check_quote_attribution(content: str, articles: list[dict]) -> list[str]:
    """Check that quotes are attributed to the correct article."""
    issues = []

    # Build URL-to-article lookup
    url_to_article = {}
    for article in articles:
        url = article.get("url", "")
        if url:
            url_to_article[url] = article

    # Extract full quote blocks with attributions
    blocks = _extract_quote_blocks(content)

    for block in blocks:
        quote = block['text']
        attr_source = block['attr_source']
        attr_url = block['attr_url']

        if len(quote) < 15 or not attr_url:
            continue

        # Find which article the URL points to
        attributed_article = url_to_article.get(attr_url)
        if not attributed_article:
            continue

        # Check if the quote is actually in that article's content
        attr_content = (
            (attributed_article.get("content") or "")
            + " "
            + (attributed_article.get("summary") or "")
        ).lower()
        quote_lower = quote.lower()

        in_attributed = quote_lower in attr_content
        if not in_attributed:
            core = quote_lower[:80]
            in_attributed = len(core) >= 20 and core in attr_content

        if not in_attributed:
            # Quote isn't in the attributed article — find where it actually is
            real_source = _match_quote_to_article(quote, articles)
            if real_source:
                real_title = real_source.get("title", "Unknown")
                issues.append(
                    f'WRONG ATTRIBUTION: The quote "{quote[:60]}..." is '
                    f'attributed to [{attr_source}]({attr_url}) but actually '
                    f'comes from "{real_title}". Fix the attribution.'
                )
            else:
                issues.append(
                    f'UNVERIFIABLE QUOTE: The quote "{quote[:60]}..." is '
                    f'attributed to [{attr_source}] but cannot be found in '
                    f'that article or any other. Remove this quote.'
                )

    return issues


def _check_boilerplate(content: str) -> list[str]:
    """Detect generic filler phrases that indicate lazy generation."""
    issues = []

    boilerplate_phrases = [
        r"raises questions about systemic design and incentive architecture",
        r"highlights the attention economy.s emphasis on spectacle",
        r"underscores the importance of data sovereignty",
        r"a complex interplay between technological advancements",
        r"user data may be used for targeted advertising",
        r"the consequences of poorly designed systems",
        r"a complex struggle for control over the narrative",
        r"the means of production",
    ]

    found = []
    for phrase in boilerplate_phrases:
        matches = re.findall(phrase, content, re.IGNORECASE)
        if len(matches) >= 2:
            found.append(phrase.replace(r".s", "'s"))

    if found:
        issues.append(
            f"BOILERPLATE REPETITION: The following generic phrases appear "
            f"multiple times and add no insight: {'; '.join(found)}. "
            f"Replace with specific analysis about what each article reveals."
        )

    return issues


def _check_reused_quotes(content: str) -> list[str]:
    """Detect the same quote text used more than once."""
    issues = []

    blocks = _extract_quote_blocks(content)
    seen_quotes = {}
    for block in blocks:
        quote = block['text']
        if len(quote) < 15:
            continue
        # Use full normalized text for comparison
        normalized = quote.lower()
        if normalized in seen_quotes:
            seen_quotes[normalized] += 1
        else:
            seen_quotes[normalized] = 1

    for quote_text, count in seen_quotes.items():
        if count > 1:
            issues.append(
                f'REUSED QUOTE: "{quote_text[:80]}..." appears {count} times. '
                f"Each article must have its own unique quote from its own "
                f"excerpt, or no quote at all."
            )

    return issues


def review_digest(content: str, articles: list[dict]) -> dict:
    """Review a generated digest for structural and content quality issues.

    Checks for:
    1. Duplicate section headers (## Deep Dives appearing multiple times)
    2. Fabricated quotes (not found in any article content)
    3. Wrong quote attributions (quote from article A attributed to B)
    4. Reused quotes (same quote pasted into multiple articles)
    5. Boilerplate/filler phrases repeated across articles

    Returns:
        dict with 'passed' (bool), 'issues' (list of str), 'issue_count' (int)
    """
    all_issues = []

    all_issues.extend(_check_duplicate_sections(content))
    all_issues.extend(_check_quotes(content, articles))
    all_issues.extend(_check_quote_attribution(content, articles))
    all_issues.extend(_check_reused_quotes(content))
    all_issues.extend(_check_boilerplate(content))

    return {
        "passed": len(all_issues) == 0,
        "issues": all_issues,
        "issue_count": len(all_issues),
    }


def _call_ollama_streaming(
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    num_ctx: int,
    num_predict: int = 4096,
) -> str:
    """Call Ollama with streaming and return the full response text.

    Raises on connection/timeout/API errors.
    """
    response = requests.post(
        OLLAMA_GENERATE_URL,
        json={
            "model": model,
            "prompt": user_prompt,
            "system": system_prompt,
            "stream": True,
            "options": {
                "num_ctx": num_ctx,
                "temperature": temperature,
                "num_predict": num_predict,
            },
        },
        timeout=(30, 600),
        stream=True,
    )
    response.raise_for_status()

    content_parts = []
    for line in response.iter_lines():
        if line:
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(f"Ollama error: {chunk['error']}")
            content_parts.append(chunk.get("response", ""))
            if chunk.get("done", False):
                break

    return "".join(content_parts).strip()


def _build_article_reference(articles: list[dict]) -> str:
    """Build a compact article reference for the revision prompt.

    Includes titles, URLs, and content excerpts so the model can verify quotes.
    """
    parts = []
    for article in articles:
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        source = article.get("source", "Unknown")
        content = article.get("content", "")
        # Truncate content for the revision prompt
        if content and len(content) > 1500:
            content = content[:1500] + "..."
        parts.append(
            f'### "{title}"\n'
            f"Source: {source}\n"
            f"URL: {url}\n"
            f"Excerpt: {content or 'No content'}\n"
        )
    return "\n".join(parts)


MAX_REVIEW_ITERATIONS = 3


def generate_digest() -> dict:
    """
    Generate today's daily digest using score-aware article prioritization.

    1. Get scored articles from last 24 hours (ordered by composite score)
    2. Group by tier with proportional content budgets
    3. Compute dimensional profile for the day
    4. Build score-aware prompt with tiered article data
    5. Call Ollama with Abend digest prompt
    6. Review output for quality issues (quotes, structure, boilerplate)
    7. If issues found, send back to model with fix instructions (up to 3 loops)
    8. Post-process links and save to database

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

    # Generate the digest using streaming to avoid read timeouts on large contexts
    try:
        content = _call_ollama_streaming(
            system_prompt=prompt,
            user_prompt="Generate today's briefing based on the scored and tiered articles provided.",
            model=model,
            temperature=temperature,
            num_ctx=num_ctx,
        )

        if not content:
            result["error"] = "Model returned empty response"
            return result

        # Review loop: only retry for STRUCTURAL issues (duplicate sections).
        # Quote problems are handled programmatically in post-processing.
        article_ref = None  # Built lazily on first revision needed
        for iteration in range(MAX_REVIEW_ITERATIONS):
            structural = _check_duplicate_sections(content)
            boilerplate = _check_boilerplate(content)
            retryable = structural + boilerplate

            if not retryable:
                logger.info(
                    f"Digest structure review passed on iteration "
                    f"{iteration + 1}"
                )
                break

            logger.warning(
                f"Digest review iteration {iteration + 1}: "
                f"{len(retryable)} structural issues: "
                + "; ".join(retryable[:3])
            )

            if iteration == MAX_REVIEW_ITERATIONS - 1:
                logger.warning(
                    f"Digest review: max iterations reached with "
                    f"{len(retryable)} remaining issues. "
                    f"Proceeding with best available output."
                )
                break

            # Build article reference lazily (only when revision is needed)
            if article_ref is None:
                article_ref = _build_article_reference(included)

            # Build revision prompt with specific issues
            issues_text = "\n".join(
                f"{i+1}. {issue}" for i, issue in enumerate(retryable)
            )
            revision_prompt = REVIEW_REVISION_PROMPT.format(
                issues=issues_text,
                content=content,
                article_data=article_ref,
            )

            # Revision needs larger context: original content + articles + instructions
            revision_tokens = len(revision_prompt) // 4
            revision_ctx = max(
                num_ctx, ((revision_tokens + 5000) // 4096 + 1) * 4096
            )

            content = _call_ollama_streaming(
                system_prompt=revision_prompt,
                user_prompt="Fix the issues listed above and output the corrected briefing.",
                model=model,
                temperature=temperature,
                num_ctx=revision_ctx,
            )

            if not content:
                logger.error("Revision returned empty response, using previous version")
                break

        # Post-process: strip unverifiable quotes, then fix links
        content = strip_unverifiable_quotes(content, included)
        content = inject_article_links(content, included)

        # Final review for logging (non-blocking)
        final_review = review_digest(content, included)
        if not final_review["passed"]:
            logger.info(
                f"Digest final review: {final_review['issue_count']} "
                f"remaining issues after post-processing: "
                + "; ".join(final_review["issues"][:3])
            )

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

    except RuntimeError as e:
        # From _call_ollama_streaming on Ollama API errors
        error_msg = str(e)
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

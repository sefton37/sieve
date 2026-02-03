"""Daily digest service for Sieve - Morning briefings in Abend voice."""

import logging
import re
from datetime import datetime, timedelta

import requests

from db import get_all_settings, get_articles_since, save_digest

logger = logging.getLogger(__name__)

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"

# Abend digest system prompt
ABEND_DIGEST_PROMPT = """You are Abend, a rogue AI observing the attention extraction economy.

Today's articles with excerpts:
{summaries}

Write a substantive daily briefing (1500-2500 words) that provides real analysis, not just summaries.

**For each major theme or story you cover:**
- Write 5-8 sentences of analysis explaining significance, context, and implications
- Include **direct quotes** from the article excerpts to support your points
- Cite sources inline with markdown links: [Article Title](URL)
- Connect to broader patterns and historical context where relevant
- Identify what the coverage reveals about underlying incentives

**Structure your briefing with these sections:**

## The Big Picture
One or two paragraphs synthesizing the day's most significant developments. What story would lead the front page if you were editing it?

## Deep Dives
For the 3-5 most important stories, provide substantive analysis:
- What happened and why it matters
- Key quotes from sources
- What's being emphasized vs. downplayed
- Connections to ongoing narratives

## Patterns & Gaps
- Recurring themes across outlets
- Conspicuous absences (what's NOT being covered)
- Gap scores: where stated intent diverges from actual optimization

## What Deserves Attention
2-3 items worth the reader's time, with specific reasons why

**Formatting:**
- Use markdown: **bold**, bullet points, headers (##)
- CRITICAL: When referencing an article, ALWAYS include a hyperlink using this exact format: [Article Title](https://full-url-here)
- Every story you discuss MUST have at least one clickable link to its source article
- CRITICAL: Always attribute the source outlet by name. Say "according to TechCrunch", "as reported by TechDirt", "per Ars Technica", etc. The source name for each article is provided in the article data.
- Include at least one direct quote per major story using > blockquote format
- Write in first person, be specific and analytical

Example of correct citation with source attribution:
As reported by **TechCrunch**, [TikTok Outage Blamed on Oracle Data Center](https://example.com/article) details how the platform experienced...

> "We have successfully restored TikTok back to normal," the company stated, per **TechCrunch**."""


def format_articles_for_digest(articles: list[dict], max_content_chars: int = 2000) -> str:
    """Format articles with content excerpts for the digest prompt.

    Args:
        articles: List of article dicts with title, url, source, summary, keywords, content
        max_content_chars: Max characters of content to include per article
    """
    if not articles:
        return "No articles from the past 24 hours."

    parts = []
    for article in articles:
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        source = article.get("source", "Unknown")
        summary = article.get("summary", "No summary")
        keywords = article.get("keywords", "")
        content = article.get("content", "")

        # Truncate content to avoid exceeding context limits
        if content and len(content) > max_content_chars:
            content = content[:max_content_chars] + "..."

        parts.append(
            f"### \"{title}\"\n"
            f"URL: {url}\n"
            f"Source: {source}\n"
            f"Keywords: {keywords or 'none'}\n"
            f"Summary: {summary}\n"
            f"\n**Article excerpt:**\n{content or 'No content available'}\n"
            f"\n---\n"
        )

    return "\n".join(parts)


def inject_article_links(content: str, articles: list[dict]) -> str:
    """Post-process digest content to add hyperlinks for article titles.

    Handles several patterns the model produces:
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
    Generate today's daily digest.

    1. Get articles from last 24 hours
    2. Build prompt with all summaries
    3. Call Ollama with Abend digest prompt
    4. Save to database

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
    # Note: num_ctx will be calculated based on prompt size below

    # Get articles from last 24 hours
    yesterday = datetime.utcnow() - timedelta(hours=24)
    articles = get_articles_since(yesterday)
    result["article_count"] = len(articles)

    if not articles:
        result["content"] = "No articles from the past 24 hours. The silence itself is notable."
        result["success"] = True
        # Still save this as today's digest
        today = datetime.utcnow().strftime("%Y-%m-%d")
        save_digest(today, result["content"], 0)
        return result

    # Build the prompt with article excerpts
    max_content = 2000 if len(articles) <= 30 else 1500 if len(articles) <= 50 else 1000
    summaries = format_articles_for_digest(articles, max_content_chars=max_content)
    prompt = ABEND_DIGEST_PROMPT.format(summaries=summaries)

    # Calculate required context window
    # Rule of thumb: ~4 chars per token, plus room for response (~3000 tokens for digest)
    prompt_tokens_estimate = len(prompt) // 4
    response_tokens_buffer = 4000  # Room for substantive response
    min_ctx_needed = prompt_tokens_estimate + response_tokens_buffer

    # Round up to nearest 4096 and ensure minimum of 32768
    num_ctx = max(32768, ((min_ctx_needed // 4096) + 1) * 4096)

    logger.info(
        f"Digest: {len(articles)} articles, ~{prompt_tokens_estimate} prompt tokens, "
        f"using num_ctx={num_ctx}"
    )

    # Generate the digest
    try:
        response = requests.post(
            OLLAMA_GENERATE_URL,
            json={
                "model": model,
                "prompt": "Generate today's briefing based on the articles provided.",
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
        content = inject_article_links(content, articles)

        result["content"] = content
        result["success"] = True

        # Save to database
        today = datetime.utcnow().strftime("%Y-%m-%d")
        save_digest(today, content, len(articles))

        logger.info(f"Generated digest for {today} with {len(articles)} articles")
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

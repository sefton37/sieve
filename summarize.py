"""Summarize service for Sieve - Ollama API integration for article summarization."""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

import requests

from db import (
    get_all_settings,
    get_articles_needing_context_resummarization,
    get_unsummarized_articles,
    search_by_embedding_with_date,
    update_summary,
    update_summary_with_context,
)

logger = logging.getLogger(__name__)

OLLAMA_API_URL = "http://localhost:11434/api/generate"
MAX_CONTENT_LENGTH = 6000

# System prompt that requests both summary and keywords
SYSTEM_PROMPT = """Analyze the following news article and provide:

1. SUMMARY: A paragraph (5-8 sentences) covering the key facts, context, and implications. Write directly without preamble.

2. KEYWORDS: 3-5 keywords or short phrases that capture the main topics, separated by commas.

Format your response exactly like this:
SUMMARY:
[Your summary paragraph here]

KEYWORDS:
[keyword1, keyword2, keyword3, keyword4, keyword5]"""


class ErrorType(Enum):
    """Categories of errors for fail-fast logic."""
    NONE = "none"
    CONNECTION = "connection"  # Can't reach Ollama - fail fast
    MODEL_NOT_FOUND = "model_not_found"  # Model doesn't exist - fail fast
    SERVER_ERROR = "server_error"  # 500 error, usually OOM - fail fast
    TIMEOUT = "timeout"  # Request timed out - might be transient
    API_ERROR = "api_error"  # Other Ollama error
    EMPTY_RESPONSE = "empty_response"  # Model returned nothing
    UNKNOWN = "unknown"


@dataclass
class SummarizeResult:
    """Result of a single summarization attempt."""
    success: bool
    summary: str | None
    keywords: list[str] = field(default_factory=list)
    error_type: ErrorType = ErrorType.NONE
    error_message: str | None = None


def parse_response(text: str) -> tuple[str | None, list[str]]:
    """
    Parse the model response to extract summary and keywords.

    Returns:
        (summary, keywords_list)
    """
    summary = None
    keywords = []

    # Try to find SUMMARY: section
    summary_match = re.search(r'SUMMARY:\s*\n?(.*?)(?=KEYWORDS:|$)', text, re.DOTALL | re.IGNORECASE)
    if summary_match:
        summary = summary_match.group(1).strip()

    # Try to find KEYWORDS: section
    keywords_match = re.search(r'KEYWORDS:\s*\n?(.*?)$', text, re.DOTALL | re.IGNORECASE)
    if keywords_match:
        keywords_text = keywords_match.group(1).strip()
        # Remove brackets if present
        keywords_text = keywords_text.strip('[]')
        # Split by comma and clean up
        keywords = [kw.strip().lower() for kw in keywords_text.split(',') if kw.strip()]
        # Limit to 5 keywords
        keywords = keywords[:5]

    # Fallback: if no SUMMARY: marker, use the whole text as summary
    if not summary and text.strip():
        # Take everything before KEYWORDS if it exists, otherwise whole text
        if 'KEYWORDS:' in text.upper():
            summary = text[:text.upper().index('KEYWORDS:')].strip()
        else:
            summary = text.strip()

    return summary, keywords


def _format_context_block(context_articles):
    """Format related articles into a context block for the summarization prompt.

    Args:
        context_articles: List of article dicts with title, source, pub_date, summary

    Returns:
        Formatted string to prepend to the prompt, or empty string if no context
    """
    if not context_articles:
        return ""

    lines = ["Related coverage from the past 30 days:"]
    for ctx in context_articles[:5]:
        date_str = ctx.get("pub_date", "unknown")[:10]
        source = ctx.get("source", "Unknown")
        ctx_title = ctx.get("title", "Untitled")
        summary = ctx.get("summary", "")
        lines.append(f'- [{date_str}] [{source}]: "{ctx_title}"')
        if summary:
            lines.append(f"  Summary: {summary}")

    lines.append("")
    lines.append(
        "If this article represents a development in an ongoing story, "
        "note how it relates to prior coverage. Note contradictions or "
        "new developments compared to earlier reporting."
    )

    return "\n".join(lines)


def summarize_article(title, content, settings=None, context_articles=None) -> SummarizeResult:
    """
    Summarize a single article using Ollama.

    Args:
        title: Article title
        content: Article content text
        settings: Optional settings dict (fetched if not provided)
        context_articles: Optional list of related article dicts for contextualized summarization

    Returns:
        SummarizeResult with success status, summary text, keywords, and error details
    """
    if settings is None:
        settings = get_all_settings()

    model = settings.get("ollama_model", "llama3.2")
    num_ctx = int(settings.get("ollama_num_ctx", 4096))
    temperature = float(settings.get("ollama_temperature", 0.3))

    # Use our structured prompt instead of the user's system prompt for keywords extraction
    system_prompt = SYSTEM_PROMPT

    # Truncate content to fit context window
    if content and len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH] + "..."

    # Build prompt with optional context block
    context_block = _format_context_block(context_articles)
    if context_block:
        prompt = f"Title: {title}\n\n{context_block}\n\nContent:\n{content}"
    else:
        prompt = f"Title: {title}\n\nContent:\n{content}"

    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={
                "model": model,
                "prompt": prompt,
                "system": system_prompt,
                "stream": False,
                "options": {
                    "num_ctx": num_ctx,
                    "temperature": temperature,
                },
            },
            timeout=120,
        )
        response.raise_for_status()

        result = response.json()

        # Ollama returns errors in JSON body with 200 status
        if "error" in result:
            error_msg = result["error"]
            logger.error(f"Ollama error: {error_msg}")

            # Detect model not found
            if "not found" in error_msg.lower():
                return SummarizeResult(
                    success=False, summary=None,
                    error_type=ErrorType.MODEL_NOT_FOUND,
                    error_message=error_msg
                )

            return SummarizeResult(
                success=False, summary=None,
                error_type=ErrorType.API_ERROR,
                error_message=error_msg
            )

        response_text = result.get("response", "").strip()
        if not response_text:
            logger.warning("Ollama returned empty response")
            return SummarizeResult(
                success=False, summary=None,
                error_type=ErrorType.EMPTY_RESPONSE,
                error_message="Model returned empty response"
            )

        # Parse the response to extract summary and keywords
        summary, keywords = parse_response(response_text)

        if not summary:
            logger.warning("Could not parse summary from response")
            return SummarizeResult(
                success=False, summary=None,
                error_type=ErrorType.EMPTY_RESPONSE,
                error_message="Could not parse summary from model response"
            )

        return SummarizeResult(
            success=True,
            summary=summary,
            keywords=keywords,
            error_type=ErrorType.NONE,
            error_message=None
        )

    except requests.exceptions.ConnectionError as e:
        error_msg = f"Cannot connect to Ollama at {OLLAMA_API_URL}. Is it running?"
        logger.error(error_msg)
        return SummarizeResult(
            success=False, summary=None,
            error_type=ErrorType.CONNECTION,
            error_message=error_msg
        )

    except requests.exceptions.Timeout:
        error_msg = f"Request timed out after 120s (model: {model})"
        logger.error(error_msg)
        return SummarizeResult(
            success=False, summary=None,
            error_type=ErrorType.TIMEOUT,
            error_message=error_msg
        )

    except requests.exceptions.HTTPError as e:
        # Capture response body for HTTP errors (like 500)
        error_msg = f"HTTP {e.response.status_code}"
        error_type = ErrorType.API_ERROR

        if e.response.status_code == 500:
            error_msg = f"Ollama server error (500) - likely num_ctx={num_ctx} is too large for available memory. Try reducing context window."
            error_type = ErrorType.SERVER_ERROR

        logger.error(f"Request failed: {error_msg}")
        return SummarizeResult(
            success=False, summary=None,
            error_type=error_type,
            error_message=error_msg
        )

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        return SummarizeResult(
            success=False, summary=None,
            error_type=ErrorType.API_ERROR,
            error_message=error_msg
        )

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        return SummarizeResult(
            success=False, summary=None,
            error_type=ErrorType.UNKNOWN,
            error_message=error_msg
        )


# Errors that should stop the batch immediately
FATAL_ERRORS = {ErrorType.CONNECTION, ErrorType.MODEL_NOT_FOUND, ErrorType.SERVER_ERROR}

# Number of consecutive failures before stopping (for non-fatal errors)
MAX_CONSECUTIVE_FAILURES = 3


def _fetch_context_for_article(article_id, title, settings):
    """Fetch related articles for contextualized summarization.

    Embeds the article title and searches for similar recently-published articles
    that already have embeddings (from previous pipeline runs).

    Args:
        article_id: ID of the article being summarized (excluded from results)
        title: Article title to use as embedding query
        settings: Settings dict with embed model config

    Returns:
        Tuple of (context_articles list, context_ids list), or ([], []) on failure
    """
    from embed import embed_text, embedding_to_blob

    try:
        er = embed_text(title, settings)
        if not er.success:
            logger.warning(f"Context search failed for article {article_id}: could not embed title")
            return [], []

        query_blob = embedding_to_blob(er.embedding)
        context_articles = search_by_embedding_with_date(
            query_blob, limit=5, days=30, exclude_id=article_id
        )
        context_ids = [a["id"] for a in context_articles]
        return context_articles, context_ids

    except Exception as e:
        logger.warning(f"Context search failed for article {article_id}: {e}")
        return [], []


def summarize_batch(on_progress=None):
    """
    Process all unsummarized articles with fail-fast on systemic errors.

    For each article, searches for related previously-embedded articles to
    provide context for a more informed summary. If context search fails,
    proceeds without context.

    Stops immediately on connection errors or model not found.
    Stops after MAX_CONSECUTIVE_FAILURES consecutive failures for other errors.

    Returns:
        dict with summarized, failed, errors, last_error, stopped_early
    """
    result = {
        "summarized": 0,
        "failed": 0,
        "errors": [],
        "last_error": None,
        "stopped_early": False,
    }

    articles = get_unsummarized_articles()
    total = len(articles)

    if total == 0:
        logger.info("No unsummarized articles found")
        return result

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")

    logger.info(f"Starting batch summarization: {total} articles with model '{model}'")

    consecutive_failures = 0

    for i, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]
        content = article.get("content", "")

        logger.info(f"[{i + 1}/{total}] Summarizing: {title[:60]}...")

        # Fetch related articles for context (non-fatal if it fails)
        context_articles, context_ids = _fetch_context_for_article(
            article_id, title, settings
        )
        if context_articles:
            logger.info(f"[{i + 1}/{total}] Found {len(context_articles)} context articles")

        sr = summarize_article(title, content, settings, context_articles=context_articles)

        if sr.success:
            update_summary_with_context(article_id, sr.summary, sr.keywords, context_ids)
            result["summarized"] += 1
            consecutive_failures = 0
            logger.info(f"[{i + 1}/{total}] Success - {len(sr.keywords)} keywords extracted")
        else:
            result["failed"] += 1
            consecutive_failures += 1

            error_msg = f"Article {article_id}: {sr.error_message}"
            result["errors"].append(error_msg)
            result["last_error"] = sr.error_message

            logger.warning(f"[{i + 1}/{total}] Failed: {sr.error_message}")

            # Check for fatal errors - stop immediately
            if sr.error_type in FATAL_ERRORS:
                result["stopped_early"] = True
                result["last_error"] = f"FATAL: {sr.error_message} - stopping batch"
                logger.error(f"Fatal error detected, stopping batch: {sr.error_message}")
                break

            # Check for too many consecutive failures
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result["stopped_early"] = True
                result["last_error"] = f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures. Last: {sr.error_message}"
                logger.error("Too many consecutive failures, stopping batch")
                break

        # Progress callback
        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(
        f"Batch complete: {result['summarized']} summarized, "
        f"{result['failed']} failed, stopped_early={result['stopped_early']}"
    )

    return result


def resummarize_with_context_batch(on_progress=None):
    """
    Re-summarize articles that were previously summarized without context.

    Backfill function: finds articles that have been summarized and embedded
    but lack context_article_ids, and re-summarizes them with related article
    context from the existing embedded corpus.

    Same fail-fast pattern as summarize_batch.

    Returns:
        dict with resummarized, failed, errors, last_error, stopped_early
    """
    result = {
        "resummarized": 0,
        "failed": 0,
        "errors": [],
        "last_error": None,
        "stopped_early": False,
    }

    articles = get_articles_needing_context_resummarization()
    total = len(articles)

    if total == 0:
        logger.info("No articles need context re-summarization")
        return result

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")

    logger.info(f"Starting context re-summarization: {total} articles with model '{model}'")

    consecutive_failures = 0

    for i, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]
        content = article.get("content", "")

        logger.info(f"[{i + 1}/{total}] Re-summarizing: {title[:60]}...")

        # Fetch related articles for context
        context_articles, context_ids = _fetch_context_for_article(
            article_id, title, settings
        )
        if context_articles:
            logger.info(f"[{i + 1}/{total}] Found {len(context_articles)} context articles")

        sr = summarize_article(title, content, settings, context_articles=context_articles)

        if sr.success:
            update_summary_with_context(article_id, sr.summary, sr.keywords, context_ids)
            result["resummarized"] += 1
            consecutive_failures = 0
            logger.info(f"[{i + 1}/{total}] Success - {len(sr.keywords)} keywords extracted")
        else:
            result["failed"] += 1
            consecutive_failures += 1

            error_msg = f"Article {article_id}: {sr.error_message}"
            result["errors"].append(error_msg)
            result["last_error"] = sr.error_message

            logger.warning(f"[{i + 1}/{total}] Failed: {sr.error_message}")

            # Check for fatal errors - stop immediately
            if sr.error_type in FATAL_ERRORS:
                result["stopped_early"] = True
                result["last_error"] = f"FATAL: {sr.error_message} - stopping batch"
                logger.error(f"Fatal error detected, stopping batch: {sr.error_message}")
                break

            # Check for too many consecutive failures
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result["stopped_early"] = True
                result["last_error"] = f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures. Last: {sr.error_message}"
                logger.error("Too many consecutive failures, stopping batch")
                break

        # Progress callback
        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(
        f"Re-summarization complete: {result['resummarized']} resummarized, "
        f"{result['failed']} failed, stopped_early={result['stopped_early']}"
    )

    return result

"""Summarize service for Sieve - Ollama API integration for article summarization."""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

import requests

from db import get_all_settings, get_unsummarized_articles, update_summary

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


def summarize_article(title, content, settings=None) -> SummarizeResult:
    """
    Summarize a single article using Ollama.

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


def summarize_batch(on_progress=None):
    """
    Process all unsummarized articles with fail-fast on systemic errors.

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

        sr = summarize_article(title, content, settings)

        if sr.success:
            update_summary(article_id, sr.summary, sr.keywords)
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
                logger.error(f"Too many consecutive failures, stopping batch")
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

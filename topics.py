"""Topic classification service for Sieve - Fixed taxonomy classification via Ollama."""

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum

import requests

from db import get_all_settings, get_unclassified_articles, update_topics

logger = logging.getLogger(__name__)

OLLAMA_API_URL = "http://localhost:11434/api/generate"
MAX_CONTENT_LENGTH = 6000

# Fixed topic taxonomy
TOPIC_TAXONOMY = [
    "ai_regulation", "ai_capabilities", "surveillance", "platform_dynamics",
    "labor_displacement", "consolidation", "privacy", "content_moderation",
    "startup_funding", "layoffs", "acquisitions", "open_source",
    "hardware", "infrastructure", "cybersecurity", "crypto", "other",
]

TOPIC_SYSTEM_PROMPT = """Classify this article into 1-3 topics from this fixed taxonomy:

ai_regulation, ai_capabilities, surveillance, platform_dynamics,
labor_displacement, consolidation, privacy, content_moderation,
startup_funding, layoffs, acquisitions, open_source,
hardware, infrastructure, cybersecurity, crypto, other

Return ONLY a JSON object:
{"topics": ["topic1", "topic2"]}"""


class ErrorType(Enum):
    """Categories of errors for fail-fast logic."""
    NONE = "none"
    CONNECTION = "connection"
    MODEL_NOT_FOUND = "model_not_found"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    API_ERROR = "api_error"
    EMPTY_RESPONSE = "empty_response"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"


@dataclass
class TopicResult:
    """Result of a single topic classification attempt."""
    success: bool
    topics: list[str] | None = None
    error_type: ErrorType = ErrorType.NONE
    error_message: str | None = None


def parse_topic_response(text: str) -> list[str] | None:
    """
    Parse the model's JSON response to extract topics.

    Validates each topic against the taxonomy. Unknown topics are mapped to "other".

    Returns:
        List of valid topic strings, or None on complete parse failure
    """
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    raw_topics = data.get("topics", [])
    if not isinstance(raw_topics, list):
        return None

    # Validate against taxonomy
    taxonomy_set = set(TOPIC_TAXONOMY)
    validated = []
    for t in raw_topics:
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if t in taxonomy_set:
            validated.append(t)
        elif t:
            # Unknown topic — map to "other"
            if "other" not in validated:
                validated.append("other")

    # Limit to 3 topics
    validated = validated[:3]

    if not validated:
        return None

    return validated


def classify_article(title, content, summary, keywords, settings=None) -> TopicResult:
    """
    Classify a single article into topics from the fixed taxonomy.

    Args:
        title: Article title
        content: Article content text
        summary: Article summary
        keywords: Article keywords (comma-separated string)
        settings: Optional settings dict (fetched if not provided)

    Returns:
        TopicResult with success status, topics list, and error details
    """
    if settings is None:
        settings = get_all_settings()

    model = settings.get("ollama_model", "llama3.2")
    num_ctx = int(settings.get("ollama_num_ctx", 4096))
    temperature = float(settings.get("ollama_temperature", 0.3))

    # Truncate content to fit context window
    if content and len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH] + "..."

    keywords_str = keywords if keywords else "none"

    prompt = f"""Title: {title}

Summary: {summary}

Keywords: {keywords_str}

Content:
{content}"""

    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={
                "model": model,
                "prompt": prompt,
                "system": TOPIC_SYSTEM_PROMPT,
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

        if "error" in result:
            error_msg = result["error"]
            logger.error(f"Ollama error: {error_msg}")

            if "not found" in error_msg.lower():
                return TopicResult(
                    success=False,
                    error_type=ErrorType.MODEL_NOT_FOUND,
                    error_message=error_msg,
                )

            return TopicResult(
                success=False,
                error_type=ErrorType.API_ERROR,
                error_message=error_msg,
            )

        response_text = result.get("response", "").strip()
        if not response_text:
            return TopicResult(
                success=False,
                error_type=ErrorType.EMPTY_RESPONSE,
                error_message="Model returned empty response",
            )

        topics = parse_topic_response(response_text)

        if topics is None:
            logger.warning(f"Could not parse topics from response: {response_text[:200]}")
            return TopicResult(
                success=False,
                error_type=ErrorType.PARSE_ERROR,
                error_message=f"Could not parse topics from model response: {response_text[:200]}",
            )

        return TopicResult(
            success=True,
            topics=topics,
        )

    except requests.exceptions.ConnectionError:
        error_msg = f"Cannot connect to Ollama at {OLLAMA_API_URL}. Is it running?"
        logger.error(error_msg)
        return TopicResult(
            success=False,
            error_type=ErrorType.CONNECTION,
            error_message=error_msg,
        )

    except requests.exceptions.Timeout:
        error_msg = f"Request timed out after 120s (model: {model})"
        logger.error(error_msg)
        return TopicResult(
            success=False,
            error_type=ErrorType.TIMEOUT,
            error_message=error_msg,
        )

    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP {e.response.status_code}"
        error_type = ErrorType.API_ERROR

        if e.response.status_code == 500:
            error_msg = f"Ollama server error (500) - likely num_ctx={num_ctx} is too large for available memory. Try reducing context window."
            error_type = ErrorType.SERVER_ERROR

        logger.error(f"Request failed: {error_msg}")
        return TopicResult(
            success=False,
            error_type=error_type,
            error_message=error_msg,
        )

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        return TopicResult(
            success=False,
            error_type=ErrorType.API_ERROR,
            error_message=error_msg,
        )

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        return TopicResult(
            success=False,
            error_type=ErrorType.UNKNOWN,
            error_message=error_msg,
        )


# Errors that should stop the batch immediately
FATAL_ERRORS = {ErrorType.CONNECTION, ErrorType.MODEL_NOT_FOUND, ErrorType.SERVER_ERROR}

# Number of consecutive failures before stopping (for non-fatal errors)
MAX_CONSECUTIVE_FAILURES = 3


def classify_batch(on_progress=None):
    """
    Classify all summarized but unclassified articles into topics.

    Same fail-fast pattern as score_batch.

    Returns:
        dict with classified, failed, errors, last_error, stopped_early
    """
    result = {
        "classified": 0,
        "failed": 0,
        "errors": [],
        "last_error": None,
        "stopped_early": False,
    }

    articles = get_unclassified_articles()
    total = len(articles)

    if total == 0:
        logger.info("No articles need topic classification")
        return result

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")

    logger.info(f"Starting batch topic classification: {total} articles with model '{model}'")

    consecutive_failures = 0

    for i, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]
        content = article.get("content", "")
        summary = article.get("summary", "")
        keywords = article.get("keywords", "")

        logger.info(f"[{i + 1}/{total}] Classifying topics: {title[:60]}...")

        tr = classify_article(title, content, summary, keywords, settings)

        if tr.success:
            topics_str = ",".join(tr.topics)
            update_topics(article_id, topics_str)
            result["classified"] += 1
            consecutive_failures = 0

            logger.info(f"[{i + 1}/{total}] Topics: {topics_str}")
        else:
            result["failed"] += 1
            consecutive_failures += 1

            error_msg = f"Article {article_id}: {tr.error_message}"
            result["errors"].append(error_msg)
            result["last_error"] = tr.error_message

            logger.warning(f"[{i + 1}/{total}] Failed: {tr.error_message}")

            if tr.error_type in FATAL_ERRORS:
                result["stopped_early"] = True
                result["last_error"] = f"FATAL: {tr.error_message} - stopping batch"
                logger.error(f"Fatal error detected, stopping batch: {tr.error_message}")
                break

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result["stopped_early"] = True
                result["last_error"] = (
                    f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures. "
                    f"Last: {tr.error_message}"
                )
                logger.error("Too many consecutive failures, stopping batch")
                break

        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(
        f"Batch topic classification complete: {result['classified']} classified, "
        f"{result['failed']} failed, stopped_early={result['stopped_early']}"
    )

    return result

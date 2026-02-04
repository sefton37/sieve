"""Entity extraction service for Sieve - Named entity extraction via Ollama."""

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum

import requests

from db import get_all_settings, get_unextracted_articles, update_entities

logger = logging.getLogger(__name__)

OLLAMA_API_URL = "http://localhost:11434/api/generate"
MAX_CONTENT_LENGTH = 6000

# Entity categories we extract
ENTITY_CATEGORIES = ["companies", "people", "products", "legislation", "other"]

ENTITY_SYSTEM_PROMPT = """Extract named entities from this article.

Return ONLY a JSON object:
{
  "companies": ["Company A", "Company B"],
  "people": ["Person Name"],
  "products": ["Product Name"],
  "legislation": ["Bill Name", "Regulation"],
  "other": ["Notable Entity"]
}

Rules:
- Only include entities explicitly mentioned. No inference.
- Use full official names where possible.
- Empty array for categories with no entities.
- Max 10 per category."""


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
class EntityResult:
    """Result of a single entity extraction attempt."""
    success: bool
    entities: dict | None = None
    error_type: ErrorType = ErrorType.NONE
    error_message: str | None = None


def parse_entity_response(text: str) -> dict | None:
    """
    Parse the model's JSON response to extract entities.

    Returns:
        Dict with entity categories, or None on failure
    """
    # Greedy match to handle nested arrays inside the JSON object
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    # Validate and normalize each category
    entities = {}
    for category in ENTITY_CATEGORIES:
        values = data.get(category, [])
        if not isinstance(values, list):
            values = []
        # Normalize: strip whitespace, remove empties, limit to 10
        cleaned = []
        for v in values[:10]:
            if isinstance(v, str):
                v = v.strip()
                if v:
                    cleaned.append(v)
        entities[category] = cleaned

    # Only return if at least one entity was found
    total = sum(len(v) for v in entities.values())
    if total == 0:
        # Valid but empty — still store it so we don't re-process
        return entities

    return entities


def extract_entities(title, content, summary, settings=None) -> EntityResult:
    """
    Extract named entities from a single article using Ollama.

    Args:
        title: Article title
        content: Article content text
        summary: Article summary
        settings: Optional settings dict (fetched if not provided)

    Returns:
        EntityResult with success status, entities dict, and error details
    """
    if settings is None:
        settings = get_all_settings()

    model = settings.get("ollama_model", "llama3.2")
    num_ctx = int(settings.get("ollama_num_ctx", 4096))
    temperature = float(settings.get("ollama_temperature", 0.3))

    # Truncate content to fit context window
    if content and len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH] + "..."

    prompt = f"""Title: {title}

Summary: {summary}

Content:
{content}"""

    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={
                "model": model,
                "prompt": prompt,
                "system": ENTITY_SYSTEM_PROMPT,
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
                return EntityResult(
                    success=False,
                    error_type=ErrorType.MODEL_NOT_FOUND,
                    error_message=error_msg,
                )

            return EntityResult(
                success=False,
                error_type=ErrorType.API_ERROR,
                error_message=error_msg,
            )

        response_text = result.get("response", "").strip()
        if not response_text:
            return EntityResult(
                success=False,
                error_type=ErrorType.EMPTY_RESPONSE,
                error_message="Model returned empty response",
            )

        entities = parse_entity_response(response_text)

        if entities is None:
            logger.warning(f"Could not parse entities from response: {response_text[:200]}")
            return EntityResult(
                success=False,
                error_type=ErrorType.PARSE_ERROR,
                error_message=f"Could not parse JSON entities from model response: {response_text[:200]}",
            )

        return EntityResult(
            success=True,
            entities=entities,
        )

    except requests.exceptions.ConnectionError:
        error_msg = f"Cannot connect to Ollama at {OLLAMA_API_URL}. Is it running?"
        logger.error(error_msg)
        return EntityResult(
            success=False,
            error_type=ErrorType.CONNECTION,
            error_message=error_msg,
        )

    except requests.exceptions.Timeout:
        error_msg = f"Request timed out after 120s (model: {model})"
        logger.error(error_msg)
        return EntityResult(
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
        return EntityResult(
            success=False,
            error_type=error_type,
            error_message=error_msg,
        )

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        return EntityResult(
            success=False,
            error_type=ErrorType.API_ERROR,
            error_message=error_msg,
        )

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        return EntityResult(
            success=False,
            error_type=ErrorType.UNKNOWN,
            error_message=error_msg,
        )


# Errors that should stop the batch immediately
FATAL_ERRORS = {ErrorType.CONNECTION, ErrorType.MODEL_NOT_FOUND, ErrorType.SERVER_ERROR}

# Number of consecutive failures before stopping (for non-fatal errors)
MAX_CONSECUTIVE_FAILURES = 3


def extract_batch(on_progress=None):
    """
    Extract entities from all summarized but unextracted articles.

    Same fail-fast pattern as score_batch.

    Returns:
        dict with extracted, failed, errors, last_error, stopped_early
    """
    result = {
        "extracted": 0,
        "failed": 0,
        "errors": [],
        "last_error": None,
        "stopped_early": False,
    }

    articles = get_unextracted_articles()
    total = len(articles)

    if total == 0:
        logger.info("No articles need entity extraction")
        return result

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")

    logger.info(f"Starting batch entity extraction: {total} articles with model '{model}'")

    consecutive_failures = 0

    for i, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]
        content = article.get("content", "")
        summary = article.get("summary", "")

        logger.info(f"[{i + 1}/{total}] Extracting entities: {title[:60]}...")

        er = extract_entities(title, content, summary, settings)

        if er.success:
            entities_json = json.dumps(er.entities)
            update_entities(article_id, entities_json)
            result["extracted"] += 1
            consecutive_failures = 0

            entity_count = sum(len(v) for v in er.entities.values())
            logger.info(f"[{i + 1}/{total}] Extracted {entity_count} entities")
        else:
            result["failed"] += 1
            consecutive_failures += 1

            error_msg = f"Article {article_id}: {er.error_message}"
            result["errors"].append(error_msg)
            result["last_error"] = er.error_message

            logger.warning(f"[{i + 1}/{total}] Failed: {er.error_message}")

            if er.error_type in FATAL_ERRORS:
                result["stopped_early"] = True
                result["last_error"] = f"FATAL: {er.error_message} - stopping batch"
                logger.error(f"Fatal error detected, stopping batch: {er.error_message}")
                break

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result["stopped_early"] = True
                result["last_error"] = (
                    f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures. "
                    f"Last: {er.error_message}"
                )
                logger.error("Too many consecutive failures, stopping batch")
                break

        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(
        f"Batch entity extraction complete: {result['extracted']} extracted, "
        f"{result['failed']} failed, stopped_early={result['stopped_early']}"
    )

    return result

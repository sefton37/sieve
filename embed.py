"""Embedding service for Sieve - Ollama API integration for semantic search."""

import logging
import struct
from dataclasses import dataclass
from enum import Enum

import requests

from db import get_all_settings, get_setting, get_unembedded_articles, update_embedding

logger = logging.getLogger(__name__)

OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768  # nomic-embed-text produces 768-dimensional embeddings


class ErrorType(Enum):
    """Categories of errors for fail-fast logic."""
    NONE = "none"
    CONNECTION = "connection"  # Can't reach Ollama - fail fast
    MODEL_NOT_FOUND = "model_not_found"  # Model doesn't exist - fail fast
    SERVER_ERROR = "server_error"  # 500 error - fail fast
    TIMEOUT = "timeout"  # Request timed out - might be transient
    API_ERROR = "api_error"  # Other Ollama error
    EMPTY_RESPONSE = "empty_response"  # Model returned nothing
    UNKNOWN = "unknown"


@dataclass
class EmbedResult:
    """Result of a single embedding attempt."""
    success: bool
    embedding: list[float] | None
    error_type: ErrorType = ErrorType.NONE
    error_message: str | None = None


def embedding_to_blob(embedding: list[float]) -> bytes:
    """Convert list of floats to binary blob for sqlite-vec storage."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def blob_to_embedding(blob: bytes) -> list[float]:
    """Convert binary blob back to list of floats."""
    count = len(blob) // 4  # 4 bytes per float
    return list(struct.unpack(f'{count}f', blob))


def embed_text(text: str, settings: dict | None = None) -> EmbedResult:
    """
    Generate embedding for a single text using Ollama.

    Args:
        text: Text to embed
        settings: Optional settings dict (fetched if not provided)

    Returns:
        EmbedResult with success status, embedding vector, and error details
    """
    if settings is None:
        settings = get_all_settings()

    model = settings.get("ollama_embed_model", DEFAULT_EMBED_MODEL)

    try:
        response = requests.post(
            OLLAMA_EMBED_URL,
            json={
                "model": model,
                "input": text,
            },
            timeout=60,
        )
        response.raise_for_status()

        result = response.json()

        # Ollama returns errors in JSON body with 200 status
        if "error" in result:
            error_msg = result["error"]
            logger.error(f"Ollama embed error: {error_msg}")

            # Detect model not found
            if "not found" in error_msg.lower():
                return EmbedResult(
                    success=False, embedding=None,
                    error_type=ErrorType.MODEL_NOT_FOUND,
                    error_message=error_msg
                )

            return EmbedResult(
                success=False, embedding=None,
                error_type=ErrorType.API_ERROR,
                error_message=error_msg
            )

        # Ollama returns {"embeddings": [[768 floats]]}
        embeddings = result.get("embeddings", [])
        if not embeddings or not embeddings[0]:
            logger.warning("Ollama returned empty embedding")
            return EmbedResult(
                success=False, embedding=None,
                error_type=ErrorType.EMPTY_RESPONSE,
                error_message="Model returned empty embedding"
            )

        embedding = embeddings[0]
        return EmbedResult(
            success=True,
            embedding=embedding,
            error_type=ErrorType.NONE,
            error_message=None
        )

    except requests.exceptions.ConnectionError:
        error_msg = f"Cannot connect to Ollama at {OLLAMA_EMBED_URL}. Is it running?"
        logger.error(error_msg)
        return EmbedResult(
            success=False, embedding=None,
            error_type=ErrorType.CONNECTION,
            error_message=error_msg
        )

    except requests.exceptions.Timeout:
        error_msg = f"Embed request timed out after 60s (model: {model})"
        logger.error(error_msg)
        return EmbedResult(
            success=False, embedding=None,
            error_type=ErrorType.TIMEOUT,
            error_message=error_msg
        )

    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP {e.response.status_code}"
        error_type = ErrorType.API_ERROR

        if e.response.status_code == 500:
            error_msg = "Ollama server error (500)"
            error_type = ErrorType.SERVER_ERROR

        logger.error(f"Embed request failed: {error_msg}")
        return EmbedResult(
            success=False, embedding=None,
            error_type=error_type,
            error_message=error_msg
        )

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        return EmbedResult(
            success=False, embedding=None,
            error_type=ErrorType.API_ERROR,
            error_message=error_msg
        )

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        return EmbedResult(
            success=False, embedding=None,
            error_type=ErrorType.UNKNOWN,
            error_message=error_msg
        )


def embed_article(article: dict, settings: dict | None = None) -> EmbedResult:
    """
    Generate embedding for an article (title + summary).

    Args:
        article: Dict with 'title' and 'summary' keys
        settings: Optional settings dict

    Returns:
        EmbedResult with embedding vector
    """
    title = article.get("title", "")
    summary = article.get("summary", "")

    # Combine title and summary for embedding
    text = f"{title}\n\n{summary}"

    return embed_text(text, settings)


# Errors that should stop the batch immediately
FATAL_ERRORS = {ErrorType.CONNECTION, ErrorType.MODEL_NOT_FOUND, ErrorType.SERVER_ERROR}

# Number of consecutive failures before stopping (for non-fatal errors)
MAX_CONSECUTIVE_FAILURES = 3


def embed_batch(on_progress=None):
    """
    Process all unembedded articles with fail-fast on systemic errors.

    Stops immediately on connection errors or model not found.
    Stops after MAX_CONSECUTIVE_FAILURES consecutive failures for other errors.

    Args:
        on_progress: Optional callback(current, total)

    Returns:
        dict with embedded, failed, errors, last_error, stopped_early
    """
    result = {
        "embedded": 0,
        "failed": 0,
        "errors": [],
        "last_error": None,
        "stopped_early": False,
    }

    articles = get_unembedded_articles()
    total = len(articles)

    if total == 0:
        logger.info("No unembedded articles found")
        return result

    settings = get_all_settings()
    model = settings.get("ollama_embed_model", DEFAULT_EMBED_MODEL)

    logger.info(f"Starting batch embedding: {total} articles with model '{model}'")

    consecutive_failures = 0

    for i, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]

        logger.info(f"[{i + 1}/{total}] Embedding: {title[:60]}...")

        er = embed_article(article, settings)

        if er.success:
            # Store embedding as blob
            embedding_blob = embedding_to_blob(er.embedding)
            update_embedding(article_id, embedding_blob)
            result["embedded"] += 1
            consecutive_failures = 0
            logger.info(f"[{i + 1}/{total}] Success - {len(er.embedding)} dimensions")
        else:
            result["failed"] += 1
            consecutive_failures += 1

            error_msg = f"Article {article_id}: {er.error_message}"
            result["errors"].append(error_msg)
            result["last_error"] = er.error_message

            logger.warning(f"[{i + 1}/{total}] Failed: {er.error_message}")

            # Check for fatal errors - stop immediately
            if er.error_type in FATAL_ERRORS:
                result["stopped_early"] = True
                result["last_error"] = f"FATAL: {er.error_message} - stopping batch"
                logger.error(f"Fatal error detected, stopping batch: {er.error_message}")
                break

            # Check for too many consecutive failures
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result["stopped_early"] = True
                result["last_error"] = f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures. Last: {er.error_message}"
                logger.error("Too many consecutive failures, stopping batch")
                break

        # Progress callback
        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(
        f"Batch complete: {result['embedded']} embedded, "
        f"{result['failed']} failed, stopped_early={result['stopped_early']}"
    )

    return result

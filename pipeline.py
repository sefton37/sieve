"""Pipeline orchestrator for Sieve - Hourly processing flow."""

import logging
from datetime import datetime

from db import get_setting
from embed import embed_batch
from ingest import compress_jsonl, ingest_articles
from score import score_batch
from summarize import summarize_batch

logger = logging.getLogger(__name__)


def run_pipeline(on_progress=None):
    """
    Execute the full hourly pipeline:
    1. Ingest new articles from JSONL → DB
    2. Compress JSONL (dedupe by URL, keep most recent)
    3. Summarize unsummarized articles + extract keywords
    4. Embed summarized articles for semantic search
    5. Score articles for relevance across 7 dimensions

    Args:
        on_progress: Optional callback(stage, current, total, message)
            stage: "ingest", "compress", "summarize", "embed", "score"

    Returns:
        dict with results from each stage and overall status
    """
    start_time = datetime.now()

    result = {
        "success": True,
        "started_at": start_time.isoformat(),
        "finished_at": None,
        "ingest": None,
        "compress": None,
        "summarize": None,
        "embed": None,
        "score": None,
        "error": None,
    }

    jsonl_path = get_setting("jsonl_path")
    if not jsonl_path:
        result["success"] = False
        result["error"] = "No JSONL path configured"
        logger.error(result["error"])
        return result

    logger.info(f"=== Pipeline started at {start_time.isoformat()} ===")

    # Stage 1: Ingest new articles
    try:
        logger.info("Stage 1/5: Ingesting new articles...")
        if on_progress:
            on_progress("ingest", 0, 1, "Ingesting articles from JSONL")

        ingest_result = ingest_articles(jsonl_path)
        result["ingest"] = ingest_result

        if on_progress:
            on_progress("ingest", 1, 1, f"Ingested {ingest_result['inserted']} new articles")

        logger.info(
            f"Stage 1/5 complete: {ingest_result['inserted']} inserted, "
            f"{ingest_result['skipped']} skipped"
        )

    except Exception as e:
        error_msg = f"Ingest failed: {e}"
        logger.error(error_msg)
        result["success"] = False
        result["error"] = error_msg
        result["finished_at"] = datetime.now().isoformat()
        return result

    # Stage 2: Compress JSONL (remove duplicates)
    try:
        logger.info("Stage 2/5: Compressing JSONL...")
        if on_progress:
            on_progress("compress", 0, 1, "Removing duplicates from JSONL")

        compress_result = compress_jsonl(jsonl_path)
        result["compress"] = compress_result

        if on_progress:
            on_progress("compress", 1, 1, f"Removed {compress_result['removed_count']} duplicates")

        logger.info(
            f"Stage 2/5 complete: {compress_result['removed_count']} duplicates removed"
        )

    except Exception as e:
        error_msg = f"Compression failed: {e}"
        logger.error(error_msg)
        result["success"] = False
        result["error"] = error_msg
        result["finished_at"] = datetime.now().isoformat()
        return result

    # Stage 3: Summarize unsummarized articles
    try:
        logger.info("Stage 3/5: Summarizing articles...")

        def summarize_progress(current, total):
            if on_progress:
                on_progress("summarize", current, total, f"Summarizing {current}/{total}")

        summarize_result = summarize_batch(on_progress=summarize_progress)
        result["summarize"] = summarize_result

        if summarize_result.get("stopped_early"):
            logger.warning(f"Summarization stopped early: {summarize_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 3/5 complete: {summarize_result['summarized']} summarized, "
                f"{summarize_result['failed']} failed"
            )

    except Exception as e:
        error_msg = f"Summarization failed: {e}"
        logger.error(error_msg)
        result["success"] = False
        result["error"] = error_msg
        result["finished_at"] = datetime.now().isoformat()
        return result

    # Stage 4: Embed summarized articles
    try:
        logger.info("Stage 4/5: Embedding articles...")

        def embed_progress(current, total):
            if on_progress:
                on_progress("embed", current, total, f"Embedding {current}/{total}")

        embed_result = embed_batch(on_progress=embed_progress)
        result["embed"] = embed_result

        if embed_result.get("stopped_early"):
            logger.warning(f"Embedding stopped early: {embed_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 4/5 complete: {embed_result['embedded']} embedded, "
                f"{embed_result['failed']} failed"
            )

    except Exception as e:
        error_msg = f"Embedding failed: {e}"
        logger.error(error_msg)
        result["success"] = False
        result["error"] = error_msg
        result["finished_at"] = datetime.now().isoformat()
        return result

    # Stage 5: Score articles for relevancy
    try:
        logger.info("Stage 5/5: Scoring articles...")

        def score_progress(current, total):
            if on_progress:
                on_progress("score", current, total, f"Scoring {current}/{total}")

        score_result = score_batch(on_progress=score_progress)
        result["score"] = score_result

        if score_result.get("stopped_early"):
            logger.warning(f"Scoring stopped early: {score_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 5/5 complete: {score_result['scored']} scored, "
                f"{score_result['failed']} failed"
            )

    except Exception as e:
        error_msg = f"Scoring failed: {e}"
        logger.error(error_msg)
        result["success"] = False
        result["error"] = error_msg
        result["finished_at"] = datetime.now().isoformat()
        return result

    # Pipeline complete
    result["finished_at"] = datetime.now().isoformat()
    elapsed = (datetime.now() - start_time).total_seconds()

    scored_count = result['score']['scored'] if result['score'] else 0
    logger.info(
        f"=== Pipeline complete in {elapsed:.1f}s: "
        f"{result['ingest']['inserted']} ingested, "
        f"{result['compress']['removed_count']} deduped, "
        f"{result['summarize']['summarized']} summarized, "
        f"{result['embed']['embedded']} embedded, "
        f"{scored_count} scored ==="
    )

    return result

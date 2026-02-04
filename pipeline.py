"""Pipeline orchestrator for Sieve - Hourly processing flow."""

import logging
from datetime import datetime

from db import get_setting
from embed import embed_batch
from entities import extract_batch
from ingest import compress_jsonl, ingest_articles
from score import score_batch
from summarize import summarize_batch
from threads import detect_threads
from topics import classify_batch

logger = logging.getLogger(__name__)


def run_pipeline(on_progress=None):
    """
    Execute the full hourly pipeline:
    1. Ingest new articles from JSONL → DB
    2. Compress JSONL (dedupe by URL, keep most recent)
    3. Summarize unsummarized articles + extract keywords (with context)
    4. Embed summarized articles for semantic search
    5. Score articles for relevance across 7 dimensions
    6. Extract entities from summarized articles
    7. Classify topics for summarized articles
    8. Detect story threads from entities + embeddings

    Stages 1-5 are fatal: failure stops the pipeline.
    Stages 6-8 are non-fatal: failure is logged but the pipeline continues.

    Args:
        on_progress: Optional callback(stage, current, total, message)
            stage: "ingest", "compress", "summarize", "embed", "score",
                   "entities", "topics", "threads"

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
        "entities": None,
        "topics": None,
        "threads": None,
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
        logger.info("Stage 1/8: Ingesting new articles...")
        if on_progress:
            on_progress("ingest", 0, 1, "Ingesting articles from JSONL")

        ingest_result = ingest_articles(jsonl_path)
        result["ingest"] = ingest_result

        if on_progress:
            on_progress("ingest", 1, 1, f"Ingested {ingest_result['inserted']} new articles")

        logger.info(
            f"Stage 1/8 complete: {ingest_result['inserted']} inserted, "
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
        logger.info("Stage 2/8: Compressing JSONL...")
        if on_progress:
            on_progress("compress", 0, 1, "Removing duplicates from JSONL")

        compress_result = compress_jsonl(jsonl_path)
        result["compress"] = compress_result

        if on_progress:
            on_progress("compress", 1, 1, f"Removed {compress_result['removed_count']} duplicates")

        logger.info(
            f"Stage 2/8 complete: {compress_result['removed_count']} duplicates removed"
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
        logger.info("Stage 3/8: Summarizing articles...")

        def summarize_progress(current, total):
            if on_progress:
                on_progress("summarize", current, total, f"Summarizing {current}/{total}")

        summarize_result = summarize_batch(on_progress=summarize_progress)
        result["summarize"] = summarize_result

        if summarize_result.get("stopped_early"):
            logger.warning(f"Summarization stopped early: {summarize_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 3/8 complete: {summarize_result['summarized']} summarized, "
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
        logger.info("Stage 4/8: Embedding articles...")

        def embed_progress(current, total):
            if on_progress:
                on_progress("embed", current, total, f"Embedding {current}/{total}")

        embed_result = embed_batch(on_progress=embed_progress)
        result["embed"] = embed_result

        if embed_result.get("stopped_early"):
            logger.warning(f"Embedding stopped early: {embed_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 4/8 complete: {embed_result['embedded']} embedded, "
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
        logger.info("Stage 5/8: Scoring articles...")

        def score_progress(current, total):
            if on_progress:
                on_progress("score", current, total, f"Scoring {current}/{total}")

        score_result = score_batch(on_progress=score_progress)
        result["score"] = score_result

        if score_result.get("stopped_early"):
            logger.warning(f"Scoring stopped early: {score_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 5/8 complete: {score_result['scored']} scored, "
                f"{score_result['failed']} failed"
            )

    except Exception as e:
        error_msg = f"Scoring failed: {e}"
        logger.error(error_msg)
        result["success"] = False
        result["error"] = error_msg
        result["finished_at"] = datetime.now().isoformat()
        return result

    # Stages 6-8 are non-fatal: log errors but continue to the next stage

    # Stage 6: Extract entities
    try:
        logger.info("Stage 6/8: Extracting entities...")

        def entity_progress(current, total):
            if on_progress:
                on_progress("entities", current, total, f"Extracting entities {current}/{total}")

        entity_result = extract_batch(on_progress=entity_progress)
        result["entities"] = entity_result

        if entity_result.get("stopped_early"):
            logger.warning(f"Entity extraction stopped early: {entity_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 6/8 complete: {entity_result['extracted']} extracted, "
                f"{entity_result['failed']} failed"
            )

    except Exception as e:
        logger.error(f"Entity extraction failed: {e}")
        result["entities"] = {"error": str(e)}

    # Stage 7: Classify topics
    try:
        logger.info("Stage 7/8: Classifying topics...")

        def topic_progress(current, total):
            if on_progress:
                on_progress("topics", current, total, f"Classifying topics {current}/{total}")

        topic_result = classify_batch(on_progress=topic_progress)
        result["topics"] = topic_result

        if topic_result.get("stopped_early"):
            logger.warning(f"Topic classification stopped early: {topic_result.get('last_error')}")
        else:
            logger.info(
                f"Stage 7/8 complete: {topic_result['classified']} classified, "
                f"{topic_result['failed']} failed"
            )

    except Exception as e:
        logger.error(f"Topic classification failed: {e}")
        result["topics"] = {"error": str(e)}

    # Stage 8: Detect threads
    try:
        logger.info("Stage 8/8: Detecting story threads...")

        if on_progress:
            on_progress("threads", 0, 1, "Detecting story threads")

        thread_result = detect_threads()
        result["threads"] = thread_result

        if on_progress:
            on_progress("threads", 1, 1, f"Threads: {thread_result['threads_created']} new, {thread_result['threads_updated']} updated")

        logger.info(
            f"Stage 8/8 complete: {thread_result['threads_created']} threads created, "
            f"{thread_result['threads_updated']} updated, "
            f"{thread_result['articles_linked']} articles linked"
        )

    except Exception as e:
        logger.error(f"Thread detection failed: {e}")
        result["threads"] = {"error": str(e)}

    # Pipeline complete
    result["finished_at"] = datetime.now().isoformat()
    elapsed = (datetime.now() - start_time).total_seconds()

    scored_count = result['score']['scored'] if result['score'] else 0
    entities_count = result['entities'].get('extracted', 0) if isinstance(result.get('entities'), dict) else 0
    topics_count = result['topics'].get('classified', 0) if isinstance(result.get('topics'), dict) else 0
    threads_created = result['threads'].get('threads_created', 0) if isinstance(result.get('threads'), dict) else 0
    logger.info(
        f"=== Pipeline complete in {elapsed:.1f}s: "
        f"{result['ingest']['inserted']} ingested, "
        f"{result['compress']['removed_count']} deduped, "
        f"{result['summarize']['summarized']} summarized, "
        f"{result['embed']['embedded']} embedded, "
        f"{scored_count} scored, "
        f"{entities_count} entities, "
        f"{topics_count} topics, "
        f"{threads_created} threads ==="
    )

    return result

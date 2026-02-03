"""Scheduler for Sieve - APScheduler background jobs for automated pipeline."""

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db import get_setting

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()
INGEST_JOB_ID = "auto_ingest"  # Legacy, kept for compatibility
PIPELINE_JOB_ID = "hourly_pipeline"
DIGEST_JOB_ID = "daily_digest"

# Track if pipeline is currently running (to skip overlapping runs)
_pipeline_lock = threading.Lock()
_pipeline_running = False

# Track if digest is currently running
_digest_lock = threading.Lock()
_digest_running = False


def is_pipeline_running():
    """Check if pipeline is currently running."""
    with _pipeline_lock:
        return _pipeline_running


def _run_scheduled_pipeline():
    """Execute scheduled pipeline job with skip-if-running protection."""
    global _pipeline_running

    # Skip if already running
    with _pipeline_lock:
        if _pipeline_running:
            logger.warning("Pipeline already running, skipping scheduled execution")
            return
        _pipeline_running = True

    try:
        # Import here to avoid circular imports
        from pipeline import run_pipeline

        logger.info("Running scheduled pipeline")
        result = run_pipeline()

        if result.get("success"):
            logger.info(f"Scheduled pipeline complete: {result}")
        else:
            logger.error(f"Scheduled pipeline failed: {result.get('error')}")

    except Exception as e:
        logger.error(f"Scheduled pipeline error: {e}")

    finally:
        with _pipeline_lock:
            _pipeline_running = False


def _run_scheduled_ingest():
    """Execute scheduled ingestion job (legacy, for backwards compatibility)."""
    from ingest import ingest_articles

    jsonl_path = get_setting("jsonl_path")
    if not jsonl_path:
        logger.warning("No JSONL path configured for scheduled ingest")
        return

    logger.info(f"Running scheduled ingest from {jsonl_path}")
    try:
        result = ingest_articles(jsonl_path)
        logger.info(f"Scheduled ingest complete: {result}")
    except Exception as e:
        logger.error(f"Scheduled ingest failed: {e}")


def _run_scheduled_digest():
    """Execute scheduled daily digest generation."""
    global _digest_running

    # Skip if already running
    with _digest_lock:
        if _digest_running:
            logger.warning("Digest already running, skipping scheduled execution")
            return
        _digest_running = True

    try:
        from digest import generate_digest

        logger.info("Running scheduled digest generation")
        result = generate_digest()

        if result.get("success"):
            logger.info(f"Scheduled digest complete: {result['article_count']} articles")
        else:
            logger.error(f"Scheduled digest failed: {result.get('error')}")

    except Exception as e:
        logger.error(f"Scheduled digest error: {e}")

    finally:
        with _digest_lock:
            _digest_running = False


def start_scheduler(app=None):
    """
    Initialize and start the background scheduler.

    Args:
        app: Optional Flask app for context (not currently used)
    """
    if scheduler.running:
        logger.info("Scheduler already running")
        return

    scheduler.start()
    logger.info("Scheduler started")

    # Set up hourly pipeline (always enabled)
    schedule_pipeline("0 * * * *")  # Every hour at minute 0

    # Set up legacy auto-ingest if enabled (for backwards compatibility)
    auto_ingest = get_setting("auto_ingest")
    schedule = get_setting("ingest_schedule")

    if auto_ingest == "true" and schedule:
        schedule_ingest(schedule)

    # Set up daily digest if enabled
    auto_digest = get_setting("auto_digest")
    digest_schedule = get_setting("digest_schedule")

    if auto_digest == "true" and digest_schedule:
        schedule_digest(digest_schedule)


def schedule_pipeline(cron_expr):
    """
    Set up the hourly pipeline cron job.

    Args:
        cron_expr: Cron expression string (default: "0 * * * *" for every hour)
    """
    # Remove existing job if present
    if scheduler.get_job(PIPELINE_JOB_ID):
        scheduler.remove_job(PIPELINE_JOB_ID)
        logger.info("Removed existing pipeline job")

    if not cron_expr:
        logger.info("No cron expression provided, pipeline disabled")
        return

    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: expected 5 parts, got {len(parts)}")

        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )

        scheduler.add_job(
            _run_scheduled_pipeline,
            trigger=trigger,
            id=PIPELINE_JOB_ID,
            name="Hourly Pipeline (ingest + compress + summarize + embed + score)",
            replace_existing=True,
        )

        logger.info(f"Scheduled hourly pipeline with cron: {cron_expr}")

    except Exception as e:
        logger.error(f"Failed to schedule pipeline: {e}")
        raise


def schedule_ingest(cron_expr):
    """
    Set up or update the cron job for auto-ingestion (legacy).

    Args:
        cron_expr: Cron expression string (e.g., "0 */6 * * *" for every 6 hours)
    """
    # Remove existing job if present
    if scheduler.get_job(INGEST_JOB_ID):
        scheduler.remove_job(INGEST_JOB_ID)
        logger.info("Removed existing ingest job")

    if not cron_expr:
        logger.info("No cron expression provided, auto-ingest disabled")
        return

    try:
        # Parse cron expression: minute hour day month day_of_week
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: expected 5 parts, got {len(parts)}")

        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )

        scheduler.add_job(
            _run_scheduled_ingest,
            trigger=trigger,
            id=INGEST_JOB_ID,
            name="Auto-ingest JSONL",
            replace_existing=True,
        )

        logger.info(f"Scheduled auto-ingest with cron: {cron_expr}")

    except Exception as e:
        logger.error(f"Failed to schedule ingest: {e}")
        raise


def stop_scheduler():
    """Stop the background scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def remove_ingest_job():
    """Remove the auto-ingest job if it exists."""
    if scheduler.get_job(INGEST_JOB_ID):
        scheduler.remove_job(INGEST_JOB_ID)
        logger.info("Removed ingest job")


def remove_pipeline_job():
    """Remove the pipeline job if it exists."""
    if scheduler.get_job(PIPELINE_JOB_ID):
        scheduler.remove_job(PIPELINE_JOB_ID)
        logger.info("Removed pipeline job")


def get_next_pipeline_run():
    """Get the next scheduled pipeline run time."""
    job = scheduler.get_job(PIPELINE_JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def schedule_digest(cron_expr):
    """
    Set up the daily digest cron job.

    Args:
        cron_expr: Cron expression string (default: "0 6 * * *" for 6 AM daily)
    """
    # Remove existing job if present
    if scheduler.get_job(DIGEST_JOB_ID):
        scheduler.remove_job(DIGEST_JOB_ID)
        logger.info("Removed existing digest job")

    if not cron_expr:
        logger.info("No cron expression provided, digest disabled")
        return

    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: expected 5 parts, got {len(parts)}")

        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )

        scheduler.add_job(
            _run_scheduled_digest,
            trigger=trigger,
            id=DIGEST_JOB_ID,
            name="Daily Digest (6 AM)",
            replace_existing=True,
        )

        logger.info(f"Scheduled daily digest with cron: {cron_expr}")

    except Exception as e:
        logger.error(f"Failed to schedule digest: {e}")
        raise


def remove_digest_job():
    """Remove the digest job if it exists."""
    if scheduler.get_job(DIGEST_JOB_ID):
        scheduler.remove_job(DIGEST_JOB_ID)
        logger.info("Removed digest job")


def get_next_digest_run():
    """Get the next scheduled digest run time."""
    job = scheduler.get_job(DIGEST_JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None

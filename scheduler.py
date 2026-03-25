"""Scheduler for Sieve - APScheduler background jobs for automated pipeline.

Resilience features:
- misfire_grace_time: Jobs delayed up to 120s still execute (handles load spikes)
- coalesce: Multiple missed fires collapse into a single run
- Event listeners: Log all job errors, misses, and scheduler crashes
- Watchdog thread: Checks scheduler health every 5 minutes, restarts if dead
"""

import logging
import subprocess
import threading

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_SCHEDULER_SHUTDOWN,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db import get_setting

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    job_defaults={
        "misfire_grace_time": 120,
        "coalesce": True,
        "max_instances": 1,
    },
)
INGEST_JOB_ID = "auto_ingest"  # Legacy, kept for compatibility
PIPELINE_JOB_ID = "hourly_pipeline"
DIGEST_JOB_ID = "daily_digest"

# Track if pipeline is currently running (to skip overlapping runs)
_pipeline_lock = threading.Lock()
_pipeline_running = False

# Track if digest is currently running
_digest_lock = threading.Lock()
_digest_running = False

# Watchdog state
_watchdog_thread = None
_watchdog_stop = threading.Event()
_app_ref = None  # Stored so watchdog can restart scheduler with same config


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
    """Execute scheduled digest generation for all days that need it.

    Checks every day with scored articles and generates a digest if:
    - No digest exists for that day, or
    - New articles were scored after the existing digest was created.
    """
    global _digest_running

    # Skip if already running
    with _digest_lock:
        if _digest_running:
            logger.warning("Digest already running, skipping scheduled execution")
            return
        _digest_running = True

    try:
        from db import get_days_needing_digest
        from digest import generate_digest

        days = get_days_needing_digest()

        if not days:
            logger.info("Scheduled digest: all days up to date")
            return

        logger.info(f"Scheduled digest: {len(days)} day(s) need digests: {days}")

        for day in days:
            logger.info(f"Generating digest for {day}")
            result = generate_digest(target_date=day)

            if result.get("success"):
                logger.info(
                    f"Digest for {day} complete: {result['article_count']} articles"
                )
            else:
                logger.error(f"Digest for {day} failed: {result.get('error')}")

    except Exception as e:
        logger.error(f"Scheduled digest error: {e}")

    finally:
        with _digest_lock:
            _digest_running = False

    # Deploy Rogue Routine site after digest run
    _deploy_rogue_routine()


def _deploy_rogue_routine():
    """Deploy the Rogue Routine static site after pipeline/digest completion.

    Runs `make deploy` in the rogue_routine project, which exports digests
    and articles from the Sieve database, builds the Hugo site, and rsyncs
    to the VPS. Failures are logged but never block Sieve's pipeline.
    """
    try:
        logger.info("Deploying Rogue Routine site")
        result = subprocess.run(
            ["make", "deploy"],
            cwd="/home/kellogg/dev/rogue_routine",
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            logger.info("Rogue Routine deploy succeeded")
        else:
            logger.error(
                f"Rogue Routine deploy failed (exit {result.returncode}): "
                f"{result.stderr[-500:]}"
            )
    except Exception as e:
        logger.error(f"Rogue Routine deploy error: {e}")


def _on_job_executed(event):
    """Log successful job execution."""
    logger.debug(f"Job {event.job_id} executed successfully")


def _on_job_error(event):
    """Log job execution errors — these would otherwise be swallowed."""
    logger.error(
        f"Job {event.job_id} raised an exception: {event.exception}\n"
        f"{event.traceback}"
    )


def _on_job_missed(event):
    """Log misfired jobs — these indicate scheduler health issues."""
    logger.warning(
        f"Job {event.job_id} missed its scheduled run at {event.scheduled_run_time}"
    )


def _on_scheduler_shutdown(event):
    """Log unexpected scheduler shutdown."""
    logger.error("APScheduler shut down unexpectedly — watchdog will attempt restart")


def _watchdog_loop():
    """Check scheduler health every 5 minutes. Restart if dead."""
    global _app_ref
    while not _watchdog_stop.wait(timeout=300):
        if not scheduler.running:
            logger.error("Watchdog: scheduler not running — restarting")
            try:
                _start_scheduler_jobs()
            except Exception as e:
                logger.error(f"Watchdog: failed to restart scheduler: {e}")
        else:
            jobs = scheduler.get_jobs()
            if not jobs:
                logger.warning("Watchdog: scheduler running but has 0 jobs — re-adding")
                try:
                    _add_all_jobs()
                except Exception as e:
                    logger.error(f"Watchdog: failed to re-add jobs: {e}")


def _add_all_jobs():
    """Add all standard jobs to the scheduler."""
    schedule_pipeline("0 * * * *")

    auto_ingest = get_setting("auto_ingest")
    ingest_schedule = get_setting("ingest_schedule")
    if auto_ingest == "true" and ingest_schedule:
        schedule_ingest(ingest_schedule)

    digest_schedule = get_setting("digest_schedule") or "0 20 * * *"
    schedule_digest(digest_schedule)


def _start_scheduler_jobs():
    """Start the scheduler and add all jobs. Used by both start_scheduler and watchdog."""
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    _add_all_jobs()


def start_scheduler(app=None):
    """
    Initialize and start the background scheduler with resilience features.

    Sets up event listeners for error visibility, configures all jobs,
    and starts a watchdog thread that restarts the scheduler if it dies.

    Args:
        app: Optional Flask app for context (not currently used)
    """
    global _watchdog_thread, _app_ref
    _app_ref = app

    if scheduler.running:
        logger.info("Scheduler already running")
        return

    # Event listeners for visibility into scheduler health
    scheduler.add_listener(_on_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)
    scheduler.add_listener(_on_scheduler_shutdown, EVENT_SCHEDULER_SHUTDOWN)

    _start_scheduler_jobs()

    # Start watchdog thread to detect and recover from scheduler death
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, name="sieve-scheduler-watchdog", daemon=True
    )
    _watchdog_thread.start()
    logger.info("Scheduler watchdog started (5-minute health check interval)")


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
            name="Hourly Pipeline (ingest + compress + summarize + embed + score + entities + topics + threads)",
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
    """Stop the background scheduler and watchdog."""
    _watchdog_stop.set()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler and watchdog stopped")


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

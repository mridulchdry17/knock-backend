"""In-process APScheduler that drives the autopilot cycle.

v0 substitute for an external cron / systemd timer: a BackgroundScheduler
fires `autopilot_cycle_cron.run_cycle` on a fixed interval. The cycle itself
is idempotent (batch-gen guards on has_batch_for_date; the send worker only
touches status='ready'; reply ingest advances gmail_history_id forward only),
so re-running every interval is safe:

  - batch generation effectively runs once per UTC day (first fire after midnight)
  - the send drain delivers each staggered send slot as its send_time comes due
  - reply ingest pulls new replies each fire

Manual vs autopilot is enforced *inside* the cycle, not here: only paid users
with autopilot_enabled and no paused_at get cards marked 'ready', and the send
worker only sends 'ready' rows. Free/manual users' cards stay 'default' and are
never auto-sent — see batch_generator._is_autopilot_active.

SINGLE-WORKER ASSUMPTION: this scheduler lives in the API process. If the API
is ever run with multiple worker processes (uvicorn --workers N / gunicorn),
each worker would start its own scheduler and the cycle would fire N times.
v0 runs a single process (systemd unit, one uvicorn). If we scale out, move
this to a dedicated one-off worker / external cron and keep RUN_SCHEDULER off
in the web processes.
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.logging_config import get_logger

log = get_logger("scheduler")

# Module-level handle so start/stop are idempotent across the app lifespan.
_scheduler: BackgroundScheduler | None = None

_JOB_ID = "autopilot_cycle"


def _run_cycle_safe() -> None:
    """Job target. Imported lazily so the scheduler module stays import-cheap
    and we don't pull the whole service graph at module load.

    Swallows + logs any exception so one bad cycle never kills the scheduler
    thread (APScheduler would otherwise keep the job but surface the error).
    """
    from app.jobs import autopilot_cycle_cron

    try:
        result = autopilot_cycle_cron.run_cycle()
        log.info(
            "scheduler.cycle_ok",
            batch_users=result.batch_users_processed,
            batch_items=result.batch_items_created,
            sent=result.sent,
            failed=result.failed,
            replies_matched=result.replies_matched,
            explicit_stops=result.explicit_stops,
        )
    except Exception as exc:  # pragma: no cover — defensive guard
        log.exception("scheduler.cycle_failed", error=str(exc))


def build_scheduler() -> BackgroundScheduler:
    """Construct (but do not start) a scheduler with the autopilot job wired.

    Split from start_scheduler so tests can assert the job registration without
    spawning a background thread.
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_cycle_safe,
        trigger=IntervalTrigger(minutes=settings.AUTOPILOT_CYCLE_INTERVAL_MINUTES),
        id=_JOB_ID,
        name="autopilot daily cycle",
        # Don't pile up overlapping runs if one cycle outlasts the interval
        # (a large drain against slow remote Gmail). Collapse missed fires.
        max_instances=1,
        coalesce=True,
        # Fire once shortly after startup so a fresh deploy doesn't wait a full
        # interval before generating the day's batch. misfire grace keeps a
        # delayed-start fire from being skipped.
        misfire_grace_time=300,
    )
    return scheduler


def start_scheduler() -> None:
    """Start the scheduler if RUN_SCHEDULER is on. Idempotent — a second call
    while already running is a no-op. Called from the FastAPI lifespan."""
    global _scheduler
    if not settings.RUN_SCHEDULER:
        log.info("scheduler.disabled")
        return
    if _scheduler is not None and _scheduler.running:
        return

    _scheduler = build_scheduler()
    _scheduler.start()
    log.info(
        "scheduler.started",
        interval_minutes=settings.AUTOPILOT_CYCLE_INTERVAL_MINUTES,
    )


def shutdown_scheduler() -> None:
    """Stop the scheduler if running. Called from the FastAPI lifespan on
    shutdown. wait=False so process exit isn't blocked on an in-flight cycle."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler.stopped")
    _scheduler = None

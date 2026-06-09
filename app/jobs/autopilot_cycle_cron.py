"""B5.7 — daily autopilot cycle entry point.

Runs the three Phase-5 phases in sequence:
  1. batch_gen   — generate today's batch for every eligible user.
  2. send_worker — drain any items already due (also picks up future-dated
                   items as their `send_time` arrives if you re-run hourly).
  3. reply_ingestor — pull replies for every user, write per-user / platform
                      locks via the existing locks service.

In v0 there is NO scheduler wiring; super_admin triggers this via
`POST /api/v1/admin/autopilot/cycle` or `python -m app.jobs.autopilot_cycle_cron`.
The three phases are intentionally idempotent so any cadence is safe:
  - batch_gen guards on `has_batch_for_date`
  - send_worker only drains status='ready'
  - reply_ingestor advances `gmail_history_id` only forward
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.time import utcnow
from app.db.session import SessionLocal
from app.logging_config import get_logger
from app.services import batch_generator as batch_gen
from app.services import followup_planner, reply_ingestor, send_worker

log = get_logger("autopilot_cycle_cron")


@dataclass(frozen=True, slots=True)
class CycleResult:
    batch_users_processed: int
    batch_items_created: int
    sent: int
    failed: int
    skipped_sends: int
    ingest_users_processed: int
    replies_matched: int
    explicit_stops: int
    # New in 0018 — defaults to 0 so existing test constructors stay valid.
    followups_planned: int = 0


def run_cycle() -> CycleResult:
    """Run the full daily cycle. Caller-managed session — opens its own."""
    today = utcnow().date()
    db = SessionLocal()
    try:
        # 1. Batch generation (initials only).
        batch_results = batch_gen.generate_batch_for_all_users(db, batch_date=today)
        batch_users = len(batch_results)
        batch_items = sum(r.items_created for r in batch_results)

        # 1b. Plan due follow-ups — adds TBI rows with kind='followup' for any
        # SENT-but-no-reply send_queue rows older than FOLLOWUP_DELAY_DAYS.
        # Idempotent (guards on already-planned + same-company-already-busy).
        followup_summary = followup_planner.plan_due_followups(db, today=today)

        # 2. Send drain — picks up any 'ready' items whose send_time has arrived.
        # Recognises kind='followup' and routes through send_followup() to keep
        # the email on the original Gmail thread.
        drain = send_worker.drain_due_items(db)

        # 3. Reply ingest. Side effect: cancels any pending follow-ups on the
        # replied-to thread (status='skipped', skip_reason='reply_received').
        ingest_summaries = reply_ingestor.ingest_replies_for_all_users(db)
        replies = sum(s.replies_matched for s in ingest_summaries)
        stops = sum(s.explicit_stops for s in ingest_summaries)

        result = CycleResult(
            batch_users_processed=batch_users,
            batch_items_created=batch_items,
            followups_planned=followup_summary.planned,
            sent=drain.sent,
            failed=drain.failed,
            skipped_sends=drain.skipped,
            ingest_users_processed=len(ingest_summaries),
            replies_matched=replies,
            explicit_stops=stops,
        )
        log.info(
            "autopilot_cycle.done",
            batch_users=batch_users,
            batch_items=batch_items,
            followups_planned=followup_summary.planned,
            sent=drain.sent,
            failed=drain.failed,
            replies_matched=replies,
            explicit_stops=stops,
        )
        return result
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    res = run_cycle()
    print(res)

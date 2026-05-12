"""B5.4 batch cron entry point.

v0: NOT wired into a scheduler. The admin manual-trigger endpoint
(`POST /api/v1/admin/today/run-cron`) is how we fire batch generation
during the launch ceremony.

Swap-point: a systemd timer or APScheduler config should call
`run_batch_cron()` at 6 AM server time. When that lands, this module
gets imported by the scheduler and `__main__` stays for ad-hoc runs.

Usage (manual):
    .venv/bin/python -m app.jobs.batch_cron
"""
from __future__ import annotations

import sys
from datetime import date

from app.core.time import utcnow
from app.db.base import SessionLocal
from app.logging_config import configure_logging, get_logger
from app.services import batch_generator as batch_gen_svc


def run_batch_cron(batch_date: date | None = None) -> list[batch_gen_svc.BatchGenerationResult]:
    """Entry point for both the scheduled cron (future) and the admin trigger.

    Owns its own DB session so it can be called outside a request context.
    """
    target = batch_date or utcnow().date()
    log = get_logger("batch_cron")
    db = SessionLocal()
    try:
        log.info("batch_cron.start", batch_date=str(target))
        results = batch_gen_svc.generate_batch_for_all_users(db, batch_date=target)
        total_created = sum(r.items_created for r in results)
        log.info(
            "batch_cron.done",
            batch_date=str(target),
            users_processed=len(results),
            total_items_created=total_created,
        )
        return results
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    configure_logging()
    results = run_batch_cron()
    skipped_reasons: dict[str, int] = {}
    for r in results:
        if r.reason_if_skipped:
            skipped_reasons[r.reason_if_skipped] = (
                skipped_reasons.get(r.reason_if_skipped, 0) + 1
            )
    print(f"users_processed={len(results)}")
    print(f"items_created={sum(r.items_created for r in results)}")
    for reason, n in skipped_reasons.items():
        print(f"skip[{reason}]={n}")
    sys.exit(0)

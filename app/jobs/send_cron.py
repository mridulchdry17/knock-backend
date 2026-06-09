"""Cron entrypoint for the send worker.

Run with `python -m app.jobs.send_cron`. NOT wired into APScheduler in v0 —
launch ceremony invokes this manually + super_admin can POST
/admin/send/drain from the dashboard.
"""
from __future__ import annotations

from app.db.base import SessionLocal
from app.logging_config import configure_logging, get_logger
from app.services.send_worker import drain_due_items


def main() -> int:
    configure_logging()
    log = get_logger("send_cron")
    db = SessionLocal()
    try:
        summary = drain_due_items(db)
        log.info(
            "send_cron.summary",
            attempted=summary.attempted,
            sent=summary.sent,
            failed=summary.failed,
            skipped=summary.skipped,
            failures_by_kind=summary.failures_by_kind,
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

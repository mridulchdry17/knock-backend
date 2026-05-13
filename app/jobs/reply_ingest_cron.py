"""Cron entrypoint for the B5.6 reply ingestion worker.

Run with `python -m app.jobs.reply_ingest_cron`. NOT wired into APScheduler
in v0 — launch ceremony invokes this manually and super_admin can POST
`/admin/replies/ingest` from the dashboard.
"""
from __future__ import annotations

from app.db.base import SessionLocal
from app.logging_config import configure_logging, get_logger
from app.services.reply_ingestor import ingest_replies_for_all_users


def run_reply_ingest_cron() -> list:
    """Reusable entrypoint (also imported by tests / admin endpoint)."""
    log = get_logger("reply_ingest_cron")
    db = SessionLocal()
    try:
        summaries = ingest_replies_for_all_users(db)
        log.info(
            "reply_ingest_cron.summary",
            users_processed=len(summaries),
            replies_matched=sum(s.replies_matched for s in summaries),
            explicit_stops=sum(s.explicit_stops for s in summaries),
            user_reply_locks_written=sum(
                s.user_reply_locks_written for s in summaries
            ),
            error_counts={
                s.error_kind: 1
                for s in summaries
                if s.error_kind is not None
            },
        )
        return summaries
    finally:
        db.close()


def main() -> int:
    configure_logging()
    run_reply_ingest_cron()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

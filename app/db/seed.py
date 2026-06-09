"""Seed reference data. Idempotent — re-runnable without duplicating rows.

Currently a placeholder. Per-user starter templates are seeded on first
login (see auth flow). System-wide reference data lands here when needed.
"""
from __future__ import annotations

from app.db.base import SessionLocal
from app.logging_config import configure_logging, get_logger


def main() -> None:
    configure_logging()
    log = get_logger("seed")
    with SessionLocal():
        # No global seed data yet. Migrations create tables; user-scoped
        # starter templates are inserted in the auth callback (services/auth.py).
        log.info("seed.complete", inserted=0)


if __name__ == "__main__":
    main()

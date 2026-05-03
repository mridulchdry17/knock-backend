from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db.base import SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Yields a short-lived session, ensures close."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

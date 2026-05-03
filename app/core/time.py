from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_in(**kwargs: float) -> datetime:
    return utcnow() + timedelta(**kwargs)

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_in(**kwargs: float) -> datetime:
    return utcnow() + timedelta(**kwargs)


def ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime carries a UTC tzinfo.

    SQLite (and Turso's libsql layer) does not preserve timezone information
    on DateTime(timezone=True) columns — it stores ISO strings without offsets
    and returns naive datetimes on read. Any code that compares a DB-loaded
    timestamp against `utcnow()` (tz-aware) must coerce through this helper
    first to avoid `TypeError: can't compare offset-naive and offset-aware`.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

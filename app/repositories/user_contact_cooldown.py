"""Repo for the per-user "I already emailed this contact" cooldown.

Pure functions, caller commits. Used by:
  - The send worker after a successful dispatch (upsert the to_contact + every
    cc_contact with cooldown_until = now + N days).
  - The daily batch picker, to fetch the user's currently-blocked contacts in
    one query (powers picker.blocked_contact_ids).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import UserContactCooldown


def list_blocked_contact_ids(
    db: OrmSession, user_id: int, *, now: datetime
) -> set[int]:
    """Contacts this user can't be shown in their daily batch right now.
    Returns a set for cheap membership tests inside the picker."""
    rows = db.scalars(
        select(UserContactCooldown.contact_id)
        .where(UserContactCooldown.user_id == user_id)
        .where(UserContactCooldown.cooldown_until > now)
    ).all()
    return {int(r) for r in rows}


def upsert_after_send(
    db: OrmSession,
    *,
    user_id: int,
    contact_ids: Iterable[int],
    now: datetime,
    cooldown_days: int,
) -> int:
    """Record a fresh cooldown for every contact the user just emailed (TO + CC).
    Idempotent — re-runs update last_sent_at + cooldown_until. Returns count
    written/updated. Caller commits."""
    written = 0
    until = now + timedelta(days=cooldown_days)
    for contact_id in {int(c) for c in contact_ids}:
        existing = db.get(UserContactCooldown, (user_id, contact_id))
        if existing is None:
            db.add(
                UserContactCooldown(
                    user_id=user_id,
                    contact_id=contact_id,
                    last_sent_at=now,
                    cooldown_until=until,
                )
            )
        else:
            existing.last_sent_at = now
            existing.cooldown_until = until
            db.add(existing)
        written += 1
    return written

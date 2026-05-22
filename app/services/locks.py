"""Locks service — the 3-tier lock check that B5.4/B5.5/B5.6 consume.

This is the single source of truth for "can user X email @company.com right now?"
B5.3 ships the service + foundation; the batch picker (B5.4) and send worker
(B5.5) will import `check_can_send_to_company` to gate dispatch. The reply
ingestor (B5.6) will call `record_reply_from_company` to set per-user and
platform locks.

Priority order when blocked (most absolute → least):
  1. PLATFORM_PERMANENT — someone at @company replied "stop emailing"
  2. PLATFORM_COOLDOWN  — 36h rolling cooldown after any user's send
  3. USER_REPLY_LOCK    — this user got a reply on their @company thread

The check returns a structured `LockCheckResult` so UI / logs can present the
specific reason rather than a generic "blocked".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.orm import Session as OrmSession

from app.core.time import ensure_utc, utcnow
from app.models import GlobalContactLock, PlatformCompanyLock, UserCompanyLock
from app.repositories import locks as locks_repo

# Default lock durations — kept here (not in config) so they live alongside
# the semantics they implement. If a knob needs to be tunable per-env later,
# we lift to settings; for v0 the product values are stable.
DEFAULT_COOLDOWN_HOURS = 36
DEFAULT_USER_REPLY_LOCK_DAYS = 30


class LockStatus(StrEnum):
    AVAILABLE = "available"
    PLATFORM_COOLDOWN = "platform_cooldown"
    USER_REPLY_LOCK = "user_reply_lock"
    PLATFORM_PERMANENT = "platform_permanent"


@dataclass(frozen=True, slots=True)
class LockCheckResult:
    status: LockStatus
    # None when AVAILABLE or PLATFORM_PERMANENT (never auto-unlocks); ISO
    # datetime for the time-bounded statuses so UI can render countdowns.
    unlocked_at: datetime | None
    # Short human-readable hint for logs/UI ("explicit-stop reply", "user reply").
    reason: str | None


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower()


def _resolve_lock_state(
    *,
    now: datetime,
    platform_lock: PlatformCompanyLock | None,
    global_lock: GlobalContactLock | None,
    user_lock: UserCompanyLock | None,
) -> LockCheckResult:
    """The 3-tier priority decision, given already-fetched lock rows.

    Single source of truth for ordering so the per-domain and batch entry
    points can't drift. Order: platform-permanent (brand-protective) >
    36h cooldown (shared rate-limit) > the user's own reply lock.
    """
    # 1. Platform permanent stop
    if platform_lock is not None:
        return LockCheckResult(
            status=LockStatus.PLATFORM_PERMANENT,
            unlocked_at=None,
            reason=platform_lock.reason,
        )

    # 2. Platform 36h cooldown
    if global_lock is not None:
        until = ensure_utc(global_lock.locked_until)
        if until > now:
            return LockCheckResult(
                status=LockStatus.PLATFORM_COOLDOWN,
                unlocked_at=until,
                reason="platform_cooldown_36h",
            )

    # 3. Per-user 30-day reply lock
    if user_lock is not None:
        if user_lock.is_permanent:
            return LockCheckResult(
                status=LockStatus.USER_REPLY_LOCK,
                unlocked_at=None,
                reason=user_lock.reason,
            )
        until = ensure_utc(user_lock.locked_until)
        if until > now:
            return LockCheckResult(
                status=LockStatus.USER_REPLY_LOCK,
                unlocked_at=until,
                reason=user_lock.reason,
            )

    return LockCheckResult(status=LockStatus.AVAILABLE, unlocked_at=None, reason=None)


def check_can_send_to_company(
    db: OrmSession, *, user_id: int, company_domain: str
) -> LockCheckResult:
    """Single source of truth for the 3-tier check (one domain).

    For listing endpoints that need many domains at once, use
    `check_can_send_to_companies` — it batches the three lookups into three
    queries total instead of three-per-row.
    """
    domain = _normalize_domain(company_domain)
    return _resolve_lock_state(
        now=utcnow(),
        platform_lock=locks_repo.get_platform_lock(db, domain),
        global_lock=locks_repo.get_global_lock(db, domain),
        user_lock=locks_repo.get_user_company_lock(db, user_id, domain),
    )


def check_can_send_to_companies(
    db: OrmSession, *, user_id: int, company_domains: list[str] | set[str]
) -> dict[str, LockCheckResult]:
    """Batched 3-tier check for a page of domains.

    Three queries total (platform / global / user) regardless of how many
    domains — kills the per-row N+1 in /inbox and /contacts browse. Returns a
    dict keyed by NORMALIZED domain; callers should normalize their lookup key
    the same way (lowercase+strip) or use the returned keys directly.
    """
    domains = {_normalize_domain(d) for d in company_domains if d}
    if not domains:
        return {}

    now = utcnow()
    platform_by_domain = {
        row.company_domain: row
        for row in locks_repo.list_platform_locks_for_domains(db, domains)
    }
    global_by_domain = {
        row.company_domain: row
        for row in locks_repo.list_global_locks_for_domains(db, domains)
    }
    user_by_domain = {
        row.company_domain: row
        for row in locks_repo.list_user_locks_for_domains(db, user_id, domains)
    }

    return {
        domain: _resolve_lock_state(
            now=now,
            platform_lock=platform_by_domain.get(domain),
            global_lock=global_by_domain.get(domain),
            user_lock=user_by_domain.get(domain),
        )
        for domain in domains
    }


def record_send_to_company(
    db: OrmSession, *, user_id: int, company_domain: str
) -> None:
    """Called by B5.5 send worker after a successful outbound. Upserts the
    36h platform cooldown. Caller commits.
    """
    domain = _normalize_domain(company_domain)
    locks_repo.upsert_global_lock(
        db,
        company_domain=domain,
        locked_by_user_id=user_id,
        lock_duration_hours=DEFAULT_COOLDOWN_HOURS,
    )


def record_reply_from_company(
    db: OrmSession,
    *,
    user_id: int,
    company_domain: str,
    is_explicit_stop: bool,
) -> None:
    """Called by B5.6 reply ingestion.

    - `is_explicit_stop=True`: regex-detected stop language → write a platform-wide
      permanent lock. We do NOT also set a per-user lock — the platform lock is
      strictly more restrictive and supersedes it in `check_can_send_to_company`.
    - `is_explicit_stop=False`: ordinary reply → upsert the user's 30-day lock
      ("user is in conversation, don't autopilot more sends to this company").

    Caller commits.
    """
    domain = _normalize_domain(company_domain)
    if is_explicit_stop:
        locks_repo.upsert_platform_lock(
            db, company_domain=domain, reason="explicit_stop_reply"
        )
        return
    locks_repo.upsert_user_company_lock(
        db,
        user_id=user_id,
        company_domain=domain,
        reason="reply",
        duration_days=DEFAULT_USER_REPLY_LOCK_DAYS,
    )

"""B5.4 batch generator — orchestration glue between the pure picker and the DB.

`generate_batch_for_user` is the end-to-end pipeline for one user on one day.
`generate_batch_for_all_users` is the cron entry point (and the manual-trigger
admin endpoint).

Skip reasons (returned in `reason_if_skipped`) — frontend / admin dashboard
can show these without re-deriving the gating logic:
  - 'pending_tier'       — tier not in (free, paid, super_admin)
  - 'suspended'          — user.is_suspended=True
  - 'gmail_disconnected' — no Google refresh token; the send worker would
                           fail anyway, so we skip batch generation too
  - 'already_run_today'  — idempotent re-run guard
  - 'no_eligible_contacts' — pool exhausted after filters (separate from skipped
                             so the cron can distinguish empty-pool from "we
                             ran today already")

Templating: v0 ships a hardcoded student-persona body (see _DEFAULT_BODY).
B5.7 will introduce a real templates table and per-user template selection.
`template_id` is left NULL on these v0 rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from random import Random

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core.time import utcnow
from app.logging_config import get_logger
from app.models import Company, Contact, TodayBatchItem, User
from app.repositories import locks as locks_repo
from app.repositories import preferences as prefs_repo
from app.repositories import templates as templates_repo
from app.repositories import today_batch as today_repo
from app.repositories import user_contact_cooldown as cooldown_repo
from app.repositories import users as users_repo
from app.services import send_caps
from app.services import templates as templates_svc
from app.services.today_picker import (
    ContactCandidate,
    pick_companies_for_user,
)

log = get_logger("batch_generator")

# ─────────────────────────── tier → daily cap ───────────────────────────
# Canonical cap logic lives in send_caps so the picker (batch size) and the
# send worker (dispatch enforcement) share one source and can't drift.
_TIER_DEFAULT_CAPS = send_caps.TIER_DEFAULT_CAPS

# v0 default template body — student persona, hardcoded until B5.7 ships
# real templates. Mustache-ish placeholders so we can dropreplace cheaply
# during render; we deliberately keep the renderer dumb (no Jinja) to avoid
# introducing a templating engine for a one-off default.
_DEFAULT_SUBJECT = "Quick hello from a student exploring {{company}}"
_DEFAULT_BODY = (
    "Hi {{first_name}},\n\n"
    "I'm a student exploring opportunities at {{company}}. Would love to "
    "connect and learn more about what your team is working on.\n\n"
    "Best,\n"
    "{{sender_name}}\n"
)


@dataclass(frozen=True, slots=True)
class BatchGenerationResult:
    """One row per generate_batch_for_user invocation. Aggregated across the
    cron run for observability."""

    user_id: int
    batch_date: date
    items_created: int
    items_skipped: int
    reason_if_skipped: str | None


# ─────────────────────────── helpers ───────────────────────────


def _resolve_cap(user: User) -> int:
    """Thin alias to the shared resolver (kept for local call-site readability)."""
    return send_caps.resolve_daily_cap(user)


def _is_eligible(user: User) -> str | None:
    """Returns a skip reason if the user is NOT eligible for batch generation,
    or None if they are. Order matters: most fundamental gates first."""
    if user.is_suspended:
        return "suspended"
    if user.tier not in _TIER_DEFAULT_CAPS:
        return "pending_tier"
    if not user.has_gmail_connected:
        # The send worker would 401 anyway. Save the cycles.
        return "gmail_disconnected"
    return None


def _reset_daily_counter_if_new_day(
    db: OrmSession, user: User, batch_date: date
) -> None:
    """Zero `sent_today` once per day. Idempotent within a day."""
    if user.last_reset_date == batch_date:
        return
    user.sent_today = 0
    user.last_reset_date = batch_date
    db.add(user)


def _load_candidates(db: OrmSession) -> list[ContactCandidate]:
    """Pull every valid (contact, company) pair into ContactCandidate rows.

    Filter: `is_invalid=False` and `email IS NOT NULL`. The picker would do
    these checks too, but pushing them into SQL keeps the candidate list small.
    """
    rows = db.execute(
        select(Contact.id, Contact.company_id, Company.domain, Contact.email, Contact.is_invalid)
        .join(Company, Contact.company_id == Company.id)
        .where(Contact.is_invalid.is_(False))
        .where(Contact.email.is_not(None))
    ).all()
    return [
        ContactCandidate(
            contact_id=int(r[0]),
            company_id=int(r[1]),
            company_domain=str(r[2]),
            email=str(r[3]),
            is_invalid=bool(r[4]),
        )
        for r in rows
    ]


def _build_filter_sets(
    db: OrmSession, user: User, *, now: datetime
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return (excluded, user_locks, platform_permanent, cooldown) domain sets."""
    excluded = {row.domain for row in prefs_repo.list_excluded_domains(db, user.id)}
    user_locks = {
        row.company_domain
        for row in locks_repo.list_active_user_locks(db, user.id, now=now)
    }
    platform_permanent = {row.company_domain for row in locks_repo.list_platform_locks(db)}
    cooldown = {
        row.company_domain
        for row in locks_repo.list_active_global_locks(db, now=now)
    }
    return excluded, user_locks, platform_permanent, cooldown


def _is_autopilot_active(user: User) -> bool:
    """Autopilot is active when the user opted in AND hasn't paused.
    Free users can't enable autopilot via /preferences (validation lives
    there) but if they ever could, tier alone shouldn't override the flag —
    keep the check on the columns."""
    return bool(user.autopilot_enabled) and user.autopilot_paused_at is None


def _render_default_template(
    *, to_contact: Contact | None, company: Company | None, sender_name: str | None
) -> tuple[str, str]:
    """Fallback render for users with no templates (shouldn't happen post-seed,
    but defensive). Routes the hardcoded default through the shared renderer so
    the full placeholder set + fallbacks apply consistently."""
    return templates_svc.render_template(
        _DEFAULT_SUBJECT,
        _DEFAULT_BODY,
        to_contact=to_contact,
        company=company,
        sender_name=sender_name,
    )


# ─────────────────────────── public API ───────────────────────────


def generate_batch_for_user(
    db: OrmSession,
    user: User,
    *,
    batch_date: date,
    rng: Random | None = None,
) -> BatchGenerationResult:
    """End-to-end batch generation for one user. Owns the commit boundary."""
    rng = rng or Random()

    skip_reason = _is_eligible(user)
    if skip_reason is not None:
        return BatchGenerationResult(
            user_id=user.id,
            batch_date=batch_date,
            items_created=0,
            items_skipped=0,
            reason_if_skipped=skip_reason,
        )

    if today_repo.has_batch_for_date(db, user.id, batch_date):
        return BatchGenerationResult(
            user_id=user.id,
            batch_date=batch_date,
            items_created=0,
            items_skipped=0,
            reason_if_skipped="already_run_today",
        )

    _reset_daily_counter_if_new_day(db, user, batch_date)

    cap = _resolve_cap(user)
    if cap <= 0:
        return BatchGenerationResult(
            user_id=user.id,
            batch_date=batch_date,
            items_created=0,
            items_skipped=0,
            reason_if_skipped="pending_tier",
        )

    now = utcnow()
    candidates = _load_candidates(db)
    excluded, user_locks, platform_perm, cooldown = _build_filter_sets(
        db, user, now=now
    )
    # Per-user "I already emailed this contact" 30-day cooldown — keeps
    # tomorrow's batch fresh by skipping contacts this user has touched recently.
    blocked_contact_ids = cooldown_repo.list_blocked_contact_ids(
        db, user.id, now=now
    )

    picks = pick_companies_for_user(
        user_id=user.id,
        candidates=candidates,
        cap=cap,
        excluded_domains=excluded,
        blocked_user_lock_domains=user_locks,
        blocked_platform_permanent_domains=platform_perm,
        cooldown_domains=cooldown,
        blocked_contact_ids=blocked_contact_ids,
        batch_date=batch_date,
        tier=user.tier,  # type: ignore[arg-type]
        rng=rng,
    )

    if not picks:
        db.commit()  # commit the sent_today reset even if no items
        return BatchGenerationResult(
            user_id=user.id,
            batch_date=batch_date,
            items_created=0,
            items_skipped=0,
            reason_if_skipped="no_eligible_contacts",
        )

    autopilot = _is_autopilot_active(user)
    status = "ready" if autopilot else "default"
    sender_name = user.sender_signature_name or user.full_name

    # Pre-fetch the contact/company rows we'll need for template rendering.
    contact_ids = {pick.to_contact_id for pick in picks}
    company_ids = {pick.company_id for pick in picks}
    contacts_by_id = {
        c.id: c
        for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
    }
    companies_by_id = {
        co.id: co
        for co in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    }

    # The day's batch renders from the user's default template (their first
    # starter / oldest). Manual users can switch a card's template afterward via
    # PATCH /today/items; autopilot uses this default as-is. Falls back to the
    # hardcoded body only if the user somehow has no templates.
    default_tpl = templates_repo.default_for_user(db, user.id)

    items_created = 0
    for pick in picks:
        to_contact = contacts_by_id.get(pick.to_contact_id)
        company = companies_by_id.get(pick.company_id)
        if default_tpl is not None:
            subject, body = templates_svc.render_template(
                default_tpl.subject,
                default_tpl.body,
                to_contact=to_contact,
                company=company,
                sender_name=sender_name,
            )
            template_id = default_tpl.id
        else:
            subject, body = _render_default_template(
                to_contact=to_contact, company=company, sender_name=sender_name
            )
            template_id = None

        item = TodayBatchItem(
            user_id=user.id,
            batch_date=batch_date,
            company_id=pick.company_id,
            company_domain=pick.company_domain,
            to_contact_id=pick.to_contact_id,
            cc_contact_ids=TodayBatchItem.encode_cc(pick.cc_contact_ids),
            template_id=template_id,
            subject=subject,
            body=body,
            send_time=pick.send_time,
            status=status,
        )
        today_repo.add(db, item)
        items_created += 1

    db.commit()
    log.info(
        "batch.generated",
        user_id=user.id,
        batch_date=str(batch_date),
        items_created=items_created,
        cap=cap,
        autopilot=autopilot,
    )
    return BatchGenerationResult(
        user_id=user.id,
        batch_date=batch_date,
        items_created=items_created,
        items_skipped=0,
        reason_if_skipped=None,
    )


def generate_batch_for_all_users(
    db: OrmSession, *, batch_date: date, rng: Random | None = None
) -> list[BatchGenerationResult]:
    """Iterate every user and run generate_batch_for_user. The 'pending'
    gating happens per-user inside `_is_eligible`, so we don't pre-filter
    in SQL — keeps the count of skip reasons accurate for observability.
    """
    rng = rng or Random()
    users = users_repo.list_paginated(db, limit=10_000, offset=0)[0]
    return [
        generate_batch_for_user(db, user, batch_date=batch_date, rng=rng)
        for user in users
    ]

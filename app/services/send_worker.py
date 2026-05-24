"""B5.5 send worker — drains due today_batch_items via Gmail send.

Public entry: `drain_due_items(db, *, now=None) -> DrainSummary`.

Per-item commit boundary: each item's full state transition (today_batch_item
→ sent + send_queue insert + user.sent_today bump + global lock advance) is
ONE commit. On failure, the email_failures row + status='failed' are ALSO
ONE commit. This way a partial drain (e.g. process killed midway) makes
progress without redoing already-sent items.

Concurrency: v0 is single-worker single-process. We use status='ready' as the
guard: the moment we pull an item we transition it to 'sending' and commit;
any concurrent caller skips it. This is approximate row-locking and is good
enough for v0; Turso/libSQL doesn't support FOR UPDATE SKIP LOCKED.

Skip conditions (status='skipped' with skip_reason; no email_failures row):
  - user.is_suspended
  - user.gmail_disconnected
  - to_contact_id missing / contact deleted / contact has no email
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core.time import utcnow
from app.logging_config import get_logger
from app.models import Contact, EmailFailure, SendQueue, TodayBatchItem, User
from app.repositories import email_failures as failures_repo
from app.repositories import locks as locks_repo
from app.services import gmail_send, send_caps
from app.services.google_oauth import OAuthError, get_user_credentials

log = get_logger("send_worker")


# ─────────────────────────── summary ───────────────────────────


@dataclass
class DrainSummary:
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    failures_by_kind: dict[str, int] = field(default_factory=dict)


# ─────────────────────────── helpers ───────────────────────────


def _parse_cc_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [int(x) for x in parsed if isinstance(x, int | str)]
    except (ValueError, TypeError):
        log.warning("send_worker.cc_parse_failed", raw=raw[:200])
    return []


def _load_contacts(db: OrmSession, ids: list[int]) -> dict[int, Contact]:
    if not ids:
        return {}
    rows = db.scalars(select(Contact).where(Contact.id.in_(ids))).all()
    return {c.id: c for c in rows}


def _record_skip(
    db: OrmSession, item: TodayBatchItem, reason: str
) -> None:
    item.status = "skipped"
    item.skip_reason = reason
    db.add(item)
    db.commit()
    log.info(
        "send_worker.skipped",
        today_batch_item_id=item.id,
        user_id=item.user_id,
        reason=reason,
    )


def _record_failure(
    db: OrmSession,
    *,
    item: TodayBatchItem,
    user: User,
    result: gmail_send.SendResult,
    summary: DrainSummary,
) -> None:
    item.status = "failed"
    db.add(item)

    failures_repo.add(
        db,
        user_id=user.id,
        today_batch_item_id=item.id,
        company_domain=item.company_domain,
        failure_kind=result.failure_kind or "unknown",
        error_message=result.error_message or "",
        gmail_error_code=result.gmail_error_code,
    )

    if result.failure_kind == "gmail_auth_revoked":
        user.gmail_disconnected = True
        db.add(user)

    db.commit()

    summary.failed += 1
    kind = result.failure_kind or "unknown"
    summary.failures_by_kind[kind] = summary.failures_by_kind.get(kind, 0) + 1
    log.warning(
        "send_worker.failed",
        today_batch_item_id=item.id,
        user_id=user.id,
        failure_kind=kind,
        gmail_error_code=result.gmail_error_code,
    )


def _record_success(
    db: OrmSession,
    *,
    item: TodayBatchItem,
    user: User,
    to_contact: Contact,
    cc_ids: list[int],
    result: gmail_send.SendResult,
    now: datetime,
    summary: DrainSummary,
) -> None:
    item.status = "sent"
    item.sent_at = now
    item.gmail_message_id = result.gmail_message_id
    item.gmail_thread_id = result.gmail_thread_id
    db.add(item)

    user.sent_today = (user.sent_today or 0) + 1
    db.add(user)

    # Advance the 36h platform cooldown for the company domain.
    locks_repo.upsert_global_lock(
        db, item.company_domain, locked_by_user_id=user.id
    )

    # Audit row in send_queue. `contact_id` is the legacy column; we set it to
    # to_contact_id for back-compat with any old reader.
    sq = SendQueue(
        user_id=user.id,
        contact_id=to_contact.id,
        today_batch_item_id=item.id,
        to_contact_id=to_contact.id,
        cc_contact_ids=json.dumps(cc_ids),
        company_domain=item.company_domain,
        subject=item.subject,
        body_text=item.body,
        gmail_message_id=result.gmail_message_id,
        gmail_thread_id=result.gmail_thread_id,
        kind="INITIAL",
        scheduled_for=item.send_time,
        status="SENT",
        sent_at=now,
    )
    db.add(sq)
    db.commit()

    summary.sent += 1
    log.info(
        "send_worker.sent",
        today_batch_item_id=item.id,
        user_id=user.id,
        company_domain=item.company_domain,
        gmail_message_id=result.gmail_message_id,
    )


# ─────────────────────────── main entry ───────────────────────────


def drain_due_items(
    db: OrmSession,
    *,
    now: datetime | None = None,
    user_id: int | None = None,
    ignore_schedule: bool = False,
) -> DrainSummary:
    """Process 'ready' today_batch_items and send them.

    Defaults (scheduler/autopilot path): every user's 'ready' item whose
    send_time <= now. `user_id` scopes to one user; `ignore_schedule=True`
    drops the send_time gate (the manual "Send today's batch" path, where the
    user explicitly chose to send now regardless of the staggered slot).
    """
    now = now or utcnow()
    summary = DrainSummary()

    # Read the due-set in one query, oldest first. We don't hold a transaction
    # across the iteration — each item runs in its own commit boundary.
    query = select(TodayBatchItem).where(TodayBatchItem.status == "ready")
    if user_id is not None:
        query = query.where(TodayBatchItem.user_id == user_id)
    if not ignore_schedule:
        query = query.where(TodayBatchItem.send_time <= now)
    due = list(db.scalars(query.order_by(TodayBatchItem.send_time.asc())).all())

    for item in due:
        summary.attempted += 1
        # Re-read to avoid acting on stale state if a sibling process raced us.
        db.refresh(item)
        if item.status != "ready":
            summary.skipped += 1
            log.info(
                "send_worker.no_longer_ready",
                today_batch_item_id=item.id,
                status=item.status,
            )
            continue

        user = db.get(User, item.user_id)
        if user is None:
            _record_skip(db, item, reason="user_not_found")
            summary.skipped += 1
            continue
        if user.is_suspended:
            _record_skip(db, item, reason="user_suspended")
            summary.skipped += 1
            continue
        if user.gmail_disconnected:
            _record_skip(db, item, reason="gmail_disconnected")
            summary.skipped += 1
            continue

        # Enforce the daily cap at dispatch — the picker caps batch *size* at
        # generation, but extra 'ready' rows can appear afterward (lazy-gen on
        # GET /today, a user re-readying skipped cards, a lowered cap). Without
        # this re-check the worker would send them all and blow past the limit,
        # wrecking Gmail deliverability. We DON'T mark the item skipped (that's
        # terminal) — leave it 'ready' so it sends after tomorrow's sent_today
        # reset. `user` is the identity-mapped instance, so sent_today stays
        # current across this user's items within the run (each success bumps it).
        cap = send_caps.resolve_daily_cap(user)
        if (user.sent_today or 0) >= cap:
            summary.skipped += 1
            log.info(
                "send_worker.daily_cap_reached",
                user_id=user.id,
                today_batch_item_id=item.id,
                sent_today=user.sent_today,
                cap=cap,
            )
            continue

        if item.to_contact_id is None:
            _record_skip(db, item, reason="no_to_contact")
            summary.skipped += 1
            continue

        to_contact = db.get(Contact, item.to_contact_id)
        if to_contact is None or not to_contact.email:
            _record_skip(db, item, reason="contact_missing_email")
            summary.skipped += 1
            continue

        cc_ids = _parse_cc_ids(item.cc_contact_ids)
        cc_contacts_by_id = _load_contacts(db, cc_ids)
        # Preserve the original CC ordering as planned by the picker.
        cc_emails = [
            cc_contacts_by_id[cid].email
            for cid in cc_ids
            if cid in cc_contacts_by_id and cc_contacts_by_id[cid].email
        ]

        try:
            creds = get_user_credentials(user)
        except OAuthError as e:
            # Treat a missing-refresh-token as auth-revoked so admin sees it.
            _record_failure(
                db,
                item=item,
                user=user,
                result=gmail_send.SendResult(
                    ok=False,
                    failure_kind="gmail_auth_revoked",
                    gmail_error_code=str(e),
                    error_message=f"OAuth credential missing: {e}",
                ),
                summary=summary,
            )
            continue

        result = gmail_send.send_email(
            creds,
            sender_email=user.email,
            sender_name=user.sender_signature_name or user.full_name,
            to_email=to_contact.email,
            cc_emails=cc_emails,
            subject=item.subject,
            body_text=item.body,
        )

        if result.ok:
            _record_success(
                db,
                item=item,
                user=user,
                to_contact=to_contact,
                cc_ids=cc_ids,
                result=result,
                now=now,
                summary=summary,
            )
        else:
            _record_failure(
                db, item=item, user=user, result=result, summary=summary
            )

    log.info(
        "send_worker.drain_complete",
        attempted=summary.attempted,
        sent=summary.sent,
        failed=summary.failed,
        skipped=summary.skipped,
    )
    return summary


# Re-export for tests/admin endpoints that want the failure model.
__all__ = ["DrainSummary", "EmailFailure", "drain_due_items"]

"""B5.6 reply ingestion — pulls Gmail replies, writes locks, flips statuses.

Per-user flow:
  1. Skip suspended / gmail_disconnected users (error_kind='skipped').
  2. Get fresh Gmail credentials via google_oauth.get_user_credentials.
  3. Call gmail_reply_fetcher.fetch_new_replies(creds, start_history_id=…).
     - On FetchError(gmail_auth_revoked) → set user.gmail_disconnected=True
       and commit. Return summary with error_kind.
     - On FetchError(transient/quota_exceeded) → no DB writes, return
       summary with error_kind. Next run will retry.
  4. For each fetched reply:
       a. Match against send_queue rows by (gmail_thread_id, user_id).
          No match → ignore (not a reply to a Knock send).
          Multiple matches (shouldn't happen) → take most recent by sent_at.
       b. Compute is_explicit_stop(body_text).
       c. locks_svc.record_reply_from_company(...).
       d. Mark matched send_queue row status='REPLIED', set replied_at +
          reply_is_explicit_stop.
       e. Flip source today_batch_item.status='replied' (new enum value;
          today_batch_item.status is a String(16), no schema change).
  5. After the loop, set user.gmail_history_id = new_history_id.
  6. ONE commit at the end of the per-user pass (everything is independent;
     a partial-failure replay is idempotent because we re-match by thread_id).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.orm import Session as OrmSession

from app.core.time import ensure_utc
from app.logging_config import get_logger
from app.models import Contact, SendQueue, TodayBatchItem, User
from app.repositories import contacts as contacts_repo
from app.services import email_patterns, gmail_reply_fetcher
from app.services import locks as locks_svc
from app.services.explicit_stop import is_explicit_stop
from app.services.google_oauth import OAuthError, get_user_credentials

log = get_logger("reply_ingestor")


# ─────────────────────────── summary ───────────────────────────


@dataclass(frozen=True, slots=True)
class IngestSummary:
    user_id: int
    processed: int
    replies_matched: int
    explicit_stops: int
    user_reply_locks_written: int
    bounces: int = 0
    error_kind: str | None = None


# ─────────────────────────── helpers ───────────────────────────


def _find_send_queue_row(
    db: OrmSession, *, user_id: int, gmail_thread_id: str
) -> SendQueue | None:
    """Match a reply to the most recent matching Knock send.

    Filters to status IN (SENT, REPLIED) so already-replied threads stay
    correctly matched on idempotent re-runs.
    """
    if not gmail_thread_id:
        return None
    return db.scalar(
        select(SendQueue)
        .where(SendQueue.user_id == user_id)
        .where(SendQueue.gmail_thread_id == gmail_thread_id)
        .order_by(desc(SendQueue.sent_at))
        .limit(1)
    )


def _process_reply(
    db: OrmSession,
    *,
    user: User,
    reply: gmail_reply_fetcher.FetchedReply,
    summary_counters: dict[str, int],
) -> None:
    """Handle one reply: match, classify, lock, flip statuses.

    No commit here — the caller batches the entire user's ingest into one
    commit at the end.
    """
    summary_counters["processed"] += 1

    sq = _find_send_queue_row(
        db, user_id=user.id, gmail_thread_id=reply.gmail_thread_id
    )
    if sq is None:
        # Not a reply to a Knock send (could be any other inbound mail).
        return

    # Bounce path — a delivery-failure notice lands on the original send's
    # thread. Treating it as a reply would lock a DEAD address as if the person
    # engaged. Instead mark the contact invalid (picker excludes it) so the bad
    # address stops recirculating and eroding sender reputation.
    if reply.is_bounce:
        _handle_bounce(db, user=user, sq=sq, summary_counters=summary_counters)
        return

    # Already-processed guard — same thread arriving again (e.g. user marks
    # read/unread, which can churn history events) shouldn't double-write the
    # lock. sq.gmail_message_id is the OUTBOUND id, so we can't compare it to
    # reply.gmail_message_id; status='REPLIED' is the correct idempotency
    # marker here. A future reply on the same thread is also a no-op for the
    # lock (the per-user lock is already rolling-extended by the first hit).
    if sq.status == "REPLIED":
        return

    company_domain = (sq.company_domain or "").strip().lower()
    if not company_domain:
        # Defensive: a SENT row without a company_domain is malformed; skip.
        log.warning(
            "reply_ingestor.send_queue_missing_domain",
            send_queue_id=sq.id,
            user_id=user.id,
        )
        return

    explicit = is_explicit_stop(reply.body_text or "")

    summary_counters["replies_matched"] += 1
    if explicit:
        summary_counters["explicit_stops"] += 1
    else:
        summary_counters["user_reply_locks_written"] += 1

    # Locks (no commit inside the service — caller-owns-txn).
    locks_svc.record_reply_from_company(
        db,
        user_id=user.id,
        company_domain=company_domain,
        is_explicit_stop=explicit,
    )

    # Flip send_queue row.
    sq.status = "REPLIED"
    sq.replied_at = ensure_utc(reply.internal_date)
    sq.reply_is_explicit_stop = explicit
    db.add(sq)

    # Flip the source today_batch_item if it's still tracking this send.
    if sq.today_batch_item_id is not None:
        tbi = db.get(TodayBatchItem, sq.today_batch_item_id)
        if tbi is not None and tbi.status != "replied":
            tbi.status = "replied"
            db.add(tbi)

    # Cancel any pending follow-ups planned against THIS thread — once the
    # recruiter replied, a bot-style "just bumping this up" follow-up would
    # be the worst-feeling failure mode of the feature. We mark them skipped
    # (not deleted) for audit trail.
    pending_followups = list(
        db.scalars(
            select(TodayBatchItem)
            .where(TodayBatchItem.user_id == user.id)
            .where(TodayBatchItem.kind == "followup")
            .where(TodayBatchItem.status.in_(("default", "ready")))
            .where(
                TodayBatchItem.parent_send_queue_id.in_(
                    select(SendQueue.id).where(
                        SendQueue.user_id == user.id,
                        SendQueue.gmail_thread_id == reply.gmail_thread_id,
                    )
                )
            )
        ).all()
    )
    for pf in pending_followups:
        pf.status = "skipped"
        pf.skip_reason = "reply_received"
        db.add(pf)

    log.info(
        "reply_ingestor.reply_recorded",
        user_id=user.id,
        send_queue_id=sq.id,
        company_domain=company_domain,
        is_explicit_stop=explicit,
        thread_id=reply.gmail_thread_id,
        followups_cancelled=len(pending_followups),
    )


def _handle_bounce(
    db: OrmSession,
    *,
    user: User,
    sq: SendQueue,
    summary_counters: dict[str, int],
) -> None:
    """Mark the bounced send's TO contact invalid and flag the send as bounced.

    No reply lock is written. If the contact came from the scraper (it carries
    a `scraped_pattern`), the address was a *guess* that failed — the scraper's
    retry path should try the next email-guess pattern (e.g. firstname.lastname
    → f.lastname). That alternate-pattern generation ships WITH the scraper; for
    now we mark invalid + log the retry signal so no scraped pattern is lost.
    No commit here — caller owns the txn.
    """
    summary_counters["bounces"] += 1
    sq.status = "BOUNCED"
    db.add(sq)

    contact_id = sq.to_contact_id or sq.contact_id
    contact = db.get(Contact, contact_id) if contact_id else None
    if contact is None:
        log.warning("reply_ingestor.bounce_contact_missing", send_queue_id=sq.id)
        return

    # SCRAPER path only: a scraped address is a GUESS, so on bounce try the next
    # guess pattern (firstname.lastname → firstname → …) instead of giving up.
    # CSV/manually-curated contacts (no scraped_pattern) are assumed real — they
    # skip this and get invalidated for admin review.
    if contact.scraped_pattern:
        nxt = email_patterns.next_guess(
            contact.name, sq.company_domain or "", contact.scraped_pattern
        )
        if nxt is not None:
            next_pattern, next_email = nxt
            # Don't collide with an existing contact's address.
            if contacts_repo.get_by_email(db, next_email) is None:
                contact.email = next_email
                contact.scraped_pattern = next_pattern
                contact.email_verified = False
                contact.is_invalid = False  # fresh guess — keep it in rotation
                contact.invalid_reason = None
                db.add(contact)
                log.info(
                    "reply_ingestor.scraped_pattern_advanced",
                    contact_id=contact.id,
                    company_domain=sq.company_domain,
                    next_pattern=next_pattern,
                )
                return
        # Exhausted all patterns (or every next guess collided) → give up.
        contact.is_invalid = True
        contact.invalid_reason = "bounce_patterns_exhausted"
        db.add(contact)
        log.info(
            "reply_ingestor.scraped_patterns_exhausted",
            contact_id=contact.id,
            company_domain=sq.company_domain,
        )
        return

    # CSV / manual contact: assumed real, so a bounce means the address is dead.
    # Invalidate (leaves every user's pool) + flag for admin review/delete.
    contact.is_invalid = True
    contact.invalid_reason = "bounce"
    db.add(contact)
    log.info(
        "reply_ingestor.contact_bounced_invalidated",
        user_id=user.id,
        contact_id=contact.id,
        company_domain=sq.company_domain,
    )


def _empty_counters() -> dict[str, int]:
    return {
        "processed": 0,
        "replies_matched": 0,
        "explicit_stops": 0,
        "user_reply_locks_written": 0,
        "bounces": 0,
    }


# ─────────────────────────── public API ───────────────────────────


def ingest_replies_for_user(db: OrmSession, user: User) -> IngestSummary:
    """Pull and process new replies for one user. Returns a summary.

    Skip cases (no DB writes):
      - user.is_suspended
      - user.gmail_disconnected
      - Missing OAuth creds → also flips gmail_disconnected and commits.
    """
    counters = _empty_counters()

    if user.is_suspended:
        return IngestSummary(
            user_id=user.id,
            **counters,
            error_kind="skipped_suspended",
        )
    if user.gmail_disconnected:
        return IngestSummary(
            user_id=user.id,
            **counters,
            error_kind="skipped_disconnected",
        )

    # Credentials
    try:
        creds = get_user_credentials(user)
    except OAuthError as e:
        # No tokens → equivalent to revoked; mark disconnected so the worker
        # stops attempting until the user reconnects.
        user.gmail_disconnected = True
        db.add(user)
        db.commit()
        log.warning(
            "reply_ingestor.oauth_error", user_id=user.id, error=str(e)
        )
        return IngestSummary(
            user_id=user.id,
            **counters,
            error_kind="gmail_auth_revoked",
        )

    # Fetch
    try:
        replies, new_history_id = gmail_reply_fetcher.fetch_new_replies(
            creds, start_history_id=user.gmail_history_id
        )
    except gmail_reply_fetcher.FetchError as e:
        if e.kind == "gmail_auth_revoked":
            user.gmail_disconnected = True
            db.add(user)
            db.commit()
        log.warning(
            "reply_ingestor.fetch_error", user_id=user.id, kind=e.kind
        )
        return IngestSummary(
            user_id=user.id,
            **counters,
            error_kind=e.kind,
        )

    # Process
    for reply in replies:
        _process_reply(db, user=user, reply=reply, summary_counters=counters)

    # Advance the cursor. We do this even on bootstrap (replies=[]) so the
    # next run picks up only what's genuinely new.
    if new_history_id and (
        user.gmail_history_id is None or new_history_id > user.gmail_history_id
    ):
        user.gmail_history_id = new_history_id
        db.add(user)

    db.commit()

    summary = IngestSummary(
        user_id=user.id,
        **counters,
    )
    log.info(
        "reply_ingestor.user_done",
        user_id=user.id,
        processed=summary.processed,
        replies_matched=summary.replies_matched,
        explicit_stops=summary.explicit_stops,
        new_history_id=new_history_id,
    )
    return summary


def ingest_replies_for_all_users(db: OrmSession) -> list[IngestSummary]:
    """Iterate every user eligible for ingest. Order: id asc for determinism.

    Eligible = has google_refresh_token AND tier != 'pending' AND not suspended.
    (We still call ingest_replies_for_user for suspended-checks at runtime to
    keep skip semantics in one place; the WHERE clause is just to avoid
    spamming the API for users who never connected Gmail.)
    """
    rows = list(
        db.scalars(
            select(User)
            .where(User.google_refresh_token.is_not(None))
            .where(User.tier != "pending")
            .order_by(User.id.asc())
        ).all()
    )

    summaries: list[IngestSummary] = []
    for user in rows:
        try:
            summaries.append(ingest_replies_for_user(db, user))
        except Exception as e:  # pragma: no cover — defensive
            # One bad user shouldn't poison the whole run. Roll back the
            # session and continue with the next user.
            log.exception(
                "reply_ingestor.user_failed", user_id=user.id, error=str(e)
            )
            db.rollback()
            summaries.append(
                IngestSummary(
                    user_id=user.id,
                    processed=0,
                    replies_matched=0,
                    explicit_stops=0,
                    user_reply_locks_written=0,
                    error_kind="unknown",
                )
            )
    return summaries


__all__ = [
    "IngestSummary",
    "ingest_replies_for_all_users",
    "ingest_replies_for_user",
]

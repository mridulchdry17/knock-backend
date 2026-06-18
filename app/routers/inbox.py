"""/api/v1/inbox — replies + bounces for the current user.

Sources rows from `send_queue` and projects them into the F.7 frontend wire
shape (see `app/schemas/inbox.py`). Categories:
  - reply  ← status='REPLIED'
  - bounce ← status='BOUNCED' (set by the reply ingestor's _handle_bounce)
  - nudge  ← reserved; no data yet, the filter returns empty 200.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, func, select

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.core.pagination import PaginationParams, pagination
from app.core.time import ensure_utc, utcnow
from app.logging_config import get_logger
from app.models import Company, Contact, SendQueue, User
from app.schemas.common import Ok
from app.schemas.inbox import (
    InboxCategoryLit,
    InboxItemOut,
    InboxListOut,
    InboxSenderOut,
    InboxSyncStatusOut,
    ReplyIn,
    ReplyResultOut,
    ThreadDetailOut,
    ThreadMessageOut,
    ThreadParticipantOut,
)
from app.services import gmail_send
from app.services.google_oauth import OAuthError, get_user_credentials

router = APIRouter(
    prefix="/api/v1/inbox",
    tags=["inbox"],
    dependencies=[Depends(require_tier("free", "paid", "super_admin"))],
)

log = get_logger("inbox")

# Status → frontend category mapping. The inbox only surfaces rows in these
# statuses; sent-but-no-response and skipped rows live in /today, not here.
_STATUS_TO_CATEGORY: dict[str, InboxCategoryLit] = {
    "REPLIED": "reply",
    "BOUNCED": "bounce",
}


def _snippet(body_text: str | None, *, length: int = 140) -> str:
    """One-line preview for the list row. Trim to a single line + length cap."""
    if not body_text:
        return ""
    flat = " ".join(body_text.split())  # collapse whitespace/newlines
    if len(flat) <= length:
        return flat
    return flat[: length - 1].rstrip() + "…"


@router.get("", response_model=InboxListOut)
def list_inbox(
    user: CurrentUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    category: Annotated[InboxCategoryLit | None, Query()] = None,
) -> InboxListOut:
    """Newest-first list of inbox items for the current user.

    `category=reply|bounce|nudge` filters the rows; omit it for All (replies
    + bounces). Empty result is a valid 200 — frontend renders 'All caught up.'
    """
    # Map category → status filter.
    if category == "reply":
        statuses: list[str] = ["REPLIED"]
    elif category == "bounce":
        statuses = ["BOUNCED"]
    elif category == "nudge":
        # No nudge data in v0 — return empty 200 so the tab doesn't snag.
        return InboxListOut(items=[], total=0, unread_count=0)
    else:  # None / 'all'
        statuses = ["REPLIED", "BOUNCED"]

    # COALESCE replied_at, sent_at — bounces don't set replied_at, so order by
    # whichever timestamp the row has. Newest first.
    order_ts = func.coalesce(SendQueue.replied_at, SendQueue.sent_at)

    rows = list(
        db.scalars(
            select(SendQueue)
            .where(SendQueue.user_id == user.id)
            .where(SendQueue.status.in_(statuses))
            .order_by(desc(order_ts))
            .limit(page.limit)
            .offset(page.offset)
        ).all()
    )
    total = (
        db.scalar(
            select(func.count())
            .select_from(SendQueue)
            .where(SendQueue.user_id == user.id)
            .where(SendQueue.status.in_(statuses))
        )
        or 0
    )

    # Bulk-hydrate the TO contact for each row in one query (no N+1).
    contact_ids = {r.to_contact_id for r in rows if r.to_contact_id is not None}
    contacts_by_id: dict[int, Contact] = {}
    if contact_ids:
        contacts_by_id = {
            c.id: c
            for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
        }

    items: list[InboxItemOut] = []
    for r in rows:
        contact = contacts_by_id.get(r.to_contact_id or 0)
        sender = InboxSenderOut(
            name=(contact.name if contact else None),
            email=(contact.email if contact else "") or "",
        )
        cat: InboxCategoryLit = _STATUS_TO_CATEGORY.get(r.status, "reply")
        # libsql strips tzinfo on storage — re-attach UTC at the boundary.
        # Fallback chain: replied_at (REPLIED rows have it) → sent_at (BOUNCED
        # rows always do — they came from already-dispatched send_queue rows)
        # → created_at (NOT NULL via the mixin; defensive last-resort so the
        # response schema can never see a missing timestamp).
        ts = ensure_utc(r.replied_at or r.sent_at or r.created_at)
        items.append(
            InboxItemOut(
                id=str(r.id),
                category=cat,
                subject=r.subject or "",
                sender=sender,
                snippet=_snippet(r.body_text),
                last_message_at=ts,
                unread=False,  # no unread tracking in v0
                message_count=1,
            )
        )

    return InboxListOut(items=items, total=total, unread_count=0)


@router.get("/sync-status", response_model=InboxSyncStatusOut)
def get_sync_status(user: CurrentUser, db: DbDep) -> InboxSyncStatusOut:
    """Powers the "synced X mins ago" indicator on the inbox.

    v0: `last_synced_at` is the most recent `replied_at` we've recorded for
    this user — accurate when replies are flowing, null on a quiet day.
    `healthy` is True whenever the endpoint can answer.

    NOTE — declared BEFORE the /{item_id} route on purpose: item_id is typed as
    `int`, so FastAPI would 422 the literal path /sync-status if /{item_id}
    were registered first (it tries to coerce 'sync-status' to int rather than
    falling through to the next route).
    """
    last_replied = db.scalar(
        select(SendQueue.replied_at)
        .where(SendQueue.user_id == user.id)
        .where(SendQueue.status == "REPLIED")
        .order_by(desc(SendQueue.replied_at))
        .limit(1)
    )
    return InboxSyncStatusOut(
        healthy=True,
        last_synced_at=ensure_utc(last_replied) if last_replied else None,
    )


def _load_owned_row(db, user: User, item_id: int) -> SendQueue:
    """Fetch a send_queue row scoped to the current user, or raise 404.

    Single 404 for both 'not found' and 'belongs to another user' — leaking
    existence is the kind of side channel that doesn't matter much for an
    outreach tool but costs nothing to close.
    """
    sq = db.scalar(
        select(SendQueue).where(SendQueue.id == item_id, SendQueue.user_id == user.id)
    )
    if sq is None:
        raise ApiError(
            "not_found", "Inbox item not found.", status_code=status.HTTP_404_NOT_FOUND
        )
    return sq


@router.get("/{item_id}", response_model=ThreadDetailOut, response_model_by_alias=True)
def get_thread(item_id: int, user: CurrentUser, db: DbDep) -> ThreadDetailOut:
    """Detail view for one inbox row — our outbound plus the latest inbound reply.

    Scope is intentionally narrow: we surface the conversation Knock initiated
    (the send_queue row + its denormalized reply), not the full Gmail thread.
    Bounces show only the outbound (no real recipient replied); reply rows show
    both messages when the reply body was ingested.

    Response shape is locked to the frontend's `ThreadDetailSchema`:
      { id, subject, category, sender, messages, suggested_followup }
    `response_model_by_alias=True` so messages serialize `from_` as `from`.
    """
    sq = _load_owned_row(db, user, item_id)

    # Only REPLIED + BOUNCED rows belong to the inbox surface — SENT-with-no-
    # response lives on /today, and clicking through here would be misleading.
    if sq.status not in ("REPLIED", "BOUNCED"):
        raise ApiError(
            "not_found", "Inbox item not found.", status_code=status.HTTP_404_NOT_FOUND
        )

    category: InboxCategoryLit = _STATUS_TO_CATEGORY.get(sq.status, "reply")

    # `sender` on the thread is the RECRUITER — the contact we addressed. The
    # frontend renders this in the thread header (name • role • company).
    # Hydrate contact + company together; both can be missing on legacy/test rows.
    to_contact: Contact | None = (
        db.get(Contact, sq.to_contact_id) if sq.to_contact_id is not None else None
    )
    company: Company | None = (
        db.get(Company, to_contact.company_id)
        if to_contact is not None and to_contact.company_id is not None
        else None
    )

    thread_sender = ThreadParticipantOut(
        name=(to_contact.name if to_contact else None),
        email=(to_contact.email if to_contact and to_contact.email else (sq.reply_from_email or "")),
        role=(to_contact.role if to_contact else None),
        company=(company.name if company else None),
    )

    # `from` on each message is the AUTHOR. Outbound = the Knock user; inbound =
    # whoever wrote back to us (reply_from_email, with the contact's name when
    # the from_email matches that contact).
    me = InboxSenderOut(
        name=getattr(user, "full_name", None) or None,
        email=user.email,
    )

    outbound = ThreadMessageOut(
        id=f"sq-{sq.id}-out",
        direction="outbound",
        from_=me,
        body_html=sq.body_text or "",
        sent_at=ensure_utc(sq.sent_at or sq.created_at),
    )

    messages: list[ThreadMessageOut] = [outbound]

    # Inbound — present only when the reply ingestor stored a body for this row.
    # Bounce rows never get a reply_body_text (the ingestor's bounce path takes
    # a different branch), so this naturally yields a single-message thread for
    # them.
    if sq.reply_body_text:
        inbound_email = sq.reply_from_email or ""
        inbound_name: str | None = None
        if to_contact is not None:
            # Only attach the contact's name when the reply actually came from
            # that contact's address — otherwise the reply is from a colleague
            # on the thread and we don't know their name.
            if to_contact.email and inbound_email and (
                to_contact.email.lower() == inbound_email.lower()
            ):
                inbound_name = to_contact.name
            # Fallback for legacy rows that didn't persist from_email.
            if not inbound_email and to_contact.email:
                inbound_email = to_contact.email
                inbound_name = to_contact.name

        inbound_ts = ensure_utc(
            sq.reply_internal_date or sq.replied_at or sq.sent_at or sq.created_at
        )
        messages.append(
            ThreadMessageOut(
                id=f"sq-{sq.id}-in",
                direction="inbound",
                from_=InboxSenderOut(name=inbound_name, email=inbound_email),
                body_html=sq.reply_body_text,
                sent_at=inbound_ts,
            )
        )

    # User's outbound reply from Knock (POST /inbox/{id}/reply). Surfaced AFTER
    # the inbound because chronologically a reply comes after the message it's
    # responding to; v0 stores only the most recent outbound reply so this is
    # at most one row.
    if sq.outbound_reply_text:
        messages.append(
            ThreadMessageOut(
                id=f"sq-{sq.id}-out-reply",
                direction="outbound",
                from_=me,
                body_html=sq.outbound_reply_text,
                sent_at=ensure_utc(
                    sq.outbound_reply_sent_at or sq.replied_at or sq.created_at
                ),
            )
        )

    return ThreadDetailOut(
        id=str(sq.id),
        subject=sq.subject or "",
        category=category,
        sender=thread_sender,
        messages=messages,
        suggested_followup=None,  # reserved — feature not built yet
    )


def _reply_subject(original: str | None) -> str:
    """Re:-prefix the original subject, but don't double-prefix if it's already
    a reply chain. Case-insensitive on the prefix; preserves the original
    user-authored text verbatim."""
    s = (original or "").strip()
    if not s:
        return "Re:"
    if s.lower().startswith("re:"):
        return s
    return f"Re: {s}"


def _recipient_for_reply(sq: SendQueue, db) -> str | None:
    """Pick the To address for an outbound reply.

    Default: the address that wrote back to us (`reply_from_email`) — the
    recruiter may have replied from a different alias than the one we addressed,
    and going back to whoever wrote is the conventional behavior.

    Fallback: the original `to_contact.email` for legacy rows where the
    ingestor didn't store from_email.
    """
    if sq.reply_from_email:
        return sq.reply_from_email
    if sq.to_contact_id is not None:
        contact = db.get(Contact, sq.to_contact_id)
        if contact is not None and contact.email:
            return contact.email
    return None


@router.post("/{item_id}/reply", response_model=ReplyResultOut)
def post_reply(
    item_id: int, payload: ReplyIn, user: CurrentUser, db: DbDep
) -> ReplyResultOut:
    """Send a reply on the original Gmail thread, from inside Knock.

    Threading mechanics are delegated to `gmail_send.send_followup` — it sets
    In-Reply-To + References + threadId so the reply lands inside the same
    Gmail conversation. We persist the sent body on the send_queue row so the
    next detail render shows the user their own reply without re-fetching.

    The endpoint is intentionally synchronous: replies are user-initiated and
    low-volume (vs. the scheduled batch worker), and the user wants to see a
    confirmation immediately. Errors classify the same way the send worker does
    — quota / auth-revoked / transient / recipient-rejected — and surface as
    structured 4xx/5xx so the frontend can show specific copy.
    """
    sq = _load_owned_row(db, user, item_id)

    # Same gating as can_reply on the detail view. Returning these as 409 so the
    # frontend can distinguish "the row exists but can't be replied to" from a
    # plain 404 — useful for showing a specific error message.
    if sq.status != "REPLIED":
        raise ApiError(
            "cannot_reply",
            "This item can't be replied to.",
            status_code=status.HTTP_409_CONFLICT,
        )
    if not sq.gmail_thread_id:
        raise ApiError(
            "cannot_reply",
            "Original thread is missing — replies aren't supported on this item.",
            status_code=status.HTTP_409_CONFLICT,
        )

    # Pass-through: backend stores whatever the composer sent (Tiptap's HTML by
    # default). Emptiness is a frontend concern (Tiptap emits `<p></p>` for an
    # empty editor — the composer guards against that before POSTing); here we
    # only reject a literally empty/whitespace-only payload.
    body_html = (payload.body_html or "").strip()
    if not body_html:
        raise ApiError(
            "invalid_input",
            "Reply body is required.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    to_email = _recipient_for_reply(sq, db)
    if not to_email:
        raise ApiError(
            "cannot_reply",
            "Recipient address not found.",
            status_code=status.HTTP_409_CONFLICT,
        )

    try:
        creds = get_user_credentials(user)
    except OAuthError as e:
        # Mirrors the send worker's gmail_auth_revoked path. The frontend can
        # show a "reconnect Gmail" CTA on this exact code.
        raise ApiError(
            "gmail_auth_revoked",
            f"Gmail credentials are no longer valid: {e}",
            status_code=status.HTTP_401_UNAUTHORIZED,
        ) from e

    subject = (payload.subject or "").strip() or _reply_subject(sq.subject)

    result = gmail_send.send_followup(
        creds,
        sender_email=user.email,
        sender_name=user.sender_signature_name or user.full_name,
        to_email=to_email,
        cc_emails=[],  # v0: no CC on user-initiated replies
        subject=subject,
        # build_mime flattens HTML → plain text via html_to_text; the wire is
        # still text/plain. We pass the HTML so authored formatting survives
        # at least visually (paragraph breaks etc.) when flattened.
        body_text=body_html,
        gmail_thread_id=sq.gmail_thread_id,
        # Best-effort In-Reply-To header. We persist rfc822_message_id at send
        # time, but it may be missing for older rows; Gmail's threadId alone
        # still threads correctly on Gmail's side.
        in_reply_to_rfc822_id=sq.rfc822_message_id,
    )

    if not result.ok:
        # Map gmail_send's failure_kind taxonomy to HTTP. Same taxonomy the
        # send worker logs into email_failures.failure_kind so the dashboard
        # error-counts stay coherent across batch + interactive sends.
        kind = result.failure_kind or "unknown"
        if kind == "gmail_auth_revoked":
            http_status = status.HTTP_401_UNAUTHORIZED
        elif kind == "quota_exceeded":
            http_status = status.HTTP_429_TOO_MANY_REQUESTS
        elif kind == "recipient_rejected":
            http_status = status.HTTP_400_BAD_REQUEST
        elif kind == "transient":
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            http_status = status.HTTP_502_BAD_GATEWAY

        log.warning(
            "inbox.reply_failed",
            user_id=user.id,
            send_queue_id=sq.id,
            failure_kind=kind,
            gmail_error_code=result.gmail_error_code,
        )
        raise ApiError(
            kind,
            result.error_message or "Gmail send failed.",
            status_code=http_status,
        )

    # Persist on the send_queue row so the next detail render surfaces this
    # send without a Gmail roundtrip. The column is named *_text for legacy
    # reasons (added before Tiptap shipped) — it stores Tiptap's HTML now;
    # GET /inbox/{id} passes it through to body_html directly (no _to_html
    # wrap, which would double-escape).
    sq.outbound_reply_text = body_html
    sq.outbound_reply_sent_at = utcnow()
    db.add(sq)
    db.commit()

    log.info(
        "inbox.reply_sent",
        user_id=user.id,
        send_queue_id=sq.id,
        gmail_thread_id=result.gmail_thread_id,
    )

    # message_id (not gmail_message_id) matches the frontend's ReplyResultSchema
    # — empty-string fallback because the schema requires a string, and a
    # successful Gmail send always returns one.
    return ReplyResultOut(
        ok=True,
        message_id=result.gmail_message_id or "",
    )


# Stubs for state tracking the frontend's inbox page calls but that have no
# v0 backend storage. Returning 200 keeps the page from snagging on the
# fire-and-forget read mark and the user-initiated done click. Persistence
# (an `inbox_state` row per (user, send_queue_id)) is a future migration.


@router.post("/{item_id}/read", response_model=Ok)
def post_mark_read(item_id: int, user: CurrentUser, db: DbDep) -> Ok:
    """No-op marker that the row has been read. v0 has no `read_at` column;
    the frontend calls this fire-and-forget on first load, so we just confirm
    ownership (404 otherwise) and return ok=True."""
    _load_owned_row(db, user, item_id)
    return Ok()


@router.post("/{item_id}/done", response_model=Ok)
def post_mark_done(item_id: int, user: CurrentUser, db: DbDep) -> Ok:
    """No-op marker that the user is done with the row. v0 has no `done_at`
    column; the UI removes the card optimistically on a 200, so we just
    confirm ownership and return ok=True. Real persistence is a future
    migration once inbox-zero state lands."""
    _load_owned_row(db, user, item_id)
    return Ok()

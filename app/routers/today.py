"""/api/v1/today — today's batch review surface for the user.

GET returns the user's pre-generated cards (one per company outreach).
PATCH /items/{id} lets the user edit subject/body/send_time/status before
the send worker (B5.5) picks the row up.

Tier-gated to free/paid/super_admin. Pending users get 403 (frontend routes
them to /awaiting-approval).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.core.time import ensure_utc, utcnow
from app.logging_config import get_logger
from app.models import Company, Contact, SendQueue, Template, TodayBatchItem
from app.repositories import templates as templates_repo
from app.repositories import today_batch as today_repo
from app.schemas.today import (
    ApplyTemplateIn,
    ApplyTemplateResultOut,
    ParentSendSummaryOut,
    RecipientOut,
    SendBatchResultOut,
    SkipTodayResultOut,
    TodayBatchOut,
    TodayItemOut,
    UpdateItemIn,
)
from app.services import batch_generator as batch_gen_svc
from app.services import send_caps, send_scheduling, send_worker
from app.services import templates as templates_svc

router = APIRouter(
    prefix="/api/v1/today",
    tags=["today"],
    dependencies=[Depends(require_tier("free", "paid", "super_admin"))],
)

log = get_logger("today")


# ─────────────────────────── helpers ───────────────────────────


def _body_preview(body: str, *, length: int = 200) -> str:
    return body[:length]


def _recipient_from_contact(contact: Contact, company: Company) -> RecipientOut:
    return RecipientOut(
        name=contact.name,
        email=contact.email or "",
        role=contact.role,
        company=company.name,
        company_domain=company.domain,
        linkedin_url=contact.linkedin_url,
        avatar_url=None,
    )


def _item_to_out(
    item: TodayBatchItem,
    *,
    contacts_by_id: dict[int, Contact],
    company: Company | None,
    parent_by_id: dict[int, SendQueue] | None = None,
    template_name_by_id: dict[int, str] | None = None,
) -> TodayItemOut:
    # If the company row vanished (cascade somehow lost it) fall back to the
    # denormalized domain to keep the response usable. Defensive — shouldn't
    # happen under normal operation thanks to FK CASCADE semantics.
    if company is None:
        fallback_company = Company(
            id=item.company_id, domain=item.company_domain, name=item.company_domain, source=""
        )
        company = fallback_company

    to_contact = contacts_by_id.get(item.to_contact_id)
    if to_contact is None:
        # The TO contact got deleted between batch generation and now. We still
        # return the card with a placeholder so the user sees WHY it's stale.
        to_recipient = RecipientOut(
            name=None,
            email="(contact removed)",
            role=None,
            company=company.name,
            company_domain=company.domain,
            linkedin_url=None,
            avatar_url=None,
        )
    else:
        to_recipient = _recipient_from_contact(to_contact, company)

    cc_recipients: list[RecipientOut] = []
    for cc_id in item.get_cc_contact_ids():
        cc_contact = contacts_by_id.get(cc_id)
        if cc_contact is None:
            continue
        cc_recipients.append(_recipient_from_contact(cc_contact, company))

    # Follow-up cards carry the originating send's subject/body/sent_at so the
    # reading pane can show "Sent 4 days ago · no reply yet" with the prose
    # collapsed below. parent_by_id is keyed on send_queue.id; absent on
    # initial cards.
    parent_out: ParentSendSummaryOut | None = None
    if (
        item.kind == "followup"
        and item.parent_send_queue_id is not None
        and parent_by_id is not None
    ):
        parent_sq = parent_by_id.get(item.parent_send_queue_id)
        if parent_sq is not None and parent_sq.sent_at is not None:
            parent_out = ParentSendSummaryOut(
                original_subject=parent_sq.subject or "",
                original_body=parent_sq.body_text or "",
                original_sent_at=ensure_utc(parent_sq.sent_at),
            )

    return TodayItemOut(
        id=str(item.id),
        recipient=to_recipient,
        cc_recipients=cc_recipients,
        template_id=str(item.template_id) if item.template_id is not None else None,
        template_name=(
            template_name_by_id.get(item.template_id)
            if (template_name_by_id is not None and item.template_id is not None)
            else None
        ),
        subject=item.subject,
        body_preview=_body_preview(item.body),
        body=item.body,
        # libsql/SQLite strips tzinfo on storage, so item.send_time comes back
        # naive even though we wrote it tz-aware. Re-attach UTC at the boundary
        # so the JSON contract always has an offset — Zod/strict client schemas
        # reject naive datetime strings.
        send_time=ensure_utc(item.send_time),
        status=item.status,  # type: ignore[arg-type]
        cooldown_until=None,
        sent_at=None,
        kind=item.kind if item.kind in ("initial", "followup") else "initial",  # type: ignore[arg-type]
        followup_index=item.followup_index,
        parent=parent_out,
    )


def _hydrate(db, items: list[TodayBatchItem]) -> list[TodayItemOut]:
    """Bulk-fetch contacts + companies + (for follow-ups) parent send_queue rows
    in one round-trip each. Beats the N+1 we'd get from looking them up per-item.
    """
    if not items:
        return []

    contact_ids: set[int] = set()
    company_ids: set[int] = set()
    parent_send_queue_ids: set[int] = set()
    template_ids: set[int] = set()
    for item in items:
        contact_ids.add(item.to_contact_id)
        contact_ids.update(item.get_cc_contact_ids())
        company_ids.add(item.company_id)
        if item.parent_send_queue_id is not None:
            parent_send_queue_ids.add(item.parent_send_queue_id)
        if item.template_id is not None:
            template_ids.add(item.template_id)

    contacts_by_id = {
        c.id: c
        for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
    }
    companies_by_id = {
        co.id: co
        for co in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    }
    parent_by_id: dict[int, SendQueue] = {}
    if parent_send_queue_ids:
        parent_by_id = {
            sq.id: sq
            for sq in db.scalars(
                select(SendQueue).where(SendQueue.id.in_(parent_send_queue_ids))
            ).all()
        }
    template_name_by_id: dict[int, str] = {}
    if template_ids:
        template_name_by_id = {
            t.id: t.name
            for t in db.scalars(select(Template).where(Template.id.in_(template_ids))).all()
        }

    return [
        _item_to_out(
            item,
            contacts_by_id=contacts_by_id,
            company=companies_by_id.get(item.company_id),
            parent_by_id=parent_by_id,
            template_name_by_id=template_name_by_id,
        )
        for item in items
    ]


# ─────────────────────────── endpoints ───────────────────────────


@router.get("", response_model=TodayBatchOut)
def get_today(user: CurrentUser, db: DbDep) -> TodayBatchOut:
    """Returns today's batch for the authenticated user.

    Lazy generation: if no batch exists yet for today, run the picker inline
    for this user only and return the freshly-generated cards. This is the
    v0 substitute for a scheduled cron — first GET of the day pays a
    sub-second latency cost, every subsequent GET is fast (idempotency guards
    in the generator prevent re-runs).

    404 with code='no_batch_yet' if the inline generation produced zero items
    (suspended, gmail_disconnected, pending tier, or no eligible contacts).
    Frontend renders the appropriate empty state.
    """
    today = utcnow().date()
    items = today_repo.list_for_user_date(db, user.id, today)

    if not items:
        # No batch yet — generate one for this user inline. The generator is
        # idempotent (DB UNIQUE + has_batch_for_date guard) so a concurrent
        # request from the same user is safe.
        # `db.merge` ensures the user is bound to the request's session — a
        # no-op in production (user already came from this session via
        # get_current_user) but necessary when test fixtures inject a User
        # owned by a different session.
        user_in_session = db.merge(user, load=False)
        gen_result = batch_gen_svc.generate_batch_for_user(
            db, user_in_session, batch_date=today
        )
        log.info(
            "today.lazy_generated",
            user_id=user.id,
            items_created=gen_result.items_created,
            reason_if_skipped=gen_result.reason_if_skipped,
        )
        items = today_repo.list_for_user_date(db, user.id, today)

    if not items:
        raise ApiError(
            "no_batch_yet",
            "No batch available today — try again later or check your preferences.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # generated_at: earliest created_at of the batch rows — this IS the cron
    # run time for this user. Coerce to UTC since libsql returns naive datetimes.
    generated_at = ensure_utc(min(item.created_at for item in items))

    return TodayBatchOut(
        generated_at=generated_at,
        date=today,
        # Show the real effective cap (tier ceiling, lowered by any admin
        # throttle) — not the raw daily_limit, which defaults to 20 for all.
        cap=send_caps.resolve_daily_cap(user),
        sent_today=user.sent_today,
        items=_hydrate(db, items),
    )


@router.patch("/items/{item_id}", response_model=TodayItemOut)
def update_today_item(
    item_id: int,
    payload: UpdateItemIn,
    user: CurrentUser,
    db: DbDep,
) -> TodayItemOut:
    """User edits a card before sending. Editing ANY content field (subject,
    body, send_time, template_id) auto-promotes status to 'ready' — explicit
    review action. If the caller passes `status` directly, that wins.

    404 if the item belongs to another user (don't leak existence via 403).
    """
    item = today_repo.get(db, item_id)
    if item is None or item.user_id != user.id:
        raise ApiError(
            "not_found",
            "Today item not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    content_touched = False
    # Template swap: validate ownership, then re-render subject + body with the
    # card's recipient/company placeholders. Overwrites any in-flight edits —
    # the mental model is "I'm picking which template fires," not "preserve my
    # tweaks." Apply BEFORE the explicit subject/body overrides so a same-PATCH
    # subject/body still wins (rare but lets a power-user template-then-tweak
    # in one request).
    if payload.template_id is not None:
        template = templates_repo.get(db, int(payload.template_id))
        if template is None or template.user_id != user.id:
            raise ApiError(
                "template_not_found",
                "Template not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        to_contact = db.get(Contact, item.to_contact_id) if item.to_contact_id else None
        company = db.get(Company, item.company_id) if item.company_id else None
        sender_name = user.sender_signature_name or user.full_name
        rendered_subject, rendered_body = templates_svc.render_template(
            template.subject,
            template.body,
            to_contact=to_contact,
            company=company,
            sender_name=sender_name,
        )
        item.template_id = int(payload.template_id)
        item.subject = rendered_subject
        item.body = rendered_body
        # Template swap = fresh render, so the card is no longer "personalized."
        # A later batch-template-apply should rewrite this card normally.
        item.edited_at = None
        content_touched = True

    # `manually_touched` tracks whether the user actually typed in
    # subject/body — those are what we preserve during batch-template-apply.
    # send_time changes don't count as "personalized content."
    manually_touched = False
    if payload.subject is not None:
        item.subject = payload.subject
        content_touched = True
        manually_touched = True
    if payload.body is not None:
        item.body = payload.body
        content_touched = True
        manually_touched = True
    if payload.send_time is not None:
        item.send_time = payload.send_time
        content_touched = True

    if manually_touched:
        item.edited_at = utcnow()

    if payload.status is not None:
        item.status = payload.status
    elif content_touched and item.status == "default":
        # Editing without an explicit status flips the card to 'ready' — the
        # locked product decision (edit = approval).
        item.status = "ready"

    db.add(item)
    db.commit()
    db.refresh(item)

    # If this card just transitioned to 'ready' AFTER its scheduled slot
    # passed (user approving late in the day), re-stamp it to the back of the
    # queue so it doesn't blast on the next drain tick. Future-dated items are
    # left alone. Skipped if the caller set send_time explicitly in this PATCH.
    now = utcnow()
    if (
        item.status == "ready"
        and payload.send_time is None
        and ensure_utc(item.send_time) < now
    ):
        send_scheduling.stamp_late_items_for_user(db, user, [item], now=now)
        db.commit()
        db.refresh(item)

    log.info(
        "today.item_updated",
        user_id=user.id,
        item_id=item.id,
        new_status=item.status,
    )

    out = _hydrate(db, [item])
    return out[0]


@router.post("/send-batch", response_model=SendBatchResultOut)
def send_batch(user: CurrentUser, db: DbDep) -> SendBatchResultOut:
    """Manual "Approve today's batch" — staggered, NOT immediate.

    Promotes every non-skipped/non-sent card to 'ready' (clicking Send IS the
    approval), then RE-STAMPS any card whose send_time has already passed to
    the back of the user's schedule at the tier's cadence. The scheduler
    drains items as their (now-future) send_times arrive — manual and autopilot
    share the same staggered cadence.

    Items already due (send_time <= now) get dispatched immediately in this
    call; everything else waits for its slot. 'skipped' / 'sent' / 'failed'
    cards are left untouched. Idempotent: re-calling after everything's queued
    re-stamps nothing new and dispatches only what's currently due.
    """
    today = utcnow().date()
    now = utcnow()
    items = today_repo.list_for_user_date(db, user.id, today)

    sendable = [i for i in items if i.status in ("default", "ready")]
    for item in sendable:
        if item.status == "default":
            item.status = "ready"
            db.add(item)
    db.commit()

    # Re-stamp late items so they queue at the back of the schedule instead of
    # all becoming immediately-due. Future-dated items keep their slot.
    late, _future = send_scheduling.partition_late(sendable, now=now)
    if late:
        send_scheduling.stamp_late_items_for_user(db, user, late, now=now)
        db.commit()
        # refresh local handles since we just mutated send_time
        for item in late:
            db.refresh(item)

    # Drain anything currently due (NO ignore_schedule — honors send_time).
    summary = send_worker.drain_due_items(db, user_id=user.id)

    log.info(
        "today.send_batch",
        user_id=user.id,
        approved=len(sendable),
        late_restamped=len(late),
        dispatched_now=summary.sent,
        failed=summary.failed,
        skipped=summary.skipped,
    )

    # Time span across the cards the user just approved — first send is the
    # earliest among them (possibly now if any were already due), last is the
    # latest after re-stamping. Honest window for the frontend toast.
    times = [ensure_utc(i.send_time) for i in sendable] or [now]
    return SendBatchResultOut(
        dispatched_count=len(sendable),
        scheduled_first_at=min(times),
        scheduled_last_at=max(times),
    )


@router.post("/skip", response_model=SkipTodayResultOut)
def skip_today(user: CurrentUser, db: DbDep) -> SkipTodayResultOut:
    """Skip the whole day — the user opts out of today's batch entirely.

    Flips every still-pending card ('default' / 'ready') to 'skipped'. Cards
    already 'sent' / 'failed' are terminal and left as-is. The frontend then
    transitions to the limit-reached empty state. Idempotent: re-calling with
    nothing pending flips zero rows and still returns skipped=true.
    """
    today = utcnow().date()
    items = today_repo.list_for_user_date(db, user.id, today)

    skipped = 0
    for item in items:
        if item.status in ("default", "ready"):
            item.status = "skipped"
            db.add(item)
            skipped += 1
    db.commit()

    log.info("today.skip_today", user_id=user.id, skipped=skipped)
    return SkipTodayResultOut(skipped=True)


@router.post("/apply-template", response_model=ApplyTemplateResultOut)
def apply_template_to_batch(
    payload: ApplyTemplateIn,
    user: CurrentUser,
    db: DbDep,
) -> ApplyTemplateResultOut:
    """Re-render every editable card in today's batch with `template_id`.

    Skips cards where the user has manually edited subject or body
    (`edited_at IS NOT NULL`) — those represent personalization the user is
    unlikely to want overwritten. Terminal-state cards (sent/failed/skipped/
    cooldown) are also skipped — they're frozen.

    A 'default' card whose render gets rewritten is promoted to 'ready'
    automatically (same edit=approval rule as PATCH /items).

    Returns the breakdown so the frontend can show
    "Rewrote N cards · M kept your edits".
    """
    template = templates_repo.get(db, payload.template_id)
    if template is None or template.user_id != user.id:
        raise ApiError(
            "template_not_found",
            "Template not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    today = utcnow().date()
    items = today_repo.list_for_user_date(db, user.id, today)

    # Batch-fetch the contacts/companies we'll touch so we're not doing
    # per-card lookups inside the render loop.
    contact_ids = {i.to_contact_id for i in items if i.to_contact_id}
    company_ids = {i.company_id for i in items if i.company_id}
    contacts_by_id: dict[int, Contact] = {
        c.id: c
        for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
    } if contact_ids else {}
    companies_by_id: dict[int, Company] = {
        c.id: c
        for c in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    } if company_ids else {}

    sender_name = user.sender_signature_name or user.full_name

    rewritten = 0
    kept_edited = 0
    skipped_terminal = 0
    for item in items:
        if item.status in ("sent", "failed", "skipped", "cooldown"):
            skipped_terminal += 1
            continue
        if item.edited_at is not None:
            kept_edited += 1
            continue
        rendered_subject, rendered_body = templates_svc.render_template(
            template.subject,
            template.body,
            to_contact=contacts_by_id.get(item.to_contact_id),
            company=companies_by_id.get(item.company_id),
            sender_name=sender_name,
        )
        item.template_id = template.id
        item.subject = rendered_subject
        item.body = rendered_body
        if item.status == "default":
            item.status = "ready"
        db.add(item)
        rewritten += 1

    db.commit()

    log.info(
        "today.apply_template",
        user_id=user.id,
        template_id=template.id,
        rewritten=rewritten,
        kept_edited=kept_edited,
        skipped_terminal=skipped_terminal,
    )
    return ApplyTemplateResultOut(
        rewritten=rewritten,
        kept_edited=kept_edited,
        skipped_terminal=skipped_terminal,
    )

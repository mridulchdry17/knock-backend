"""/api/v1/today — today's batch review surface for the user.

GET returns the user's pre-generated cards (one per company outreach).
PATCH /items/{id} lets the user edit subject/body/send_time/status before
the send worker (B5.5) picks the row up.

Tier-gated to free/paid/super_admin. Pending users get 403 (frontend routes
them to /awaiting-approval).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.core.time import utcnow
from app.logging_config import get_logger
from app.models import Company, Contact, TodayBatchItem
from app.repositories import today_batch as today_repo
from app.schemas.today import (
    RecipientOut,
    TodayBatchOut,
    TodayItemOut,
    UpdateItemIn,
)
from app.services import batch_generator as batch_gen_svc

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

    return TodayItemOut(
        id=str(item.id),
        recipient=to_recipient,
        cc_recipients=cc_recipients,
        template_id=str(item.template_id) if item.template_id is not None else None,
        # template_name resolved at the router layer when templates ship in
        # B5.7. v0: always None.
        template_name=None,
        subject=item.subject,
        body_preview=_body_preview(item.body),
        body=item.body,
        send_time=item.send_time,
        status=item.status,  # type: ignore[arg-type]
        cooldown_until=None,
        sent_at=None,
    )


def _hydrate(db, items: list[TodayBatchItem]) -> list[TodayItemOut]:
    """Bulk-fetch contacts + companies for all items in one round-trip each.
    Beats the N+1 we'd get from looking them up per-item.
    """
    if not items:
        return []

    contact_ids: set[int] = set()
    company_ids: set[int] = set()
    for item in items:
        contact_ids.add(item.to_contact_id)
        contact_ids.update(item.get_cc_contact_ids())
        company_ids.add(item.company_id)

    from sqlalchemy import select

    contacts_by_id = {
        c.id: c
        for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
    }
    companies_by_id = {
        co.id: co
        for co in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    }

    return [
        _item_to_out(
            item,
            contacts_by_id=contacts_by_id,
            company=companies_by_id.get(item.company_id),
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
    # run time for this user.
    generated_at = min(item.created_at for item in items)

    return TodayBatchOut(
        generated_at=generated_at,
        date=today,
        cap=user.daily_limit if user.daily_limit and user.daily_limit > 0 else len(items),
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
    if payload.subject is not None:
        item.subject = payload.subject
        content_touched = True
    if payload.body is not None:
        item.body = payload.body
        content_touched = True
    if payload.send_time is not None:
        item.send_time = payload.send_time
        content_touched = True
    if payload.template_id is not None:
        item.template_id = payload.template_id
        content_touched = True

    if payload.status is not None:
        item.status = payload.status
    elif content_touched and item.status == "default":
        # Editing without an explicit status flips the card to 'ready' — the
        # locked product decision (edit = approval).
        item.status = "ready"

    db.add(item)
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

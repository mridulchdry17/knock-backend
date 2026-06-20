"""Super-admin endpoints.

All endpoints require tier='super_admin' (router-level dependency). Excluded
from the public OpenAPI schema via include_in_schema=False so admin surface
doesn't leak to frontend devs / external API consumers.

Day-one workflow: super_admin reviews `?tier=pending` users and approves
them via PATCH /tier with `{tier: "free"}`. Once Phase 5 ships paid features,
the same endpoint upgrades to `paid`.
"""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.core.deps import DbDep, SuperAdminUser
from app.core.errors import ApiError
from app.core.pagination import PaginationParams, pagination
from app.core.time import utcnow
from app.logging_config import get_logger
from app.repositories import contacts as contacts_repo
from app.repositories import email_failures as failures_repo
from app.repositories import locks as locks_repo
from app.repositories import users as users_repo
from app.repositories import waitlist as waitlist_repo
from app.schemas.admin import (
    AdminContactOut,
    AdminGlobalLockOut,
    AdminPlatformLockOut,
    AdminUserOut,
    AdminWaitlistOut,
    ApproveWaitlistIn,
    BulkApproveWaitlistIn,
    BulkApproveWaitlistOut,
    CompanySummaryOut,
    ContactUploadIn,
    ContactUploadResultOut,
    IngestSummaryOut,
    Page,
    RowErrorOut,
    Tier,
    TierUpdateIn,
)
from app.schemas.common import Ok
from app.schemas.failures import DrainSummaryOut, FailureOut, FailureSummaryOut
from app.schemas.today import (
    AdminCronRunResultItemOut,
    AdminCronRunResultOut,
)
from app.services import batch_generator as batch_gen_svc
from app.services import contact_upload as contact_upload_svc
from app.services import reply_ingestor as reply_ingestor_svc
from app.services import send_worker as send_worker_svc

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], include_in_schema=False)

log = get_logger("admin")


# ─────────────────────────── users ───────────────────────────


@router.get("/users", response_model=Page[AdminUserOut])
def list_users(
    _admin: SuperAdminUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    tier: Tier | None = None,
    search: str | None = None,
) -> Page[AdminUserOut]:
    rows, total = users_repo.list_paginated(
        db, tier=tier, search=search, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[AdminUserOut.model_validate(u) for u in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(_admin: SuperAdminUser, db: DbDep, user_id: int) -> AdminUserOut:
    user = users_repo.get(db, user_id)
    if user is None:
        raise ApiError("not_found", "User not found", status_code=status.HTTP_404_NOT_FOUND)
    return AdminUserOut.model_validate(user)


@router.patch("/users/{user_id}/tier", response_model=AdminUserOut)
def update_tier(
    _admin: SuperAdminUser, db: DbDep, user_id: int, payload: TierUpdateIn
) -> AdminUserOut:
    user = users_repo.get(db, user_id)
    if user is None:
        raise ApiError("not_found", "User not found", status_code=status.HTTP_404_NOT_FOUND)

    # Pydantic Literal already validates the string; defensive check covers any
    # future schema drift.
    if payload.tier not in ("pending", "free", "paid", "super_admin"):
        raise ApiError(
            "invalid_tier", "Unknown tier", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    previous = user.tier
    users_repo.set_tier(db, user, payload.tier)
    db.commit()
    log.info("admin.tier_changed", user_id=user.id, from_tier=previous, to_tier=payload.tier)
    return AdminUserOut.model_validate(user)


@router.post("/users/{user_id}/suspend", response_model=AdminUserOut)
def suspend_user(_admin: SuperAdminUser, db: DbDep, user_id: int) -> AdminUserOut:
    user = users_repo.get(db, user_id)
    if user is None:
        raise ApiError("not_found", "User not found", status_code=status.HTTP_404_NOT_FOUND)
    user.is_suspended = True
    db.add(user)
    db.commit()
    log.info("admin.user_suspended", user_id=user.id)
    return AdminUserOut.model_validate(user)


@router.post("/users/{user_id}/unsuspend", response_model=AdminUserOut)
def unsuspend_user(_admin: SuperAdminUser, db: DbDep, user_id: int) -> AdminUserOut:
    user = users_repo.get(db, user_id)
    if user is None:
        raise ApiError("not_found", "User not found", status_code=status.HTTP_404_NOT_FOUND)
    user.is_suspended = False
    db.add(user)
    db.commit()
    log.info("admin.user_unsuspended", user_id=user.id)
    return AdminUserOut.model_validate(user)


# ─────────────────────────── waitlist ───────────────────────────


_STATUS_LITERALS = ("pending", "approved", "all")
_SORT_LITERALS = ("newest", "oldest")


@router.get("/waitlist", response_model=Page[AdminWaitlistOut])
def list_waitlist(
    _admin: SuperAdminUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    search: Annotated[str | None, Query(max_length=255)] = None,
    status_filter: Annotated[str, Query(alias="status")] = "pending",
    sort: Annotated[str, Query()] = "newest",
) -> Page[AdminWaitlistOut]:
    """List waitlist entries with optional search + status + sort filters.

    `status` defaults to `pending` (the action queue — that's almost always
    what admin wants by default). `?status=approved` / `?status=all` flip
    the filter; anything else falls back to `all` so a typo doesn't crash."""
    if status_filter not in _STATUS_LITERALS:
        status_filter = "all"
    if sort not in _SORT_LITERALS:
        sort = "newest"

    rows, total = waitlist_repo.list_paginated(
        db,
        limit=page.limit,
        offset=page.offset,
        search=search,
        status_filter=status_filter,
        sort=sort,
    )
    return Page(
        items=[AdminWaitlistOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/waitlist.csv")
def export_waitlist_csv(_admin: SuperAdminUser, db: DbDep) -> StreamingResponse:
    """Streams the full waitlist as CSV. Uses yield_per under the hood so this
    scales past in-memory limits if the waitlist grows."""

    def _iter_csv():
        # Use a small in-memory buffer per batch — generator-friendly.
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "email", "created_at"])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()

        for entry in waitlist_repo.stream_all(db):
            writer.writerow([entry.id, entry.email, entry.created_at.isoformat()])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()

    return StreamingResponse(
        _iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="waitlist.csv"'},
    )


def _set_waitlist_approval(
    db: DbDep,
    entry_id: int,
    *,
    approved: bool,
    intended_tier: str = "free",
) -> AdminWaitlistOut:
    """Allow (or revoke) a waitlist entry, and keep any already-linked account
    in sync so the decision takes effect immediately — an approved tester
    shouldn't have to sign in again to get unblocked. When approving, the
    intended tier ('free' or 'paid') is recorded on the entry AND applied to
    any matching account."""
    entry = waitlist_repo.get(db, entry_id)
    if entry is None:
        raise ApiError(
            "not_found", "Waitlist entry not found.", status_code=status.HTTP_404_NOT_FOUND
        )

    waitlist_repo.set_approved(
        db, entry, approved=approved, intended_tier=intended_tier
    )

    # Find the account this entry belongs to (claimed via waitlist_email, or
    # signed in with the same address) and sync its tier.
    user = users_repo.get_by_waitlist_email(db, entry.email) or users_repo.get_by_email(
        db, entry.email
    )
    if user is not None:
        if approved:
            # Bump pending → intended; OR promote free → paid if pre-marked paid.
            # Never downgrade paid/super_admin via this path (use the user-level
            # tier endpoint for that).
            should_bump = user.tier == "pending" or (
                user.tier == "free" and intended_tier == "paid"
            )
            if should_bump:
                if user.waitlist_email != entry.email:
                    users_repo.set_waitlist_email(db, user, entry.email)
                users_repo.set_tier(db, user, intended_tier)
                # Lazy import — avoid pulling the templates/Gmail graph at module load.
                from app.services import templates as templates_svc

                # Idempotent: only seeds if the user has 0 templates.
                templates_svc.seed_starters(db, user)
        elif not approved and user.tier in ("free", "paid"):
            # Pull access back. Don't touch super_admin tier.
            users_repo.set_tier(db, user, "pending")

    db.commit()
    log.info(
        "admin.waitlist_approval",
        entry_id=entry.id,
        email=entry.email,
        approved=approved,
        intended_tier=intended_tier,
        synced_user_id=user.id if user else None,
    )
    return AdminWaitlistOut.model_validate(entry)


@router.post("/waitlist/{entry_id}/approve", response_model=AdminWaitlistOut)
def approve_waitlist_entry(
    _admin: SuperAdminUser,
    db: DbDep,
    entry_id: int,
    payload: ApproveWaitlistIn | None = None,
) -> AdminWaitlistOut:
    """Allow a waitlist entry in. Empty body / `{tier:'free'}` = the legacy
    default (Allow → free); `{tier:'paid'}` pre-marks the entry so the user
    lands at 'paid' on sign-in (or gets bumped to paid now if already linked)."""
    tier = payload.tier if payload is not None else "free"
    return _set_waitlist_approval(db, entry_id, approved=True, intended_tier=tier)


@router.post("/waitlist/{entry_id}/revoke", response_model=AdminWaitlistOut)
def revoke_waitlist_entry(
    _admin: SuperAdminUser, db: DbDep, entry_id: int
) -> AdminWaitlistOut:
    """Undo an approval. Clears approved_at, resets intended_tier to 'free',
    and if a 'free'/'paid' account was linked to this entry, parks it back at
    'pending'. super_admin tier untouched."""
    return _set_waitlist_approval(db, entry_id, approved=False)


@router.post("/waitlist/approve-bulk", response_model=BulkApproveWaitlistOut)
def bulk_approve_waitlist(
    _admin: SuperAdminUser,
    db: DbDep,
    payload: BulkApproveWaitlistIn,
) -> BulkApproveWaitlistOut:
    """Approve N waitlist entries in one round-trip.

    Reuses `_set_waitlist_approval` per id so the linked-account tier sync
    behaviour is identical to the per-row endpoint. Idempotent — already-
    approved rows are counted but not re-stamped (no tier reset, no churn).
    Not-found ids are returned in the response so the caller can show the
    user which selections were stale.
    """
    if not payload.ids:
        return BulkApproveWaitlistOut(
            newly_approved=0, already_approved=0, not_found_ids=[]
        )

    newly_approved = 0
    already_approved = 0
    not_found: list[int] = []

    for entry_id in payload.ids:
        entry = waitlist_repo.get(db, entry_id)
        if entry is None:
            not_found.append(entry_id)
            continue
        if entry.approved_at is not None:
            already_approved += 1
            continue
        # Defer to the existing helper so account-sync side-effects (promote
        # any linked user from pending → free/paid) fire per-row exactly as
        # they do via the single-row endpoint.
        _set_waitlist_approval(
            db, entry_id, approved=True, intended_tier=payload.tier
        )
        newly_approved += 1

    return BulkApproveWaitlistOut(
        newly_approved=newly_approved,
        already_approved=already_approved,
        not_found_ids=not_found,
    )


# ─────────────────────────── contacts (B5.1) ───────────────────────────


def _result_to_out(result: contact_upload_svc.ContactUploadResult) -> ContactUploadResultOut:
    return ContactUploadResultOut(
        inserted=result.inserted,
        updated=result.updated,
        skipped=result.skipped,
        failed=result.failed,
        row_errors=[
            RowErrorOut(
                row_index=e.row_index,
                email=e.email,
                error_code=e.error_code,
                message=e.message,
            )
            for e in result.row_errors
        ],
    )


@router.post("/contacts/bulk", response_model=ContactUploadResultOut)
def bulk_upload_contacts(
    _admin: SuperAdminUser,
    db: DbDep,
    payload: ContactUploadIn,
) -> ContactUploadResultOut:
    rows: list[dict[str, object]] = [dict(r) for r in payload.rows]
    result = contact_upload_svc.bulk_upload(db, rows, dry_run=payload.dry_run)
    if not payload.dry_run:
        db.commit()
    log.info(
        "admin.contacts_bulk_upload",
        inserted=result.inserted,
        updated=result.updated,
        skipped=result.skipped,
        failed=result.failed,
        dry_run=payload.dry_run,
    )
    return _result_to_out(result)


@router.post("/contacts/bulk/csv", response_model=ContactUploadResultOut)
async def bulk_upload_contacts_csv(
    _admin: SuperAdminUser,
    db: DbDep,
    file: Annotated[UploadFile, File(...)],
    dry_run: Annotated[bool, Query()] = False,
) -> ContactUploadResultOut:
    """Multipart CSV upload. Case-insensitive column matching; hard-capped at
    `MAX_UPLOAD_ROWS` rows (10k). For larger batches, chunk client-side."""
    raw = await file.read()
    try:
        rows = contact_upload_svc.parse_csv(raw)
    except Exception as e:
        raise ApiError(
            "invalid_csv",
            f"Could not parse CSV: {e}",
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from e

    result = contact_upload_svc.bulk_upload(db, rows, dry_run=dry_run)
    if not dry_run:
        db.commit()
    log.info(
        "admin.contacts_csv_upload",
        filename=file.filename,
        inserted=result.inserted,
        updated=result.updated,
        skipped=result.skipped,
        failed=result.failed,
        dry_run=dry_run,
    )
    return _result_to_out(result)


@router.get("/contacts", response_model=Page[AdminContactOut])
def list_contacts(
    _admin: SuperAdminUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    search: str | None = None,
    company_domain: str | None = None,
    invalid_only: bool = False,
) -> Page[AdminContactOut]:
    """`invalid_only=true` returns just the invalidated contacts (bounced /
    flagged) — backs the admin 'these bounced, review & delete' view."""
    pairs, total = contacts_repo.list_admin_paginated(
        db,
        search=search,
        company_domain=company_domain,
        invalid_only=invalid_only,
        limit=page.limit,
        offset=page.offset,
    )
    items = [
        AdminContactOut(
            id=c.id,
            email=c.email or "",
            name=c.name,
            role=c.role,
            company_id=co.id,
            company_name=co.name,
            company_domain=co.domain,
            linkedin_url=c.linkedin_url,
            source=c.source,
            notes=c.notes,
            scraped_pattern=c.scraped_pattern,
            is_invalid=c.is_invalid,
            invalid_reason=c.invalid_reason,
            created_at=c.created_at,
        )
        for c, co in pairs
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.delete("/contacts/{contact_id}", response_model=Ok)
def delete_contact(_admin: SuperAdminUser, db: DbDep, contact_id: int) -> Ok:
    contact = contacts_repo.get(db, contact_id)
    if contact is None:
        raise ApiError("not_found", "Contact not found", status_code=status.HTTP_404_NOT_FOUND)
    contacts_repo.delete_by_id(db, contact_id)
    db.commit()
    log.info("admin.contact_deleted", contact_id=contact_id)
    return Ok()


# ─────────────────────────── locks (B5.3) ───────────────────────────


@router.get("/locks/global", response_model=Page[AdminGlobalLockOut])
def list_global_locks(
    _admin: SuperAdminUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
) -> Page[AdminGlobalLockOut]:
    """Paginated view of active 36h platform cooldowns. Sorted by soonest-expiring."""
    now = utcnow()
    rows, total = locks_repo.list_global_locks_paginated(
        db, now=now, limit=page.limit, offset=page.offset
    )
    items = [
        AdminGlobalLockOut(
            company_domain=r.company_domain,
            locked_at=r.locked_at,
            locked_until=r.locked_until,
            last_locked_by_user_id=r.last_locked_by_user_id,
        )
        for r in rows
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/locks/platform", response_model=list[AdminPlatformLockOut])
def list_platform_locks(
    _admin: SuperAdminUser, db: DbDep
) -> list[AdminPlatformLockOut]:
    """All platform-permanent locks. Unpaginated — expected to be a small set
    (tens, not thousands) even at scale. Add pagination when it actually hurts."""
    rows = locks_repo.list_platform_locks(db)
    return [
        AdminPlatformLockOut(
            company_domain=r.company_domain,
            reason=r.reason,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/locks/platform/{company_domain}", response_model=Ok)
def clear_platform_lock(
    _admin: SuperAdminUser, db: DbDep, company_domain: str
) -> Ok:
    """Removes the explicit-stop / manual permanent lock for a company."""
    domain = company_domain.strip().lower()
    was_cleared = locks_repo.clear_platform_lock(db, domain)
    if not was_cleared:
        raise ApiError(
            "not_found",
            "No platform lock found for that domain.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    db.commit()
    log.info("admin.platform_lock_cleared", company_domain=domain)
    return Ok()


@router.delete("/locks/user/{user_id}/{company_domain}", response_model=Ok)
def clear_user_company_lock(
    _admin: SuperAdminUser, db: DbDep, user_id: int, company_domain: str
) -> Ok:
    """Removes a per-user reply lock manually (super_admin re-engagement override)."""
    domain = company_domain.strip().lower()
    was_cleared = locks_repo.clear_user_company_lock(db, user_id, domain)
    if not was_cleared:
        raise ApiError(
            "not_found",
            "No user-company lock found for that pair.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    db.commit()
    log.info(
        "admin.user_company_lock_cleared",
        target_user_id=user_id,
        company_domain=domain,
    )
    return Ok()


# ─────────────────────────── today batch cron (B5.4) ───────────────────────────


@router.post("/today/run-cron", response_model=AdminCronRunResultOut)
def run_today_cron_now(
    _admin: SuperAdminUser,
    db: DbDep,
    target_user_id: int | None = None,
) -> AdminCronRunResultOut:
    """Manually trigger the B5.4 batch cron — generate today's picks for all
    eligible users, or just one user if `target_user_id` is set.

    The scheduled cron isn't wired up in v0; this endpoint is how we exercise
    batch generation in dev and during the launch ceremony. Swap-point: a
    systemd timer or APScheduler running `run_batch_cron` will replace this
    once the daily flow is observed working end-to-end.
    """
    today = utcnow().date()

    if target_user_id is not None:
        user = users_repo.get(db, target_user_id)
        if user is None:
            raise ApiError(
                "not_found",
                "User not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        results = [batch_gen_svc.generate_batch_for_user(db, user, batch_date=today)]
    else:
        results = batch_gen_svc.generate_batch_for_all_users(db, batch_date=today)

    total_created = sum(r.items_created for r in results)
    log.info(
        "admin.today_cron_run",
        target_user_id=target_user_id,
        users_processed=len(results),
        total_items_created=total_created,
    )

    return AdminCronRunResultOut(
        batch_date=today,
        results=[
            AdminCronRunResultItemOut(
                user_id=r.user_id,
                items_created=r.items_created,
                items_skipped=r.items_skipped,
                reason_if_skipped=r.reason_if_skipped,
            )
            for r in results
        ],
        total_items_created=total_created,
        total_users_processed=len(results),
    )


# ─────────────────────────── send worker + failures (B5.5) ───────────────────────────


@router.post("/send/drain", response_model=DrainSummaryOut)
def trigger_send_drain(_admin: SuperAdminUser, db: DbDep) -> DrainSummaryOut:
    """Manually fires the Gmail send worker against all due today_batch_items.

    Used by Mridul in dev + the launch ceremony before an APScheduler hookup
    exists. Each item commits independently — a partial run still makes progress.
    """
    summary = send_worker_svc.drain_due_items(db)
    log.info(
        "admin.send_drain",
        attempted=summary.attempted,
        sent=summary.sent,
        failed=summary.failed,
        skipped=summary.skipped,
    )
    return DrainSummaryOut(
        attempted=summary.attempted,
        sent=summary.sent,
        failed=summary.failed,
        skipped=summary.skipped,
        failures_by_kind=summary.failures_by_kind,
    )


@router.get("/failures", response_model=Page[FailureOut])
def list_failures(
    _admin: SuperAdminUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    user_id: int | None = None,
    kind: str | None = None,
) -> Page[FailureOut]:
    """Newest-first listing of email_failures, paginated. Filters: user_id, kind."""
    rows, total = failures_repo.list_recent(
        db,
        limit=page.limit,
        offset=page.offset,
        user_id=user_id,
        failure_kind=kind,
    )
    # Resolve emails in one batched lookup — most pages will have a handful of
    # distinct user_ids.
    user_ids = {r.user_id for r in rows}
    users_by_id = {
        u.id: u for u in (users_repo.get(db, uid) for uid in user_ids) if u is not None
    }
    items = [
        FailureOut(
            id=r.id,
            user_id=r.user_id,
            user_email=(users_by_id.get(r.user_id).email if r.user_id in users_by_id else ""),
            today_batch_item_id=r.today_batch_item_id,
            company_domain=r.company_domain,
            failure_kind=r.failure_kind,
            error_message=r.error_message,
            gmail_error_code=r.gmail_error_code,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/failures/summary", response_model=FailureSummaryOut)
def failures_summary(_admin: SuperAdminUser, db: DbDep) -> FailureSummaryOut:
    """Failure counts grouped by `failure_kind` for the last 24h and last 7d."""
    now = utcnow()
    return FailureSummaryOut(
        last_24h=failures_repo.count_by_kind(db, since=now - timedelta(hours=24)),
        last_7d=failures_repo.count_by_kind(db, since=now - timedelta(days=7)),
    )


# ─────────────────────────── reply ingestion (B5.6) ───────────────────────────


@router.post("/replies/ingest", response_model=list[IngestSummaryOut])
def trigger_reply_ingest(
    _admin: SuperAdminUser,
    db: DbDep,
    target_user_id: int | None = None,
) -> list[IngestSummaryOut]:
    """Manually run the Gmail reply ingestor across all users (or one).

    No scheduler is wired in v0; this endpoint plus `python -m
    app.jobs.reply_ingest_cron` are the only ways to run ingest. The
    response is the list of per-user summaries so the admin UI can show
    explicit-stop counts and any auth-revoked users in one shot.
    """
    if target_user_id is not None:
        user = users_repo.get(db, target_user_id)
        if user is None:
            raise ApiError(
                "not_found",
                "User not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        summaries = [reply_ingestor_svc.ingest_replies_for_user(db, user)]
    else:
        summaries = reply_ingestor_svc.ingest_replies_for_all_users(db)

    log.info(
        "admin.replies_ingest",
        target_user_id=target_user_id,
        users_processed=len(summaries),
        total_replies_matched=sum(s.replies_matched for s in summaries),
        total_explicit_stops=sum(s.explicit_stops for s in summaries),
    )
    return [
        IngestSummaryOut(
            user_id=s.user_id,
            processed=s.processed,
            replies_matched=s.replies_matched,
            explicit_stops=s.explicit_stops,
            user_reply_locks_written=s.user_reply_locks_written,
            error_kind=s.error_kind,
        )
        for s in summaries
    ]


# ─────────────────────────── contacts companies summary ───────────────────────────


@router.get("/contacts/companies/summary", response_model=list[CompanySummaryOut])
def list_companies_summary(_admin: SuperAdminUser, db: DbDep) -> list[CompanySummaryOut]:
    """Aggregated overview for the admin UI: every company with at least one
    contact, plus its contact count. Sorted by count desc."""
    return [
        CompanySummaryOut(
            company_id=co.id,
            company_name=co.name,
            company_domain=co.domain,
            contact_count=n,
        )
        for co, n in contacts_repo.count_by_company(db)
    ]


# ─────────────────────────── autopilot cycle (B5.7) ───────────────────────────


@router.post("/autopilot/cycle")
def trigger_autopilot_cycle(_admin: SuperAdminUser) -> dict:
    """Manually run the full Phase-5 daily cycle: batch_gen → send → ingest.

    Idempotent at each stage. Used by Mridul to fire the autopilot end-to-end
    in dev / during the v0 launch ceremony, before a real scheduler is wired.
    The session is closed/reopened inside `run_cycle` to mirror cron semantics.
    Returns the structured cycle summary.
    """
    from app.jobs.autopilot_cycle_cron import run_cycle

    result = run_cycle()
    log.info(
        "admin.autopilot_cycle_run",
        batch_users=result.batch_users_processed,
        batch_items=result.batch_items_created,
        sent=result.sent,
        failed=result.failed,
        replies_matched=result.replies_matched,
        explicit_stops=result.explicit_stops,
    )
    return {
        "batch_users_processed": result.batch_users_processed,
        "batch_items_created": result.batch_items_created,
        "sent": result.sent,
        "failed": result.failed,
        "skipped_sends": result.skipped_sends,
        "ingest_users_processed": result.ingest_users_processed,
        "replies_matched": result.replies_matched,
        "explicit_stops": result.explicit_stops,
    }

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
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.core.deps import DbDep, SuperAdminUser
from app.core.errors import ApiError
from app.core.pagination import PaginationParams, pagination
from app.core.time import utcnow
from app.logging_config import get_logger
from app.repositories import contacts as contacts_repo
from app.repositories import locks as locks_repo
from app.repositories import users as users_repo
from app.repositories import waitlist as waitlist_repo
from app.schemas.admin import (
    AdminContactOut,
    AdminGlobalLockOut,
    AdminPlatformLockOut,
    AdminUserOut,
    AdminWaitlistOut,
    CompanySummaryOut,
    ContactUploadIn,
    ContactUploadResultOut,
    Page,
    RowErrorOut,
    Tier,
    TierUpdateIn,
)
from app.schemas.common import Ok
from app.schemas.today import (
    AdminCronRunResultItemOut,
    AdminCronRunResultOut,
)
from app.services import batch_generator as batch_gen_svc
from app.services import contact_upload as contact_upload_svc

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


@router.get("/waitlist", response_model=Page[AdminWaitlistOut])
def list_waitlist(
    _admin: SuperAdminUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
) -> Page[AdminWaitlistOut]:
    rows, total = waitlist_repo.list_paginated(db, limit=page.limit, offset=page.offset)
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
) -> Page[AdminContactOut]:
    pairs, total = contacts_repo.list_admin_paginated(
        db,
        search=search,
        company_domain=company_domain,
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

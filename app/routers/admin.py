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
from app.logging_config import get_logger
from app.repositories import contacts as contacts_repo
from app.repositories import users as users_repo
from app.repositories import waitlist as waitlist_repo
from app.schemas.admin import (
    AdminContactOut,
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
            source=c.email_source,
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

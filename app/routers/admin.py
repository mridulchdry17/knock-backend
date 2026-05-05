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

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.core.deps import DbDep, SuperAdminUser
from app.core.errors import ApiError
from app.core.pagination import PaginationParams, pagination
from app.logging import get_logger
from app.repositories import users as users_repo
from app.repositories import waitlist as waitlist_repo
from app.schemas.admin import (
    AdminUserOut,
    AdminWaitlistOut,
    Page,
    Tier,
    TierUpdateIn,
)

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

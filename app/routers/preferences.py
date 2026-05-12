"""User preferences endpoints — backs the frontend F.8 settings screen and
the autopilot toggles on /today.

Layered: router translates payload <-> service result enums. All persistence
goes through app.services.preferences.
"""
from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbDep
from app.core.errors import ApiError
from app.logging_config import get_logger
from app.repositories import preferences as prefs_repo
from app.schemas.common import Ok
from app.schemas.preferences import (
    AddDomainIn,
    ExcludedDomainOut,
    ExcludedDomainsListOut,
    PreferencesOut,
    PreferencesPatch,
)
from app.services import preferences as prefs_service

router = APIRouter(prefix="/api/v1", tags=["preferences"])

log = get_logger("preferences")


# ─────────────────────────── preferences ───────────────────────────


@router.get("/preferences", response_model=PreferencesOut)
def get_preferences(user: CurrentUser, db: DbDep) -> PreferencesOut:
    # db is currently unused at the read path — model fields are already
    # hydrated on the CurrentUser object — but we keep the dep so future
    # eager-loading of related rows is a one-line change.
    del db
    return prefs_service.build_preferences_out(user)


@router.patch("/preferences", response_model=PreferencesOut)
def update_preferences(
    payload: PreferencesPatch, user: CurrentUser, db: DbDep
) -> PreferencesOut:
    result = prefs_service.update_preferences(db, user, payload)
    log.info("preferences.updated", user_id=user.id, fields=list(payload.model_fields_set))
    return result


# ─────────────────────────── excluded domains ───────────────────────────


@router.get("/preferences/excluded-domains", response_model=ExcludedDomainsListOut)
def list_excluded_domains(user: CurrentUser, db: DbDep) -> ExcludedDomainsListOut:
    rows = prefs_repo.list_excluded_domains(db, user.id)
    return ExcludedDomainsListOut(
        items=[ExcludedDomainOut.model_validate(r) for r in rows]
    )


@router.post(
    "/preferences/excluded-domains",
    response_model=ExcludedDomainOut,
    status_code=status.HTTP_201_CREATED,
)
def add_excluded_domain(
    payload: AddDomainIn, user: CurrentUser, db: DbDep
) -> ExcludedDomainOut:
    result = prefs_service.add_excluded_domain(db, user, payload.domain)
    if result is prefs_service.DomainResult.INVALID:
        raise ApiError(
            "invalid_domain",
            "That doesn't look like a valid domain. Try something like 'acme.com'.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if result is prefs_service.DomainResult.DUPLICATE:
        raise ApiError(
            "already_excluded",
            "That domain is already on your excluded list.",
            status_code=status.HTTP_409_CONFLICT,
        )

    # Re-fetch the row to return its created_at. We just normalized the
    # input the same way the service did, so the lookup is exact.
    normalized = payload.domain.strip().lstrip("@").lower()
    row = prefs_repo.get_excluded_domain(db, user.id, normalized)
    assert row is not None  # service just inserted it
    log.info("preferences.domain_excluded", user_id=user.id, domain=normalized)
    return ExcludedDomainOut.model_validate(row)


@router.delete("/preferences/excluded-domains/{domain}", response_model=Ok)
def remove_excluded_domain(domain: str, user: CurrentUser, db: DbDep) -> Ok:
    was_deleted = prefs_service.remove_excluded_domain(db, user, domain)
    if not was_deleted:
        raise ApiError(
            "not_found",
            "That domain isn't on your excluded list.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    log.info("preferences.domain_unexcluded", user_id=user.id, domain=domain)
    return Ok()


# ─────────────────────────── autopilot ───────────────────────────


def _require_paid_for_autopilot(user: CurrentUser) -> None:
    # Inline (rather than dependency) so we can return PreferencesOut as the
    # response model uniformly across the four autopilot endpoints.
    if user.tier not in ("paid", "super_admin"):
        raise ApiError(
            "paid_required",
            "Autopilot is a paid feature. Upgrade to enable hands-off sending.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


@router.post("/autopilot/enable", response_model=PreferencesOut)
def enable_autopilot(user: CurrentUser, db: DbDep) -> PreferencesOut:
    _require_paid_for_autopilot(user)
    prefs_service.enable_autopilot(db, user)
    log.info("autopilot.enabled", user_id=user.id)
    return prefs_service.build_preferences_out(user)


@router.post("/autopilot/disable", response_model=PreferencesOut)
def disable_autopilot(user: CurrentUser, db: DbDep) -> PreferencesOut:
    prefs_service.disable_autopilot(db, user)
    log.info("autopilot.disabled", user_id=user.id)
    return prefs_service.build_preferences_out(user)


@router.post("/autopilot/pause", response_model=PreferencesOut)
def pause_autopilot(user: CurrentUser, db: DbDep) -> PreferencesOut:
    prefs_service.pause_autopilot(db, user)
    log.info("autopilot.paused", user_id=user.id)
    return prefs_service.build_preferences_out(user)


@router.post("/autopilot/resume", response_model=PreferencesOut)
def resume_autopilot(user: CurrentUser, db: DbDep) -> PreferencesOut:
    prefs_service.resume_autopilot(db, user)
    log.info("autopilot.resumed", user_id=user.id)
    return prefs_service.build_preferences_out(user)

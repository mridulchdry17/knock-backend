"""/api/v1/templates — per-user template library (cap 3).

Matches the F6 frontend contract. Tier-gated to free/paid/super_admin
(pending users 403 like every feature route). Test-send goes to the user's
own inbox.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.logging_config import get_logger
from app.repositories import templates as templates_repo
from app.schemas.common import Ok
from app.schemas.templates import (
    TemplateInput,
    TemplateOut,
    TemplatePatch,
    TemplatesListOut,
    TestSendResult,
)
from app.services import templates as templates_svc
from app.services.templates import MAX_TEMPLATES_PER_USER, TemplateView

router = APIRouter(
    prefix="/api/v1/templates",
    tags=["templates"],
    dependencies=[Depends(require_tier("free", "paid", "super_admin"))],
)

log = get_logger("templates")


def _view_to_out(v: TemplateView) -> TemplateOut:
    t = v.template
    return TemplateOut(
        id=str(t.id),
        name=t.name,
        subject=t.subject,
        body=t.body,
        is_starter=t.is_starter,
        is_default=t.is_default,
        used_count=v.used_count,
        reply_rate=v.reply_rate,
        created_at=t.created_at,
        # updated_at is nullable for legacy rows; fall back to created_at so the
        # contract's non-null datetime holds.
        updated_at=t.updated_at or t.created_at,
    )


@router.get("", response_model=TemplatesListOut)
def list_templates(user: CurrentUser, db: DbDep) -> TemplatesListOut:
    views = templates_svc.list_views(db, user.id)
    return TemplatesListOut(
        items=[_view_to_out(v) for v in views],
        count=len(views),
        cap=MAX_TEMPLATES_PER_USER,
    )


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: TemplateInput, user: CurrentUser, db: DbDep
) -> TemplateOut:
    t = templates_svc.create(
        db, user, name=payload.name, subject=payload.subject, body=payload.body
    )
    db.commit()
    db.refresh(t)
    log.info("templates.created", user_id=user.id, template_id=t.id)
    return _view_to_out(TemplateView(template=t, used_count=0, reply_rate=None))


@router.patch("/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int, payload: TemplatePatch, user: CurrentUser, db: DbDep
) -> TemplateOut:
    t = templates_svc.update(
        db,
        user,
        template_id,
        name=payload.name,
        subject=payload.subject,
        body=payload.body,
    )
    db.commit()
    db.refresh(t)
    used = templates_repo.used_counts_for_user(db, user.id)
    log.info("templates.updated", user_id=user.id, template_id=t.id)
    return _view_to_out(
        TemplateView(template=t, used_count=used.get(t.id, 0), reply_rate=None)
    )


@router.delete("/{template_id}", response_model=Ok)
def delete_template(template_id: int, user: CurrentUser, db: DbDep) -> Ok:
    templates_svc.delete(db, user, template_id)
    db.commit()
    log.info("templates.deleted", user_id=user.id, template_id=template_id)
    return Ok()


@router.post("/{template_id}/test-send", response_model=TestSendResult)
def test_send_template(
    template_id: int, user: CurrentUser, db: DbDep
) -> TestSendResult:
    templates_svc.test_send(db, user, template_id)
    return TestSendResult(sent=True)


@router.post("/{template_id}/default", response_model=TemplateOut)
def set_default_template(
    template_id: int, user: CurrentUser, db: DbDep
) -> TemplateOut:
    """Make this template the user's autopilot default.

    Atomic — unsets is_default on every OTHER template for this user, then
    sets it on the target. Returns the now-default template so the
    frontend can update its UI without a refetch. 404 if the id doesn't
    belong to the user (no existence-side-channel for other users' ids)."""
    t = templates_repo.set_default(db, user_id=user.id, template_id=template_id)
    if t is None:
        raise ApiError(
            "not_found",
            "Template not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    db.commit()
    db.refresh(t)
    used = templates_repo.used_counts_for_user(db, user.id)
    log.info("templates.set_default", user_id=user.id, template_id=t.id)
    return _view_to_out(
        TemplateView(template=t, used_count=used.get(t.id, 0), reply_rate=None)
    )

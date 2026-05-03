from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import DbDep
from app.logging import get_logger
from app.repositories import waitlist as waitlist_repo
from app.schemas.waitlist import WaitlistJoinIn, WaitlistJoinOut

router = APIRouter(prefix="/api/v1/waitlist", tags=["waitlist"])

log = get_logger("waitlist")


@router.post("", response_model=WaitlistJoinOut)
def join_waitlist(payload: WaitlistJoinIn, db: DbDep) -> WaitlistJoinOut:
    """Public — no auth. Idempotent: duplicate emails return ok=true without leaking presence."""
    email = payload.email.strip().lower()
    existing = waitlist_repo.get_by_email(db, email)
    if existing is None:
        waitlist_repo.add(db, email)
        db.commit()
        log.info("waitlist.joined", email=email)
    return WaitlistJoinOut()

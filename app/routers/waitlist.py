from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import DbDep
from app.core.errors import ApiError
from app.logging_config import get_logger
from app.repositories import waitlist as waitlist_repo
from app.schemas.waitlist import WaitlistJoinIn, WaitlistJoinOut

router = APIRouter(prefix="/api/v1/waitlist", tags=["waitlist"])

log = get_logger("waitlist")


@router.post("", response_model=WaitlistJoinOut)
def join_waitlist(payload: WaitlistJoinIn, db: DbDep) -> WaitlistJoinOut:
    """Public — no auth. Returns 200 on first signup, 409 on duplicate.

    The duplicate signal is intentional UX (so users see "already on list" instead
    of being silently re-confirmed). It makes the endpoint a presence-oracle for
    arbitrary emails — acceptable for a public marketing waitlist, would not be
    acceptable for a privacy-sensitive list. See memory.md for the trade-off log.
    """
    email = payload.email.strip().lower()
    existing = waitlist_repo.get_by_email(db, email)
    if existing is not None:
        raise ApiError(
            "already_registered",
            "This email is already on the waitlist.",
            status_code=status.HTTP_409_CONFLICT,
        )

    waitlist_repo.add(db, email)
    db.commit()
    log.info("waitlist.joined", email=email)
    return WaitlistJoinOut()

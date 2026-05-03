from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.config import settings
from app.core.deps import DbDep
from app.schemas.common import HealthOut, ReadyOut

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthOut)
def healthz() -> HealthOut:
    return HealthOut(status="ok")


@router.get("/readyz", response_model=ReadyOut)
def readyz(db: DbDep) -> ReadyOut:
    db.execute(text("SELECT 1"))
    return ReadyOut(
        db="ok",
        scheduler="running" if settings.RUN_SCHEDULER else "off-process",
    )

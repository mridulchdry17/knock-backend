from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class WaitlistJoinIn(BaseModel):
    email: EmailStr = Field(..., max_length=254)


class WaitlistJoinOut(BaseModel):
    ok: bool = True


class WaitlistEntryOut(ORMModel):
    id: int
    email: str
    created_at: datetime

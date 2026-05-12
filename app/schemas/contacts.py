"""Contacts schemas — user-facing (NOT admin).

B5.1b only ships the per-user notes endpoints; B5.3 will fill in the read-only
contact browser shapes (`ContactOut`, list pagination) using this module.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class MyContactNoteIn(BaseModel):
    # Empty string is permitted — the router treats it as a delete signal.
    # 5000 chars matches the column's effective ceiling for a single textarea
    # in the UI; longer values get a 422.
    notes: str = Field(min_length=0, max_length=5000)


class MyContactNoteOut(ORMModel):
    contact_id: int
    notes: str
    created_at: datetime
    updated_at: datetime

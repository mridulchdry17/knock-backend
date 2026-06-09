"""Reusable pagination dependency.

Used across admin endpoints (and Phase 5 listings). Single source of truth
for limit/offset bounds and validation.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel


class PaginationParams(BaseModel):
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginationParams:
    return PaginationParams(limit=limit, offset=offset)

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class Ok(BaseModel):
    ok: bool = True


class HealthOut(BaseModel):
    status: str


class ReadyOut(BaseModel):
    db: str
    scheduler: str

from __future__ import annotations

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class Company(Base, CreatedAtMixin):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    article_url: Mapped[str | None] = mapped_column(String(1024))
    funding_stage: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("idx_companies_stage", "funding_stage"),
        Index("idx_companies_industry", "industry"),
    )

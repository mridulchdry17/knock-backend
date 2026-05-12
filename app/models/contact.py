from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class Contact(Base, CreatedAtMixin):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    email_source: Mapped[str | None] = mapped_column(String(16))  # 'guess'|'hunter'|'manual'
    email_confidence: Mapped[int | None] = mapped_column(Integer)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_invalid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    # Set by future scraper to record which guess pattern produced this address
    # (e.g. "firstname.lastname"). Used by B5.5 to try an alternate pattern on
    # bounce.
    scraped_pattern: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("email", name="uq_contacts_email"),
        Index("idx_contacts_company", "company_id"),
    )

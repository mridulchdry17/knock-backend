from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class UserCompanyLock(Base):
    """Lock #2: per-user, per-company reply lock (30-day auto-expire).

    Triggered by B5.6 reply ingestion: when ANY reply lands on a user's
    outbound thread to @acme.com, this user is locked out of @acme.com for
    30 days from the latest reply. Other users on Knock can still email
    @acme.com (subject to Lock #1's 36h platform cooldown).

    Composite PK mirrors the `UserExcludedDomain` pattern — re-upserts of
    the same (user_id, company_domain) extend `locked_until` in place
    rather than 500ing on a unique-key violation.

    `is_permanent=True` is a super_admin escape hatch (clear-and-set-permanent
    via the admin endpoint) for cases where the auto-expire isn't desired.
    """

    __tablename__ = "user_company_locks"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    company_domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    locked_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_permanent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 'reply' | 'manual_admin' | future codes. Kept as a short string instead
    # of an enum so admin tooling can introduce new reasons without a migration.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_ucl_user_domain", "user_id", "company_domain"),
        Index("idx_ucl_locked_until", "locked_until"),
    )

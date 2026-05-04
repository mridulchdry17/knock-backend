from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class GlobalContactLock(Base):
    """Cross-user lock: while a row exists with locked_until > now(),
    no user other than locked_by_user_id may queue this contact."""

    __tablename__ = "global_contact_lock"

    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    locked_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_lock_until", "locked_until"),)

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class UserContactNote(Base):
    """Per-user, private notes on a contact.

    Composite PK (user_id, contact_id) gives free uniqueness — re-upserting
    the same pair updates the row in place rather than 500ing. Mirrors the
    pattern in `user_excluded_domain.py`.

    Distinct from `Contact.notes`, which is admin-curated and shared across
    every user who sees that contact. These rows are private observations
    a student keeps for themselves ("I noticed she retweets papers from my
    advisor's lab"). No other user reads them.
    """

    __tablename__ = "user_contact_notes"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (Index("ix_user_contact_notes_user_id", "user_id"),)

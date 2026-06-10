"""Per-user "I already emailed this contact" cooldown.

One row per (user, contact). Written by the send worker every time a user
sends to a contact (initial or follow-up — the to_contact AND every cc_contact
get a row, since being CC'd counts as a touch from this user). The daily
picker reads `cooldown_until > now()` to skip contacts the user has already
emailed recently. Default window is 30 days; tuneable via
`USER_CONTACT_COOLDOWN_DAYS` (settings).

Distinct from `UserCompanyLock` (post-REPLY, 2-day, domain-scope) — this is
post-SEND, 30-day, contact-scope.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserContactCooldown(Base):
    __tablename__ = "user_contact_cooldown"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_ucc_user_until", "user_id", "cooldown_until"),
    )

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class UserContactMap(Base, CreatedAtMixin):
    """Per-user lifecycle row for outreach to a contact (initial + follow-ups).

    Combined with global_contact_lock, this is the moat: the pre-send filter
    checks both before queueing.
    """

    __tablename__ = "user_contact_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    # QUEUED | SENT | FOLLOWUP_SENT | REPLIED | BOUNCED | UNSUBSCRIBED | DEAD

    gmail_thread_id: Mapped[str | None] = mapped_column(String(128), index=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(128))

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reply_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounce_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_action_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "contact_id", name="uq_ucm_user_contact"),
        Index("idx_ucm_user_status", "user_id", "status"),
        Index("idx_ucm_followup_scan", "status", "sent_at", "followup_count"),
    )

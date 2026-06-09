from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class WaitlistEntry(Base, CreatedAtMixin):
    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # NULL = on the list but not allowed in yet. Set by a super_admin "Allow"
    # action. Only an approved entry grants access on sign-in / claim.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What tier the admin wants this person to land at when they sign in.
    # 'free' is the default; admin can pre-mark 'paid' so the user lands on
    # paid without a second per-user promotion step. Honoured by auth's
    # auto-claim, onboarding.claim_waitlist, and the admin Allow handler.
    intended_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="free"
    )

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class GlobalContactLock(Base):
    """Platform-wide 36-hour cooldown, keyed by company domain.

    Lock #1 of the Phase 5 three-tier model (project_phase5_send_model.md).
    After ANY user emails @acme.com, no other user on Knock may email
    @acme.com until `locked_until` has passed. Rolling: every send
    upserts this row with `locked_until = now() + 36h`.

    Re-keyed from the 0001_init `contact_id` shape in migration
    0009_lock_tables_rekey — the contact-level grain was wrong for the
    company-grouped send model.
    """

    __tablename__ = "global_contact_lock"

    company_domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    locked_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Nullable + SET NULL on delete: we want lock history to outlive a user
    # being purged. The lock state matters (cooldown is active); attribution
    # is best-effort.
    last_locked_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("idx_global_lock_until", "locked_until"),)

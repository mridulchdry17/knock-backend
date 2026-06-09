from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class PlatformCompanyLock(Base):
    """Lock #3: platform-wide PERMANENT stop for a company domain.

    Set by B5.6 when an explicit-stop reply ("unsubscribe", "stop emailing",
    "remove me", etc.) is detected from anyone at @acme.com. From that
    point forward, NO user on Knock may email @acme.com. No auto-expiry —
    only a super_admin clear via /api/v1/admin/locks/platform/{domain}.

    Rationale: an explicit stop is brand-protective. One student gets a
    "stop emailing" from acme.com? Knock as a platform can't keep firing
    cold emails to that domain across other users.
    """

    __tablename__ = "platform_company_locks"

    company_domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    # 'explicit_stop_reply' | 'manual_admin' | future codes.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

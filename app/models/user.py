from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db.base import Base
from app.models._mixins import TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))

    # Google identity + OAuth tokens (refresh + access encrypted with Fernet at rest)
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True)
    google_refresh_token: Mapped[str | None] = mapped_column(String)
    google_access_token: Mapped[str | None] = mapped_column(String)
    google_token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    google_scopes: Mapped[str | None] = mapped_column(String)
    google_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    daily_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=settings.DEFAULT_DAILY_LIMIT
    )
    sent_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reset_date: Mapped[date | None] = mapped_column(Date)

    sender_signature_name: Mapped[str | None] = mapped_column(String(255))
    sender_signature_city: Mapped[str | None] = mapped_column(String(255))

    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Flipped True by the send worker (B5.5) when Gmail returns
    # invalid_grant / failedPrecondition. Frontend surfaces a "Reconnect Gmail"
    # CTA when this is True. Reset to False on a successful OAuth re-link.
    gmail_disconnected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # B5.6 reply ingestion watermark. Gmail History API ids are 64-bit unsigned
    # integers; we use BigInteger to be safe. NULL means "not bootstrapped yet" —
    # the first ingest run reads the latest historyId from users.getProfile
    # and stores it WITHOUT fetching messages (bootstrap), so the next run
    # only sees genuinely-new history events.
    gmail_history_id: Mapped[int | None] = mapped_column(BigInteger)

    # Phase 4 soft-gate + tier model. See memory.md log entries 2026-05-05.
    waitlist_email: Mapped[str | None] = mapped_column(String(255), unique=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    tier_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # B5.2 preferences. Free-text targeting metadata is collected here but not
    # yet read by the v0 batch picker — locked product decision per
    # project_targeting_model.md. `target_industries` is a JSON-encoded list[str];
    # serialize/deserialize at the Pydantic boundary.
    target_role: Mapped[str | None] = mapped_column(String(255))
    target_industries: Mapped[str | None] = mapped_column(Text)
    target_location: Mapped[str | None] = mapped_column(String(255))

    notify_gmail_disconnect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    notify_daily_summary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    autopilot_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    autopilot_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    autopilot_auto_pause_on_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # ─────────────── stop conditions (migration 0025) ───────────────
    # Anchor for counter helpers and platform ceilings. Set on every
    # toggle-on so counters reset; preserved on toggle-off for audit.
    autopilot_enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # 'none' | 'replies' | 'end_date' | 'budget'. Radio-group semantics —
    # only ONE of the value columns below is meaningful at a time; the
    # preferences service nulls the siblings on stop_type change.
    autopilot_stop_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none"
    )
    autopilot_stop_at_replies: Mapped[int | None] = mapped_column(Integer)
    autopilot_stop_at_date: Mapped[date | None] = mapped_column(Date)
    autopilot_stop_at_budget: Mapped[int | None] = mapped_column(Integer)
    # Written by the autopilot cycle when a stop condition fires. Reads:
    # 'user' | 'replies' | 'end_date' | 'budget' | 'ceiling_sends' |
    # 'ceiling_days'. Cleared on resume.
    autopilot_paused_reason: Mapped[str | None] = mapped_column(String(32))

    resume_url: Mapped[str | None] = mapped_column(String(2048))

    @property
    def has_gmail_connected(self) -> bool:
        return bool(self.google_refresh_token)

    @property
    def is_onboarded(self) -> bool:
        """A user is onboarded once they have claimed (or been auto-claimed
        for) a waitlist email. Pending users with `waitlist_email IS NOT NULL`
        are still onboarded but awaiting approval — see `tier`."""
        return self.waitlist_email is not None

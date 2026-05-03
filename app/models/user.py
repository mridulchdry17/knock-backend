from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String
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

    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def has_gmail_connected(self) -> bool:
        return bool(self.google_refresh_token)

    @property
    def is_onboarded(self) -> bool:
        return bool(self.sender_signature_name)

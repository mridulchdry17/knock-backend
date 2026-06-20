from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_ENV: Literal["development", "production", "test"] = "development"

    DATABASE_URL: str = "sqlite:///./db/outreach.db"
    DB_ECHO: bool = False

    # Turso embedded replica (perf). When set to a writable file path AND
    # DATABASE_URL is a libsql:// URL, the app keeps a LOCAL SQLite replica at
    # this path: reads come from local disk (microseconds), writes forward to
    # the remote Turso primary, and the replica pulls changes every
    # LIBSQL_SYNC_INTERVAL seconds. Empty = disabled (talk to remote directly,
    # current behavior). Off by default — enable on the single-process VM only.
    LIBSQL_REPLICA_PATH: str = ""
    LIBSQL_SYNC_INTERVAL: int = 30

    FRONTEND_ORIGIN: str = "http://localhost:3000"
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    COOKIE_DOMAIN: str = ""
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    TOKEN_ENCRYPTION_KEY: str = ""

    HUNTER_API_KEY: str = ""

    TIMEZONE: str = "Asia/Kolkata"
    SEND_HOURS_START: int = 9
    SEND_HOURS_END: int = 19
    DEFAULT_DAILY_LIMIT: int = 20
    HARD_DAILY_CEILING: int = 30
    GLOBAL_LOCK_DAYS: int = 2
    FOLLOWUP_DELAY_DAYS: int = 4
    MAX_FOLLOWUPS: int = 2
    # Per-user "I already emailed this contact" cooldown. Once a user emails
    # a contact (initial or follow-up, TO or CC), the contact is hidden from
    # that user's daily batch for this many days. Distinct from the 36h
    # platform-wide cohort hold (any user → blocks everyone) and the 2-day
    # post-reply user lock.
    USER_CONTACT_COOLDOWN_DAYS: int = 30
    # Two-token auth (migration 0022). The `sessions` row is now the SHORT-
    # lived access token; SESSION_TTL_DAYS=30 was the v0 single-token TTL but
    # we keep the same column for backwards compatibility. The frontend never
    # holds the access token in persistent storage — it lives in memory only,
    # so the 15-minute TTL bounds the XSS exfil window. The long-lived state
    # lives in refresh_tokens (HttpOnly cookie, REFRESH_TOKEN_TTL_DAYS).
    SESSION_TTL_DAYS: int = 30  # legacy alias; effective TTL is the minutes below
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 30

    LOG_LEVEL: str = "INFO"
    # Master switch for the in-process autopilot scheduler (APScheduler). OFF by
    # default so dev / test / CI never auto-send. Turn ON only in the prod
    # deployment (single process — see app/jobs/scheduler.py for the
    # single-worker assumption).
    RUN_SCHEDULER: bool = False
    # How often the autopilot cycle (batch-gen → send-drain → reply-ingest)
    # fires. The cycle is idempotent, so batch generation effectively runs once
    # per UTC day while the drain delivers each staggered send slot as it comes
    # due and ingest pulls replies. 30 min gives finer granularity than the
    # paid ~44-min send spacing without hammering the Gmail API.
    AUTOPILOT_CYCLE_INTERVAL_MINUTES: int = 30

    # Comma-separated emails auto-promoted to tier='super_admin' on every login.
    # Renamed from ADMIN_EMAILS in Phase 4 to disambiguate from feature tiers.
    SUPER_ADMIN_EMAILS: str = ""

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def super_admin_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.SUPER_ADMIN_EMAILS.split(",") if e.strip()}

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV == "production"

    @field_validator("DEFAULT_DAILY_LIMIT", "HARD_DAILY_CEILING")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

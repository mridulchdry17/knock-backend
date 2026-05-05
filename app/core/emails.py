"""Email normalization — the single rule about how we store and compare emails.

All persisted email columns and all comparison sites (waitlist match, OAuth
callback, super_admin allowlist, claim endpoint) MUST go through normalize_email
before storage or comparison. Centralizing it here means a future rule change
(e.g. Gmail dot-handling) is one edit, not a grep across the codebase.
"""
from __future__ import annotations


def normalize_email(email: str) -> str:
    """Lowercase + strip. The canonical form for comparison and storage."""
    return email.strip().lower()

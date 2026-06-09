"""Tests for the bounce detector in gmail_reply_fetcher.

Recall > precision: a bounce mis-filed as a reply locks a dead address, so any
one signal (mailer-daemon From, failure subject, delivery-status part) counts.
"""
from __future__ import annotations

import pytest

from app.services.gmail_reply_fetcher import _detect_bounce


@pytest.mark.parametrize(
    "from_email, subject",
    [
        ("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)"),
        ("MAILER-DAEMON@google.com", "anything"),
        ("postmaster@acme.com", "anything"),
        ("someone@acme.com", "Undelivered Mail Returned to Sender"),
        ("someone@acme.com", "Mail delivery failed: returning message to sender"),
        ("someone@acme.com", "failure notice"),
        ("noreply@acme.com", "Address not found"),
    ],
)
def test_detects_bounce(from_email: str, subject: str) -> None:
    assert _detect_bounce(from_email=from_email, subject=subject, payload={}) is True


def test_detects_delivery_status_mime_part() -> None:
    payload = {
        "mimeType": "multipart/report",
        "parts": [
            {"mimeType": "text/plain", "body": {}},
            {"mimeType": "message/delivery-status", "body": {}},
        ],
    }
    assert _detect_bounce(from_email="x@acme.com", subject="Re: hi", payload=payload) is True


@pytest.mark.parametrize(
    "from_email, subject",
    [
        ("john@acme.com", "Re: Quick intro"),
        ("jane@startup.io", "Sounds good — let's chat"),
        ("hr@bigco.com", "Thanks for reaching out"),
    ],
)
def test_real_reply_is_not_a_bounce(from_email: str, subject: str) -> None:
    assert _detect_bounce(from_email=from_email, subject=subject, payload={}) is False

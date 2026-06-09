"""Tests for the Gmail send adapter (B5.5).

Covers:
- MIME builder: To/Cc/From/Subject correctness, display-name escaping.
- HttpError classification: every failure_kind branch.
- Success path: returns ok=True with message/thread ids.
- Generic exceptions classified as 'unknown' without raising.
"""
from __future__ import annotations

import base64
import json
from email import message_from_bytes
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from app.services import gmail_send

# ─────────────────────────── MIME builder ───────────────────────────


def _decode_to_message(msg):
    raw = msg.as_bytes()
    return message_from_bytes(raw)


def test_build_mime_basic_headers() -> None:
    msg, _ = gmail_send.build_mime(
        sender_email="alice@example.com",
        sender_name="Alice Doe",
        to_email="bob@acme.com",
        cc_emails=["c1@acme.com", "c2@acme.com"],
        subject="Hello",
        body_text="Body text here.",
    )
    parsed = _decode_to_message(msg)
    assert "Alice Doe" in parsed["From"]
    assert "alice@example.com" in parsed["From"]
    assert parsed["To"] == "bob@acme.com"
    assert "c1@acme.com" in parsed["Cc"] and "c2@acme.com" in parsed["Cc"]
    assert parsed["Subject"] == "Hello"
    assert "Body text here." in parsed.get_payload()


def test_build_mime_omits_cc_when_empty() -> None:
    msg, _ = gmail_send.build_mime(
        sender_email="alice@example.com",
        sender_name=None,
        to_email="bob@acme.com",
        cc_emails=[],
        subject="s",
        body_text="b",
    )
    parsed = _decode_to_message(msg)
    assert parsed["Cc"] is None


def test_build_mime_display_name_with_comma_is_escaped() -> None:
    """RFC-compliant: 'Doe, Alice' must be quoted in From."""
    msg, _ = gmail_send.build_mime(
        sender_email="alice@example.com",
        sender_name='Doe, Alice',
        to_email="bob@acme.com",
        cc_emails=[],
        subject="s",
        body_text="b",
    )
    parsed = _decode_to_message(msg)
    # Quoted form survives the round-trip:
    assert "Doe, Alice" in parsed["From"]
    # And the email address remains intact:
    assert "alice@example.com" in parsed["From"]


def test_build_mime_no_sender_name_uses_bare_email() -> None:
    msg, _ = gmail_send.build_mime(
        sender_email="alice@example.com",
        sender_name=None,
        to_email="bob@acme.com",
        cc_emails=[],
        subject="s",
        body_text="b",
    )
    parsed = _decode_to_message(msg)
    assert parsed["From"] == "alice@example.com"


def test_build_mime_flattens_html_body_to_plain_text() -> None:
    # Safety net: a body stored as HTML before the render-time flatten shipped
    # must still reach the recipient as plain text, not literal <p> tags.
    msg, _ = gmail_send.build_mime(
        sender_email="alice@example.com",
        sender_name=None,
        to_email="bob@acme.com",
        cc_emails=[],
        subject="s",
        body_text="<p>Hi Alex,</p><p>Best,<br>Mridul</p>",
    )
    payload = _decode_to_message(msg).get_payload()
    assert "<p>" not in payload and "</p>" not in payload and "<br>" not in payload
    assert "Hi Alex," in payload
    assert "Best,\nMridul" in payload


def test_encode_for_gmail_is_base64url() -> None:
    msg, _ = gmail_send.build_mime(
        sender_email="a@x.com",
        sender_name=None,
        to_email="b@y.com",
        cc_emails=[],
        subject="s",
        body_text="hello",
    )
    encoded = gmail_send._encode_for_gmail(msg)
    # base64url is "-_" alphabet, no padding spec but we keep '=' OK.
    assert "+" not in encoded and "/" not in encoded
    decoded = base64.urlsafe_b64decode(encoded.encode())
    assert b"hello" in decoded


# ─────────────────────────── error classification ───────────────────────────


def _http_error(status: int, *, reason: str = "", message: str = "boom") -> HttpError:
    """Build an HttpError matching googleapiclient's shape (errors[0].reason)."""
    resp = MagicMock()
    resp.status = status
    resp.reason = ""
    payload = {"error": {"message": message, "errors": [{"reason": reason}] if reason else []}}
    content = json.dumps(payload).encode()
    return HttpError(resp=resp, content=content)


@pytest.mark.parametrize("status, reason", [(401, ""), (403, ""), (401, "invalid_grant")])
def test_classify_auth_revoked(status: int, reason: str) -> None:
    kind, _code, msg = gmail_send.classify_http_error(_http_error(status, reason=reason))
    assert kind == "gmail_auth_revoked"
    assert msg


def test_classify_failed_precondition_is_auth_revoked() -> None:
    kind, _, _ = gmail_send.classify_http_error(
        _http_error(400, reason="failedPrecondition", message="Mail service not enabled")
    )
    assert kind == "gmail_auth_revoked"


@pytest.mark.parametrize(
    "status, reason",
    [
        (429, ""),
        (403, "quotaExceeded"),
        (429, "userRateLimitExceeded"),
        (429, "rateLimitExceeded"),
    ],
)
def test_classify_quota(status: int, reason: str) -> None:
    """403+quotaExceeded specifically routes to quota_exceeded, not auth_revoked."""
    kind, code, _ = gmail_send.classify_http_error(_http_error(status, reason=reason))
    assert kind == "quota_exceeded"
    # gmail_error_code preserves the reason when present
    if reason:
        assert code == reason


def test_classify_recipient_rejected() -> None:
    kind, _, _ = gmail_send.classify_http_error(
        _http_error(400, message="Invalid recipient address: bob@acme.com")
    )
    assert kind == "recipient_rejected"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_classify_transient_5xx(status: int) -> None:
    kind, _, _ = gmail_send.classify_http_error(_http_error(status))
    assert kind == "transient"


def test_classify_unknown_400_without_recipient() -> None:
    kind, _, _ = gmail_send.classify_http_error(
        _http_error(400, message="some other validation problem")
    )
    assert kind == "unknown"


def test_classify_truncates_error_message_to_2000_chars() -> None:
    big = "x" * 5000
    _, _, msg = gmail_send.classify_http_error(_http_error(500, message=big))
    assert len(msg) == 2000


# ─────────────────────────── send_email integration ───────────────────────────


def test_send_email_success_returns_ids() -> None:
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "msg-123", "threadId": "thr-456"
    }

    with patch.object(gmail_send, "_build_service", return_value=fake_service):
        result = gmail_send.send_email(
            creds=MagicMock(),
            sender_email="a@x.com",
            sender_name="A",
            to_email="b@y.com",
            cc_emails=[],
            subject="hi",
            body_text="hello",
        )
    assert result.ok
    assert result.gmail_message_id == "msg-123"
    assert result.gmail_thread_id == "thr-456"
    assert result.failure_kind is None


def test_send_email_http_error_returns_failure_not_raise() -> None:
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.send.return_value.execute.side_effect = (
        _http_error(429, reason="rateLimitExceeded")
    )
    with patch.object(gmail_send, "_build_service", return_value=fake_service):
        result = gmail_send.send_email(
            creds=MagicMock(),
            sender_email="a@x.com",
            sender_name=None,
            to_email="b@y.com",
            cc_emails=[],
            subject="hi",
            body_text="hello",
        )
    assert not result.ok
    assert result.failure_kind == "quota_exceeded"
    assert result.gmail_error_code == "rateLimitExceeded"


def test_send_email_generic_exception_classified_unknown() -> None:
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.send.return_value.execute.side_effect = (
        RuntimeError("network broke")
    )
    with patch.object(gmail_send, "_build_service", return_value=fake_service):
        result = gmail_send.send_email(
            creds=MagicMock(),
            sender_email="a@x.com",
            sender_name=None,
            to_email="b@y.com",
            cc_emails=[],
            subject="hi",
            body_text="hello",
        )
    assert not result.ok
    assert result.failure_kind == "unknown"
    assert "network broke" in (result.error_message or "")

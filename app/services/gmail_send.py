"""Gmail send adapter — pure-ish wrapper around googleapiclient.

Why a thin adapter:
  - Worker code is testable without monkey-patching the Google SDK directly.
  - Error classification lives in ONE place, so the dashboard's failure_kind
    taxonomy stays consistent.

MIME: text/plain only. The body may arrive as rich-text HTML (the template
editor serializes via getHTML()), so build_mime flattens it with html_to_text
before set_content — a personal plain-text note lands better for cold student
outreach than an HTML email. A future HTML variant would be additive
(.add_alternative on the message).
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from typing import TYPE_CHECKING, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.services.html_to_text import html_to_text

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


_MAX_ERROR_LEN = 2000


# ─────────────────────────── result type ───────────────────────────


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of a single Gmail send attempt.

    On success: ok=True, gmail_message_id is set, all error fields None.
    On failure: ok=False, gmail_message_id None, failure_kind/error_message set.

    `rfc822_message_id` is the literal "Message-ID:" header value we generated
    pre-send (e.g. "<abc@mail.gmail.com>"). Distinct from gmail_message_id
    (Gmail's API id). Persisted on send_queue so a follow-up can build the
    In-Reply-To / References headers without a second Gmail API roundtrip.
    """

    ok: bool
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    rfc822_message_id: str | None = None
    failure_kind: str | None = None
    gmail_error_code: str | None = None
    error_message: str | None = None


# ─────────────────────────── MIME builder ───────────────────────────


def build_mime(
    *,
    sender_email: str,
    sender_name: str | None,
    to_email: str,
    cc_emails: list[str],
    subject: str,
    body_text: str,
    in_reply_to_rfc822_id: str | None = None,
    references_rfc822_ids: list[str] | None = None,
) -> tuple[EmailMessage, str]:
    """Build a text/plain EmailMessage with properly-encoded headers.

    Returns (msg, rfc822_message_id) — the Message-ID we self-generated and set
    on the outgoing message. We control it (vs. letting Gmail assign one) so
    a later follow-up can put it in In-Reply-To / References without a second
    API roundtrip.

    `in_reply_to_rfc822_id` + `references_rfc822_ids` enable threading on
    non-Gmail MUAs (Gmail uses its own `threadId` on the API call). Both are
    optional; when set, they're the RFC822 Message-ID values of prior
    messages in the chain (already wrapped in '<…>' or not — we normalize).
    """
    msg = EmailMessage()

    if sender_name:
        local, _, domain = sender_email.partition("@")
        msg["From"] = Address(display_name=sender_name, username=local, domain=domain)
    else:
        msg["From"] = sender_email

    msg["To"] = to_email
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg["Subject"] = subject

    # Self-controlled Message-ID — domain comes from the sender's address so it
    # looks legitimate to spam filters.
    sender_domain = sender_email.partition("@")[2] or "knock.app"
    rfc822_id = make_msgid(domain=sender_domain)
    msg["Message-ID"] = rfc822_id

    if in_reply_to_rfc822_id:
        normalized = _wrap_msgid(in_reply_to_rfc822_id)
        msg["In-Reply-To"] = normalized
        # References = full chain history; if caller didn't pass the chain, at
        # least include the immediate parent so threading works.
        if references_rfc822_ids:
            msg["References"] = " ".join(_wrap_msgid(r) for r in references_rfc822_ids)
        else:
            msg["References"] = normalized

    # Safety net: render_template already flattens HTML, but a body that was
    # stored as HTML before this shipped (or any path that bypasses render)
    # would otherwise reach the recipient with literal <p>…</p> tags, since
    # the message is text/plain. Idempotent on already-plain text.
    msg.set_content(html_to_text(body_text))
    return msg, rfc822_id


def _wrap_msgid(value: str) -> str:
    """Normalize a Message-ID to its angle-bracketed form. make_msgid() already
    produces `<…>` but we accept either shape for caller convenience."""
    v = value.strip()
    if v.startswith("<") and v.endswith(">"):
        return v
    return f"<{v}>"


def _encode_for_gmail(msg: EmailMessage) -> str:
    """Gmail API wants the raw RFC822 bytes, base64url-encoded, no padding."""
    raw = msg.as_bytes()
    return base64.urlsafe_b64encode(raw).decode("ascii")


# ─────────────────────────── error classification ───────────────────────────


def _truncate(s: str | None) -> str:
    return (s or "")[:_MAX_ERROR_LEN]


def _extract_gmail_error(err: HttpError) -> tuple[int, str, str]:
    """Return (http_status, gmail_reason_code, human_message).

    `gmail_reason_code` is whatever Gmail puts in errors[0].reason
    ("rateLimitExceeded", "invalid_grant", "failedPrecondition", ...). Falls
    back to "" if absent.
    """
    status = int(getattr(err.resp, "status", 0) or 0)
    reason = ""
    message = str(err)

    content = getattr(err, "content", None)
    if content:
        try:
            payload = json.loads(content.decode() if isinstance(content, bytes) else content)
            err_obj = payload.get("error") or {}
            errors_list = err_obj.get("errors") or []
            if errors_list:
                reason = errors_list[0].get("reason") or ""
            message = err_obj.get("message") or message
        except (ValueError, AttributeError):
            pass

    return status, reason, message


def classify_http_error(err: HttpError) -> tuple[str, str, str]:
    """Map HttpError to (failure_kind, gmail_error_code, error_message).

    Kinds:
      - gmail_auth_revoked: 401/403 with invalid_grant / failedPrecondition
      - quota_exceeded:     429 / quotaExceeded / userRateLimitExceeded /
                            rateLimitExceeded
      - recipient_rejected: 400 with "recipient" in the message
      - transient:          5xx
      - unknown:            everything else
    """
    status, reason, message = _extract_gmail_error(err)

    # Quota reasons are checked first because Gmail returns 403 + quotaExceeded
    # for quota issues — that's NOT an auth-revoked situation.
    if reason in ("quotaExceeded", "userRateLimitExceeded", "rateLimitExceeded"):
        return "quota_exceeded", reason, _truncate(message)
    if status == 429:
        return "quota_exceeded", reason or "http_429", _truncate(message)

    if reason in ("invalid_grant", "failedPrecondition"):
        return "gmail_auth_revoked", reason, _truncate(message)
    if status in (401, 403):
        return "gmail_auth_revoked", reason or f"http_{status}", _truncate(message)

    if status == 400 and "recipient" in message.lower():
        return "recipient_rejected", reason or "http_400", _truncate(message)

    if 500 <= status < 600:
        return "transient", reason or f"http_{status}", _truncate(message)

    return "unknown", reason or (f"http_{status}" if status else "unknown"), _truncate(message)


# ─────────────────────────── public API ───────────────────────────


def _build_service(creds: Credentials) -> Any:
    """Indirection so tests can patch this without touching googleapiclient.

    cache_discovery=False suppresses the cache warning on each call; we don't
    long-run the worker so the discovery cost is acceptable.
    """
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(
    creds: Credentials,
    *,
    sender_email: str,
    sender_name: str | None,
    to_email: str,
    cc_emails: list[str],
    subject: str,
    body_text: str,
) -> SendResult:
    """Send one email via Gmail API. Never raises — always returns a SendResult.

    Caller (the send worker) decides what to do with each result:
      - on ok → record sent_at, write send_queue row, advance lock.
      - on failure_kind=='gmail_auth_revoked' → also flip user.gmail_disconnected.
      - on others → just log + insert email_failures row.
    """
    msg, rfc822_id = build_mime(
        sender_email=sender_email,
        sender_name=sender_name,
        to_email=to_email,
        cc_emails=cc_emails,
        subject=subject,
        body_text=body_text,
    )
    return _dispatch(msg, rfc822_id, creds, thread_id=None)


def send_followup(
    creds: Credentials,
    *,
    sender_email: str,
    sender_name: str | None,
    to_email: str,
    cc_emails: list[str],
    subject: str,
    body_text: str,
    gmail_thread_id: str,
    in_reply_to_rfc822_id: str | None,
    references_rfc822_ids: list[str] | None = None,
) -> SendResult:
    """Send a follow-up that lands on the SAME Gmail thread as the original.

    Threading mechanics:
      - Gmail-side: pass `threadId` on the API body so Gmail groups it.
      - Universal: set `In-Reply-To` + `References` headers so non-Gmail MUAs
        also thread it (mostly belt-and-suspenders since recruiters are mostly
        on Gmail, but free correctness).
      - Subject: caller is responsible for the "Re: " prefix (kept here so the
        function stays mechanical — no string surgery on a user-authored body).
    """
    msg, rfc822_id = build_mime(
        sender_email=sender_email,
        sender_name=sender_name,
        to_email=to_email,
        cc_emails=cc_emails,
        subject=subject,
        body_text=body_text,
        in_reply_to_rfc822_id=in_reply_to_rfc822_id,
        references_rfc822_ids=references_rfc822_ids,
    )
    return _dispatch(msg, rfc822_id, creds, thread_id=gmail_thread_id)


def _dispatch(
    msg: EmailMessage,
    rfc822_id: str,
    creds: Credentials,
    *,
    thread_id: str | None,
) -> SendResult:
    """Common send path — never raises, always returns SendResult."""
    try:
        service = _build_service(creds)
        body: dict[str, Any] = {"raw": _encode_for_gmail(msg)}
        if thread_id:
            body["threadId"] = thread_id
        response = service.users().messages().send(userId="me", body=body).execute()
        return SendResult(
            ok=True,
            gmail_message_id=response.get("id"),
            gmail_thread_id=response.get("threadId"),
            rfc822_message_id=rfc822_id,
        )
    except HttpError as e:
        kind, code, message = classify_http_error(e)
        return SendResult(
            ok=False,
            failure_kind=kind,
            gmail_error_code=code,
            error_message=message,
        )
    except Exception as e:
        return SendResult(
            ok=False,
            failure_kind="unknown",
            gmail_error_code=None,
            error_message=_truncate(repr(e)),
        )

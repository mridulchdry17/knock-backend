"""Gmail History API wrapper for B5.6 reply ingestion.

Pure-ish adapter around `googleapiclient.discovery.build("gmail", "v1", ...)`.
Why a thin wrapper:
  - Reply ingestor logic stays decoupled from the Google SDK (easy to mock).
  - Error classification lives in ONE place, matching the failure_kind
    taxonomy from `app.services.gmail_send.classify_http_error`.

Bootstrap behavior: on the user's very first run, `start_history_id` is None.
Calling `users.history.list(startHistoryId=…)` with no anchor would either
fail or surface the entire mailbox — neither is acceptable for a worker
that runs against many users on a single thread. We instead read the
latest historyId via `users.getProfile()` and return ([], that_id). The
NEXT run then picks up only what's genuinely new.

Quotas + caps:
  - Per run: hard cap at 500 fetched messages (`_MAX_MESSAGES_PER_RUN`).
    We still walk the history pages to compute the highest seen historyId
    so we don't replay events forever, but we stop fetching message bodies
    once the cap is hit. The user's next ingest run will catch up.
  - HttpError → mapped to FetchError with `kind` ∈
    {gmail_auth_revoked, transient, quota_exceeded, unknown}.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.logging_config import get_logger
from app.services.gmail_send import classify_http_error

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

log = get_logger("gmail_reply_fetcher")


# Hard cap to keep one bad mailbox from draining the worker. The next run
# will pick up from the highest seen historyId so progress isn't lost.
_MAX_MESSAGES_PER_RUN = 500


# ─────────────────────────── public types ───────────────────────────


@dataclass(frozen=True, slots=True)
class FetchedReply:
    """One inbound Gmail message that may be a reply to a Knock send."""

    gmail_message_id: str
    gmail_thread_id: str
    from_email: str
    from_domain: str
    subject: str
    body_text: str
    internal_date: datetime


class FetchError(Exception):
    """Raised on any Gmail API failure the ingestor needs to react to.

    `kind` mirrors `app.services.gmail_send` taxonomy:
      - gmail_auth_revoked: 401/403 / invalid_grant → ingestor flips user.gmail_disconnected
      - quota_exceeded:     429 / quotaExceeded → ingestor skips this user this run
      - transient:          5xx → ingestor skips, retried next run
      - unknown:            anything else
    """

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


# ─────────────────────────── helpers ───────────────────────────


def _build_service(creds: Credentials) -> Any:
    """Indirection so tests can patch this without touching googleapiclient."""
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_b64url(data: str) -> bytes:
    """Gmail returns base64url-encoded body parts; pad correctly before decode."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _extract_plain_body(payload: dict[str, Any]) -> str:
    """Walk the MIME tree and return the first text/plain part's body.

    Falls back to text/html → strip tags coarsely if no text/plain found.
    Empty string when neither is present.
    """
    # Single-part case: payload.body.data holds the content.
    mime_type = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    data = body.get("data")

    if mime_type == "text/plain" and data:
        try:
            return _decode_b64url(data).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover — defensive
            return ""

    # Multipart: depth-first, prefer text/plain.
    parts = payload.get("parts") or []
    if parts:
        for part in parts:
            if (part.get("mimeType") or "").lower() == "text/plain":
                body_data = (part.get("body") or {}).get("data")
                if body_data:
                    try:
                        return _decode_b64url(body_data).decode("utf-8", errors="replace")
                    except Exception:  # pragma: no cover
                        return ""
        # Recurse into nested multipart/alternative or multipart/mixed.
        for part in parts:
            nested = _extract_plain_body(part)
            if nested:
                return nested

    # Last resort: an html-only message. Crudely strip tags — we only need
    # enough plain text for the explicit-stop regex.
    if mime_type == "text/html" and data:
        try:
            raw = _decode_b64url(data).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            return ""
        import re

        return re.sub(r"<[^>]+>", " ", raw)

    return ""


def _extract_header(headers: list[dict[str, str]], name: str) -> str:
    target = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == target:
            return h.get("value") or ""
    return ""


def _parse_from(raw: str) -> tuple[str, str]:
    """Extract (email_address, lowercased_domain) from a `From:` header value."""
    if not raw:
        return "", ""
    import re

    match = re.search(r"<([^>]+)>", raw)
    addr = match.group(1) if match else raw.strip()
    addr = addr.strip().strip("<>").strip()
    domain = addr.partition("@")[2].lower()
    return addr, domain


def _parse_internal_date(raw: str | int | None) -> datetime:
    """Gmail returns internalDate as a string of millis-since-epoch (UTC)."""
    if raw is None:
        return datetime.now(UTC)
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return datetime.fromtimestamp(millis / 1000.0, tz=UTC)


# ─────────────────────────── public API ───────────────────────────


def fetch_new_replies(
    creds: Credentials, *, start_history_id: int | None
) -> tuple[list[FetchedReply], int]:
    """Fetch reply candidates from Gmail since `start_history_id`.

    Returns (replies, new_history_id_high_watermark).

    Contract:
      - start_history_id is None → bootstrap. We read users.getProfile() to
        get the current historyId, return ([], that_id). NO message fetches.
      - start_history_id given → paginate users.history.list, collect
        messageAdded events, fetch each via users.messages.get(format='full'),
        filter out SENT-labeled messages (only inbound matters), decode body.

    Raises FetchError(kind=…) on Gmail API failure. Never raises HttpError
    directly — the ingestor doesn't need to know about the SDK.
    """
    try:
        service = _build_service(creds)

        # ── Bootstrap path ──────────────────────────────────────────
        if start_history_id is None:
            profile = service.users().getProfile(userId="me").execute()
            latest = int(profile.get("historyId") or 0)
            log.info("gmail_reply_fetcher.bootstrap", historyId=latest)
            return [], latest

        # ── Incremental path ────────────────────────────────────────
        # Walk history pages, collecting messageAdded ids. We stop fetching
        # bodies once we hit the per-run cap, but we still track the highest
        # historyId seen so the cursor advances.
        message_ids: list[str] = []
        highest_history_id = start_history_id
        page_token: str | None = None

        while True:
            req = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=str(start_history_id),
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
            )
            page = req.execute()

            for entry in page.get("history") or []:
                try:
                    entry_hid = int(entry.get("id") or 0)
                except (TypeError, ValueError):
                    entry_hid = 0
                if entry_hid > highest_history_id:
                    highest_history_id = entry_hid

                for added in entry.get("messagesAdded") or []:
                    msg = added.get("message") or {}
                    labels = msg.get("labelIds") or []
                    # Skip our own sends — only inbound matters.
                    if "SENT" in labels:
                        continue
                    mid = msg.get("id")
                    if mid:
                        message_ids.append(mid)

            # If Gmail reported a fresher historyId in the page envelope, take it.
            page_history_id = page.get("historyId")
            if page_history_id:
                try:
                    candidate = int(page_history_id)
                    if candidate > highest_history_id:
                        highest_history_id = candidate
                except (TypeError, ValueError):
                    pass

            page_token = page.get("nextPageToken")
            if not page_token:
                break

        # De-dup while preserving order (a single message can appear under
        # multiple history entries).
        seen: set[str] = set()
        unique_ids: list[str] = []
        for mid in message_ids:
            if mid in seen:
                continue
            seen.add(mid)
            unique_ids.append(mid)

        # Cap fetches.
        capped = unique_ids[:_MAX_MESSAGES_PER_RUN]
        if len(unique_ids) > _MAX_MESSAGES_PER_RUN:
            log.warning(
                "gmail_reply_fetcher.cap_hit",
                total_candidates=len(unique_ids),
                cap=_MAX_MESSAGES_PER_RUN,
            )

        replies: list[FetchedReply] = []
        for mid in capped:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            # Defensive: getMessage can sometimes return SENT in label_ids
            # even when history listed it as messageAdded (e.g. cross-label
            # bookkeeping). Skip again.
            labels = full.get("labelIds") or []
            if "SENT" in labels:
                continue

            payload = full.get("payload") or {}
            headers = payload.get("headers") or []
            from_raw = _extract_header(headers, "From")
            from_email, from_domain = _parse_from(from_raw)
            subject = _extract_header(headers, "Subject")
            body_text = _extract_plain_body(payload)
            internal_date = _parse_internal_date(full.get("internalDate"))

            replies.append(
                FetchedReply(
                    gmail_message_id=mid,
                    gmail_thread_id=full.get("threadId") or "",
                    from_email=from_email,
                    from_domain=from_domain,
                    subject=subject,
                    body_text=body_text,
                    internal_date=internal_date,
                )
            )

        return replies, highest_history_id

    except HttpError as e:
        kind, code, message = classify_http_error(e)
        log.warning(
            "gmail_reply_fetcher.http_error",
            kind=kind,
            gmail_error_code=code,
            message=message[:200],
        )
        raise FetchError(kind, message) from e


__all__ = ["FetchError", "FetchedReply", "fetch_new_replies"]

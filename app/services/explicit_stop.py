"""Explicit-stop language detector for B5.6 reply ingestion.

The output drives the 3-tier lock model:
- True  → platform-permanent lock on the company domain (brand-protective)
- False → ordinary 30-day per-user reply lock

v0 trade-off: recall > precision. We want to catch every "please stop"
even if that means occasionally locking a domain on a sarcastic sentence
("I'd unsubscribe if you spam me" trips the simple regex). Super-admin has
DELETE /admin/locks/platform/{domain} as the manual override. We can layer
LLM-classification on top later without changing the public contract.

Public API:
    is_explicit_stop(body: str) -> bool

Implementation notes:
- Patterns are compiled ONCE at module import (regex compilation is not
  free, and this runs on every inbound message).
- We strip the quoted-reply block before scanning, so the user's original
  Knock outreach (which itself may contain "unsubscribe" copy in the
  signature/footer one day) does NOT cause a false-positive on every
  reply.
- We scan only the first 2000 chars after quote-stripping — replies are
  short; pathological lengths shouldn't drive runaway regex work.
"""
from __future__ import annotations

import re

# Cap how much body we inspect AFTER stripping quoted blocks. Real reply
# language for "stop emailing me" sits in the first few lines; long replies
# are usually quoted threads, which we already strip.
_BODY_SCAN_CAP = 2000

# A reply's quoted-original block typically begins with "On <date>, <person> wrote:".
# Match the standard Gmail/Outlook variants; the date format is locale-dependent
# so we keep it loose.
_GMAIL_REPLY_HEADER = re.compile(
    r"^On\s.+\swrote:\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Compile core stop-phrase patterns ONCE. \b word boundaries keep us from
# matching mid-word (e.g., "unsubscribed" still matches; "presubscribe"
# would not — desirable v0 behavior).
_STOP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
    re.compile(r"\bstop\s+emailing\b", re.IGNORECASE),
    re.compile(r"\bstop\s+contacting\b", re.IGNORECASE),
    re.compile(r"\bdon'?t\s+email\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+email\b", re.IGNORECASE),
    re.compile(r"\bremove\s+me\s+from\b", re.IGNORECASE),
    re.compile(r"\btake\s+me\s+off\b", re.IGNORECASE),
)

# "not interested" alone is too soft (users may say "not interested in the
# role but happy to chat"). Require a stop-adjacent word within ~6 tokens.
_NOT_INTERESTED_FOLLOWUP = re.compile(
    r"\bnot\s+interested\b(?:\W+\w+){0,6}?\W+(?:stop|emails?|future|further)\b",
    re.IGNORECASE,
)


def _strip_quoted_block(body: str) -> str:
    """Remove the reply's quoted-original block before scanning.

    Drops:
      - every line starting with '>' (RFC 3676 / classic quote convention)
      - everything from "On <date>... wrote:" to end-of-body
    """
    # Truncate at the Gmail-style attribution header if present.
    match = _GMAIL_REPLY_HEADER.search(body)
    if match is not None:
        body = body[: match.start()]

    # Drop quote-prefixed lines.
    kept: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def is_explicit_stop(body: str) -> bool:
    """Return True if `body` contains explicit stop-language.

    Strips quoted-reply blocks first, caps inspection to 2000 chars, then
    runs all compiled patterns. Empty / falsy input → False.
    """
    if not body:
        return False

    scan = _strip_quoted_block(body)[:_BODY_SCAN_CAP]

    for pattern in _STOP_PATTERNS:
        if pattern.search(scan):
            return True

    return bool(_NOT_INTERESTED_FOLLOWUP.search(scan))


__all__ = ["is_explicit_stop"]

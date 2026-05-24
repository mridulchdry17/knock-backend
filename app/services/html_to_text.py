"""Flatten the rich-text (TipTap) email body to clean plain text.

The template editor serializes bodies to HTML via getHTML() — e.g. <p>…</p>,
<br>, and <span data-variable="first_name">{{first_name}}</span> for variable
chips (see the frontend lib/templates/tiptap-variable.ts, whose comment says the
"backend's HTML→plaintext renderer at send time substitutes values"). Outbound
mail is text/plain — better deliverability and a personal, hand-typed feel for a
student→recruiter cold email — so we flatten the HTML to text right before the
body is stored (batch generation) and again right before send (a safety net for
anything already stored as HTML).

The variable chips emit the literal `{{token}}` as the span's inner text, so
placeholder substitution works whether it runs before or after this flatten.

Plain-text input (the seeded starter templates, or bodies edited in the plain
card editor) passes through untouched — we only act when the string actually
contains a tag.
"""
from __future__ import annotations

import html as _html
import re

# A real tag opens with '<' immediately followed by a letter, '/', or '!'. This
# avoids touching plain prose like "I work < 10 hrs/week" (space after '<').
_LOOKS_LIKE_HTML = re.compile(r"<[A-Za-z/!]")

_BR_RE = re.compile(r"(?i)<br\s*/?>")
_LIST_ITEM_RE = re.compile(r"(?i)<li[^>]*>")
# Closing block tags become a paragraph break.
_BLOCK_CLOSE_RE = re.compile(r"(?i)</(p|div|li|h[1-6]|blockquote|tr|ul|ol)\s*>")
_TAG_RE = re.compile(r"<[^>]+>")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def html_to_text(s: str) -> str:
    """Return `s` as plain text. No-op for strings that aren't HTML."""
    if not s or not _LOOKS_LIKE_HTML.search(s):
        return s

    out = _BR_RE.sub("\n", s)
    out = _LIST_ITEM_RE.sub("• ", out)
    out = _BLOCK_CLOSE_RE.sub("\n\n", out)
    out = _TAG_RE.sub("", out)
    out = _html.unescape(out)
    out = out.replace("\xa0", " ")  # any &nbsp; that slipped through
    out = _TRAILING_WS_RE.sub("\n", out)
    out = _MANY_NEWLINES_RE.sub("\n\n", out)
    return out.strip()

"""Reflow plain-text email bodies into natural prose.

Users author templates in Tiptap. Tiptap creates one `<p>` per Enter key,
which our html_to_text flattens to `\\n`-separated lines. When the result is
sent as text/plain email, EVERY line break is a HARD line break in the
recipient's mail client — turning prose written for typographic line-wrap
into newspaper-column-style chopped fragments:

    I'm Mridul, currently an AI Automation Intern at Joveo
    (previously askhonestly.ai and adopt.ai). I've been following
    vlt and would love to explore an AI Engineering
    internship with your team.

The user TYPED this with Enter at line ends as a cosmetic affordance while
composing — they expected the email to render as one flowing paragraph.

`reflow_for_email` does the right thing at the send boundary:

  - Splits the body into BLOCKS separated by blank lines.
  - For each block, if it looks like a list (any line starts with `-`, `*`,
    `• `, or `1.` etc.), preserve internal newlines so bullets render
    correctly.
  - Otherwise, JOIN consecutive non-empty lines with a single space — one
    flowing paragraph per block.
  - Rejoin blocks with `\\n\\n` so the recipient still sees the user's
    intentional paragraph breaks.

This only runs at the email-send boundary (gmail_send.build_mime). The
on-page /today preview deliberately preserves the user's exact line breaks
so the WYSIWYG-ish editing experience isn't lost.
"""
from __future__ import annotations

import re

# A "list-like" line: starts with -, *, • (after optional leading whitespace)
# or with a numbered marker like "1." / "2)". We detect lists per BLOCK so a
# single bullet inside a paragraph doesn't make us preserve the whole block's
# line breaks — typically lists are their own paragraph anyway.
_LIST_LINE_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s+")

# Sign-off lines that conventionally end an email: "Best,", "Thanks,",
# "Regards", etc. A block containing one of these is almost certainly a
# signature, where each line is meant to stand alone (name, role, contact
# links). Match case-insensitively, optional terminal comma / period.
_SIGNOFF_RE = re.compile(
    r"^\s*(best|thanks|thank you|regards|kind regards|warm regards|"
    r"cheers|sincerely|yours|talk soon|looking forward)[,.!]?\s*$",
    re.IGNORECASE,
)

# Contact-field lines that appear in signatures: "LinkedIn: ...",
# "Resume: ...", "Phone: ...", "X: ...", "GitHub: ...", etc.
# Capital first letter + word + colon + space + value.
_CONTACT_FIELD_RE = re.compile(r"^[A-Z][A-Za-z]{1,15}:\s+\S")


def _looks_like_list_block(block: str) -> bool:
    """A block is treated as a list if at least one line in it starts with a
    bullet/numbered marker. Conservative: any mixed block keeps its newlines."""
    return any(_LIST_LINE_RE.match(line) for line in block.split("\n"))


def _looks_like_signature_block(block: str) -> bool:
    """A block is treated as a signature if any of its lines is a sign-off
    keyword (Best, / Thanks, / Regards) or a labeled contact field
    (LinkedIn: / Resume: / X:). Each line in such a block stays on its own."""
    for line in block.split("\n"):
        if _SIGNOFF_RE.match(line) or _CONTACT_FIELD_RE.match(line):
            return True
    return False


def reflow_for_email(text: str) -> str:
    """Join cosmetic line wraps within paragraphs; preserve blank-line breaks
    and list structure. Idempotent on already-reflowed text."""
    if not text:
        return text

    # Normalize CRLF first so the split is consistent.
    normalized = text.replace("\r\n", "\n")

    # Split into blocks by blank lines. `\n\s*\n+` matches one OR MORE blank
    # lines (allowing the in-between line to contain only whitespace, which
    # html_to_text can leave after stripping a `<p></p>`).
    blocks = re.split(r"\n\s*\n+", normalized)

    reflowed_blocks: list[str] = []
    for block in blocks:
        block = block.strip("\n")  # strip leading/trailing newlines per block
        if not block:
            continue

        if _looks_like_list_block(block) or _looks_like_signature_block(block):
            # Preserve structure (lists, signatures). Strip per-line trailing
            # whitespace but keep newlines so the recipient sees the same
            # vertical layout the author intended.
            lines = [line.rstrip() for line in block.split("\n")]
            reflowed_blocks.append("\n".join(lines))
        else:
            # Reflow: join non-empty lines with a single space.
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            reflowed_blocks.append(" ".join(lines))

    return "\n\n".join(reflowed_blocks)

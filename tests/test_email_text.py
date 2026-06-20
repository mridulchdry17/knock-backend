"""Tests for `app.services.email_text.reflow_for_email`.

The real bug it fixes: a template authored in Tiptap with one `<p>` per
Enter ends up as plain text with single newlines between every line. When
sent as text/plain email, every newline is a HARD line break — the
recipient sees newspaper-column-style chopped fragments instead of the
flowing prose the user intended.
"""
from __future__ import annotations

from app.services.email_text import reflow_for_email


def test_empty_is_empty() -> None:
    assert reflow_for_email("") == ""


def test_single_paragraph_with_line_wraps_is_reflowed_to_one_line() -> None:
    # The exact prose pattern from the bug report.
    body = (
        "I'm Mridul, currently an AI Automation Intern at Joveo\n"
        "(previously askhonestly.ai and adopt.ai). I've been following\n"
        "vlt and would love to explore an AI Engineering\n"
        "internship with your team."
    )
    out = reflow_for_email(body)
    assert out == (
        "I'm Mridul, currently an AI Automation Intern at Joveo "
        "(previously askhonestly.ai and adopt.ai). I've been following "
        "vlt and would love to explore an AI Engineering "
        "internship with your team."
    )


def test_blank_line_between_paragraphs_is_preserved() -> None:
    body = "Hi Alex,\n\nI'm reaching out about...\n\nBest, Mridul"
    out = reflow_for_email(body)
    # Three paragraphs, blank line between each.
    assert out == "Hi Alex,\n\nI'm reaching out about...\n\nBest, Mridul"


def test_multi_paragraph_with_internal_wraps_keeps_paragraph_breaks() -> None:
    """The realistic case: greeting + reflowed body + signature, with blank
    lines between. The signature block ('Best,\\nMridul') is detected as a
    sign-off and stays on separate lines."""
    body = (
        "Hi Darcy,\n"
        "\n"
        "I'm Mridul, currently an AI Automation Intern at Joveo\n"
        "(previously askhonestly.ai and adopt.ai). I've been following\n"
        "vlt and would love to explore an AI Engineering\n"
        "internship with your team.\n"
        "\n"
        "Best,\n"
        "Mridul"
    )
    out = reflow_for_email(body)
    assert out == (
        "Hi Darcy,\n"
        "\n"
        "I'm Mridul, currently an AI Automation Intern at Joveo "
        "(previously askhonestly.ai and adopt.ai). I've been following "
        "vlt and would love to explore an AI Engineering "
        "internship with your team.\n"
        "\n"
        "Best,\n"
        "Mridul"
    )


def test_signoff_keyword_keeps_signature_lines_separate() -> None:
    """Any block containing 'Best,' / 'Thanks,' / 'Regards' etc. is treated
    as a signature — every line stays on its own."""
    for signoff in ("Best,", "Thanks,", "Regards", "Cheers,", "Sincerely,"):
        body = f"{signoff}\nMridul"
        out = reflow_for_email(body)
        assert out == f"{signoff}\nMridul", f"failed for {signoff!r}"


def test_contact_field_lines_keep_signature_lines_separate() -> None:
    """A LinkedIn: / Resume: / X: line is a strong signature signal — keep
    line breaks in the block even without a 'Best,' marker."""
    body = (
        "Mridul Chaudhary\n"
        "LinkedIn: https://www.linkedin.com/in/mridulchdry/\n"
        "Resume: https://drive.google.com/file/d/abc\n"
        "X: https://x.com/Mridulchdry"
    )
    out = reflow_for_email(body)
    # All four lines stay on their own.
    assert out == body


def test_dash_bullet_list_keeps_line_breaks() -> None:
    """Lists must preserve newlines so bullets render one-per-line."""
    body = (
        "A few things I've shipped recently:\n"
        "- Scaled users 500 to 1,200+\n"
        "- Built AI tools that cut manual reporting\n"
        "- Designed and shipped a hiring workflow"
    )
    out = reflow_for_email(body)
    # The block has dash bullets → list-block, keep newlines.
    # NOTE: the leading non-list line gets pulled into the list block (one
    # block, terminated by no blank line) — that's the documented behavior
    # to keep "label line + bullets" together.
    assert out == (
        "A few things I've shipped recently:\n"
        "- Scaled users 500 to 1,200+\n"
        "- Built AI tools that cut manual reporting\n"
        "- Designed and shipped a hiring workflow"
    )


def test_numbered_list_keeps_line_breaks() -> None:
    body = "Steps:\n1. Open the app\n2. Click connect\n3. Pick Gmail"
    out = reflow_for_email(body)
    assert "\n1. " in out
    assert "\n2. " in out
    assert "\n3. " in out


def test_bullet_unicode_keeps_line_breaks() -> None:
    body = "Highlights:\n• Item one\n• Item two"
    out = reflow_for_email(body)
    assert out == "Highlights:\n• Item one\n• Item two"


def test_idempotent_on_already_reflowed_prose() -> None:
    body = "Already a flowing paragraph with no internal newlines."
    assert reflow_for_email(body) == body
    # And running it twice yields the same result.
    assert reflow_for_email(reflow_for_email(body)) == body


def test_idempotent_on_paragraph_separated_prose() -> None:
    body = "Paragraph one is already on a single line.\n\nParagraph two too."
    assert reflow_for_email(body) == body


def test_collapses_excess_blank_lines() -> None:
    # Multiple blank lines between paragraphs → exactly one blank line.
    body = "Hi.\n\n\n\nHow are you?\n\n\nFine."
    out = reflow_for_email(body)
    assert out == "Hi.\n\nHow are you?\n\nFine."


def test_carriage_returns_are_normalized() -> None:
    # Windows-style CRLF should be treated the same as plain \n.
    body = "Line one\r\nLine two\r\n\r\nNext paragraph."
    out = reflow_for_email(body)
    assert out == "Line one Line two\n\nNext paragraph."

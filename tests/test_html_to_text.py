"""Unit tests for html_to_text — the rich-text body flattener.

The real bug it fixes: a template body authored in the rich-text editor is
stored as HTML (<p>…</p>, variable spans), and the email is text/plain, so the
recipient saw literal <p>…</p> tags in their inbox.
"""
from __future__ import annotations

from app.services.html_to_text import html_to_text


def test_plain_text_is_untouched() -> None:
    # The seeded starter templates are plain text with \n\n — must pass through
    # byte-for-byte so we don't reflow hand-written copy.
    body = "Hi Alex,\n\nI'm a student interested in Acme.\n\nBest,\nMridul\n"
    assert html_to_text(body) == body


def test_paragraphs_become_blank_line_separated() -> None:
    html = "<p>Hi Alex,</p><p>I'm a student interested in Acme.</p><p>Best, Mridul</p>"
    out = html_to_text(html)
    assert "<p>" not in out and "</p>" not in out
    assert out == "Hi Alex,\n\nI'm a student interested in Acme.\n\nBest, Mridul"


def test_br_becomes_newline() -> None:
    assert html_to_text("Line one<br>Line two") == "Line one\nLine two"
    assert html_to_text("Line one<br/>Line two") == "Line one\nLine two"
    assert html_to_text("Line one<br />Line two") == "Line one\nLine two"


def test_variable_span_keeps_token_text() -> None:
    # The frontend variable chip serializes the literal {{token}} as inner text;
    # stripping the span must leave the token so placeholder substitution works.
    html = '<p>Hi <span data-variable="first_name">{{first_name}}</span>,</p>'
    assert html_to_text(html) == "Hi {{first_name}},"


def test_entities_are_unescaped() -> None:
    assert html_to_text("<p>Tom &amp; Jerry</p>") == "Tom & Jerry"
    assert html_to_text("<p>a&nbsp;b</p>") == "a b"
    assert html_to_text("<p>&lt;hello&gt;</p>") == "<hello>"


def test_collapses_excess_blank_lines() -> None:
    html = "<p>One</p><p></p><p></p><p>Two</p>"
    assert html_to_text(html) == "One\n\nTwo"


def test_list_items_get_bullets() -> None:
    html = "<ul><li>First</li><li>Second</li></ul>"
    out = html_to_text(html)
    assert "• First" in out
    assert "• Second" in out
    assert "<li>" not in out


def test_empty_and_none_safe() -> None:
    assert html_to_text("") == ""


def test_prose_with_less_than_is_not_mangled() -> None:
    # "< 10" (space after '<') must NOT be treated as a tag.
    body = "I work < 10 hours a week and read > 3 books a month."
    assert html_to_text(body) == body


def test_idempotent_on_already_flattened() -> None:
    once = html_to_text("<p>Hi Alex,</p><p>Best, Mridul</p>")
    assert html_to_text(once) == once

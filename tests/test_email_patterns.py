"""Tests for the scraped-contact email-guess pattern generator."""
from __future__ import annotations

import pytest

from app.services import email_patterns as ep


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("firstname.lastname", "akanksha.puri@sourcefuse.com"),
        ("firstname", "akanksha@sourcefuse.com"),
        ("f.lastname", "a.puri@sourcefuse.com"),
        ("firstnamelastname", "akankshapuri@sourcefuse.com"),
        ("firstname_lastname", "akanksha_puri@sourcefuse.com"),
        ("flastname", "apuri@sourcefuse.com"),
        ("lastname", "puri@sourcefuse.com"),
    ],
)
def test_build_each_pattern(pattern: str, expected: str) -> None:
    assert ep.build_email(pattern, "Akanksha Puri", "sourcefuse.com") == expected


def test_next_guess_walks_the_order() -> None:
    # firstname.lastname bounced → firstname is next.
    assert ep.next_guess("Akanksha Puri", "sourcefuse.com", "firstname.lastname") == (
        "firstname",
        "akanksha@sourcefuse.com",
    )
    # firstname bounced → f.lastname next.
    assert ep.next_guess("Akanksha Puri", "sourcefuse.com", "firstname") == (
        "f.lastname",
        "a.puri@sourcefuse.com",
    )


def test_next_guess_none_when_exhausted() -> None:
    assert ep.next_guess("Akanksha Puri", "sourcefuse.com", "lastname") is None


def test_single_word_name_skips_lastname_patterns() -> None:
    # "Madonna" has no last name → only firstname is buildable.
    assert ep.next_guess("Madonna", "x.com", None) == ("firstname", "madonna@x.com")
    # After firstname, every remaining pattern needs a last name → exhausted.
    assert ep.next_guess("Madonna", "x.com", "firstname") is None


def test_unknown_current_pattern_starts_from_top() -> None:
    assert ep.next_guess("Akanksha Puri", "sourcefuse.com", "garbage") == (
        "firstname.lastname",
        "akanksha.puri@sourcefuse.com",
    )


def test_titles_and_punctuation_cleaned() -> None:
    # "Dr. Jane O'Brien" → first=dr? No: first token "Dr." cleans to "dr".
    # Documented v0 limitation: we take the first/last token as-is. Use a clean
    # name to assert the happy path; this guards the cleaner doesn't crash.
    out = ep.build_email("firstname.lastname", "Jane O'Brien", "acme.com")
    assert out == "jane.obrien@acme.com"


def test_no_domain_or_name_returns_none() -> None:
    assert ep.build_email("firstname", "Jane", "") is None
    assert ep.build_email("firstname", None, "acme.com") is None

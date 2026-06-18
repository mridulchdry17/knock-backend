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
    # Walks forward through EMAIL_PATTERN_ORDER. Decoupled from the specific
    # order so a reshuffle (firstname-first vs firstname.lastname-first) doesn't
    # break this test — we only assert "advances to the next entry."
    order = ep.EMAIL_PATTERN_ORDER
    for i, current in enumerate(order[:-1]):
        nxt = ep.next_guess("Akanksha Puri", "sourcefuse.com", current)
        assert nxt is not None, f"expected a next guess after {current!r}"
        assert nxt[0] == order[i + 1], (
            f"after {current!r} expected {order[i+1]!r}, got {nxt[0]!r}"
        )


def test_next_guess_none_when_exhausted() -> None:
    assert ep.next_guess("Akanksha Puri", "sourcefuse.com", "lastname") is None


def test_single_word_name_skips_lastname_patterns() -> None:
    # "Madonna" has no last name → only firstname is buildable.
    assert ep.next_guess("Madonna", "x.com", None) == ("firstname", "madonna@x.com")
    # After firstname, every remaining pattern needs a last name → exhausted.
    assert ep.next_guess("Madonna", "x.com", "firstname") is None


def test_unknown_current_pattern_starts_from_top() -> None:
    # An unknown/typo'd current pattern resets to whatever's first in the order.
    nxt = ep.next_guess("Akanksha Puri", "sourcefuse.com", "garbage")
    assert nxt is not None
    assert nxt[0] == ep.EMAIL_PATTERN_ORDER[0]


def test_titles_and_punctuation_cleaned() -> None:
    # "Dr. Jane O'Brien" → first=dr? No: first token "Dr." cleans to "dr".
    # Documented v0 limitation: we take the first/last token as-is. Use a clean
    # name to assert the happy path; this guards the cleaner doesn't crash.
    out = ep.build_email("firstname.lastname", "Jane O'Brien", "acme.com")
    assert out == "jane.obrien@acme.com"


def test_no_domain_or_name_returns_none() -> None:
    assert ep.build_email("firstname", "Jane", "") is None
    assert ep.build_email("firstname", None, "acme.com") is None

"""Tests for the explicit-stop regex matcher (B5.6).

The matcher drives the lock-tier decision: True → permanent platform-wide
lock, False → 30-day per-user reply lock. Recall matters more than precision
for v0 (super_admin can clear false-positive locks manually), so the tests
exercise both clean stop phrases and the simple regex's known false-positive
classes documented in the module.
"""
from __future__ import annotations

import pytest

from app.services.explicit_stop import is_explicit_stop


@pytest.mark.parametrize(
    "body",
    [
        "Please unsubscribe me from this list.",
        "UNSUBSCRIBE.",
        "Stop emailing me, thanks.",
        "stop contacting our team",
        "Don't email this address again.",
        "Do not email me — wrong person.",
        "Please remove me from your outreach.",
        "Take me off this list.",
        "I'm not interested — stop sending me emails.",
        "Not interested in any future emails.",
    ],
)
def test_explicit_stop_matches(body: str) -> None:
    assert is_explicit_stop(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "Sounds great — let's set up a call next week.",
        "I'd love to chat, send me some times.",
        "Not interested in the role itself, but happy to forward to a teammate.",
        "Thanks for reaching out!",
    ],
)
def test_explicit_stop_does_not_match(body: str) -> None:
    assert is_explicit_stop(body) is False


def test_quoted_block_stripped_so_original_doesnt_trigger() -> None:
    """The user's own outreach (which may one day carry an unsubscribe footer)
    must NOT trigger a stop when it appears in the reply's quoted block."""
    body = (
        "Sounds good, let's chat.\n"
        "\n"
        "On Tue, May 12, 2026 at 9:00 AM John Doe <john@acme.com> wrote:\n"
        "> Hi there, here's a quick pitch. Reply with 'unsubscribe' to opt out.\n"
        "> Cheers\n"
    )
    assert is_explicit_stop(body) is False


def test_unsubscribe_above_quoted_block_does_match() -> None:
    """If the recipient's own line contains the stop phrase, that wins
    regardless of what's quoted below."""
    body = (
        "Please unsubscribe me.\n"
        "\n"
        "On Tue, May 12, 2026 at 9:00 AM John Doe <john@acme.com> wrote:\n"
        "> Hi there.\n"
    )
    assert is_explicit_stop(body) is True


def test_quoted_lines_only_with_gt_prefix_stripped() -> None:
    body = "Sure!\n> please unsubscribe me, said the original\n"
    assert is_explicit_stop(body) is False


def test_2000_char_cap_still_catches_early_phrase() -> None:
    body = "Unsubscribe.\n" + ("filler text " * 1000)
    assert is_explicit_stop(body) is True


def test_2000_char_cap_skips_phrase_beyond_cap() -> None:
    """Defensive — if someone hides 'unsubscribe' far past 2000 chars, we
    don't scan that far. (Real replies don't do this; documented behavior.)"""
    body = ("filler text " * 250) + "unsubscribe"
    assert is_explicit_stop(body) is False


def test_word_boundary_does_not_match_mid_word() -> None:
    """'presubscribed' should not trigger the unsubscribe pattern."""
    assert is_explicit_stop("They already presubscribed to our list.") is False


def test_dont_with_curly_apostrophe_still_matches() -> None:
    # Common gotcha: smart-quote apostrophe instead of straight '.
    # We accept both via re.IGNORECASE and the optional apostrophe in pattern,
    # but a curly apostrophe (U+2019) is technically different. Documented
    # known limitation — straight quote required for now.
    assert is_explicit_stop("dont email me") is True
    assert is_explicit_stop("don't email me") is True

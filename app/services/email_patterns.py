"""Email-guess pattern generation for scraped contacts.

When a SCRAPED contact's guessed address bounces, we advance to the next guess
pattern and retry (instead of giving up). The pattern order is priority-ranked
by how common each format is for company email. Only fires for contacts that
carry a `scraped_pattern` (i.e. the address was a guess) — CSV/manually-curated
contacts are assumed real and are simply invalidated on bounce.

Tracking: `Contact.scraped_pattern` stores the CURRENT pattern name. "Tried" =
every pattern up to and including the current one in EMAIL_PATTERN_ORDER, so
`next_guess` just walks forward from the current index — no repeats, bounded by
the list (exhaustion → give up / mark invalid).
"""
from __future__ import annotations

import re

# Priority order. firstname.lastname is the v0 default first guess; firstname@
# is the common second; the rest cover the usual corporate variants.
EMAIL_PATTERN_ORDER: list[str] = [
    "firstname.lastname",   # akanksha.puri@
    "firstname",            # akanksha@
    "f.lastname",           # a.puri@
    "firstnamelastname",    # akankshapuri@
    "firstname_lastname",   # akanksha_puri@
    "flastname",            # apuri@
    "lastname",             # puri@
]


def _clean(token: str) -> str:
    """Lowercase + strip everything but a-z0-9 (drops accents-as-ascii, dots,
    spaces, titles like 'Dr.'). Empty if nothing usable remains."""
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _name_parts(name: str | None) -> tuple[str, str]:
    """Return (first, last) cleaned tokens. last is '' for single-word names."""
    if not name:
        return "", ""
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    if not tokens:
        return "", ""
    first = _clean(tokens[0])
    last = _clean(tokens[-1]) if len(tokens) > 1 else ""
    return first, last


def build_email(pattern: str, name: str | None, domain: str) -> str | None:
    """Build an address for a pattern, or None if the name lacks the parts the
    pattern needs (e.g. a lastname pattern for a single-word name)."""
    first, last = _name_parts(name)
    domain = (domain or "").strip().lower()
    if not first or not domain:
        return None

    local: str | None
    if pattern == "firstname.lastname":
        local = f"{first}.{last}" if last else None
    elif pattern == "firstname":
        local = first
    elif pattern == "f.lastname":
        local = f"{first[0]}.{last}" if last else None
    elif pattern == "firstnamelastname":
        local = f"{first}{last}" if last else None
    elif pattern == "firstname_lastname":
        local = f"{first}_{last}" if last else None
    elif pattern == "flastname":
        local = f"{first[0]}{last}" if last else None
    elif pattern == "lastname":
        local = last or None
    else:
        local = None

    return f"{local}@{domain}" if local else None


def next_guess(
    name: str | None, domain: str, current_pattern: str | None
) -> tuple[str, str] | None:
    """Given the pattern that just bounced, return the next (pattern, email)
    that can actually be built for this name/domain, or None when exhausted.

    Walks EMAIL_PATTERN_ORDER from just after `current_pattern`, skipping any
    pattern the name can't satisfy. An unknown/absent current_pattern starts
    from the top.
    """
    try:
        start = EMAIL_PATTERN_ORDER.index(current_pattern) + 1 if current_pattern else 0
    except ValueError:
        start = 0

    for pattern in EMAIL_PATTERN_ORDER[start:]:
        email = build_email(pattern, name, domain)
        if email is not None:
            return pattern, email
    return None

"""Classify every contact's email by validity.

Why this exists: the scraper-side email-guesser occasionally produces
malformed addresses (e.g. localpart 'k.' for a name like 'K. Chandrasekhar'),
and a single bad row was crashing the frontend's strict Zod parse on
/today (see fix/today-zod-relaxed-validation in the frontend repo).

Run this any time to check the contact pool's email hygiene:

    python -m scripts.audit_contact_emails

Categories:
  - well_formed   : exactly one '@', non-empty local, host has a TLD
  - dot_local     : localpart starts/ends with '.' or has '..' inside
  - empty_local   : '@' with nothing before it
  - no_tld        : host has no '.'
  - multi_at      : more than one '@'
  - whitespace    : whitespace anywhere
  - empty_or_null : NULL or whitespace-only
  - other_weird   : anything else
"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

# Bypass the routing session's read-replica preference so we see the freshest
# remote state every time this runs (the local replica may not be synced on
# a cold script start).
from app.db.base import _WRITE_ENGINE


def classify(email: str | None) -> str:
    if email is None or not email.strip():
        return "empty_or_null"
    if re.search(r"\s", email):
        return "whitespace"
    if email.count("@") > 1:
        return "multi_at"
    if email.count("@") != 1:
        return "other_weird"
    local, host = email.split("@", 1)
    if not local:
        return "empty_local"
    if local.endswith(".") or local.startswith(".") or ".." in local:
        return "dot_local"
    if "." not in host:
        return "no_tld"
    return "well_formed"


def main() -> None:
    with Session(_WRITE_ENGINE) as db:
        rows = db.execute(
            text("SELECT id, name, email, company_id FROM contacts ORDER BY id")
        ).all()

    by_cat: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by_cat[classify(r.email)].append(r)

    total = len(rows)
    print(f"\n=== Total contacts: {total} ===\n")
    order = [
        "well_formed", "dot_local", "empty_local", "no_tld",
        "multi_at", "whitespace", "empty_or_null", "other_weird",
    ]
    for cat in order:
        lst = by_cat.get(cat, [])
        pct = (len(lst) * 100 / total) if total else 0
        print(f"  {cat:15s} {len(lst):5d}  ({pct:.1f}%)")

    print("\n=== Malformed samples (up to 30) ===")
    bad_cats = ["dot_local", "empty_local", "no_tld", "multi_at", "whitespace", "other_weird"]
    sample = [(cat, r) for cat in bad_cats for r in by_cat.get(cat, [])]
    for cat, r in sample[:30]:
        print(f"  [{cat:12s}] id={r.id:5d}  email={r.email!r:40s}  name={r.name!r}")
    if len(sample) > 30:
        print(f"  ... and {len(sample) - 30} more")

    bad_ids = [r.id for _cat, r in sample]
    if bad_ids:
        print(f"\n=== Bad contact IDs ({len(bad_ids)}) ===")
        print(", ".join(str(i) for i in bad_ids))


if __name__ == "__main__":
    main()

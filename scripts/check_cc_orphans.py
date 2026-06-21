"""Audit today_batch_items.cc_contact_ids for orphaned references.

cc_contact_ids is a JSON list of ints stored as a String — there's no
foreign-key enforcement on the list elements (only on to_contact_id).
After any contact-row delete, run this to confirm no stale IDs are
lingering in CC lists.

Usage:
    python -m scripts.check_cc_orphans
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.base import _WRITE_ENGINE


def main() -> None:
    with Session(_WRITE_ENGINE) as db:
        live_ids = {
            row.id
            for row in db.execute(text("SELECT id FROM contacts")).all()
        }
        rows = db.execute(
            text(
                "SELECT id, batch_date, user_id, cc_contact_ids "
                "FROM today_batch_items WHERE cc_contact_ids != '[]'"
            )
        ).all()
        print(f"Scanning {len(rows)} today_batch_items rows with non-empty CC lists...")

        orphan_hits: list[tuple[int, str, int, list[int], list[int]]] = []
        for r in rows:
            try:
                ids = json.loads(r.cc_contact_ids)
            except Exception:
                continue
            stale = [i for i in ids if i not in live_ids]
            if stale:
                orphan_hits.append((r.id, str(r.batch_date), r.user_id, stale, ids))

        if not orphan_hits:
            print("\nClean — no orphan refs in cc_contact_ids.")
            return
        print(f"\nFound {len(orphan_hits)} batch items with orphan CC refs:")
        for item_id, batch_date, user_id, stale, full in orphan_hits:
            print(
                f"  batch_item_id={item_id} user_id={user_id} "
                f"date={batch_date} stale={stale} full={full}"
            )


if __name__ == "__main__":
    main()

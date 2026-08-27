"""Fix all multi-person blob names in Turso directly (no local replica reads).

Strategy
--------
1. Connect to Turso remote for ALL reads AND writes — bypasses replica.db entirely.
2. Fetch every contact whose name has 3+ spaces (= 4+ words).
3. Auto-split using the 2-words-per-person rule:
     - Strip invisible unicode chars (ZWJ, ZWNJ, etc.)
     - Strip parentheticals:  "(CEO)", "(CEO since 2017)", "(CEO since X)" → removed
     - Strip known title prefixes from group boundaries so "Dr." merges forward
     - Every 2 cleaned words = 1 person
4. Single-person result → just rename in-place (clean the parenthetical).
5. Multi-person result → update row to person-1, insert rows for person 2-N.
6. Hard-delete non-person rows (fund names, partnership descriptions, etc.).
7. At the end: pull a fresh sync from Turso → local replica.

Run:
    .venv/bin/python -m scripts.fix_blobs_remote
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import libsql_experimental as libsql


# ── connection ──────────────────────────────────────────────────────────────

def _turso_conn():
    url = os.environ.get("DATABASE_URL", "")
    parts = urlsplit(url)
    sync_url = f"https://{parts.netloc}"
    qs = parse_qs(parts.query)
    token = (qs.get("authToken") or qs.get("auth_token") or [""])[0]
    # Remote-only connection (no local file path) — all reads+writes go to Turso
    return libsql.connect(f"libsql://{parts.netloc}", auth_token=token)


def _local_sync() -> None:
    """Pull Turso → local replica so the running app sees the changes."""
    url = os.environ.get("DATABASE_URL", "")
    replica = os.environ.get("LIBSQL_REPLICA_PATH", "")
    if not replica:
        return
    parts = urlsplit(url)
    sync_url = f"https://{parts.netloc}"
    qs = parse_qs(parts.query)
    token = (qs.get("authToken") or qs.get("auth_token") or [""])[0]
    # Wipe corrupted replica files first so the sync starts clean
    for ext in ("", "-wal", "-shm", "-info"):
        p = Path(replica + ext)
        if p.exists():
            p.unlink()
    conn = libsql.connect(replica, sync_url=sync_url, auth_token=token)
    conn.sync()
    conn.close()
    print("  local replica synced ✓")


# ── name helpers ─────────────────────────────────────────────────────────────

# Invisible unicode chars that creep into scraped names
_INVISIBLE_RE = re.compile(
    r"[​‌‍­﻿⁠᠎͏  ]+"
)
# Parentheticals anywhere in the name
_PARENS_RE = re.compile(r"\([^)]*\)")

TITLE_PREFIXES = {"dr", "mr", "ms", "mrs", "prof"}

# Rows that are clearly NOT founder contacts — delete them
NON_PERSONS = {
    "NPTK Emerging Asia Fund 1",
    "Rajaraman Santhanam; Krishnamoorthy Subramanian; Saravanan Kolathupalaya Ponnuswamy; Thiyagarajan Thiyagu",
    "Kate Honqian Ma; Jacky Im; Elizabeth Chan",
}


def _clean(name: str) -> str:
    """Strip invisible chars and parentheticals, then normalise whitespace."""
    s = _INVISIBLE_RE.sub("", name)
    s = _PARENS_RE.sub("", s)
    return " ".join(s.split())


def _split_persons(name: str) -> list[str]:
    """
    Split a cleaned blob name into individual person names.
    Rule: every 2 consecutive words = 1 person, with one exception:
    if a word is a known title prefix (Dr., Mr., etc.) it merges forward
    so that person gets 3 words instead of 2.
    """
    words = name.split()
    persons: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        is_title = w.lower().rstrip(".") in TITLE_PREFIXES
        if is_title and i + 2 < len(words):
            # Title + next 2 words = 1 person
            persons.append(" ".join(words[i : i + 3]))
            i += 3
        else:
            # Normal: 2 words = 1 person
            persons.append(" ".join(words[i : i + 2]))
            i += 2
    return [p.strip() for p in persons if p.strip()]


def _first_name(full_name: str) -> str:
    """Return the lowercase first usable word (skip title prefixes, skip 1-char initials)."""
    for part in full_name.strip().split():
        c = part.lower().rstrip(".")
        if c not in TITLE_PREFIXES and len(c) > 1:
            return c
    return full_name.strip().split()[0].lower().rstrip(".")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:  # noqa: C901 — long but linear
    # Load .env manually so the script runs standalone
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    print("Connecting to Turso (remote, no local replica)…")
    conn = _turso_conn()

    # ── snapshot before ──
    before_row = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()
    before = before_row[0]
    print(f"  contacts before: {before}")

    # ── fetch all blobs (4+ words in name) ──
    blobs = conn.execute(
        "SELECT id, company_id, name, source, role, scraped_pattern "
        "FROM contacts "
        "WHERE LENGTH(name) - LENGTH(REPLACE(name,' ','')) >= 3"
    ).fetchall()
    print(f"  blobs fetched:   {len(blobs)}  (names with 4+ words)")

    deleted = inserted = updated = skipped = 0

    for row in blobs:
        contact_id = row[0]
        company_id = row[1]
        raw_name   = row[2]
        source     = row[3] or "scraping"
        role       = row[4] or "Founder"
        pattern    = row[5] or "firstname"

        # ── hard-delete non-person rows ──
        if raw_name.strip() in NON_PERSONS:
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
            print(f"  DELETE non-person: {raw_name!r}")
            deleted += 1
            continue

        # ── semicolon-split names ──
        if ";" in raw_name:
            parts = [p.strip() for p in raw_name.split(";") if p.strip()]
        else:
            cleaned = _clean(raw_name)
            if not cleaned:
                conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
                deleted += 1
                continue
            # Re-check: after cleaning, does it still have 3+ spaces?
            if cleaned.count(" ") < 3:
                # Clean name in-place if parenthetical was the only issue
                if cleaned != raw_name.strip():
                    conn.execute(
                        "UPDATE contacts SET name = ? WHERE id = ?",
                        (cleaned, contact_id),
                    )
                    updated += 1
                else:
                    skipped += 1
                continue
            parts = _split_persons(cleaned)

        if len(parts) <= 1:
            # Only 1 person after split — just clean the name
            p = (parts[0] if parts else cleaned).strip()
            if p != raw_name.strip():
                conn.execute(
                    "UPDATE contacts SET name = ? WHERE id = ?",
                    (p, contact_id),
                )
                updated += 1
            else:
                skipped += 1
            continue

        # ── multi-person split ──
        domain_row = conn.execute(
            "SELECT domain FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if not domain_row:
            print(f"  NO DOMAIN company_id={company_id} name={raw_name!r}")
            skipped += 1
            continue
        domain = domain_row[0]

        existing_emails = {
            r[0]
            for r in conn.execute(
                "SELECT email FROM contacts WHERE company_id = ?", (company_id,)
            ).fetchall()
        }

        # Person 1 — update in-place
        p1 = parts[0]
        p1_email = f"{_first_name(p1)}@{domain}"
        conn.execute(
            "UPDATE contacts SET name = ?, email = ? WHERE id = ?",
            (p1, p1_email, contact_id),
        )
        existing_emails.add(p1_email)
        updated += 1

        # Persons 2-N — insert
        for person in parts[1:]:
            email = f"{_first_name(person)}@{domain}"
            if email in existing_emails:
                continue
            existing_emails.add(email)
            conn.execute(
                "INSERT INTO contacts "
                "(company_id, name, email, role, email_verified, email_confidence,"
                " scraped_pattern, source, created_at)"
                " VALUES (?, ?, ?, ?, 0, 60, ?, ?, CURRENT_TIMESTAMP)",
                (company_id, person, email, role, pattern, source),
            )
            inserted += 1

    conn.commit()
    conn.close()

    # ── verify via fresh connection ──
    conn2 = _turso_conn()
    after_row = conn2.execute("SELECT COUNT(*) FROM contacts").fetchone()
    after = after_row[0]
    conn2.close()

    net = inserted - deleted
    print("\n=== blob fix complete ===")
    print(f"  contacts before:  {before}")
    print(f"  contacts after:   {after}")
    print(f"  blobs processed:  {len(blobs)}")
    print(f"  deleted:          {deleted}  (non-person rows)")
    print(f"  updated:          {updated}  (renamed in-place)")
    print(f"  inserted:         {inserted}  (new rows for persons 2-N)")
    print(f"  skipped:          {skipped}  (already clean or unfixable)")
    print(f"  net change:       {'+' if net >= 0 else ''}{net}")

    print("\nSyncing local replica from Turso…")
    _local_sync()

    return 0


if __name__ == "__main__":
    sys.exit(main())

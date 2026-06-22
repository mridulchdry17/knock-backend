"""Scrape Y Combinator portfolio companies (2022-present) into Knock DB.

Pipeline
--------
1. Algolia API  → fetch all company slugs for target batches (no rate limit)
2. YC company pages → parse Inertia JSON for founder names + LinkedIn URLs
3. Turso (direct) → upsert companies + contacts

Usage:
    .venv/bin/python -m scripts.scrape_yc           # full run
    .venv/bin/python -m scripts.scrape_yc --test    # 5 companies, no DB writes
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

SOURCE_TAG = "yc-scraping"
ALGOLIA_APP_ID = "45BWZJ1SGC"
ALGOLIA_API_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJiMWM4ZTU0ZDlhYTZjOTJiMjlh"
    "MWFuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0Nv"
    "bXBhbnlfQnlfTGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIy"
    "JTVE"
)
ALGOLIA_INDEX = "YCCompany_production"
ALGOLIA_URL = f"https://45bwzj1sgc-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

TARGET_BATCHES = [
    "Winter 2022", "Summer 2022",
    "Winter 2023", "Summer 2023",
    "Winter 2024", "Summer 2024",
    "Winter 2025", "Summer 2025",
]

TITLE_PREFIXES = {"dr", "mr", "ms", "mrs", "prof"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _first_name(full_name: str) -> str:
    for part in full_name.strip().split():
        c = part.lower().rstrip(".")
        if c not in TITLE_PREFIXES and len(c) > 1:
            return c
    return full_name.strip().split()[0].lower()


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _batch_short(batch_full: str) -> str:
    """'Winter 2022' → 'W22'"""
    parts = batch_full.split()
    if len(parts) == 2:
        season = "W" if parts[0].lower().startswith("w") else "S"
        return f"{season}{parts[1][2:]}"
    return batch_full


# ── step 1: Algolia → company slugs ─────────────────────────────────────────

def fetch_all_slugs() -> list[dict]:
    """Return list of {slug, name, website, batch} for all target batches."""
    headers = {
        "X-Algolia-Application-Id": ALGOLIA_APP_ID,
        "X-Algolia-API-Key": ALGOLIA_API_KEY,
        "Content-Type": "application/json",
    }
    batch_filter = [f"batch:{b}" for b in TARGET_BATCHES]  # OR within inner list
    page, hits_per_page = 0, 200
    results = []

    while True:
        payload = {
            "query": "",
            "facetFilters": [batch_filter],
            "hitsPerPage": hits_per_page,
            "page": page,
            "attributesToRetrieve": ["name", "slug", "website", "batch", "status"],
        }
        resp = requests.post(ALGOLIA_URL, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", [])
        results.extend(hits)
        nb_pages = data.get("nbPages", 1)
        print(f"  Algolia page {page+1}/{nb_pages}: {len(hits)} companies")
        if page + 1 >= nb_pages:
            break
        page += 1

    print(f"  Total from Algolia: {len(results)} companies")
    return results


# ── step 2: YC company page → founder data ──────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
})


def fetch_founders(slug: str) -> list[dict]:
    """Fetch a YC company page and return list of founder dicts."""
    url = f"https://www.ycombinator.com/companies/{slug}"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    FETCH ERROR {slug}: {e}")
        return []

    # Inertia embeds props as data-page JSON attribute
    m = re.search(r'data-page="([^"]+)"', resp.text)
    if not m:
        return []

    raw = html.unescape(m.group(1))
    try:
        page_data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    company = page_data.get("props", {}).get("company", {})
    return company.get("founders", [])


# ── step 3: Turso connection ─────────────────────────────────────────────────

def _turso_conn():
    import libsql_experimental as libsql
    url = os.environ.get("DATABASE_URL", "")
    parts = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parts.query)
    token = (qs.get("authToken") or qs.get("auth_token") or [""])[0]
    return libsql.connect(f"libsql://{parts.netloc}", auth_token=token)


def _local_sync() -> None:
    import libsql_experimental as libsql
    url = os.environ.get("DATABASE_URL", "")
    replica = os.environ.get("LIBSQL_REPLICA_PATH", "")
    if not replica:
        return
    parts = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parts.query)
    token = (qs.get("authToken") or qs.get("auth_token") or [""])[0]
    sync_url = f"https://{parts.netloc}"
    for ext in ("", "-wal", "-shm", "-info"):
        p = Path(replica + ext)
        if p.exists():
            p.unlink()
    conn = libsql.connect(replica, sync_url=sync_url, auth_token=token)
    conn.sync()
    conn.close()
    print("  local replica synced ✓")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Fetch 5 companies, print, no DB write")
    args = parser.parse_args()

    # Load .env
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    # ── Algolia: get all company slugs ──
    print(f"\nFetching company list from Algolia ({', '.join(TARGET_BATCHES)})…")
    all_companies = fetch_all_slugs()

    if args.test:
        all_companies = all_companies[:5]
        print(f"\n[TEST MODE] Processing {len(all_companies)} companies — no DB writes\n")

    # ── Scrape company pages ──
    print(f"\nFetching founder data from {len(all_companies)} company pages…")
    scraped: list[dict] = []

    for i, co in enumerate(all_companies):
        slug    = co.get("slug", "")
        name    = co.get("name", "")
        website = co.get("website", "") or ""
        batch   = co.get("batch", "")
        status  = co.get("status", "")

        domain = _parse_domain(website)
        if not domain:
            if args.test:
                print(f"  [{i+1}] SKIP {name!r} — no domain")
            continue

        founders = fetch_founders(slug)
        if not founders and not args.test:
            time.sleep(0.3)
            continue

        record = {
            "name": name,
            "slug": slug,
            "website": website,
            "domain": domain,
            "batch": batch,
            "batch_short": _batch_short(batch),
            "status": status,
            "founders": [
                {
                    "full_name": f.get("full_name", "").strip(),
                    "title": f.get("title", ""),
                    "linkedin_url": f.get("linkedin_url") or None,
                }
                for f in founders
                if f.get("full_name", "").strip()
            ],
        }
        scraped.append(record)

        if args.test:
            print(f"\n  [{i+1}] {name}  ({batch})  {domain}")
            for fd in record["founders"]:
                email = f"{_first_name(fd['full_name'])}@{domain}"
                print(f"       • {fd['full_name']} | {email} | {fd.get('linkedin_url','')}")
        elif (i + 1) % 50 == 0:
            print(f"  scraped {i+1}/{len(all_companies)}…")

        time.sleep(0.6)  # polite rate limit

    if args.test:
        print(f"\n[TEST] Done — {len(scraped)} companies with founders")
        return 0

    # ── DB writes ──
    print(f"\nWriting {len(scraped)} companies to Turso…")
    conn = _turso_conn()

    before = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    companies_created = companies_existing = contacts_created = contacts_skipped = 0

    for record in scraped:
        domain      = record["domain"]
        name        = record["name"]
        batch_short = record["batch_short"]

        # Upsert company
        existing_co = conn.execute(
            "SELECT id FROM companies WHERE domain = ?", (domain,)
        ).fetchone()

        if existing_co:
            company_id = existing_co[0]
            # Backfill batch if not set
            conn.execute(
                "UPDATE companies SET batch = ? WHERE id = ? AND (batch IS NULL OR batch = '')",
                (batch_short, company_id),
            )
            companies_existing += 1
        else:
            conn.execute(
                "INSERT INTO companies (name, domain, source, batch, created_at)"
                " VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (name, domain, SOURCE_TAG, batch_short),
            )
            row = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
            company_id = row[0]
            companies_created += 1

        # Existing emails for this company
        existing_emails = {
            r[0]
            for r in conn.execute(
                "SELECT email FROM contacts WHERE company_id = ?", (company_id,)
            ).fetchall()
        }

        for fd in record["founders"]:
            full_name    = fd["full_name"]
            linkedin_url = fd.get("linkedin_url")
            email        = f"{_first_name(full_name)}@{domain}"

            if email in existing_emails:
                contacts_skipped += 1
                continue
            existing_emails.add(email)

            conn.execute(
                "INSERT INTO contacts "
                "(company_id, name, email, role, email_verified, email_confidence,"
                " scraped_pattern, linkedin_url, source, created_at)"
                " VALUES (?, ?, ?, 'Founder', 0, 65, 'firstname', ?, ?, CURRENT_TIMESTAMP)",
                (company_id, full_name, email, linkedin_url, f"{SOURCE_TAG}-{batch_short}"),
            )
            contacts_created += 1

    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conn.close()

    print("\n=== YC scrape complete ===")
    print(f"  contacts before:      {before}")
    print(f"  contacts after:       {after}")
    print(f"  companies created:    {companies_created}")
    print(f"  companies existing:   {companies_existing}")
    print(f"  contacts created:     {contacts_created}")
    print(f"  contacts skipped:     {contacts_skipped}  (email already in DB)")
    print(f"  net new contacts:     {after - before}")

    print("\nSyncing local replica…")
    _local_sync()
    return 0


if __name__ == "__main__":
    sys.exit(main())

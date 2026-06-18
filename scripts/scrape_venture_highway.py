"""Scrape Venture Highway's portfolio via Wayback Machine snapshot.

The live domain venturehighway.vc is currently broken (DNS reassigned to an
unrelated server). A 2023-02-17 Wayback Machine snapshot confirms the site was
plain static HTML with portfolio companies listed in a div#investments section.
Company data: name (from img alt text) and website URL (from anchor hrefs).
No per-company founder names were present in the HTML.

For each portfolio company:
  - extracts name + website URL from the snapshot HTML
  - derives domain from website URL
  - skips contact creation (no founder names in HTML)
  - upserts Company into the pool

Usage:
    .venv/bin/python -m scripts.scrape_venture_highway

Idempotent — re-runs skip existing companies.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request

from sqlalchemy import select, text

from app.db.base import SessionLocal, trigger_sync
from app.models import Company, Contact

SOURCE_TAG = "venture-highway-scraping"

# Wayback Machine snapshot — live site is down
SNAPSHOT_URL = "https://web.archive.org/web/20230217210528/https://www.venturehighway.vc/"

EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+\.[a-z]{2,}', re.IGNORECASE)
LINKEDIN_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?', re.IGNORECASE)


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; outreach-scraper/1.0; "
                "+https://github.com/outreach)"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    # Try UTF-8, fall back to latin-1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        # strip Wayback Machine prefix if accidentally captured
        # e.g. web.archive.org/web/20230217.../http://example.com
        if "web.archive.org" in host:
            return None
        return host if "." in host else None
    except Exception:
        return None


def _extract_companies(html: str) -> list[dict]:
    """
    The VH portfolio page lists companies inside a div#investments (or similar)
    section. Each company is an <a href="..."> wrapping an <img alt="CompanyName">.

    Strategy:
      1. Try to narrow to the investments section first.
      2. Find all <a href> + <img alt> pairs within that section.
      3. Deduplicate by domain.
    """
    companies: list[dict] = []
    seen_domains: set[str] = set()

    # Narrow to the investments/portfolio section of the page
    # The snapshot wraps portfolio items in a section with id="investments"
    section_match = re.search(
        r'id=["\']investments["\'](.+?)(?=<(?:section|footer))',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    section_html = section_match.group(0) if section_match else html

    # Match <a href="..."><img ... alt="..."> patterns (Wayback rewrites hrefs)
    # href may be absolute or Wayback-prefixed
    anchor_re = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>.*?<img[^>]+alt=["\']([^"\']+)["\']',
        re.DOTALL | re.IGNORECASE,
    )

    for m in anchor_re.finditer(section_html):
        raw_href = m.group(1).strip()
        alt_text = m.group(2).strip()

        if not alt_text or not raw_href:
            continue

        # Skip Wayback infrastructure links
        if "web.archive.org/web" in raw_href and "/http" not in raw_href:
            continue

        # Normalise Wayback-prefixed URLs: extract the real URL
        wayback_prefix = re.search(r'web\.archive\.org/web/\d+[^/]*/(.+)', raw_href)
        if wayback_prefix:
            real_url = wayback_prefix.group(1)
            if not real_url.startswith("http"):
                real_url = "http://" + real_url
        else:
            real_url = raw_href

        # Skip non-HTTP links (anchors, mailto, js, etc.)
        if not real_url.startswith("http"):
            continue

        domain = _parse_domain(real_url)
        if not domain:
            continue

        # Skip VH's own domain
        if "venturehighway" in domain:
            continue

        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        companies.append(
            {
                "name": alt_text,
                "website": real_url,
                "domain": domain,
            }
        )

    return companies


def _fallback_companies() -> list[dict]:
    """
    Hardcoded fallback list derived from the 2023-02-17 Wayback snapshot.
    Used if the live Wayback fetch fails or returns too few companies.
    """
    raw = [
        ("Airmeet", "https://www.airmeet.com"),
        ("BetterPlace", "https://www.betterplace.co.in"),
        ("BuildSupply", "https://www.buildsupply.in"),
        ("Cars24", "https://www.cars24.com"),
        ("Chalo", "https://chalo.com"),
        ("CheQ", "https://www.cheq.one"),
        ("CityFurnish", "https://www.cityfurnish.com"),
        ("CureLink", "https://www.curelink.in"),
        ("Emitrr", "https://emitrr.com"),
        ("Farmizen", "https://farmizen.com"),
        ("FamPay", "https://fampay.in"),
        ("Findeed", "https://findeed.in"),
        ("FlashPrep", "https://flashprep.com"),
        ("GripInvest", "https://www.gripinvest.in"),
        ("Hiration", "https://www.hiration.com"),
        ("Headout", "https://www.headout.com"),
        ("Ivy.homes", "https://www.ivy.homes"),
        ("Kisan Network", "https://www.kisannetwork.com"),
        ("Kula", "https://www.kula.ai"),
        ("LetsVenture", "https://letsventure.com"),
        ("LooPin", "https://www.loopin.network"),
        ("Meesho", "https://meesho.com"),
        ("Meragi", "https://meragi.com"),
        ("MPL", "https://www.mpl.live"),
        ("Moglix", "https://www.moglix.com"),
        ("MyScoot", "https://www.myscoot.in"),
        ("MyPetrolPump", "https://www.mypetrolpump.in"),
        ("OKCredit", "https://www.okcredit.in"),
        ("Original4Sure", "https://www.original4sure.com"),
        ("Perpule", "https://perpule.com"),
        ("ShareChat", "https://sharechat.com"),
        ("ShieldSquare", "https://www.shieldsquare.com"),
        ("Sigtuple", "https://sigtuple.com"),
        ("Stellapps", "https://www.stellapps.com"),
        ("Swiflearn", "https://swiflearn.com"),
        ("Tracxn", "https://tracxn.com"),
        ("UrbanYogi", "https://www.urbanyogi.in"),
        ("VaultEdge", "https://www.vaultedge.com"),
        ("Wealthy", "https://wealthy.in"),
        ("Wishfin", "https://www.wishfin.com"),
        ("Wmall", "https://www.wmall.com"),
    ]
    seen_domains: set[str] = set()
    result = []
    for name, website in raw:
        domain = _parse_domain(website)
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        result.append({"name": name, "website": website, "domain": domain})
    return result


def main() -> int:
    print("Fetching Venture Highway portfolio from Wayback Machine snapshot...")
    print(f"  URL: {SNAPSHOT_URL}")

    companies: list[dict] = []
    try:
        html = _fetch_html(SNAPSHOT_URL)
        print(f"  Fetched {len(html):,} bytes of HTML")
        companies = _extract_companies(html)
        print(f"  Parsed {len(companies)} companies from HTML")
    except Exception as exc:
        print(f"  WARNING: Live fetch failed ({exc}), using hardcoded fallback list")

    if len(companies) < 10:
        print("  Too few companies from HTML parse — using hardcoded fallback list")
        companies = _fallback_companies()
        print(f"  Fallback list: {len(companies)} companies")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
    }
    seen_emails: set[str] = set()

    # Sync the replica so our pre-fetch reads reflect the latest remote state
    trigger_sync()

    db = SessionLocal()
    try:
        # Mark session as "wrote" so all subsequent queries go to the write engine
        # (the remote Turso primary). This avoids stale-replica false negatives
        # where domains already inserted by a prior run aren't visible yet in the
        # local replica but ARE in the primary.
        db.info["_wrote"] = True

        # Pre-fetch all domains already in the DB to avoid flush conflicts
        candidate_domains = {r["domain"] for r in companies if r.get("domain")}
        if candidate_domains:
            already_in_db: set[str] = set(
                row[0]
                for row in db.execute(
                    select(Company.domain).where(Company.domain.in_(candidate_domains))
                ).all()
            )
        else:
            already_in_db = set()

        for record in companies:
            name = record.get("name", "").strip()
            website = record.get("website", "").strip()
            domain = record.get("domain", "").strip()

            if not website:
                counts["skipped_no_website"] += 1
                continue

            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            company_name = name or domain

            if domain in already_in_db:
                counts["companies_existing"] += 1
                continue

            # Insert new company
            company = Company(
                domain=domain,
                name=company_name,
                source=SOURCE_TAG,
            )
            db.add(company)
            db.flush()
            already_in_db.add(domain)
            counts["companies_created"] += 1

            # No founder names in the HTML — no contacts to create.
            # (Founder mentions in the VH HTML refer to VH fund partners,
            # not portfolio company founders.)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("\n=== Scrape complete ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

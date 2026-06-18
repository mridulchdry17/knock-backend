"""Scrape Blume Ventures' portfolio via their public static/server-rendered site.

Strategy:
  1. Enumerate all 272 company slugs from sitemap-startups-{1,2,3}.xml
  2. For each company slug, fetch https://blume.vc/{slug} to extract:
       - Company name (og:title meta tag)
       - Website URL (first non-Blume, non-CDN external href near globe icon)
       - LinkedIn company URL (linkedin.com/company/ href)
       - Founders (h2 "Founders" section — with or without LinkedIn links)
       - Founder LinkedIn URLs (linkedin.com/in/ hrefs inside Founders section)
       - Fund number (Fund I–V text)
       - Founding year ("Founded YYYY" pattern)
       - Location (startups/location: links)
       - Description (og:description meta tag)
       - Industry (sectors/ links)
  3. Parse domain from website URL
  4. For each founder:
       - Try real email from HTML regex (email_verified=True, confidence=95)
       - Fall back to firstname@domain guess (email_verified=False, confidence=60)
       - Store LinkedIn URL if found
  5. Upsert Company + Contact rows (idempotent on re-run)

Usage:
    .venv/bin/python -m scripts.scrape_blume_ventures

Rate-limited to ~3s/req to stay below Cloudflare's burst threshold.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.parse
from typing import Optional

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "blume-ventures-scraping"
BASE_URL = "https://blume.vc"
SITEMAP_URLS = [
    f"{BASE_URL}/sitemap-startups-1.xml",
    f"{BASE_URL}/sitemap-startups-2.xml",
    f"{BASE_URL}/sitemap-startups-3.xml",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Paths that are NOT company pages
EXCLUDED_PATHS = {
    "", "/", "/startups", "/exits", "/spotlight", "/funds", "/library",
    "/news", "/about", "/contact-us", "/search", "/corporate-governance",
    "/complaint-handling-and-grievance-redressal", "/sitemap.xml",
}


def _fetch(url: str, timeout: int = 30, retries: int = 5) -> str:
    """Fetch URL via curl subprocess with retry/backoff for 429s.

    Using curl instead of urllib so each request opens a fresh TCP connection
    with no persistent cookie/session state that Cloudflare might fingerprint.
    """
    last_err: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        result = subprocess.run(
            [
                "curl", "-s", "-L",
                "-A", USER_AGENT,
                "--max-time", str(timeout),
                "-w", "\n__HTTP_STATUS__:%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
        )
        body = result.stdout
        # Extract status code appended at end
        status = 0
        if "__HTTP_STATUS__:" in body:
            parts = body.rsplit("__HTTP_STATUS__:", 1)
            body = parts[0]
            try:
                status = int(parts[1].strip())
            except ValueError:
                status = 0

        if status == 200:
            return body
        elif status == 429:
            wait = 8 * (2 ** attempt)  # 8, 16, 32, 64, 128 seconds
            print(f" [429, backing off {wait}s]", end="", flush=True)
            last_err = RuntimeError(f"HTTP 429 after {attempt+1} attempts")
            time.sleep(wait)
        elif status == 0:
            last_err = RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr[:200]}")
            break
        else:
            raise RuntimeError(f"HTTP {status} for {url}")
    raise last_err


def _get_slugs_from_sitemaps() -> list[str]:
    """Fetch all company slugs from the three startup sitemaps.

    Uses pre-cached local files from /tmp if they exist (from earlier curl run)
    to avoid burning 3 extra HTTP requests at startup.
    """
    import os
    local_cache = [
        "/tmp/blume_sitemap1.xml",
        "/tmp/blume_sitemap2.xml",
        "/tmp/blume_sitemap3.xml",
    ]
    slugs: list[str] = []
    for sitemap_url, local_path in zip(SITEMAP_URLS, local_cache):
        try:
            if os.path.exists(local_path):
                with open(local_path) as f:
                    xml = f.read()
            else:
                xml = _fetch(sitemap_url)
            found = re.findall(r"<loc>(https://blume\.vc(/[^<]+))</loc>", xml)
            for full_url, path in found:
                if path not in EXCLUDED_PATHS:
                    slugs.append(path.lstrip("/"))
        except Exception as e:
            print(f"  WARNING: failed to fetch {sitemap_url}: {e}")
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def _get_slugs_from_listing(html: str) -> list[str]:
    """Fallback: extract slugs from the /startups listing page."""
    slugs: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="https://blume\.vc(/[a-z0-9][a-z0-9-]*)"', html):
        path = m.group(1)
        if path not in EXCLUDED_PATHS and "/" not in path[1:]:
            slug = path.lstrip("/")
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs


def _parse_domain(website: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        # Strip port if present
        host = host.split(":")[0]
        return host if "." in host else None
    except Exception:
        return None


def _parse_company_page(slug: str, html: str) -> dict:
    """Extract all fields from a company detail page."""

    # --- Company name from og:title ---
    name_match = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    company_name = name_match.group(1).strip() if name_match else slug.replace("-", " ").title()

    # --- Description from og:description ---
    desc_match = re.search(r'property="og:description"\s+content="([^"]+)"', html)
    description = desc_match.group(1).strip() if desc_match else None

    # --- Website URL: first external href after the globe SVG ---
    # The globe SVG appears just before the website anchor.
    # Pattern: globe SVG path appears, then shortly after href="https://..."
    # The website link is in the ul.flex section near the top of main content.
    # Strategy: find the href that appears right before "Website" label text.
    website_url: Optional[str] = None

    # Primary: look for the JSON-LD mainEntityOfPage url (most reliable)
    jsonld_match = re.search(
        r'"mainEntityOfPage"\s*:\s*\{[^}]*"url"\s*:\s*"(https?://[^"]+)"', html
    )
    if jsonld_match:
        candidate = jsonld_match.group(1).strip()
        # Make sure it's not a blume.vc URL
        if "blume.vc" not in candidate:
            website_url = candidate

    # Fallback: find href near the globe SVG / "Website" text
    if not website_url:
        # The website link appears in: <a href="..."><span ...>...(globe SVG)...<span>Website</span>
        # Find all hrefs in the top-of-page flex section before the Founders heading
        main_section_match = re.search(
            r'richtext.*?(?=title-label.*?Founders|Investment Lead|$)',
            html,
            re.DOTALL,
        )
        if main_section_match:
            section_html = main_section_match.group(0)[:3000]
            for m in re.finditer(r'href="(https?://[^"]+)"', section_html):
                href = m.group(1)
                if (
                    "blume.vc" not in href
                    and "linkedin.com" not in href
                    and "cdn." not in href
                    and "fonts." not in href
                    and "google" not in href
                    and "twitter.com" not in href
                    and "youtube.com" not in href
                    and "instagram.com" not in href
                    and "facebook.com" not in href
                ):
                    website_url = href
                    break

    # --- LinkedIn company URL ---
    company_linkedin: Optional[str] = None
    # The company LinkedIn link appears in the same flex section as the website link
    li_company_match = re.search(
        r'href="(https://www\.linkedin\.com/company/[^"]+)"', html
    )
    if li_company_match:
        company_linkedin = li_company_match.group(1)

    # --- Founders section ---
    # Pattern: <h2 class="title-label...">Founders</h2><ul ...>...</ul>
    founders: list[tuple[str, Optional[str]]] = []  # (full_name, linkedin_url)

    founders_match = re.search(
        r'title-label[^>]*>Founders</h2>\s*<ul[^>]*>(.*?)</ul>',
        html,
        re.DOTALL,
    )
    if founders_match:
        founders_html = founders_match.group(1)
        # Each founder is in a <li>...</li>
        for li_match in re.finditer(r"<li>(.*?)</li>", founders_html, re.DOTALL):
            li_html = li_match.group(1)
            # Try to extract LinkedIn URL for this founder
            li_personal = re.search(
                r'href="(https?://(?:www\.)?linkedin\.com/in/[^"]+)"', li_html
            )
            founder_linkedin = li_personal.group(1) if li_personal else None
            # Extract name: strip all tags
            name_clean = re.sub(r"<[^>]+>", "", li_html).strip()
            name_clean = re.sub(r"\s+", " ", name_clean).strip()
            if name_clean and len(name_clean) > 1:
                founders.append((name_clean, founder_linkedin))

    # --- Real emails anywhere in the page ---
    real_emails = set(
        re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", html)
    )
    # Filter out blume.vc emails and common noise
    real_emails = {
        e for e in real_emails
        if not e.endswith("blume.vc")
        and "example.com" not in e
        and "sentry.io" not in e
        and "@2x" not in e
    }

    # --- Fund number ---
    fund_match = re.search(r"\bFund\s+(I{1,3}|IV|V|VI)\b", html)
    fund = fund_match.group(0) if fund_match else None

    # --- Founding year ---
    year_match = re.search(r"Founded\s+(\d{4})", html)
    founding_year = year_match.group(1) if year_match else None

    # --- Location ---
    loc_matches = re.findall(
        r'href="https://blume\.vc/startups/location:[^"]*"[^>]*>([^<]+)</a>', html
    )
    location = ", ".join(loc_matches) if loc_matches else None

    # --- Industry/Sector ---
    sector_matches = re.findall(
        r'href="https://blume\.vc/sectors/[^"]*"[^>]*>([^<]+)</a>', html
    )
    industry = sector_matches[0].strip() if sector_matches else None

    return {
        "name": company_name,
        "description": description,
        "website_url": website_url,
        "company_linkedin": company_linkedin,
        "founders": founders,
        "real_emails": real_emails,
        "fund": fund,
        "founding_year": founding_year,
        "location": location,
        "industry": industry,
    }


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _upsert_company_and_contacts(
    domain: str,
    data: dict,
    seen_emails: set,
    counts: dict,
) -> None:
    """Upsert one company + its founders using a fresh DB session.

    Opens a new SessionLocal each call so Turso Hrana stream timeouts
    (which occur on long-lived sessions) never carry over between companies.
    On a UNIQUE constraint violation (domain or email already exists from a
    prior partial run), closes the broken session, opens another, and re-reads
    the existing row rather than inserting.
    """
    db = SessionLocal()
    try:
        # --- Company ---
        company = db.scalar(select(Company).where(Company.domain == domain))
        if company is None:
            company = Company(
                domain=domain,
                name=data["name"],
                source=SOURCE_TAG,
                industry=data["industry"],
                description=data["description"],
            )
            db.add(company)
            db.flush()          # get company.id before contacts
            counts["companies_created"] += 1
        else:
            counts["companies_existing"] += 1

        # --- Contacts ---
        for full_name, founder_linkedin in data["founders"]:
            first = _first_name(full_name)
            if not first:
                continue

            # Priority 1: real email found in the page HTML
            real_email: Optional[str] = None
            for candidate in data["real_emails"]:
                cand_lower = candidate.lower()
                if cand_lower.startswith(first + "@") or (
                    domain in cand_lower and cand_lower.startswith(first)
                ):
                    real_email = cand_lower
                    break

            if real_email:
                email = real_email
                email_verified = True
                email_confidence = 95
                scraped_pattern = None
            else:
                # Priority 2: guess firstname@domain
                email = f"{first}@{domain}"
                email_verified = False
                email_confidence = 60
                scraped_pattern = "firstname"

            if email in seen_emails:
                counts["contacts_existing"] += 1
                continue

            existing = db.scalar(select(Contact).where(Contact.email == email))
            if existing is not None:
                if founder_linkedin and not existing.linkedin_url:
                    existing.linkedin_url = founder_linkedin
                counts["contacts_existing"] += 1
                seen_emails.add(email)
                continue

            seen_emails.add(email)
            contact = Contact(
                company_id=company.id,
                name=full_name,
                email=email,
                role="Founder",
                email_verified=email_verified,
                email_confidence=email_confidence,
                scraped_pattern=scraped_pattern,
                linkedin_url=founder_linkedin,
                source=SOURCE_TAG,
            )
            db.add(contact)
            counts["contacts_created"] += 1

        db.commit()

    except Exception as exc:
        # Turso raises ValueError (not IntegrityError) for UNIQUE violations.
        # Close the broken session and open a fresh one to re-read existing rows.
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass

        err_str = str(exc)
        if "UNIQUE constraint failed: companies.domain" in err_str:
            # Company already exists — re-read and count contacts as existing
            db2 = SessionLocal()
            try:
                company = db2.scalar(select(Company).where(Company.domain == domain))
                if company is not None:
                    counts["companies_existing"] += 1
                    # Count all founders as existing since company was already there
                    for full_name, _ in data["founders"]:
                        first = _first_name(full_name)
                        if first:
                            email = f"{first}@{domain}"
                            if email not in seen_emails:
                                seen_emails.add(email)
                                counts["contacts_existing"] += 1
                    db2.commit()
                    return
            finally:
                db2.close()
        elif "UNIQUE constraint failed: contacts.email" in err_str:
            # One contact email already exists; treat company as written, contact as existing
            counts["contacts_existing"] += 1
            return
        # Unknown error — re-raise so caller sees it
        raise

    finally:
        try:
            db.close()
        except Exception:
            pass


def main() -> int:
    print("Fetching Blume Ventures slugs from sitemaps...")
    slugs = _get_slugs_from_sitemaps()

    if not slugs:
        # Fallback to listing page
        print("  Sitemaps empty — falling back to /startups listing page")
        listing_html = _fetch(f"{BASE_URL}/startups")
        slugs = _get_slugs_from_listing(listing_html)

    print(f"  {len(slugs)} company slugs found")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_fetch_error": 0,
    }
    seen_emails: set[str] = set()

    for i, slug in enumerate(slugs, 1):
        url = f"{BASE_URL}/{slug}"
        print(f"  [{i}/{len(slugs)}] {url}", end="  ", flush=True)

        try:
            html = _fetch(url)
        except Exception as e:
            print(f"ERROR: {e}")
            counts["skipped_fetch_error"] += 1
            time.sleep(2.0)
            continue

        data = _parse_company_page(slug, html)

        # Require a website URL to create the company
        if not data["website_url"]:
            print("no website found — skipping")
            counts["skipped_no_website"] += 1
            time.sleep(1.5)
            continue

        domain = _parse_domain(data["website_url"])
        if not domain:
            print(f"bad domain ({data['website_url']}) — skipping")
            counts["skipped_bad_domain"] += 1
            time.sleep(1.5)
            continue

        print(f"domain={domain}  founders={len(data['founders'])}")

        # Use a helper that retries with a fresh session if we hit a UNIQUE violation
        _upsert_company_and_contacts(domain, data, seen_emails, counts)

        # Polite rate-limit: 3s/req to stay well under Cloudflare's burst threshold
        time.sleep(3.0)

    print("\n=== Scrape complete ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

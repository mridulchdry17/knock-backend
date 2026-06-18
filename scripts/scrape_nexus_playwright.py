"""Scrape Nexus Venture Partners portfolio using Playwright.

The Nexus portfolio page (nexusvp.com/in/companies/) shows company cards in a
grid. Clicking each card opens a side panel with: company name, website,
founders + LinkedIn URLs, description, categories, and funded year.

This script uses Playwright to click each card, extract the side panel data,
and insert Company + Contact rows into the DB.

Usage:
    .venv/bin/python -m scripts.scrape_nexus_playwright
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "nexus-scraping"
URL = "https://nexusvp.com/in/companies/"


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


_ROLE_LABELS = {
    "CEO", "CTO", "COO", "CFO", "CPO", "CMO", "CRO", "CSO",
    "FOUNDER", "CO-FOUNDER", "COFOUNDER", "MD", "VP", "PRESIDENT",
}


def _is_role_line(line: str) -> bool:
    return line.upper().strip() in _ROLE_LABELS


def _scrape_companies() -> list[dict]:
    """Launch Playwright, click every .nvp-logogrid card, parse .nvp-sidebar panel."""
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        print("Loading Nexus portfolio page...")
        page.goto(URL, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=20000)
        time.sleep(2)

        # Scroll to load all lazy cards
        for _ in range(8):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.2)

        cards = page.query_selector_all(".nvp-logogrid")
        if not cards:
            print("ERROR: could not find company cards (.nvp-logogrid)")
            browser.close()
            return []

        print(f"Found {len(cards)} cards — clicking through...")

        for i, card in enumerate(cards):
            try:
                card.scroll_into_view_if_needed()
                card.click(force=True, timeout=5000)
                time.sleep(1.3)

                panel = page.query_selector(".nvp-sidebar")
                if not panel:
                    continue

                panel_text = panel.inner_text()
                lines = [l.strip() for l in panel_text.split("\n") if l.strip()]

                # Company name is the first line
                company_name = lines[0] if lines else ""

                # Extract website — first non-nexusvp, non-linkedin http link
                website = ""
                for a in panel.query_selector_all("a[href^='http']"):
                    href = a.get_attribute("href") or ""
                    if "nexusvp" not in href and "linkedin" not in href:
                        website = href
                        break

                # Extract description from ABOUT section
                about_match = re.search(r"ABOUT\n(.+?)(?:\nFUNDED|\nCATEGOR|\nWEBSITE|\nLOCATION|\nFOUNDER|\Z)", panel_text, re.DOTALL)
                description = about_match.group(1).strip() if about_match else ""

                # Extract founders from FOUNDERS section (stop at PARTNERS/NEWS/etc.)
                founders: list[dict] = []
                in_founders = False
                stop_words = {"PARTNERS", "NEWS", "CATEGORIES", "FUNDED", "LOCATIONS", "WEBSITE", "ABOUT"}
                for line in lines:
                    if line.upper() == "FOUNDERS":
                        in_founders = True
                        continue
                    if in_founders and line.upper() in stop_words:
                        break
                    if in_founders and line and not _is_role_line(line):
                        founders.append({"name": line, "linkedin_url": None})

                # Pair LinkedIn URLs to founders by order of appearance
                linkedin_hrefs = [
                    a.get_attribute("href")
                    for a in panel.query_selector_all("a[href*='linkedin.com/in/']")
                ]
                for j, href in enumerate(linkedin_hrefs):
                    if j < len(founders):
                        founders[j]["linkedin_url"] = href

                if company_name or website:
                    results.append({
                        "name": company_name,
                        "website": website,
                        "founders": founders,
                        "description": description[:500] if description else None,
                    })

                if (i + 1) % 20 == 0:
                    print(f"  processed {i+1}/{len(cards)} cards, {len(results)} with data")

                page.keyboard.press("Escape")
                time.sleep(0.3)

            except PlaywrightTimeout:
                continue
            except Exception as e:
                print(f"  card {i} error: {e}")
                continue

        browser.close()

    return results


def main() -> int:
    companies = _scrape_companies()
    print(f"\nScraped {len(companies)} companies from Nexus")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for record in companies:
            website = record.get("website", "").strip()
            company_name = record.get("name", "").strip()
            founders = record.get("founders", [])
            description = record.get("description")

            if not website:
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            name = company_name or domain

            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=name,
                    source=SOURCE_TAG,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            for founder in founders:
                full_name = (founder.get("name") or "").strip()
                linkedin_url = founder.get("linkedin_url")
                if not full_name:
                    continue

                first = _first_name(full_name)
                email = f"{first}@{domain}"

                if email in seen_emails:
                    counts["contacts_existing"] += 1
                    continue
                existing = db.scalar(select(Contact).where(Contact.email == email))
                if existing is not None:
                    counts["contacts_existing"] += 1
                    continue
                seen_emails.add(email)

                contact = Contact(
                    company_id=company.id,
                    name=full_name,
                    email=email,
                    role="Founder",
                    email_verified=False,
                    email_confidence=60,
                    scraped_pattern="firstname",
                    linkedin_url=linkedin_url,
                    source=SOURCE_TAG,
                )
                db.add(contact)
                counts["contacts_created"] += 1

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

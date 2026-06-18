"""Scrape India Quotient's portfolio via their public static HTML page.

The page is hosted on Webflow (cdn.prod.website-files.com CDN). All 72+
portfolio companies are embedded directly in static HTML — no JS rendering
required, no API authentication needed.

Each company card (class="com_box") contains:
  - Website URL: <a class="link_logo" href="...">
  - Description: <div class="com_desc">
  - Exit status: <div class="exited_txt"> (non-empty when exited)
  - Category: fs-cmsfilter-field="make" on sibling div
  - Founders via <div class="all_link"> blocks:
      - Name: <div class="name_link">...</div>
      - LinkedIn: <a href="https://www.linkedin.com/in/...">

EMAIL PRIORITY for each founder:
  1. Real email found in HTML (email_verified=True, confidence=95)
  2. firstname@domain guess (email_verified=False, confidence=60)

Usage:
    .venv/bin/python -m scripts.scrape_india_quotient

Idempotent — re-runs skip existing emails/companies.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

PORTFOLIO_URL = "https://www.indiaquotient.in/portfolio"
SOURCE_TAG = "india-quotient-scraping"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[a-z]{2,}", re.IGNORECASE)


# ---------------------------------------------------------------------------
# HTML fetching
# ---------------------------------------------------------------------------

def _fetch_html() -> str:
    req = urllib.request.Request(PORTFOLIO_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Minimal HTML parser (no external libs needed — uses stdlib html.parser)
# ---------------------------------------------------------------------------

class _CardParser(HTMLParser):
    """State-machine parser that walks the Webflow-generated HTML and emits
    one dict per portfolio company card."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[dict] = []

        # Per-card state
        self._in_card = False
        self._card_depth = 0          # nesting depth when card opened
        self._current_depth = 0       # global nesting depth

        self._website: str | None = None
        self._description: str | None = None
        self._is_exited = False
        self._category: str | None = None
        self._founders: list[dict] = []  # [{name, linkedin, emails}]

        # Sub-states within a card
        self._in_com_desc = False
        self._in_exited_txt = False
        self._in_all_link = False
        self._all_link_depth = 0

        self._in_name_link = False
        self._cat_field: str | None = None   # text of fs-cmsfilter-field="make"
        self._in_cat_field = False

        # Current founder scratch
        self._cur_founder_name: str | None = None
        self._cur_founder_linkedin: str | None = None

    # -- helpers -------------------------------------------------------------

    def _attrs_dict(self, attrs: list) -> dict:
        return {k: v for k, v in attrs}

    def _has_class(self, attrs: dict, cls: str) -> bool:
        classes = (attrs.get("class") or "").split()
        return cls in classes

    def _reset_card(self) -> None:
        self._in_card = False
        self._website = None
        self._description = None
        self._is_exited = False
        self._founders = []
        self._in_com_desc = False
        self._in_exited_txt = False
        self._in_all_link = False
        self._in_name_link = False
        self._cur_founder_name = None
        self._cur_founder_linkedin = None

    def _flush_founder(self) -> None:
        name = (self._cur_founder_name or "").strip()
        linkedin = self._cur_founder_linkedin
        if name or linkedin:
            self._founders.append({"name": name, "linkedin": linkedin, "emails": []})
        self._cur_founder_name = None
        self._cur_founder_linkedin = None

    # -- HTMLParser callbacks ------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self._current_depth += 1
        a = self._attrs_dict(attrs)
        cls = a.get("class", "")

        # Detect category field (fs-cmsfilter-field="make")
        if a.get("fs-cmsfilter-field") == "make":
            self._in_cat_field = True
            return

        # Detect start of a company card
        if "com_box" in cls:
            self._in_card = True
            self._card_depth = self._current_depth
            # Extract website URL from immediate child link
            return

        if not self._in_card:
            return

        # Inside a card -------------------------------------------------------

        # Website URL — first <a class="link_logo ...">
        if tag == "a" and "link_logo" in cls and self._website is None:
            href = a.get("href", "")
            if href and href != "#":
                self._website = href

        # Description block
        if "com_desc" in cls:
            self._in_com_desc = True
            return

        # Exited text
        if "exited_txt" in cls:
            self._in_exited_txt = True
            return

        # Founder block — each founder is wrapped in class="all_link"
        if "all_link" in cls:
            # Flush previous founder before starting new one
            if self._in_all_link:
                self._flush_founder()
            self._in_all_link = True
            self._all_link_depth = self._current_depth
            return

        if self._in_all_link:
            # Founder name
            if "name_link" in cls:
                self._in_name_link = True
                return
            # LinkedIn anchor (has href to linkedin.com/in/...)
            if tag == "a":
                href = a.get("href", "")
                if "linkedin.com/in/" in href:
                    # Each founder has TWO <a> tags pointing to same linkedin —
                    # deduplicate by only storing first non-None value
                    if self._cur_founder_linkedin is None:
                        self._cur_founder_linkedin = href

    def handle_endtag(self, tag: str) -> None:
        # Category field ends on next text; we handle it in handle_data
        if self._in_cat_field and tag == "div":
            self._in_cat_field = False

        if self._in_name_link and tag == "div":
            self._in_name_link = False

        if self._in_exited_txt and tag == "div":
            self._in_exited_txt = False

        if self._in_com_desc and tag == "div":
            self._in_com_desc = False

        if self._in_card:
            # Detect end of all_link block (same depth it opened)
            if self._in_all_link and self._current_depth <= self._all_link_depth:
                self._flush_founder()
                self._in_all_link = False

            # Detect end of com_box card
            if self._current_depth <= self._card_depth:
                # Flush last founder if any
                if self._in_all_link:
                    self._flush_founder()
                    self._in_all_link = False
                # Save card
                if self._website:
                    self.cards.append({
                        "website": self._website,
                        "description": self._description,
                        "is_exited": self._is_exited,
                        "category": self._category,
                        "founders": list(self._founders),
                    })
                self._reset_card()

        self._current_depth -= 1

    def handle_data(self, data: str) -> None:
        text = data.strip()

        # Category field text
        if self._in_cat_field and text:
            self._category = text
            self._in_cat_field = False
            return

        if not self._in_card:
            return

        # Inside a card

        if self._in_exited_txt and "exited" in text.lower():
            self._is_exited = True

        if self._in_com_desc and text and self._description is None:
            # First non-empty text inside com_desc is the description
            self._description = text

        if self._in_all_link and self._in_name_link and text:
            if self._cur_founder_name is None:
                self._cur_founder_name = text

        # Scan all text nodes inside card for real email addresses
        emails = EMAIL_RE.findall(text)
        for em in emails:
            if self._founders:
                self._founders[-1]["emails"].append(em)


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        host = host.split("/")[0]
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _clean_linkedin(url: str | None) -> str | None:
    if not url:
        return None
    url = url.split("?")[0].rstrip("/")
    if "linkedin.com/in/" not in url:
        return None
    return url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"Fetching India Quotient portfolio from {PORTFOLIO_URL} ...")
    html = _fetch_html()
    print(f"  Downloaded {len(html):,} bytes")

    parser = _CardParser()
    parser.feed(html)
    cards = parser.cards
    print(f"  {len(cards)} company cards parsed")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_no_founders": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        # Force all reads through the write engine (Turso remote) so that
        # upsert SELECTs see the same data as writes.  The local embedded
        # replica may be empty or stale at script start-up time, which would
        # produce "no such table" errors for plain read routing.
        db.info["_wrote"] = True

        for card in cards:
            website = card.get("website") or ""
            founders_raw = card.get("founders") or []
            description = card.get("description") or None
            category = card.get("category") or None
            is_exited = card.get("is_exited", False)

            if not website or website == "#":
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            # Derive a company name from domain (best-effort; no text name in HTML)
            # Use the domain sans TLD, title-cased as a fallback name
            company_name = domain.split(".")[0].replace("-", " ").title()

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    funding_stage=None,
                    industry=category,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            # Filter real founders (non-empty name or linkedin)
            real_founders = [
                f for f in founders_raw
                if (f.get("name") or "").strip() or f.get("linkedin")
            ]

            if not real_founders:
                counts["skipped_no_founders"] += 1
                continue

            for founder in real_founders:
                full_name = (founder.get("name") or "").strip()
                linkedin = _clean_linkedin(founder.get("linkedin"))
                inline_emails = founder.get("emails") or []

                # Priority 1: real email from HTML
                real_email: str | None = None
                for em in inline_emails:
                    em_lower = em.lower()
                    if em_lower not in seen_emails:
                        real_email = em_lower
                        break

                if real_email:
                    email = real_email
                    email_verified = True
                    email_confidence = 95
                    scraped_pattern = None
                elif full_name:
                    # Priority 2: guess firstname@domain
                    first = _first_name(full_name)
                    if not first:
                        continue
                    email = f"{first}@{domain}"
                    email_verified = False
                    email_confidence = 60
                    scraped_pattern = "firstname"
                else:
                    # Only have LinkedIn — still create contact row (email None)
                    email = None
                    email_verified = False
                    email_confidence = 0
                    scraped_pattern = None

                # Deduplicate by email (skip if email is None — no unique key)
                if email is not None:
                    if email in seen_emails:
                        counts["contacts_existing"] += 1
                        continue
                    existing = db.scalar(
                        select(Contact).where(Contact.email == email)
                    )
                    if existing is not None:
                        counts["contacts_existing"] += 1
                        continue
                    seen_emails.add(email)

                contact = Contact(
                    company_id=company.id,
                    name=full_name or None,
                    email=email,
                    role="Founder",
                    email_verified=email_verified,
                    email_confidence=email_confidence,
                    scraped_pattern=scraped_pattern,
                    linkedin_url=linkedin,
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

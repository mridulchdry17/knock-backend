"""Admin bulk contact upload service.

Pure-ish orchestration: parses heterogeneous row dicts, normalizes, dedupes by
global email, upserts the contact + its owning company row. Failures are
per-row — one bad email doesn't abort the batch.

Used by:
- POST /api/v1/admin/contacts/bulk (JSON rows)
- POST /api/v1/admin/contacts/bulk/csv (multipart CSV)

Both endpoints flow through `bulk_upload(rows, dry_run=...)`.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Final

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session as OrmSession

from app.core.emails import normalize_email
from app.models import Contact
from app.repositories import companies as companies_repo
from app.repositories import contacts as contacts_repo

# ─────────────────────────── result types ───────────────────────────


@dataclass(frozen=True, slots=True)
class RowError:
    row_index: int  # 0-indexed within the input rows list
    email: str | None
    error_code: str  # 'missing_email' | 'invalid_email' | 'invalid_domain' | 'parse_error'
    message: str


@dataclass(frozen=True, slots=True)
class ContactUploadResult:
    inserted: int
    updated: int
    skipped: int
    failed: int
    row_errors: list[RowError] = field(default_factory=list)


# ─────────────────────────── column aliases ───────────────────────────

# Map of canonical field → set of accepted aliases (lower-cased).
_ALIASES: Final[dict[str, frozenset[str]]] = {
    "name": frozenset({"name", "full name", "fullname", "full_name"}),
    "email": frozenset({"email", "e-mail", "email address", "email_address"}),
    "role": frozenset({"title", "role", "job title", "job_title", "position"}),
    "company_name": frozenset({"company", "company name", "company_name", "organization"}),
    "company_domain": frozenset({"company_domain", "domain"}),
    "linkedin_url": frozenset({"linkedin_url", "linkedin", "linkedin url"}),
    "source": frozenset({"source"}),
    "notes": frozenset({"notes", "note"}),
    "scraped_pattern": frozenset({"scraped_pattern", "pattern"}),
}

# Max rows per upload to keep parsing/memory bounded. Configurable later.
MAX_UPLOAD_ROWS: Final[int] = 10_000

# Characters stripped from the tail of a derived/raw string field (handles the
# "Estuate," dump-style row in the real-world CSV).
_TRAILING_PUNCT = " \t\r\n.,;:|/\\"


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    name: str | None
    email: str  # validated, normalized
    role: str | None
    company_name: str
    company_domain: str
    linkedin_url: str | None
    source: str | None
    notes: str | None
    scraped_pattern: str | None


def _clean(value: object) -> str | None:
    """Whitespace+trailing-punct strip. Returns None for empty/whitespace-only.

    Used for short structured fields (names, roles, company labels) where the
    dump-style trailing comma/period is noise. NOT for freeform notes — those
    legitimately end with sentence punctuation.
    """
    if value is None:
        return None
    s = str(value).strip().strip(_TRAILING_PUNCT).strip()
    return s or None


def _clean_freeform(value: object) -> str | None:
    """Whitespace-only strip; preserves trailing punctuation. For `notes`."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _canonicalize_keys(row: dict[str, object]) -> dict[str, object]:
    """Map raw input keys to canonical names via case-insensitive alias match.
    Unknown keys are dropped — never reach validation or DB."""
    out: dict[str, object] = {}
    for raw_key, value in row.items():
        if raw_key is None:
            continue
        norm_key = str(raw_key).strip().lower()
        for canonical, aliases in _ALIASES.items():
            if norm_key in aliases:
                # Last write wins if duplicate aliases hit; doesn't matter in
                # practice because real CSVs don't have collisions.
                out[canonical] = value
                break
    return out


def _derive_company_name_from_domain(domain: str) -> str:
    """`acme.com` → 'Acme'. `sub.foo-bar.co.uk` → 'Foo-Bar'. Best-effort only;
    admin can rename later."""
    parts = [p for p in domain.split(".") if p]
    if not parts:
        return domain.title() or "Unknown"
    # Drop trailing TLD-ish parts. Keep the second-to-last meaningful label.
    # Heuristic: take the leftmost label longer than 2 chars (skips 'co' in .co.uk).
    label = parts[-2] if len(parts) >= 2 else parts[0]
    if len(parts) >= 3 and len(parts[-2]) <= 2:
        label = parts[-3]
    return label.replace("-", "-").replace("_", " ").title()


def _parse_row(row_index: int, raw: dict[str, object]) -> _ParsedRow | RowError:
    canonical = _canonicalize_keys(raw)

    raw_email = _clean(canonical.get("email"))
    if not raw_email:
        return RowError(
            row_index=row_index,
            email=None,
            error_code="missing_email",
            message="Email is required.",
        )

    try:
        # check_deliverability=False: we don't want DNS lookups during upload;
        # bounce-tracking lives in B5.5.
        validated = validate_email(raw_email, check_deliverability=False)
    except EmailNotValidError as e:
        return RowError(
            row_index=row_index,
            email=raw_email,
            error_code="invalid_email",
            message=str(e),
        )

    email = normalize_email(validated.normalized)

    # Derive company_domain from email if not explicitly provided.
    explicit_domain = _clean(canonical.get("company_domain"))
    company_domain = explicit_domain.lower() if explicit_domain else email.split("@", 1)[1]

    if "." not in company_domain or len(company_domain) < 3:
        return RowError(
            row_index=row_index,
            email=email,
            error_code="invalid_domain",
            message=f"Company domain '{company_domain}' is not well-formed.",
        )

    company_name = _clean(canonical.get("company_name")) or _derive_company_name_from_domain(
        company_domain
    )

    return _ParsedRow(
        name=_clean(canonical.get("name")),
        email=email,
        role=_clean(canonical.get("role")),
        company_name=company_name,
        company_domain=company_domain,
        linkedin_url=_clean(canonical.get("linkedin_url")),
        source=_clean(canonical.get("source")),
        notes=_clean_freeform(canonical.get("notes")),
        scraped_pattern=_clean(canonical.get("scraped_pattern")),
    )


# ─────────────────────────── public API ───────────────────────────


def bulk_upload(
    db: OrmSession, rows: list[dict[str, object]], *, dry_run: bool = False
) -> ContactUploadResult:
    """Parse, validate, dedup, upsert. Atomic per-row.

    Dedup key: normalized email (globally unique post-migration 0005).
    - Existing email → update mutable fields (name, role, linkedin_url, source,
      notes, scraped_pattern). Preserves created_at and is_invalid.
    - New email → insert. Creates the owning Company row if its domain hasn't
      been seen before.

    `dry_run=True` returns the same counts a real run would produce, without
    persisting anything. Used by the admin UI for a "preview" pass.

    Caller owns the transaction. On dry_run we rollback any flushed state.
    """
    if len(rows) > MAX_UPLOAD_ROWS:
        # Hard cap — the admin UI is expected to chunk above this.
        return ContactUploadResult(
            inserted=0,
            updated=0,
            skipped=0,
            failed=len(rows),
            row_errors=[
                RowError(
                    row_index=0,
                    email=None,
                    error_code="parse_error",
                    message=f"Upload exceeds max rows ({MAX_UPLOAD_ROWS}).",
                )
            ],
        )

    inserted = 0
    updated = 0
    skipped = 0
    failed = 0
    errors: list[RowError] = []

    parsed: list[_ParsedRow] = []
    seen_emails: set[str] = set()

    # Phase 1: parse + collect errors. Skip intra-batch duplicates (count as skipped).
    for idx, raw in enumerate(rows):
        result = _parse_row(idx, raw)
        if isinstance(result, RowError):
            failed += 1
            errors.append(result)
            continue
        if result.email in seen_emails:
            skipped += 1
            continue
        seen_emails.add(result.email)
        parsed.append(result)

    # Phase 2: bulk lookup of existing contacts by email — one query, not N.
    existing = contacts_repo.get_by_emails(db, (p.email for p in parsed))

    # Phase 3: per-row upsert.
    for p in parsed:
        try:
            company = companies_repo.get_or_create_by_domain(
                db, domain=p.company_domain, name=p.company_name
            )

            current = existing.get(p.email)
            if current is None:
                contact = Contact(
                    company_id=company.id,
                    name=p.name,
                    role=p.role,
                    email=p.email,
                    linkedin_url=p.linkedin_url,
                    source=p.source,
                    notes=p.notes,
                    scraped_pattern=p.scraped_pattern,
                )
                contacts_repo.add(db, contact)
                inserted += 1
            else:
                # Selective update — only overwrite fields the upload actually
                # carries. Preserves manual admin edits to other columns:
                # re-uploading a CSV row without notes/source does NOT clobber
                # prior admin curation.
                changed = False
                if p.name and current.name != p.name:
                    current.name = p.name
                    changed = True
                if p.role and current.role != p.role:
                    current.role = p.role
                    changed = True
                if p.linkedin_url and current.linkedin_url != p.linkedin_url:
                    current.linkedin_url = p.linkedin_url
                    changed = True
                if p.source and current.source != p.source:
                    current.source = p.source
                    changed = True
                if p.notes and current.notes != p.notes:
                    current.notes = p.notes
                    changed = True
                if p.scraped_pattern and current.scraped_pattern != p.scraped_pattern:
                    current.scraped_pattern = p.scraped_pattern
                    changed = True
                # Company swap is intentionally NOT supported here — a contact's
                # company_id only changes via a dedicated admin action.
                if changed:
                    db.add(current)
                updated += 1
        except Exception as e:  # per-row isolation: never abort the batch
            failed += 1
            errors.append(
                RowError(
                    row_index=-1,  # post-parse failures lose original index; rare path
                    email=p.email,
                    error_code="parse_error",
                    message=str(e),
                )
            )

    if dry_run:
        db.rollback()

    return ContactUploadResult(
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        failed=failed,
        row_errors=errors,
    )


def parse_csv(content: bytes) -> list[dict[str, object]]:
    """Decode CSV bytes (utf-8 with BOM tolerance) into row dicts.

    Header row is required; case is preserved for the service's alias matcher
    to handle. Empty trailing rows are dropped.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, object]] = []
    for raw in reader:
        # csv.DictReader can yield rows with None keys for ragged extras; drop.
        cleaned = {k: v for k, v in raw.items() if k is not None}
        if any((v or "").strip() for v in cleaned.values() if isinstance(v, str)):
            out.append(cleaned)
    return out

"""Templates service — per-user template library (cap 3), starter seeding,
placeholder rendering, and test-send.

Render is the single source of placeholder substitution, shared by the batch
generator (per-recipient render at send time) and test-send (sample render).
Supported placeholders fill from the contact/company pool + the sender:
  {{first_name}} {{name}} {{hr_name}}  → contact.name
  {{company}}                          → company.name
  {{role}} {{title}}                   → contact.role
  {{sender_name}}                      → user's signature/full name
Anything we don't have data for falls back gracefully (see _PLACEHOLDER_FALLBACKS).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session as OrmSession

from app.core.errors import ApiError
from app.logging_config import get_logger
from app.models import Company, Contact, Template, User
from app.repositories import templates as templates_repo
from app.services.html_to_text import html_to_text

log = get_logger("templates")

# Locked product decision (mirrored in the F6 frontend): a user holds at most
# 3 templates.
MAX_TEMPLATES_PER_USER = 3

# Seeded for every new user on first login. Placeholder-driven so they render
# per-recipient out of the box; the user personalizes from here.
STARTER_TEMPLATES: list[dict[str, str]] = [
    {
        "name": "Warm intro",
        "subject": "Quick hello from a student exploring {{company}}",
        "body": (
            "Hi {{first_name}},\n\n"
            "I'm a student really interested in {{company}} and the work your "
            "team is doing. I'd love to learn more and explore how I could "
            "contribute.\n\n"
            "Would you be open to a quick chat?\n\n"
            "Best,\n{{sender_name}}\n"
        ),
    },
    {
        "name": "Specific-role interest",
        "subject": "Interested in opportunities at {{company}}",
        "body": (
            "Hi {{first_name}},\n\n"
            "I came across {{company}} and was excited by what you're building. "
            "As {{role}}, you'd know best where a motivated student could add "
            "value — I'd love to be considered for any internship or entry-level "
            "openings.\n\n"
            "Happy to share my resume. Thanks for your time!\n\n"
            "Best,\n{{sender_name}}\n"
        ),
    },
    {
        "name": "Referral ask",
        "subject": "A quick favor re: {{company}}",
        "body": (
            "Hi {{first_name}},\n\n"
            "I'm a student hoping to break into {{company}}. If you know who on "
            "your team handles early-career hiring, a quick pointer would mean a "
            "lot — and I'm happy to send over my background.\n\n"
            "Thank you!\n{{sender_name}}\n"
        ),
    },
]


# ─────────────────────────── rendering ───────────────────────────

_FOLLOWUP_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# When a placeholder's source value is missing, substitute this instead of
# leaving a raw {{token}} in the outbound email.
_PLACEHOLDER_FALLBACKS = {
    "first_name": "there",
    "name": "there",
    "hr_name": "there",
    "company": "your company",
    "role": "your team",
    "title": "your team",
    "sender_name": "a student",
}


def _first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    first = full_name.strip().split(" ", 1)[0]
    return first or None


def render_template(
    subject: str,
    body: str,
    *,
    to_contact: Contact | None,
    company: Company | None,
    sender_name: str | None,
) -> tuple[str, str]:
    """Substitute placeholders for one recipient. Unknown tokens are left as-is
    (so a typo'd {{wrong}} is visible to the user, not silently dropped); known
    tokens with no data use the fallback."""
    values: dict[str, str | None] = {
        "first_name": _first_name(to_contact.name) if to_contact else None,
        "name": (to_contact.name if to_contact else None),
        "hr_name": (to_contact.name if to_contact else None),
        "company": (company.name if company else None),
        "role": (to_contact.role if to_contact else None),
        "title": (to_contact.role if to_contact else None),
        "sender_name": sender_name,
    }

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key not in values:
            return match.group(0)  # unknown token — leave visible
        val = values[key]
        if val:
            return val
        return _PLACEHOLDER_FALLBACKS.get(key, match.group(0))

    # Subject is authored in a plain input (never HTML); the body comes from the
    # rich-text editor as HTML, so substitute placeholders first (the {{token}}
    # text lives inside the variable spans) then flatten to plain text for the
    # text/plain email + the /today preview.
    rendered_subject = _FOLLOWUP_RE.sub(_sub, subject)
    rendered_body = html_to_text(_FOLLOWUP_RE.sub(_sub, body))
    return rendered_subject, rendered_body


# ─────────────────────────── starter seeding ───────────────────────────


def seed_starters(db: OrmSession, user: User) -> int:
    """Create the 3 starter templates for a user IFF they have none yet.
    Idempotent — safe to call on every login. Returns count created."""
    if templates_repo.count_for_user(db, user.id) > 0:
        return 0
    for spec in STARTER_TEMPLATES:
        templates_repo.add(
            db,
            Template(
                user_id=user.id,
                name=spec["name"],
                subject=spec["subject"],
                body=spec["body"],
                is_starter=True,
            ),
        )
    log.info("templates.starters_seeded", user_id=user.id, count=len(STARTER_TEMPLATES))
    return len(STARTER_TEMPLATES)


# ─────────────────────────── CRUD ───────────────────────────


@dataclass(frozen=True, slots=True)
class TemplateView:
    """A template plus its computed read-time fields."""

    template: Template
    used_count: int
    reply_rate: float | None  # always None in v0


def list_views(db: OrmSession, user_id: int) -> list[TemplateView]:
    rows = templates_repo.list_for_user(db, user_id)
    used = templates_repo.used_counts_for_user(db, user_id)
    return [
        TemplateView(template=t, used_count=used.get(t.id, 0), reply_rate=None)
        for t in rows
    ]


def create(db: OrmSession, user: User, *, name: str, subject: str, body: str) -> Template:
    """Create a template, enforcing the per-user cap. Caller commits."""
    if templates_repo.count_for_user(db, user.id) >= MAX_TEMPLATES_PER_USER:
        raise ApiError(
            "template_limit_reached",
            f"You can have at most {MAX_TEMPLATES_PER_USER} templates. "
            "Delete one to add another.",
            status_code=409,
        )
    return templates_repo.add(
        db,
        Template(
            user_id=user.id, name=name, subject=subject, body=body, is_starter=False
        ),
    )


def _owned_or_404(db: OrmSession, user: User, template_id: int) -> Template:
    t = templates_repo.get(db, template_id)
    if t is None or t.user_id != user.id:
        # 404 (not 403) — don't leak existence of another user's template.
        raise ApiError("not_found", "Template not found.", status_code=404)
    return t


def update(
    db: OrmSession,
    user: User,
    template_id: int,
    *,
    name: str | None,
    subject: str | None,
    body: str | None,
) -> Template:
    t = _owned_or_404(db, user, template_id)
    if name is not None:
        t.name = name
    if subject is not None:
        t.subject = subject
    if body is not None:
        t.body = body
    db.add(t)
    db.flush()
    return t


def delete(db: OrmSession, user: User, template_id: int) -> None:
    t = _owned_or_404(db, user, template_id)
    templates_repo.delete(db, t)


# ─────────────────────────── test send ───────────────────────────


def test_send(db: OrmSession, user: User, template_id: int) -> None:
    """Send a sample-rendered copy of the template to the user's OWN inbox so
    they can preview how it lands. Uses representative sample values for the
    placeholders. Raises ApiError on a disconnected Gmail or a send failure."""
    t = _owned_or_404(db, user, template_id)

    # Lazy imports — keep the template service light and avoid pulling the Gmail
    # SDK graph unless a test-send actually happens.
    from app.services import gmail_send
    from app.services.google_oauth import OAuthError, get_user_credentials

    if user.gmail_disconnected:
        raise ApiError(
            "gmail_disconnected",
            "Reconnect Gmail to send a test email.",
            status_code=409,
        )

    sender_name = user.sender_signature_name or user.full_name
    # Unsaved stand-ins purely so placeholders render with realistic sample data.
    sample_contact = Contact(name="Alex Rivera", role="Talent Lead", email=user.email)
    sample_company = Company(name="Acme Inc", domain="acme.com", source="sample")
    subject, body = render_template(
        t.subject,
        t.body,
        to_contact=sample_contact,
        company=sample_company,
        sender_name=sender_name,
    )

    try:
        creds = get_user_credentials(user)
    except OAuthError as e:
        raise ApiError(
            "gmail_disconnected",
            "Reconnect Gmail to send a test email.",
            status_code=409,
        ) from e

    result = gmail_send.send_email(
        creds,
        sender_email=user.email,
        sender_name=sender_name,
        to_email=user.email,  # test goes to yourself
        cc_emails=[],
        subject=f"[Test] {subject}",
        body_text=body,
    )
    if not result.ok:
        raise ApiError(
            "test_send_failed",
            result.error_message or "Test send failed.",
            status_code=502,
        )
    log.info("templates.test_sent", user_id=user.id, template_id=t.id)

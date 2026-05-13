"""Pure picker for the B5.4 batch cron.

`pick_companies_for_user` is intentionally side-effect-free: no DB, no time
queries, no I/O. The orchestration layer (`app.services.batch_generator`)
loads all inputs and passes them in. This keeps the algorithm trivially
testable — same RNG seed, same inputs → same picks.

Lock filtering: four disjoint sets are accepted as inputs rather than one
merged set so tests can assert exactly which filter rejected which company.
At runtime the caller usually merges them on the way in. Order of precedence
inside the picker doesn't matter — any membership rejects.

Send-time scheduling (UTC for v0):
  - Free tier (cap=7): 1/hour starting 6am → 6am, 7am, ..., 12pm.
  - Paid tier (cap=20): evenly spaced across the 6am-8pm UTC window so paid
    users still sleep. With 20 slots over 14 hours that's ~44 minutes apart.
    Per-user timezone is future work; v0 schedules in server UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from random import Random
from typing import Literal

# Tier alias kept local to avoid a cross-module dep on the Pydantic schema.
Tier = Literal["free", "paid", "super_admin"]

# Send window constants. Lifted to constants so the test suite can import them
# rather than hard-coding times.
SEND_WINDOW_START_HOUR_UTC = 6
PAID_WINDOW_END_HOUR_UTC = 20  # 8pm — paid sends finish by then
MAX_RECIPIENTS_PER_COMPANY = 5  # 1 TO + up to 4 CC


@dataclass(frozen=True, slots=True)
class ContactCandidate:
    """Flat row consumed by the picker. Builder lives in batch_generator."""

    contact_id: int
    company_id: int
    company_domain: str
    email: str
    is_invalid: bool


@dataclass(frozen=True, slots=True)
class CompanyPick:
    """One picked company outreach. Caller turns this into a TodayBatchItem."""

    company_id: int
    company_domain: str
    to_contact_id: int
    cc_contact_ids: list[int]
    send_time: datetime


# ─────────────────────────── send-time scheduling ───────────────────────────


def compute_send_times(batch_date: date, count: int, tier: Tier) -> list[datetime]:
    """Return `count` UTC datetimes for today's send slots.

    Free: 1/hour starting 6am UTC (count<=7 keeps everything within working
    hours; if a future free cap exceeds 7 we'd wrap past noon, which is fine).
    Paid: evenly distributed across 6am-8pm UTC (~14 hours / count). With the
    default paid cap of 20 that's ~44min per slot.
    super_admin: treated as paid for scheduling purposes.
    """
    if count <= 0:
        return []

    start = datetime.combine(
        batch_date, time(SEND_WINDOW_START_HOUR_UTC, 0), tzinfo=UTC
    )

    if tier == "free":
        return [start + timedelta(hours=i) for i in range(count)]

    # paid / super_admin: spread across the configured window.
    window_minutes = (PAID_WINDOW_END_HOUR_UTC - SEND_WINDOW_START_HOUR_UTC) * 60
    if count == 1:
        return [start]
    step = window_minutes / (count - 1) if count > 1 else window_minutes
    return [start + timedelta(minutes=step * i) for i in range(count)]


# ─────────────────────────── picker ───────────────────────────


def _group_by_company(
    candidates: list[ContactCandidate],
) -> dict[int, tuple[str, list[ContactCandidate]]]:
    """Bucket candidates by company_id. Preserves original ordering inside
    each bucket so tests with a fixed input list have predictable behavior
    before RNG-based shuffling."""
    grouped: dict[int, tuple[str, list[ContactCandidate]]] = {}
    for c in candidates:
        if c.is_invalid or not c.email:
            continue
        bucket = grouped.get(c.company_id)
        if bucket is None:
            grouped[c.company_id] = (c.company_domain, [c])
        else:
            bucket[1].append(c)
    return grouped


def pick_companies_for_user(
    *,
    user_id: int,  # accepted for symmetry / logging in caller; unused here
    candidates: list[ContactCandidate],
    cap: int,
    excluded_domains: set[str],
    blocked_user_lock_domains: set[str],
    blocked_platform_permanent_domains: set[str],
    cooldown_domains: set[str],
    batch_date: date,
    tier: Tier,
    rng: Random,
) -> list[CompanyPick]:
    """Pure: returns up to `cap` company picks for one user's daily batch.

    Filters (any membership rejects the company):
      - `excluded_domains`: user's exclusion list
      - `blocked_user_lock_domains`: this user's active 30-day reply locks
      - `blocked_platform_permanent_domains`: explicit-stop permanent locks
      - `cooldown_domains`: active 36h platform cooldowns

    Within an eligible company:
      - sample up to 5 contacts (1 TO + up to 4 CC)
      - randomize the TO pick from the sampled set
      - send_time computed from `compute_send_times(batch_date, len(picks), tier)`

    Deterministic given the same RNG seed + same inputs.
    """
    if cap <= 0:
        return []

    grouped = _group_by_company(candidates)

    blocked = (
        excluded_domains
        | blocked_user_lock_domains
        | blocked_platform_permanent_domains
        | cooldown_domains
    )

    eligible_company_ids: list[int] = [
        company_id
        for company_id, (domain, contacts) in grouped.items()
        if domain not in blocked and contacts
    ]

    # Randomize company order then take the first `cap`.
    rng.shuffle(eligible_company_ids)
    chosen_ids = eligible_company_ids[:cap]

    send_times = compute_send_times(batch_date, len(chosen_ids), tier)

    picks: list[CompanyPick] = []
    for slot_index, company_id in enumerate(chosen_ids):
        domain, contacts = grouped[company_id]

        if len(contacts) > MAX_RECIPIENTS_PER_COMPANY:
            sampled = rng.sample(contacts, MAX_RECIPIENTS_PER_COMPANY)
        else:
            sampled = list(contacts)

        # Random TO pick within the sampled set; remainder are CC.
        to_index = rng.randrange(len(sampled))
        to_contact = sampled[to_index]
        cc_contacts = [c for i, c in enumerate(sampled) if i != to_index]

        picks.append(
            CompanyPick(
                company_id=company_id,
                company_domain=domain,
                to_contact_id=to_contact.contact_id,
                cc_contact_ids=[c.contact_id for c in cc_contacts],
                send_time=send_times[slot_index],
            )
        )

    return picks

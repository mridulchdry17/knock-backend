"""Pure-function tests for the B5.4 picker.

No DB; build ContactCandidate lists inline and assert the picker honors
filters + scheduling + sampling rules deterministically.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from random import Random

from app.services.today_picker import (
    MAX_RECIPIENTS_PER_COMPANY,
    PAID_WINDOW_END_HOUR_UTC,
    SEND_WINDOW_START_HOUR_UTC,
    ContactCandidate,
    compute_send_times,
    pick_companies_for_user,
)

BATCH_DATE = date(2026, 5, 12)


def _candidate(contact_id: int, company_id: int, domain: str) -> ContactCandidate:
    return ContactCandidate(
        contact_id=contact_id,
        company_id=company_id,
        company_domain=domain,
        email=f"u{contact_id}@{domain}",
        is_invalid=False,
    )


def _pool(*specs: tuple[int, str, int]) -> list[ContactCandidate]:
    """Build a candidate pool from (company_id, domain, num_contacts) triples.
    Contact IDs are auto-assigned sequentially."""
    out: list[ContactCandidate] = []
    next_id = 1
    for company_id, domain, n in specs:
        for _ in range(n):
            out.append(_candidate(next_id, company_id, domain))
            next_id += 1
    return out


def _kwargs(**overrides):
    base = dict(
        user_id=1,
        candidates=[],
        cap=7,
        excluded_domains=set(),
        blocked_user_lock_domains=set(),
        blocked_platform_permanent_domains=set(),
        cooldown_domains=set(),
        batch_date=BATCH_DATE,
        tier="free",
        rng=Random(42),
    )
    base.update(overrides)
    return base


# ─────────────────────────── pool size ───────────────────────────


def test_empty_pool_returns_empty() -> None:
    assert pick_companies_for_user(**_kwargs()) == []


def test_pool_larger_than_cap_returns_cap() -> None:
    candidates = _pool(*[(i, f"c{i}.com", 1) for i in range(1, 11)])
    picks = pick_companies_for_user(**_kwargs(candidates=candidates, cap=7))
    assert len(picks) == 7


def test_pool_smaller_than_cap_returns_pool() -> None:
    candidates = _pool((1, "a.com", 1), (2, "b.com", 1), (3, "c.com", 1))
    picks = pick_companies_for_user(**_kwargs(candidates=candidates, cap=7))
    assert len(picks) == 3


# ─────────────────────────── filters ───────────────────────────


def test_all_excluded_returns_empty() -> None:
    candidates = _pool((1, "a.com", 2), (2, "b.com", 2))
    picks = pick_companies_for_user(
        **_kwargs(candidates=candidates, excluded_domains={"a.com", "b.com"})
    )
    assert picks == []


def test_cooldown_filters_out_companies() -> None:
    candidates = _pool((1, "a.com", 1), (2, "b.com", 1))
    picks = pick_companies_for_user(
        **_kwargs(candidates=candidates, cooldown_domains={"a.com"})
    )
    assert len(picks) == 1
    assert picks[0].company_domain == "b.com"


def test_user_lock_filters_out_companies() -> None:
    candidates = _pool((1, "a.com", 1), (2, "b.com", 1))
    picks = pick_companies_for_user(
        **_kwargs(candidates=candidates, blocked_user_lock_domains={"a.com"})
    )
    assert len(picks) == 1
    assert picks[0].company_domain == "b.com"


def test_platform_permanent_filters_out_companies() -> None:
    candidates = _pool((1, "a.com", 1), (2, "b.com", 1))
    picks = pick_companies_for_user(
        **_kwargs(
            candidates=candidates,
            blocked_platform_permanent_domains={"a.com"},
        )
    )
    assert len(picks) == 1
    assert picks[0].company_domain == "b.com"


def test_is_invalid_candidates_skipped() -> None:
    candidates = [
        ContactCandidate(1, 1, "a.com", "x@a.com", is_invalid=True),
        ContactCandidate(2, 2, "b.com", "x@b.com", is_invalid=False),
    ]
    picks = pick_companies_for_user(**_kwargs(candidates=candidates))
    assert len(picks) == 1
    assert picks[0].company_domain == "b.com"


# ─────────────────────────── per-company sampling ───────────────────────────


def test_single_contact_company_is_to_only() -> None:
    candidates = _pool((1, "a.com", 1))
    picks = pick_companies_for_user(**_kwargs(candidates=candidates))
    assert picks[0].cc_contact_ids == []


def test_company_with_seven_contacts_caps_at_one_to_plus_four_cc() -> None:
    candidates = _pool((1, "a.com", 7))
    picks = pick_companies_for_user(**_kwargs(candidates=candidates))
    assert len(picks) == 1
    total_recipients = 1 + len(picks[0].cc_contact_ids)
    assert total_recipients == MAX_RECIPIENTS_PER_COMPANY
    assert len(picks[0].cc_contact_ids) == 4


def test_company_with_two_contacts_is_to_plus_one_cc() -> None:
    candidates = _pool((1, "a.com", 2))
    picks = pick_companies_for_user(**_kwargs(candidates=candidates))
    assert len(picks[0].cc_contact_ids) == 1


def test_to_not_in_cc_list() -> None:
    candidates = _pool((1, "a.com", 5))
    picks = pick_companies_for_user(**_kwargs(candidates=candidates))
    assert picks[0].to_contact_id not in picks[0].cc_contact_ids


# ─────────────────────────── send-time scheduling ───────────────────────────


def test_send_times_free_one_per_hour_from_6am() -> None:
    times = compute_send_times(BATCH_DATE, 7, "free")
    assert len(times) == 7
    start = datetime(2026, 5, 12, SEND_WINDOW_START_HOUR_UTC, 0, tzinfo=UTC)
    assert times[0] == start
    assert times[6] == start + timedelta(hours=6)


def test_send_times_paid_spans_6am_to_8pm() -> None:
    times = compute_send_times(BATCH_DATE, 20, "paid")
    assert len(times) == 20
    start = datetime(
        2026, 5, 12, SEND_WINDOW_START_HOUR_UTC, 0, tzinfo=UTC
    )
    end = datetime(2026, 5, 12, PAID_WINDOW_END_HOUR_UTC, 0, tzinfo=UTC)
    assert times[0] == start
    assert times[-1] == end


def test_picks_have_increasing_send_times() -> None:
    candidates = _pool(*[(i, f"c{i}.com", 1) for i in range(1, 8)])
    picks = pick_companies_for_user(**_kwargs(candidates=candidates, cap=7))
    times = [p.send_time for p in picks]
    assert times == sorted(times)


# ─────────────────────────── determinism + randomness ───────────────────────────


def test_deterministic_with_same_seed() -> None:
    candidates = _pool(*[(i, f"c{i}.com", 3) for i in range(1, 8)])
    picks_a = pick_companies_for_user(
        **_kwargs(candidates=candidates, rng=Random(123))
    )
    picks_b = pick_companies_for_user(
        **_kwargs(candidates=candidates, rng=Random(123))
    )
    assert [p.company_id for p in picks_a] == [p.company_id for p in picks_b]
    assert [p.to_contact_id for p in picks_a] == [p.to_contact_id for p in picks_b]


def test_different_seeds_produce_different_picks() -> None:
    candidates = _pool(*[(i, f"c{i}.com", 1) for i in range(1, 21)])
    picks_a = pick_companies_for_user(
        **_kwargs(candidates=candidates, cap=7, rng=Random(1))
    )
    picks_b = pick_companies_for_user(
        **_kwargs(candidates=candidates, cap=7, rng=Random(99))
    )
    # With 20 companies and cap=7, two different seeds should diverge on
    # at least one company. Probability of full match is astronomically low.
    assert {p.company_id for p in picks_a} != {p.company_id for p in picks_b}


def test_company_order_is_randomized() -> None:
    """Same input pool but different seeds → different orderings on cap-limited slices."""
    candidates = _pool(*[(i, f"c{i}.com", 1) for i in range(1, 11)])
    seeds_seen: set[tuple[int, ...]] = set()
    for seed in range(20):
        picks = pick_companies_for_user(
            **_kwargs(candidates=candidates, cap=10, rng=Random(seed))
        )
        seeds_seen.add(tuple(p.company_id for p in picks))
    # Across 20 seeds we expect more than 1 unique permutation.
    assert len(seeds_seen) > 1

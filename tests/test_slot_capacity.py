"""Tests for the booking-conflict checker.

Each test is named after the failure it prevents.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from patterns.slot_capacity import (
    CapacityLedger,
    ConflictError,
    NotBookable,
    Override,
    Placement,
    Product,
    UnknownProduct,
    scope_key_for,
)

MON = date(2026, 3, 9)
WED = date(2026, 3, 11)
FRI = date(2026, 3, 13)
NEXT_MON = date(2026, 3, 16)
NOW = datetime(2026, 3, 1, 12, 0)

PER_ISSUE = Product(id="standard_slot", cap=2, cap_scope="issue", weekdays=frozenset({1, 3, 5}))
PER_WEEK = Product(id="feature_slot", cap=1, cap_scope="week", weekdays=frozenset({1, 3, 5}))


def ledger() -> CapacityLedger:
    return CapacityLedger([PER_ISSUE, PER_WEEK])


# ---- scope keys ---------------------------------------------------------


def test_per_issue_scope_key_is_the_date():
    assert scope_key_for(PER_ISSUE, WED) == "2026-03-11"


def test_per_week_scope_key_collapses_the_whole_week():
    assert scope_key_for(PER_WEEK, MON) == scope_key_for(PER_WEEK, FRI) == "2026-W11"
    assert scope_key_for(PER_WEEK, NEXT_MON) == "2026-W12"


def test_iso_week_key_does_not_break_across_the_year_boundary():
    # 2025-12-29 is a Monday in ISO week 2026-W01, which a naive
    # "%Y-W%W" on the calendar year would file under 2025.
    assert scope_key_for(PER_WEEK, date(2025, 12, 29)) == "2026-W01"
    assert scope_key_for(PER_WEEK, date(2026, 1, 2)) == "2026-W01"


# ---- the per-week cap ---------------------------------------------------


def test_per_week_product_cannot_be_sold_twice_in_one_week_on_different_days():
    lg = ledger()
    lg.hold_cart([("feature_slot", MON)], NOW, timedelta(minutes=45))
    with pytest.raises(ConflictError):
        lg.hold_cart([("feature_slot", FRI)], NOW, timedelta(minutes=45))


def test_per_week_product_sells_again_the_following_week():
    lg = ledger()
    lg.hold_cart([("feature_slot", MON)], NOW, timedelta(minutes=45))
    lg.hold_cart([("feature_slot", NEXT_MON)], NOW, timedelta(minutes=45))
    assert lg.active_count("feature_slot", NEXT_MON, NOW) == 1


def test_per_issue_product_sells_on_each_day_up_to_its_cap():
    lg = ledger()
    lg.hold_cart([("standard_slot", MON), ("standard_slot", MON)], NOW, timedelta(minutes=45))
    lg.hold_cart([("standard_slot", WED)], NOW, timedelta(minutes=45))
    assert lg.seats_available("standard_slot", MON, NOW) == 0
    assert lg.seats_available("standard_slot", WED, NOW) == 1


# ---- cart grouping ------------------------------------------------------


def test_a_single_cart_cannot_oversell_one_slot():
    """The bug: checking cart lines one at a time. Two lines each see a free
    seat against a cap of one and both pass."""
    lg = ledger()
    with pytest.raises(ConflictError) as exc:
        lg.check_cart([("feature_slot", MON), ("feature_slot", MON)], NOW)
    assert exc.value.requested == 2
    assert exc.value.available == 1


def test_cart_grouping_spans_the_scope_not_the_date():
    """Two per-week lines on DIFFERENT days of the same week are still one
    slot and must be measured together."""
    lg = ledger()
    with pytest.raises(ConflictError):
        lg.check_cart([("feature_slot", MON), ("feature_slot", FRI)], NOW)


def test_a_failed_cart_reserves_nothing():
    lg = ledger()
    with pytest.raises(ConflictError):
        lg.hold_cart(
            [("standard_slot", MON), ("feature_slot", MON), ("feature_slot", FRI)],
            NOW,
            timedelta(minutes=45),
        )
    assert lg.placements() == []


# ---- holds --------------------------------------------------------------


def test_an_unexpired_hold_occupies_the_slot():
    lg = ledger()
    lg.hold_cart([("feature_slot", MON)], NOW, timedelta(minutes=45))
    assert lg.seats_available("feature_slot", MON, NOW + timedelta(minutes=44)) == 0


def test_a_lapsed_hold_frees_the_slot():
    lg = ledger()
    lg.hold_cart([("feature_slot", MON)], NOW, timedelta(minutes=45))
    assert lg.seats_available("feature_slot", MON, NOW + timedelta(minutes=46)) == 1


def test_hold_expiry_is_exclusive_at_the_boundary():
    lg = ledger()
    lg.hold_cart([("feature_slot", MON)], NOW, timedelta(minutes=45))
    # At exactly the expiry instant the hold is over, not still running.
    assert lg.seats_available("feature_slot", MON, NOW + timedelta(minutes=45)) == 1


# ---- statuses -----------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["booked", "content_received", "approved", "locked", "live", "reported", "missed_lock"],
)
def test_occupying_statuses_consume_a_seat(status):
    lg = ledger()
    lg.add(Placement("feature_slot", MON, status))
    assert lg.seats_available("feature_slot", MON, NOW) == 0


@pytest.mark.parametrize("status", ["draft", "expired", "cancelled"])
def test_released_statuses_free_the_seat(status):
    lg = ledger()
    lg.add(Placement("feature_slot", MON, status))
    assert lg.seats_available("feature_slot", MON, NOW) == 1


def test_missed_deadline_is_not_a_free_seat():
    """A sponsor who missed the content deadline still owns the slot. Treating
    that as available resells an issue position that is already committed."""
    lg = ledger()
    lg.add(Placement("standard_slot", MON, "missed_lock"))
    lg.add(Placement("standard_slot", MON, "booked"))
    with pytest.raises(ConflictError):
        lg.check_cart([("standard_slot", MON)], NOW)


# ---- overrides ----------------------------------------------------------


def test_override_raises_the_cap_for_one_slot_only():
    lg = ledger()
    lg.set_override("standard_slot", MON, Override(cap_override=3))
    lg.hold_cart([("standard_slot", MON)] * 3, NOW, timedelta(minutes=45))
    assert lg.effective_cap("standard_slot", WED) == 2


def test_closed_beats_a_cap_override():
    lg = ledger()
    lg.set_override("standard_slot", MON, Override(cap_override=5, closed=True))
    assert lg.effective_cap("standard_slot", MON) == 0
    with pytest.raises(ConflictError):
        lg.check_cart([("standard_slot", MON)], NOW)


def test_override_on_a_per_week_product_covers_the_whole_week():
    lg = ledger()
    lg.set_override("feature_slot", WED, Override(closed=True))
    assert lg.effective_cap("feature_slot", MON) == 0
    assert lg.effective_cap("feature_slot", FRI) == 0
    assert lg.effective_cap("feature_slot", NEXT_MON) == 1


# ---- catalog rules ------------------------------------------------------


def test_a_product_cannot_be_booked_onto_a_day_it_does_not_publish():
    lg = ledger()
    tuesday = date(2026, 3, 10)
    with pytest.raises(NotBookable) as exc:
        lg.check_cart([("standard_slot", tuesday)], NOW)
    assert "weekday" in exc.value.reason


def test_not_bookable_is_distinguishable_from_sold_out():
    """An operator told "sold out" waits for a cancellation. An operator told
    "we never run this on a Tuesday" does something else entirely."""
    lg = ledger()
    tuesday = date(2026, 3, 10)
    lg.hold_cart([("feature_slot", MON)], NOW, timedelta(minutes=45))

    with pytest.raises(NotBookable):
        lg.check_cart([("standard_slot", tuesday)], NOW)
    with pytest.raises(ConflictError):
        lg.check_cart([("feature_slot", MON)], NOW)


def test_an_inactive_product_cannot_be_booked():
    lg = CapacityLedger([Product(id="retired_slot", cap=1, cap_scope="issue", active=False)])
    with pytest.raises(NotBookable) as exc:
        lg.check_cart([("retired_slot", MON)], NOW)
    assert "active" in exc.value.reason


def test_an_unknown_product_is_rejected_by_name():
    lg = ledger()
    with pytest.raises(UnknownProduct):
        lg.check_cart([("no_such_slot", MON)], NOW)


def test_zero_cap_product_is_always_full():
    lg = CapacityLedger([Product(id="sold_out", cap=0, cap_scope="issue")])
    with pytest.raises(ConflictError):
        lg.check_cart([("sold_out", MON)], NOW)


def test_empty_cart_is_a_no_op():
    lg = ledger()
    lg.check_cart([], NOW)
    assert lg.hold_cart([], NOW, timedelta(minutes=45)) == []


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(minutes=-5)])
def test_a_non_positive_hold_duration_is_rejected(duration):
    """A hold that expires the instant it is created is a capacity leak wearing
    a reservation's clothes."""
    lg = ledger()
    with pytest.raises(ValueError):
        lg.hold_cart([("feature_slot", MON)], NOW, duration)

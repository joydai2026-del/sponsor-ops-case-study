"""Tests for rep holds.

The first two tests are the shipped bugs that made these rules explicit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from patterns.rep_holds import HoldDesk, HoldError, MaxHoldReached, SlotGone, MAX_HOLD_AGE
from patterns.slot_capacity import CapacityLedger, ConflictError, NotBookable, Placement, Product

MON = date(2026, 3, 9)
WED = date(2026, 3, 11)
FRI = date(2026, 3, 13)
NOW = datetime(2026, 3, 1, 12, 0)

PER_ISSUE = Product(id="standard_slot", cap=2, cap_scope="issue", weekdays=frozenset({1, 3, 5}))
PER_WEEK = Product(id="feature_slot", cap=1, cap_scope="week", weekdays=frozenset({1, 3, 5}))
# A per-week product with room for two, so one order can legitimately hold two
# seats in the same week on different days. That is the shape the scope-key
# grouping bug needed to show itself.
PER_WEEK_PAIR = Product(id="weekly_pair", cap=2, cap_scope="week", weekdays=frozenset({1, 3, 5}))


def desk() -> tuple[HoldDesk, CapacityLedger]:
    ledger = CapacityLedger([PER_ISSUE, PER_WEEK, PER_WEEK_PAIR])
    return HoldDesk(ledger), ledger


# ---- extend means add ---------------------------------------------------


def test_extending_adds_to_the_remaining_time_it_does_not_reset_it():
    """The shipped bug: extend wrote `now + delta`, so pressing "extend by 72h"
    on a hold with 60 hours left produced 72 hours, a 12-hour gain that the UI
    reported as a success."""
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(hours=72))
    later = NOW + timedelta(hours=12)  # 60 hours still on the clock

    (hold,) = d.extend("order-1", timedelta(hours=72), later)

    assert hold.expires_at == NOW + timedelta(hours=144)


def test_an_extension_that_buys_no_time_is_refused_not_silently_accepted():
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, MAX_HOLD_AGE)
    with pytest.raises(MaxHoldReached):
        d.extend("order-1", timedelta(hours=72), NOW + timedelta(hours=1))


# ---- the ceiling --------------------------------------------------------


def test_extension_is_clamped_to_the_ceiling_measured_from_creation():
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=10))
    (hold,) = d.extend("order-1", timedelta(days=30), NOW + timedelta(days=1))
    assert hold.expires_at == NOW + MAX_HOLD_AGE


def test_repeated_extensions_cannot_hold_a_slot_forever():
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    clock = NOW
    for _ in range(4):
        clock += timedelta(days=3)
        try:
            d.extend("order-1", timedelta(days=3), clock)
        except MaxHoldReached:
            break
    (hold,) = d.holds_for("order-1")
    assert hold.expires_at <= NOW + MAX_HOLD_AGE


# ---- lapsed holds must re-win the slot ----------------------------------


def test_a_lapsed_hold_revives_when_the_slot_is_still_free():
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=1))
    after_lapse = NOW + timedelta(days=2)

    (hold,) = d.extend("order-1", timedelta(days=3), after_lapse)

    assert hold.expires_at == after_lapse + timedelta(days=3)
    assert not hold.lapsed(after_lapse)


def test_a_lapsed_hold_cannot_revive_onto_a_slot_somebody_else_took():
    d, ledger = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=1))
    after_lapse = NOW + timedelta(days=2)
    ledger.add(Placement("feature_slot", MON, "booked"))  # sold in the meantime

    with pytest.raises(SlotGone) as exc:
        d.extend("order-1", timedelta(days=3), after_lapse)
    assert exc.value.product_id == "feature_slot"


def test_two_lapsed_holds_on_one_slot_cannot_both_revive_into_one_free_seat():
    """Checked as a group, not one at a time: two independent "is a seat free"
    checks both pass against a single seat."""
    d, ledger = desk()
    d.place("order-1", [("standard_slot", MON), ("standard_slot", MON)], NOW, timedelta(days=1))
    after_lapse = NOW + timedelta(days=2)
    ledger.add(Placement("standard_slot", MON, "booked"))  # 1 of 2 seats gone

    with pytest.raises(SlotGone):
        d.extend("order-1", timedelta(days=3), after_lapse)


def test_a_failed_extension_leaves_every_hold_untouched():
    """Two-pass: validate all, then mutate. A one-pass version extends the
    first slots and then errors, leaving the order half-extended."""
    d, ledger = desk()
    d.place(
        "order-1",
        [("standard_slot", MON), ("feature_slot", MON)],
        NOW,
        timedelta(days=1),
    )
    after_lapse = NOW + timedelta(days=2)
    ledger.add(Placement("feature_slot", MON, "booked"))
    before = {(h.product_id, h.expires_at) for h in d.holds_for("order-1")}

    with pytest.raises(SlotGone):
        d.extend("order-1", timedelta(days=3), after_lapse)

    assert {(h.product_id, h.expires_at) for h in d.holds_for("order-1")} == before


def test_a_lapsed_hold_past_the_ceiling_cannot_be_revived_at_all():
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=1))
    with pytest.raises(MaxHoldReached):
        d.extend("order-1", timedelta(days=3), NOW + MAX_HOLD_AGE + timedelta(days=1))


# ---- holds and capacity -------------------------------------------------


def test_a_rep_hold_consumes_capacity_like_any_other_reservation():
    d, ledger = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    assert ledger.seats_available("feature_slot", MON, NOW) == 0


def test_a_rep_hold_cannot_be_placed_on_a_full_slot():
    d, ledger = desk()
    ledger.add(Placement("feature_slot", MON, "booked"))
    with pytest.raises(ConflictError):
        d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))


def test_a_rep_hold_cannot_be_placed_on_a_day_the_product_does_not_publish():
    d, _ = desk()
    tuesday = date(2026, 3, 10)
    with pytest.raises(NotBookable):
        d.place("order-1", [("standard_slot", tuesday)], NOW, timedelta(days=3))


def test_releasing_a_hold_frees_the_slot_immediately():
    d, ledger = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    assert d.release("order-1") == 1
    assert ledger.seats_available("feature_slot", MON, NOW) == 1


def test_release_is_idempotent():
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    assert d.release("order-1") == 1
    assert d.release("order-1") == 0


# ---- argument guards ----------------------------------------------------


def test_extending_an_order_with_no_holds_is_an_error():
    d, _ = desk()
    with pytest.raises(HoldError):
        d.extend("no-such-order", timedelta(days=1), NOW)


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(hours=-1)])
def test_a_non_positive_extension_is_rejected(delta):
    d, _ = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    with pytest.raises(ValueError):
        d.extend("order-1", delta, NOW)


# ---- lapsed holds are grouped by SCOPE, not by date ---------------------


def test_two_lapsed_holds_in_one_week_on_different_days_are_one_slot():
    """Regression: grouping lapsed holds by raw date instead of by capacity
    scope key. For a per-week product, Monday and Friday of the same week are
    ONE slot. Grouped by date, each hold independently sees the single free
    seat and both revive into it."""
    d, ledger = desk()
    d.place(
        "order-1",
        [("weekly_pair", MON), ("weekly_pair", FRI)],
        NOW,
        timedelta(days=1),
    )
    after_lapse = NOW + timedelta(days=2)
    ledger.add(Placement("weekly_pair", WED, "booked"))  # 1 of the 2 week seats gone

    assert ledger.seats_available("weekly_pair", MON, after_lapse) == 1
    with pytest.raises(SlotGone):
        d.extend("order-1", timedelta(days=3), after_lapse)


def test_lapsed_holds_in_one_week_revive_when_the_week_has_room_for_all():
    d, _ = desk()
    d.place(
        "order-1",
        [("weekly_pair", MON), ("weekly_pair", FRI)],
        NOW,
        timedelta(days=1),
    )
    after_lapse = NOW + timedelta(days=2)
    revived = d.extend("order-1", timedelta(days=3), after_lapse)
    assert len(revived) == 2
    assert all(not h.lapsed(after_lapse) for h in revived)


# ---- the ceiling binds at creation too ---------------------------------


def test_a_hold_cannot_be_opened_longer_than_the_ceiling():
    """Regression: the ceiling was only enforced on extend, so an opening hold
    of 30 days simply sat past it. A ceiling that one code path can walk around
    is not a ceiling."""
    d, _ = desk()
    with pytest.raises(MaxHoldReached):
        d.place("order-1", [("feature_slot", MON)], NOW, MAX_HOLD_AGE + timedelta(days=1))


def test_a_hold_may_open_exactly_at_the_ceiling():
    d, _ = desk()
    (hold,) = d.place("order-1", [("feature_slot", MON)], NOW, MAX_HOLD_AGE)
    assert hold.expires_at == NOW + MAX_HOLD_AGE


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(hours=-1)])
def test_a_non_positive_hold_duration_is_rejected(duration):
    d, _ = desk()
    with pytest.raises(ValueError):
        d.place("order-1", [("feature_slot", MON)], NOW, duration)


def test_a_refused_hold_reserves_no_capacity():
    d, ledger = desk()
    with pytest.raises(MaxHoldReached):
        d.place("order-1", [("feature_slot", MON)], NOW, MAX_HOLD_AGE * 2)
    assert ledger.seats_available("feature_slot", MON, NOW) == 1


# ---- documented behavior of release + extend ---------------------------


def test_a_released_hold_can_be_revived_by_extend_if_it_re_wins_the_slot():
    """Release frees the seat immediately; extend is the deliberate way back,
    and it goes through the same capacity check as any other revival."""
    d, ledger = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    d.release("order-1")
    assert ledger.seats_available("feature_slot", MON, NOW) == 1

    (hold,) = d.extend("order-1", timedelta(days=2), NOW)
    assert not hold.released
    assert ledger.seats_available("feature_slot", MON, NOW) == 0


def test_a_released_hold_cannot_be_revived_onto_a_taken_slot():
    d, ledger = desk()
    d.place("order-1", [("feature_slot", MON)], NOW, timedelta(days=3))
    d.release("order-1")
    ledger.add(Placement("feature_slot", MON, "booked"))
    with pytest.raises(SlotGone):
        d.extend("order-1", timedelta(days=2), NOW)

"""Tests for volume-tier pricing and the partial-cancel reprice.

Placeholder catalog (minor units):
    feature_slot   100_00 flat, never discounts
    standard_slot   80_00 / 70_00 / 60_00
    listing_slot    50_00 / 45_00 / 40_00
    bundle_slot     30_00 flat, never discounts
"""

from __future__ import annotations

import pytest

from patterns.tiered_pricing import (
    UnknownAddon,
    UnknownProduct,
    cancel_quote,
    quote,
    tier_for,
)


# ---- tiers --------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [(0, "1"), (1, "1"), (2, "2_3"), (3, "2_3"), (4, "4_plus"), (10, "4_plus")],
)
def test_tier_bands(count, expected):
    assert tier_for(count) == expected


def test_a_single_slot_pays_list():
    q = quote(["standard_slot"])
    assert q.tier == "1"
    assert q.total_cents == 80_00
    assert q.savings_cents == 0


def test_volume_reprices_every_line_not_just_the_last_one():
    q = quote(["standard_slot"] * 4)
    assert q.tier == "4_plus"
    assert [ln.curve_cents for ln in q.lines] == [60_00] * 4
    assert q.total_cents == 240_00
    assert q.savings_cents == 80_00


def test_a_flat_priced_product_never_discounts_even_in_a_large_cart():
    q = quote(["feature_slot"] * 5)
    assert q.tier == "4_plus"
    assert q.total_cents == 500_00
    assert q.savings_cents == 0


def test_the_tier_is_shared_across_mixed_products():
    """Four slots of any mix reach the volume band, and each product then
    prices at its own volume rate."""
    q = quote(["standard_slot", "standard_slot", "listing_slot", "feature_slot"])
    assert q.tier == "4_plus"
    assert q.total_cents == 60_00 + 60_00 + 40_00 + 100_00


# ---- add-ons ------------------------------------------------------------


def test_addons_are_priced_at_face_value_and_do_not_discount():
    q = quote(["standard_slot"] * 4, ["copywriting"])
    assert q.addons_total_cents == 20_00
    assert q.total_cents == 240_00 + 20_00


def test_addons_do_not_count_toward_the_volume_tier():
    """Otherwise a cheap extra could tip a three-slot cart into volume pricing
    on the slots."""
    q = quote(["standard_slot"] * 3, ["copywriting", "social_pair"])
    assert q.tier == "2_3"
    assert q.slots_total_cents == 210_00


def test_a_zero_priced_addon_is_a_flag_not_a_charge():
    q = quote(["standard_slot"], ["followup_only"])
    assert q.addons_total_cents == 0
    assert q.total_cents == 80_00


# ---- the partial-cancel reprice ----------------------------------------


def test_cancelling_below_a_volume_band_gives_back_the_discount():
    """THE rule. Buy four at the volume rate, cancel two: the two kept slots
    reprice at the two-slot rate. Refunding "what the cancelled lines cost"
    would leave the buyer on volume pricing for a two-slot order."""
    cq = cancel_quote(["standard_slot"] * 4, ["standard_slot"] * 2)

    assert cq.paid_cents == 240_00           # 4 x 60_00
    assert cq.remaining_cents == 140_00      # 2 x 70_00, repriced at the 2_3 tier
    assert cq.refund_cents == 100_00
    # The naive refund would have been 2 x 60_00 = 120_00, overpaying by 20_00
    # and leaving volume pricing on a cart that no longer qualifies for it.


def test_cancelling_within_the_same_band_is_a_plain_line_refund():
    cq = cancel_quote(["standard_slot"] * 5, ["standard_slot"] * 4)
    assert cq.paid_cents == 300_00
    assert cq.remaining_cents == 240_00
    assert cq.refund_cents == 60_00


def test_cancelling_everything_refunds_everything():
    cq = cancel_quote(["standard_slot"] * 4, [])
    assert cq.refund_cents == cq.paid_cents == 240_00


def test_the_reprice_can_shrink_a_refund_well_below_the_cancelled_line():
    """Dropping one slot from a four-slot cart claws back the volume discount
    on the three kept slots, so the refund is much smaller than the 40_00 the
    cancelled line cost. The function returns paid and remaining separately so
    an operator can show a buyer why."""
    cq = cancel_quote(["listing_slot"] * 4, ["listing_slot"] * 3)
    assert cq.paid_cents == 160_00        # 4 x 40_00
    assert cq.remaining_cents == 135_00   # 3 x 45_00
    assert cq.refund_cents == 25_00


def test_addons_survive_a_partial_cancel_by_default():
    cq = cancel_quote(["standard_slot"] * 4, ["standard_slot"] * 2, ["copywriting"])
    assert cq.paid_cents == 260_00
    assert cq.remaining_cents == 160_00
    assert cq.refund_cents == 100_00


def test_addons_attached_to_a_cancelled_slot_can_be_dropped_explicitly():
    cq = cancel_quote(
        ["standard_slot"] * 4,
        ["standard_slot"] * 2,
        addons=["copywriting"],
        remaining_addons=[],
    )
    assert cq.refund_cents == 120_00


def test_a_remaining_cart_that_was_never_bought_is_rejected():
    with pytest.raises(ValueError):
        cancel_quote(["standard_slot"] * 2, ["standard_slot"] * 3)

    with pytest.raises(ValueError):
        cancel_quote(["standard_slot"], ["feature_slot"])


# ---- purity and validation ---------------------------------------------


def test_the_quote_function_is_pure():
    a = quote(["standard_slot", "listing_slot"], ["copywriting"])
    b = quote(["standard_slot", "listing_slot"], ["copywriting"])
    assert a == b


def test_an_unknown_product_is_named_in_the_error():
    with pytest.raises(UnknownProduct) as exc:
        quote(["standard_slot", "no_such_slot"])
    assert "no_such_slot" in str(exc.value)


def test_an_unknown_addon_is_named_in_the_error():
    with pytest.raises(UnknownAddon) as exc:
        quote(["standard_slot"], ["gold_plating"])
    assert "gold_plating" in str(exc.value)


def test_an_empty_cart_quotes_to_zero():
    q = quote([])
    assert q.total_cents == 0
    assert q.lines == ()


def test_a_kept_addon_that_was_never_bought_is_rejected():
    """Regression: only the products were validated against the original
    order. A caller could "keep" an add-on nobody bought, inflating the
    remaining total and shrinking the refund."""
    with pytest.raises(ValueError):
        cancel_quote(
            ["standard_slot"] * 4,
            ["standard_slot"] * 2,
            addons=["copywriting"],
            remaining_addons=["copywriting", "social_pair"],
        )


def test_keeping_a_subset_of_the_original_addons_is_allowed():
    cq = cancel_quote(
        ["standard_slot"] * 4,
        ["standard_slot"] * 2,
        addons=["copywriting", "social_pair"],
        remaining_addons=["copywriting"],
    )
    assert cq.paid_cents == 240_00 + 20_00 + 15_00
    assert cq.remaining_cents == 140_00 + 20_00

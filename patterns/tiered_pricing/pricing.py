"""Volume-tier pricing, and the refund rule that stops a partial cancel from
becoming a discount.

EXTRACTED PATTERN. Standalone rewrite. See ../../README.md.

ALL PRICES BELOW ARE INVENTED PLACEHOLDERS chosen to make the arithmetic in the
tests easy to read. They are not anybody's rate card. The pattern is the tier
curve and the cancel rule, not the numbers.

The problem
-----------
Buy one placement, pay list. Buy four, every line drops to the volume price.
Some products never discount at any volume. Write that as a dict of dicts and
it is unremarkable. The interesting part is what happens on a partial cancel.

A buyer takes four placements and pays the four-plus rate on all of them. They
then cancel two. If the refund is "give back what those two lines cost", the
buyer keeps volume pricing on a two-placement order, which is a discount nobody
approved and which no amount of care at the checkout screen would catch.

The rule that closes it: a refund is

    what was paid  -  (what remains, REPRICED at the tier it now qualifies for)

so cancelling into a smaller order correctly costs the buyer their volume
discount on the lines they kept. Under this rule a cancel can even produce a
refund of zero or, in principle, a balance owed. That is the arithmetic being
honest, and it is why the function returns the components rather than only the
final number: an operator has to be able to see why.

Two smaller rules that matter as much in practice:

*   **The pricing engine is pure.** No I/O, no clock, no database. Every price
    a buyer sees on the checkout screen, every price written to the ledger, and
    every price used in a refund comes from this one function family. Duplicated
    pricing logic between the storefront and the back office is the classic way
    an invoice ends up disagreeing with the receipt.
*   **Add-ons never discount and never count toward the tier.** They are priced
    at face value and excluded from the tier calculation, so adding a cheap
    extra cannot tip a cart into a volume band.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

Tier = Literal["1", "2_3", "4_plus"]
TIERS: tuple[Tier, ...] = ("1", "2_3", "4_plus")


class UnknownProduct(Exception):
    """A cart line named a product that is not in the catalog."""


class UnknownAddon(Exception):
    """A cart named an add-on that is not in the catalog."""


# Placeholder catalog. Amounts are in minor units (cents). "flat" products are
# ones the business never discounts; encoding that as identical tier prices
# rather than a special case keeps the quote function free of branches.
CATALOG: dict[str, dict[Tier, int]] = {
    "feature_slot": {"1": 100_00, "2_3": 100_00, "4_plus": 100_00},  # never discounts
    "standard_slot": {"1": 80_00, "2_3": 70_00, "4_plus": 60_00},
    "listing_slot": {"1": 50_00, "2_3": 45_00, "4_plus": 40_00},
    "bundle_slot": {"1": 30_00, "2_3": 30_00, "4_plus": 30_00},  # never discounts
}

ADDONS: dict[str, int] = {
    "copywriting": 20_00,
    "social_pair": 15_00,
    "followup_only": 0,  # a flag, never a charged line
}

LIST_PRICES: dict[str, int] = {p: prices["1"] for p, prices in CATALOG.items()}


@dataclass(frozen=True)
class Line:
    product: str
    list_cents: int
    curve_cents: int

    @property
    def savings_cents(self) -> int:
        return self.list_cents - self.curve_cents


@dataclass(frozen=True)
class AddonLine:
    kind: str
    amount_cents: int


@dataclass(frozen=True)
class Quote:
    tier: Tier
    lines: tuple[Line, ...]
    addons: tuple[AddonLine, ...]
    slots_total_cents: int
    addons_total_cents: int
    list_total_cents: int

    @property
    def total_cents(self) -> int:
        return self.slots_total_cents + self.addons_total_cents

    @property
    def savings_cents(self) -> int:
        return self.list_total_cents - self.slots_total_cents


@dataclass(frozen=True)
class CancelQuote:
    paid_cents: int
    remaining_cents: int

    @property
    def refund_cents(self) -> int:
        return self.paid_cents - self.remaining_cents


def tier_for(slot_count: int) -> Tier:
    """Volume band by number of SLOTS, add-ons excluded."""
    if slot_count <= 1:
        return "1"
    if slot_count <= 3:
        return "2_3"
    return "4_plus"


def quote(products: Iterable[str], addons: Iterable[str] = ()) -> Quote:
    """Price a cart. Pure: same input, same output, forever.

    `products` is one entry per slot, so buying three of the same product is
    three entries. That keeps the tier count and the line count identical by
    construction, which removes a whole class of "quantity vs lines" bug.
    """
    products = list(products)
    addons = list(addons)

    unknown = sorted({p for p in products if p not in CATALOG})
    if unknown:
        raise UnknownProduct(", ".join(unknown))
    unknown_addons = sorted({a for a in addons if a not in ADDONS})
    if unknown_addons:
        raise UnknownAddon(", ".join(unknown_addons))

    tier = tier_for(len(products))
    lines = tuple(
        Line(product=p, list_cents=LIST_PRICES[p], curve_cents=CATALOG[p][tier])
        for p in products
    )
    addon_lines = tuple(AddonLine(kind=a, amount_cents=ADDONS[a]) for a in addons)

    return Quote(
        tier=tier,
        lines=lines,
        addons=addon_lines,
        slots_total_cents=sum(ln.curve_cents for ln in lines),
        addons_total_cents=sum(a.amount_cents for a in addon_lines),
        list_total_cents=sum(ln.list_cents for ln in lines),
    )


def cancel_quote(
    original: Iterable[str],
    remaining: Iterable[str],
    addons: Iterable[str] = (),
    remaining_addons: Iterable[str] | None = None,
) -> CancelQuote:
    """Refund for a partial cancel.

    Refund = paid - (remaining cart repriced at ITS new tier).

    The second term is the entire point. Pricing the remaining cart at the
    tier it now qualifies for means a buyer who drops below a volume band
    gives back the discount on the lines they kept, instead of keeping
    four-slot pricing on a two-slot order.

    `remaining_addons` defaults to the original add-ons, i.e. add-ons survive
    the cancel. Pass an explicit list when add-ons attached to the cancelled
    slots go away with them.
    """
    original = list(original)
    remaining = list(remaining)
    addons = list(addons)

    # The remaining cart must be a sub-multiset of the original; otherwise the
    # caller has passed a cart that was never bought and the refund is fiction.
    original_counts: dict[str, int] = {}
    for p in original:
        original_counts[p] = original_counts.get(p, 0) + 1
    for p in remaining:
        original_counts[p] = original_counts.get(p, 0) - 1
        if original_counts[p] < 0:
            raise ValueError(
                f"remaining cart contains {p!r} that was not in the original cart"
            )

    kept_addons = addons if remaining_addons is None else list(remaining_addons)
    # The kept add-ons must also be a sub-multiset of what was bought. Without
    # this a caller can "keep" an add-on that was never purchased, inflating the
    # remaining total and shrinking (or inverting) the refund.
    addon_counts: dict[str, int] = {}
    for a in addons:
        addon_counts[a] = addon_counts.get(a, 0) + 1
    for a in kept_addons:
        addon_counts[a] = addon_counts.get(a, 0) - 1
        if addon_counts[a] < 0:
            raise ValueError(
                f"remaining add-ons contain {a!r} that was not in the original order"
            )

    paid = quote(original, addons)
    kept = quote(remaining, kept_addons)
    return CancelQuote(paid_cents=paid.total_cents, remaining_cents=kept.total_cents)

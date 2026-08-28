"""Salesperson holds: a reservation that can be extended, and that has to win
its slot back if it ever lapsed.

EXTRACTED PATTERN. Standalone in-memory rewrite. The production version is a
PostgreSQL function; see ../../README.md.

The problem
-----------
Self-serve checkout holds inventory for minutes while a card is entered. A
salesperson working a deal holds it for days, while a contract moves. Both are
holds and both consume capacity, but they obey different rules:

    checkout hold   short, capped per browser session, cannot be extended
    rep hold        long, created by an authenticated operator, extendable

Three rules make rep holds safe:

1.  **Extend means add, not reset.** A hold with 60 hours left, extended by 72,
    should end up with 132 hours, not 72. The first implementation wrote
    `now() + hours`, which meant the "extend by 72h" button applied to a
    three-day hold did approximately nothing, and no one noticed because the
    button reported success. Extension is `max(now, current_expiry) + delta`,
    which also does the right thing for a hold that already lapsed.

2.  **A ceiling measured from creation, not from the last extension.** Without
    it, a slot can be held forever in 72-hour increments and never sold. The
    ceiling is absolute: `created_at + MAX_HOLD_AGE`. An extension that would
    cross it is clamped, and an extension that buys no time at all is refused
    rather than silently succeeding.

3.  **A lapsed hold does not get its slot back for free.** Once a hold expires,
    the seat is available and somebody else may have taken it. Reviving a
    lapsed hold therefore has to re-win capacity as if booking fresh. A revive
    that would oversell is refused with the specific slot that is gone.

The third rule has a subtlety that the two-pass structure exists for: an order
can span several slots. If validation and mutation are interleaved, an order
that fails on its fourth slot has already extended its first three, leaving it
half-extended with an error returned to the caller. So this validates every
slot first and mutates only after all of them pass, which is the in-memory
equivalent of doing the whole thing inside one database transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Literal

from patterns.slot_capacity import CapacityLedger, ConflictError, Placement, scope_key_for

HoldKind = Literal["checkout", "rep"]

# Absolute ceiling on how long a slot can stay reserved, measured from when the
# hold was first created. Deliberately a constant here; in production it is a
# configuration row, because "how long may a rep sit on inventory" is a
# commercial policy question and not an engineering one.
MAX_HOLD_AGE = timedelta(days=14)


class HoldError(Exception):
    """Base class for refusals from the hold desk."""


class MaxHoldReached(HoldError):
    """The extension would buy no additional time under the ceiling."""


class SlotGone(HoldError):
    """A lapsed hold could not re-win its slot; somebody else took it."""

    def __init__(self, product_id: str, run_date: date):
        self.product_id = product_id
        self.run_date = run_date
        super().__init__(f"{product_id} on {run_date.isoformat()} is no longer available")


@dataclass
class Hold:
    """One reserved slot inside an order."""

    order_id: str
    product_id: str
    run_date: date
    created_at: datetime
    expires_at: datetime
    kind: HoldKind = "rep"
    released: bool = False
    # The capacity row this hold occupies. Kept as a direct reference so hold
    # lifecycle and capacity can never drift apart.
    placement: Placement | None = field(default=None, repr=False, compare=False)

    def lapsed(self, now: datetime) -> bool:
        return self.released or self.expires_at <= now

    def ceiling(self) -> datetime:
        return self.created_at + MAX_HOLD_AGE


class HoldDesk:
    """Rep holds layered on top of a capacity ledger.

    The desk owns hold lifecycle. It does not own capacity: it asks the ledger,
    which is the same ledger self-serve checkout asks. One definition of "is
    this slot free", two very different callers.
    """

    def __init__(self, ledger: CapacityLedger):
        self._ledger = ledger
        self._holds: list[Hold] = []

    def holds_for(self, order_id: str) -> list[Hold]:
        return [h for h in self._holds if h.order_id == order_id]

    def place(
        self,
        order_id: str,
        items: Iterable[tuple[str, date]],
        now: datetime,
        duration: timedelta,
    ) -> list[Hold]:
        """Create rep holds for an order, capacity-checked as one cart.

        Rep holds bypass the per-browser-session hold cap that self-serve
        checkout enforces (an authenticated operator working several deals is
        not the abuse case that cap exists for), but they do not bypass
        capacity. Nothing bypasses capacity.
        """
        items = list(items)
        if duration <= timedelta(0):
            raise ValueError("hold duration must be positive")
        # The ceiling is absolute, so it binds at creation too. Without this an
        # opening hold of 30 days sails past a limit that extend() then enforces,
        # which makes the ceiling a property of the extend path rather than of
        # the hold.
        if duration > MAX_HOLD_AGE:
            raise MaxHoldReached(
                f"a hold may not open longer than the {MAX_HOLD_AGE.days}-day ceiling"
            )
        # enforce_weekday stays on: a rep may book inside the self-serve cutoff
        # window, but may not book a product onto a day it does not publish.
        placements = self._ledger.hold_cart(items, now, duration)
        created: list[Hold] = []
        for (product_id, run_date), placement in zip(items, placements):
            hold = Hold(
                order_id=order_id,
                product_id=product_id,
                run_date=run_date,
                created_at=now,
                expires_at=now + duration,
                kind="rep",
                placement=placement,
            )
            self._holds.append(hold)
            created.append(hold)
        return created

    def extend(self, order_id: str, delta: timedelta, now: datetime) -> list[Hold]:
        """Extend every rep hold on an order. All or nothing.

        Pass one validates each hold against the ceiling and, for lapsed holds,
        against live capacity. Pass two mutates. A failure in pass one leaves
        the order exactly as it was.
        """
        holds = [h for h in self.holds_for(order_id) if h.kind == "rep"]
        if not holds:
            raise HoldError(f"order {order_id} has no rep holds")
        if delta <= timedelta(0):
            raise ValueError("extension must be positive")

        # ---- pass one: validate ----
        planned: list[tuple[Hold, datetime]] = []
        # Lapsed holds have to re-win capacity, and they have to do it as a
        # group per SLOT. "Slot" means the capacity scope key, not the raw date:
        # for a per-week product, two lapsed holds on Monday and Friday of the
        # same week are one slot, and grouping them by date lets both revive
        # into a single free seat. This is the same scope-key rule the ledger
        # uses, reached through the same function, for the same reason.
        lapsed_by_slot: dict[tuple[str, str], tuple[date, int]] = {}
        for hold in holds:
            if not hold.lapsed(now):
                continue
            product = self._ledger.product(hold.product_id)
            key = (hold.product_id, scope_key_for(product, hold.run_date))
            earliest, count = lapsed_by_slot.get(key, (hold.run_date, 0))
            lapsed_by_slot[key] = (min(earliest, hold.run_date), count + 1)

        for (product_id, _scope), (run_date, count) in sorted(lapsed_by_slot.items()):
            if self._ledger.seats_available(product_id, run_date, now) < count:
                raise SlotGone(product_id, run_date)

        for hold in holds:
            base = max(now, hold.expires_at)
            new_expiry = min(hold.ceiling(), base + delta)
            # Refuse rather than pretend: an extension that buys nothing is a
            # failed operation, and reporting success for it is how the
            # original reset-instead-of-add bug stayed invisible.
            if new_expiry <= hold.expires_at and not hold.lapsed(now):
                raise MaxHoldReached(
                    f"{hold.product_id} on {hold.run_date.isoformat()} is at its "
                    f"{MAX_HOLD_AGE.days}-day ceiling"
                )
            if new_expiry <= now:
                raise MaxHoldReached(
                    f"{hold.product_id} on {hold.run_date.isoformat()} cannot be revived; "
                    f"past its {MAX_HOLD_AGE.days}-day ceiling"
                )
            planned.append((hold, new_expiry))

        # ---- pass two: mutate ----
        for hold, new_expiry in planned:
            hold.expires_at = new_expiry
            hold.released = False
            if hold.placement is not None:
                hold.placement.status = "held"
                hold.placement.hold_expires_at = new_expiry
        return [h for h, _ in planned]

    def release(self, order_id: str) -> int:
        """Drop the order's rep holds and free their seats."""
        count = 0
        for hold in self.holds_for(order_id):
            if hold.released:
                continue
            hold.released = True
            if hold.placement is not None:
                hold.placement.status = "expired"
                hold.placement.hold_expires_at = None
            count += 1
        return count

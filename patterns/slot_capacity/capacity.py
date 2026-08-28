"""Booking-conflict checker for a calendar where inventory is scarce and
two different products count their scarcity differently.

EXTRACTED PATTERN. This is a standalone, in-memory rewrite of an idea that ships
in a production system as a set of PostgreSQL functions. It is here to show the
shape of the rule, not to be dropped into an app. See ../../README.md for why the
production version lives in the database instead of in application code.

The problem
-----------
A publication sells placements against dates. Some products are capped per
issue ("at most 2 of these in any one issue"). Others are capped per publishing
week ("at most 1 of these per week, whichever day it runs"). A naive
`count(*) where run_date = ?` is correct for the first kind and silently wrong
for the second: it will happily sell one per day of the same week.

The fix is a *scope key*. Every product declares how it counts, and every
capacity question is asked against the key rather than the date:

    per-issue product   -> scope key is the ISO date        "2026-03-11"
    per-week product    -> scope key is the ISO week        "2026-W11"

Every check, every override, and every lock is keyed the same way, so the two
cap styles cannot drift apart.

Three further rules, each of which was a real bug before it was a rule:

1.  A cart is checked as a GROUP. Two lines for the same slot in one cart must
    be measured together against the cap. Checking them one at a time lets a
    cart of two oversell a cap of one.
2.  Unexpired holds occupy capacity. A hold is a soft reservation with a
    deadline; it counts as taken until it lapses.
3.  Terminal-but-not-cancelled statuses still occupy capacity. A placement that
    missed its content deadline is still in the issue. It is not a free seat.

The production system additionally takes a per-slot advisory lock, in sorted
key order, before the check and holds it through the write, so a
check-then-write pair is atomic and a multi-slot cart cannot deadlock against
another multi-slot cart. That ordering discipline is preserved here in
`_ordered_keys` even though a single-threaded in-memory ledger does not need
the lock itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

# Statuses that occupy a seat outright. "missed_lock" is deliberately in this
# set: a sponsor who missed the content deadline still owns the slot.
OCCUPYING_STATUSES = frozenset(
    {"booked", "content_received", "approved", "locked", "live", "reported", "missed_lock"}
)
# A hold occupies a seat only while it has not lapsed.
HOLD_STATUS = "held"
# Statuses that free the seat.
RELEASED_STATUSES = frozenset({"draft", "expired", "cancelled"})

CAP_SCOPES = frozenset({"issue", "week"})


class BookingRefused(Exception):
    """Base class for every reason a cart is turned away."""


class UnknownProduct(BookingRefused):
    """The cart named a product that is not in the catalog."""


class NotBookable(BookingRefused):
    """The slot is not sellable at all: the product is retired, or it does not
    run on that weekday.

    Deliberately distinct from ConflictError. "Sold out" and "we never sell
    this on a Tuesday" are different answers, and collapsing them into one
    error tells an operator to wait for a cancellation that will never help.
    """

    def __init__(self, product_id: str, run_date: date, reason: str):
        self.product_id = product_id
        self.run_date = run_date
        self.reason = reason
        super().__init__(f"{product_id} on {run_date.isoformat()}: {reason}")


class ConflictError(BookingRefused):
    """Raised when a requested booking cannot fit inside the cap.

    Carries the specific product and date that failed so the caller can point at
    the offending calendar cell rather than saying "something is full".
    """

    def __init__(self, product_id: str, run_date: date, requested: int, available: int):
        self.product_id = product_id
        self.run_date = run_date
        self.requested = requested
        self.available = available
        super().__init__(
            f"{product_id} on {run_date.isoformat()}: requested {requested}, "
            f"{available} seat(s) available"
        )


@dataclass(frozen=True)
class Product:
    """Catalog entry. `cap_scope` is what makes the scope key work."""

    id: str
    cap: int
    cap_scope: str  # "issue" (per publish date) or "week" (per ISO week)
    weekdays: frozenset[int] = frozenset()  # ISO weekday numbers, 1=Mon .. 7=Sun
    active: bool = True

    def __post_init__(self) -> None:
        if self.cap_scope not in CAP_SCOPES:
            raise ValueError(f"cap_scope must be one of {sorted(CAP_SCOPES)}")
        if self.cap < 0:
            raise ValueError("cap must not be negative")


@dataclass
class Placement:
    """One sold or reserved unit of inventory."""

    product_id: str
    run_date: date
    status: str
    hold_expires_at: datetime | None = None

    def occupies(self, now: datetime) -> bool:
        if self.status in OCCUPYING_STATUSES:
            return True
        if self.status == HOLD_STATUS:
            return self.hold_expires_at is not None and self.hold_expires_at > now
        return False


@dataclass(frozen=True)
class Override:
    """A manual capacity adjustment for one product in one scope.

    `closed=True` beats any cap_override: a closed slot has zero seats even if
    somebody also typed a number in. Encoding that precedence once, here, is
    what stops "closed but cap 3" from being ambiguous at the call site.
    """

    cap_override: int | None = None
    closed: bool = False
    reason: str = ""


def scope_key_for(product: Product, run_date: date) -> str:
    """The single definition of "which bucket does this date count in".

    Every capacity read, override, and lock in the system goes through this
    function. That is the whole trick: there is no second place where a date
    gets turned into a bucket, so the two cap styles cannot diverge.
    """
    if product.cap_scope == "week":
        iso = run_date.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return run_date.isoformat()


class CapacityLedger:
    """In-memory stand-in for the placements table plus its capacity rules."""

    def __init__(self, products: Iterable[Product]):
        self._products: dict[str, Product] = {p.id: p for p in products}
        self._placements: list[Placement] = []
        self._overrides: dict[tuple[str, str], Override] = {}

    # ---- catalog ---------------------------------------------------------

    def product(self, product_id: str) -> Product:
        try:
            return self._products[product_id]
        except KeyError:
            raise UnknownProduct(product_id) from None

    def set_override(self, product_id: str, run_date: date, override: Override) -> None:
        """Overrides are stored against the SCOPE KEY, not the date.

        Setting an override on a Tuesday for a per-week product closes the whole
        week, which is the behavior an operator expects and the behavior a
        date-keyed store would get wrong.
        """
        product = self.product(product_id)
        self._overrides[(product_id, scope_key_for(product, run_date))] = override

    # ---- reads -----------------------------------------------------------

    def effective_cap(self, product_id: str, run_date: date) -> int:
        product = self.product(product_id)
        override = self._overrides.get((product_id, scope_key_for(product, run_date)))
        if override is None:
            return product.cap
        if override.closed:
            return 0
        if override.cap_override is not None:
            return override.cap_override
        return product.cap

    def active_count(self, product_id: str, run_date: date, now: datetime) -> int:
        product = self.product(product_id)
        key = scope_key_for(product, run_date)
        return sum(
            1
            for p in self._placements
            if p.product_id == product_id
            and scope_key_for(product, p.run_date) == key
            and p.occupies(now)
        )

    def seats_available(self, product_id: str, run_date: date, now: datetime) -> int:
        return max(
            0,
            self.effective_cap(product_id, run_date)
            - self.active_count(product_id, run_date, now),
        )

    # ---- the check ------------------------------------------------------

    def check_cart(
        self, items: list[tuple[str, date]], now: datetime, *, enforce_weekday: bool = True
    ) -> None:
        """Raise ConflictError unless every slot in the cart fits.

        Groups the cart by scope key first, so N lines for one slot are measured
        against the cap together. Iterates in sorted key order to mirror the
        production lock ordering, which is what makes concurrent multi-slot
        carts deadlock-free.
        """
        wants: dict[tuple[str, str], list[date]] = {}
        for product_id, run_date in items:
            product = self.product(product_id)
            if not product.active:
                raise NotBookable(product_id, run_date, "product is not active")
            if enforce_weekday and product.weekdays:
                if run_date.isoweekday() not in product.weekdays:
                    raise NotBookable(
                        product_id, run_date, "product does not run on that weekday"
                    )
            wants.setdefault((product_id, scope_key_for(product, run_date)), []).append(run_date)

        for product_id, _key in _ordered_keys(wants):
            dates = wants[(product_id, _key)]
            requested = len(dates)
            representative = min(dates)
            available = self.seats_available(product_id, representative, now)
            if requested > available:
                raise ConflictError(product_id, representative, requested, available)

    # ---- writes ---------------------------------------------------------

    def hold_cart(
        self,
        items: list[tuple[str, date]],
        now: datetime,
        duration: timedelta,
        *,
        enforce_weekday: bool = True,
    ) -> list[Placement]:
        """Check then reserve, as one operation.

        The check and the write are not separable: any caller that checks, comes
        back, and then writes has reintroduced the race this exists to close.
        """
        if duration <= timedelta(0):
            raise ValueError("hold duration must be positive")
        self.check_cart(items, now, enforce_weekday=enforce_weekday)
        created = [
            Placement(
                product_id=product_id,
                run_date=run_date,
                status=HOLD_STATUS,
                hold_expires_at=now + duration,
            )
            for product_id, run_date in items
        ]
        self._placements.extend(created)
        return created

    def add(self, placement: Placement) -> Placement:
        """Insert a placement without a capacity check (imports, backfills)."""
        self._placements.append(placement)
        return placement

    def placements(self) -> list[Placement]:
        return list(self._placements)


def _ordered_keys(wants: dict[tuple[str, str], list[date]]) -> list[tuple[str, str]]:
    """Deterministic, sorted iteration order over (product, scope key) pairs.

    In production this is the order the per-slot advisory locks are taken in.
    Two carts that overlap therefore take their shared locks in the same
    sequence and one waits instead of both deadlocking.
    """
    return sorted(wants.keys())

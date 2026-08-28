"""Two-person sign-off that is bound to the exact content it approved.

EXTRACTED PATTERN. Standalone in-memory rewrite of a rule that ships in a
production system as PostgreSQL functions over a `placement_approvals` table.
See ../../README.md.

The problem
-----------
Paid third-party content goes out to a large list. Before it publishes, two
different people must sign off: one on fulfillment (is this the thing the client
bought?) and one on quality (is this fit to send?). The obvious implementation
is two boolean columns:

    approved_by_fulfillment BOOLEAN
    approved_by_qa         BOOLEAN

That implementation has a hole big enough to publish through. Approvals are
recorded against the ITEM, and the item's content is mutable. Both people
approve; the client then edits the copy through their own edit link; the
booleans are still true; the edited copy ships having been reviewed by nobody.
Clearing the flags on every edit closes that particular hole but leaves no
record of what was approved, and it is one forgotten write path away from
reopening.

The rule
--------
An approval is not a flag on the item. It is a row keyed by
`(item, revision, leg)`. Every content edit increments the revision. An
approval for revision 4 says nothing about revision 5, so approvals do not need
to be cleared: they simply stop matching. The gate asks "are both legs signed
off *at the current revision*", which is a question the data answers directly.

Four properties fall out of that, and all four were requirements:

1.  An edit after approval un-approves the item automatically, because the
    approval rows for the old revision no longer answer the current question.
    No cleanup write can be forgotten.
2.  A reviewer must submit the revision they believe they are approving. If it
    does not match, the approval is refused as stale rather than silently
    applied to content the reviewer never saw. This is the check that catches
    the edit-while-the-review-tab-is-open race.
3.  The two legs must be different people. One person holding both roles is not
    two-person review.
4.  The approval history is an audit trail. Who signed off on what version, and
    when, survives every subsequent edit.

A rejection is modelled as a revision bump rather than a "rejected" state. The
item goes back to awaiting-content with a note, prior approvals stop counting
for the same structural reason, and there is no separate rejected-to-approved
transition to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Leg = Literal["fulfillment", "qa"]
LEGS: frozenset[str] = frozenset({"fulfillment", "qa"})

# The only status from which an item can be reviewed. Reviewing something that
# is already live, cancelled, or still awaiting content is a bug, not a no-op.
REVIEWABLE_STATUS = "content_received"
APPROVED_STATUS = "approved"


class NotReviewable(Exception):
    """The item is not in a state where sign-off means anything."""


class StaleRevision(Exception):
    """The reviewer approved a revision that is no longer current.

    Carries the current revision so the UI can reload and show what changed.
    """

    def __init__(self, submitted: int, current: int):
        self.submitted = submitted
        self.current = current
        super().__init__(f"approved revision {submitted}, current is {current}")


class SameActorTwice(Exception):
    """One person tried to supply both legs of a two-person review."""


class LegAlreadySigned(Exception):
    """Somebody else already signed this leg at this revision.

    The approval key is (item, revision, leg), so a leg holds exactly one
    signature. Letting a second person overwrite or duplicate it would make the
    audit record ambiguous about who actually approved.
    """

    def __init__(self, leg: str, actor: str):
        self.leg = leg
        self.signed_by = actor
        super().__init__(f"the {leg} leg was already signed by {actor}")


@dataclass(frozen=True)
class Approval:
    item_id: str
    revision: int
    leg: str
    actor: str
    at: datetime


@dataclass(frozen=True)
class Rejection:
    """A request for changes, recorded against the revision it was made on."""

    item_id: str
    revision: int
    actor: str
    note: str


@dataclass
class ReviewItem:
    """The thing under review. Content fields stand in for the real ones."""

    id: str
    status: str = REVIEWABLE_STATUS
    revision: int = 1
    content: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalResult:
    legs_done: int
    revision: int
    fully_approved: bool


class ApprovalStore:
    """Holds items and the append-only approval log."""

    def __init__(self) -> None:
        self._items: dict[str, ReviewItem] = {}
        self._approvals: list[Approval] = []
        self._rejections: list[Rejection] = []

    # ---- items ----------------------------------------------------------

    def add(self, item: ReviewItem) -> ReviewItem:
        self._items[item.id] = item
        return item

    def get(self, item_id: str) -> ReviewItem:
        return self._items[item_id]

    # ---- the gate -------------------------------------------------------

    def is_approved(self, item_id: str) -> bool:
        """The published-content gate. Note it reads the CURRENT revision.

        This one predicate is the whole safety property. Anything that
        publishes calls this and nothing else.
        """
        item = self.get(item_id)
        return self._legs_at(item_id, item.revision) == LEGS

    def approvals_for(self, item_id: str, revision: int | None = None) -> list[Approval]:
        return [
            a
            for a in self._approvals
            if a.item_id == item_id and (revision is None or a.revision == revision)
        ]

    def rejections_for(self, item_id: str) -> list[Rejection]:
        """The change requests on record, oldest first."""
        return [r for r in self._rejections if r.item_id == item_id]

    def _legs_at(self, item_id: str, revision: int) -> frozenset[str]:
        return frozenset(a.leg for a in self.approvals_for(item_id, revision))

    # ---- transitions ----------------------------------------------------

    def edit(self, item_id: str, fields: dict[str, str], *, actor: str) -> int:
        """Apply a content edit. Returns the new revision.

        Every write path that can change reviewable content goes through here,
        which is what guarantees the revision moves with the content. A second
        write path that edits content without bumping the revision would defeat
        the entire pattern, so there is exactly one.
        """
        item = self.get(item_id)
        if item.status in {"locked", "live", "cancelled"}:
            raise NotReviewable(f"{item_id} is {item.status}; content is frozen")
        item.content.update(fields)
        item.revision += 1
        # Back to awaiting review: prior approvals are already dead by binding,
        # this just makes the item's own status honest about it.
        if item.status == APPROVED_STATUS:
            item.status = REVIEWABLE_STATUS
        return item.revision

    def approve(self, item_id: str, leg: str, actor: str, revision: int, *, now: datetime | None = None) -> ApprovalResult:
        """Record one leg of sign-off, bound to `revision`.

        `revision` is not optional and is not read from the item. The reviewer
        has to state which version they are approving; a mismatch means the
        content moved under them and the approval is refused.
        """
        if leg not in LEGS:
            raise ValueError(f"leg must be one of {sorted(LEGS)}")
        item = self.get(item_id)
        if item.status != REVIEWABLE_STATUS:
            raise NotReviewable(f"{item_id} is {item.status}, not {REVIEWABLE_STATUS}")
        if revision != item.revision:
            raise StaleRevision(revision, item.revision)

        (other_leg,) = LEGS - {leg}
        for existing in self.approvals_for(item_id, item.revision):
            if existing.leg == other_leg and existing.actor == actor:
                raise SameActorTwice(f"{actor} already supplied the {other_leg} leg")
            if existing.leg == leg:
                if existing.actor == actor:
                    # Idempotent: the same person clicking approve twice is not
                    # an error and must not count as two legs.
                    return self._result(item)
                raise LegAlreadySigned(leg, existing.actor)

        self._approvals.append(
            Approval(
                item_id=item_id,
                revision=item.revision,
                leg=leg,
                actor=actor,
                at=now or datetime.now(),
            )
        )
        if self._legs_at(item_id, item.revision) == LEGS:
            item.status = APPROVED_STATUS
        return self._result(item)

    def reject(self, item_id: str, actor: str, note: str) -> int:
        """Send it back for changes. Returns the new revision.

        Rejection is a revision bump, not a state. Prior approvals stop
        counting for the same reason they stop counting after an edit, so
        there is no second invalidation rule to keep in sync with the first.
        """
        item = self.get(item_id)
        if item.status not in {REVIEWABLE_STATUS, APPROVED_STATUS}:
            raise NotReviewable(f"{item_id} is {item.status}; nothing to reject")
        self._rejections.append(
            Rejection(item_id=item_id, revision=item.revision, actor=actor, note=note)
        )
        item.revision += 1
        item.status = REVIEWABLE_STATUS
        return item.revision

    def _result(self, item: ReviewItem) -> ApprovalResult:
        legs = self._legs_at(item.id, item.revision)
        return ApprovalResult(
            legs_done=len(legs), revision=item.revision, fully_approved=legs == LEGS
        )

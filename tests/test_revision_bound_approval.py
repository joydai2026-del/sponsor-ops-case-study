"""Tests for revision-bound dual sign-off.

The first test is the one the whole pattern exists for.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from patterns.revision_bound_approval import (
    ApprovalStore,
    LegAlreadySigned,
    NotReviewable,
    ReviewItem,
    SameActorTwice,
    StaleRevision,
)

NOW = datetime(2026, 3, 1, 12, 0)


def store_with_item() -> tuple[ApprovalStore, ReviewItem]:
    store = ApprovalStore()
    item = store.add(ReviewItem(id="item-1", content={"headline": "first draft"}))
    return store, item


def both_legs(store: ApprovalStore, item_id: str, revision: int) -> None:
    store.approve(item_id, "fulfillment", "reviewer-a", revision, now=NOW)
    store.approve(item_id, "qa", "reviewer-b", revision, now=NOW)


# ---- the core property --------------------------------------------------


def test_an_edit_after_approval_un_approves_the_item():
    """THE bug this pattern exists to make impossible: both people sign off,
    the client then edits the copy, and the edited copy publishes carrying an
    approval nobody gave it."""
    store, item = store_with_item()
    both_legs(store, item.id, 1)
    assert store.is_approved(item.id)

    store.edit(item.id, {"headline": "rewritten after approval"}, actor="client")

    assert not store.is_approved(item.id)
    assert store.get(item.id).status == "content_received"


def test_approvals_are_not_deleted_by_an_edit_only_bypassed():
    """Invalidation is structural, not a cleanup write. The old approvals are
    still on record as the audit trail of what revision 1 was signed off at."""
    store, item = store_with_item()
    both_legs(store, item.id, 1)
    store.edit(item.id, {"headline": "v2"}, actor="client")

    assert len(store.approvals_for(item.id, revision=1)) == 2
    assert store.approvals_for(item.id, revision=2) == []
    assert not store.is_approved(item.id)


def test_re_approving_after_an_edit_restores_the_gate():
    store, item = store_with_item()
    both_legs(store, item.id, 1)
    new_revision = store.edit(item.id, {"headline": "v2"}, actor="client")
    both_legs(store, item.id, new_revision)
    assert store.is_approved(item.id)


# ---- stale revisions ----------------------------------------------------


def test_approving_a_stale_revision_is_refused():
    """The reviewer had the item open while somebody edited it. Their approval
    is for content that no longer exists."""
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    store.edit(item.id, {"headline": "changed under the reviewer"}, actor="client")

    with pytest.raises(StaleRevision) as exc:
        store.approve(item.id, "qa", "reviewer-b", 1, now=NOW)
    assert exc.value.submitted == 1
    assert exc.value.current == 2
    assert not store.is_approved(item.id)


def test_the_error_reports_the_current_revision_so_the_ui_can_reload():
    store, item = store_with_item()
    store.edit(item.id, {"headline": "v2"}, actor="client")
    store.edit(item.id, {"headline": "v3"}, actor="client")
    with pytest.raises(StaleRevision) as exc:
        store.approve(item.id, "qa", "reviewer-b", 1, now=NOW)
    assert exc.value.current == 3


# ---- two people ---------------------------------------------------------


def test_one_person_cannot_supply_both_legs():
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    with pytest.raises(SameActorTwice):
        store.approve(item.id, "qa", "reviewer-a", 1, now=NOW)
    assert not store.is_approved(item.id)


def test_one_leg_alone_does_not_open_the_gate():
    store, item = store_with_item()
    result = store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    assert result.legs_done == 1
    assert not result.fully_approved
    assert not store.is_approved(item.id)


def test_the_same_person_clicking_approve_twice_is_idempotent():
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    result = store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    assert result.legs_done == 1
    assert len(store.approvals_for(item.id, revision=1)) == 1
    assert not store.is_approved(item.id)


def test_the_two_legs_may_be_signed_in_either_order():
    store, item = store_with_item()
    store.approve(item.id, "qa", "reviewer-b", 1, now=NOW)
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    assert store.is_approved(item.id)


def test_an_unknown_leg_is_rejected():
    store, item = store_with_item()
    with pytest.raises(ValueError):
        store.approve(item.id, "legal", "reviewer-c", 1, now=NOW)


# ---- rejection ----------------------------------------------------------


def test_rejection_bumps_the_revision_and_drops_the_approval():
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    new_revision = store.reject(item.id, actor="reviewer-b", note="wrong logo")

    assert new_revision == 2
    assert store.get(item.id).status == "content_received"
    assert not store.is_approved(item.id)
    # The first leg's sign-off on revision 1 is still on record.
    assert len(store.approvals_for(item.id, revision=1)) == 1


def test_a_fully_approved_item_can_still_be_rejected():
    store, item = store_with_item()
    both_legs(store, item.id, 1)
    store.reject(item.id, actor="reviewer-a", note="client pulled the offer")
    assert not store.is_approved(item.id)


# ---- state guards -------------------------------------------------------


@pytest.mark.parametrize("status", ["locked", "live", "cancelled"])
def test_frozen_content_cannot_be_edited(status):
    store, item = store_with_item()
    item.status = status
    with pytest.raises(NotReviewable):
        store.edit(item.id, {"headline": "too late"}, actor="client")


@pytest.mark.parametrize("status", ["locked", "live", "cancelled", "awaiting_content"])
def test_only_reviewable_items_can_be_approved(status):
    store, item = store_with_item()
    item.status = status
    with pytest.raises(NotReviewable):
        store.approve(item.id, "fulfillment", "reviewer-a", item.revision, now=NOW)


def test_an_edit_on_an_approved_item_returns_it_to_review():
    store, item = store_with_item()
    both_legs(store, item.id, 1)
    assert store.get(item.id).status == "approved"
    store.edit(item.id, {"headline": "one more tweak"}, actor="client")
    assert store.get(item.id).status == "content_received"


# ---- one signature per leg ----------------------------------------------


def test_a_second_person_cannot_sign_a_leg_somebody_already_signed():
    """Regression: the approval key is (item, revision, leg), so a leg holds
    exactly one signature. A second actor on the same leg used to insert a
    duplicate row, leaving the audit record ambiguous about who approved."""
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)

    with pytest.raises(LegAlreadySigned) as exc:
        store.approve(item.id, "fulfillment", "reviewer-c", 1, now=NOW)
    assert exc.value.signed_by == "reviewer-a"
    assert len(store.approvals_for(item.id, revision=1)) == 1


def test_a_leg_reopens_for_a_new_signer_after_the_revision_moves():
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    new_revision = store.edit(item.id, {"headline": "v2"}, actor="client")

    store.approve(item.id, "fulfillment", "reviewer-c", new_revision, now=NOW)
    store.approve(item.id, "qa", "reviewer-b", new_revision, now=NOW)
    assert store.is_approved(item.id)


# ---- rejections are on record -------------------------------------------


def test_a_rejection_records_who_asked_and_why():
    """The note is the whole point of a rejection; discarding it makes the
    audit trail say a revision happened but not why."""
    store, item = store_with_item()
    store.approve(item.id, "fulfillment", "reviewer-a", 1, now=NOW)
    store.reject(item.id, actor="reviewer-b", note="logo is the old mark")

    (rejection,) = store.rejections_for(item.id)
    assert rejection.actor == "reviewer-b"
    assert rejection.note == "logo is the old mark"
    assert rejection.revision == 1  # recorded against the revision it judged


def test_rejections_accumulate_across_rounds():
    store, item = store_with_item()
    store.reject(item.id, actor="reviewer-a", note="first pass")
    store.reject(item.id, actor="reviewer-b", note="second pass")
    assert [r.revision for r in store.rejections_for(item.id)] == [1, 2]

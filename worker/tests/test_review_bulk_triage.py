"""Context-integrity triage for the human review queue (read side only)."""
from __future__ import annotations

from agent_knowledge.session_memory.brain_steward import BrainStewardService
from test_pg_steward_authority import NoCardLedger


class FakeStore:
    """Just enough of PgVectorStore for queue reads and the current-card index."""

    def __init__(self, review, current=()):
        self.review = list(review)
        self.current = list(current)
        self.current_reads = 0

    def list_steward_current_context(self, *, project=""):
        self.current_reads += 1
        return [c for c in self.current if not project or c.get("project") == project]

    def list_steward_cards(self, *, project="", review_only=False, accepted_only=False,
                           current_only=False, limit=20):
        if accepted_only or current_only:
            self.current_reads += 1
            rows = [c for c in self.current if not project or c.get("project") == project]
            return rows[:limit]
        rows = [c for c in self.review if not project or c.get("project") == project]
        return rows[:limit]


def _review(mid, *, card_type="evidence", project="neurons", title="Field fact",
            kind="candidate", supersedes=None):
    return {"memory_id": mid, "card_type": card_type, "project": project, "title": title,
            "summary": "summary", "lifecycle_state": "candidate", "currentness": "unknown",
            "steward_proposal_kind": kind, "supersedes": supersedes or []}


def _current(mid, *, card_type="evidence", project="neurons", title="Field fact"):
    return {"memory_id": mid, "card_type": card_type, "project": project, "title": title,
            "lifecycle_state": "human_accepted", "currentness": "current"}


def _steward(store):
    return BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)


def test_additive_candidates_are_grouped_as_bulk_safe():
    store = FakeStore(review=[_review("mem_new", title="Brand new fact")],
                      current=[_current("mem_old", title="Unrelated existing fact")])
    queue = _steward(store).review_queue_list(project="neurons")

    assert queue["review_summary"]["by_class"] == {"authority_change": 0,
                                                   "competes_with_current": 0, "additive": 1}
    assert queue["bulk_safe_ids"] == ["mem_new"]
    assert queue["needs_eyes_ids"] == []
    assert queue["items"][0]["review_class"] == "additive"
    assert queue["items"][0]["review_flags"] == []


def test_same_subject_already_current_is_flagged_not_bulk_safe():
    # Approving this would leave two current cards for one subject, which is
    # exactly how a fresh reader receives a contradictory context.
    store = FakeStore(review=[_review("mem_dup", title="DTO ORM 도입 검토")],
                      current=[_current("mem_live", title="DTO ORM 도입 검토")])
    queue = _steward(store).review_queue_list(project="neurons")

    item = queue["items"][0]
    assert item["review_class"] == "competes_with_current"
    assert item["conflicts_with_memory_id"] == "mem_live"
    assert queue["needs_eyes_ids"] == ["mem_dup"]
    assert queue["bulk_safe_ids"] == []
    assert queue["review_summary"]["needs_eyes_count"] == 1


def test_authority_change_proposals_always_need_eyes():
    store = FakeStore(review=[_review("mem_old_a", kind="stale", title="Old status"),
                              _review("mem_new_a", kind="supersede", title="Replacement",
                                      supersedes=["mem_old_a"])],
                      current=[_current("mem_old_a", title="Old status")])
    queue = _steward(store).review_queue_list(project="neurons")

    classes = [i["review_class"] for i in queue["items"]]
    assert classes == ["authority_change", "authority_change"]
    assert queue["review_summary"]["by_class"]["authority_change"] == 2
    assert queue["bulk_safe_ids"] == []


def test_unreadable_current_index_fails_closed_to_individual_review():
    class BrokenStore(FakeStore):
        def list_steward_current_context(self, **kwargs):
            raise RuntimeError("read failure")

        def list_steward_cards(self, **kwargs):
            if kwargs.get("accepted_only") or kwargs.get("current_only"):
                raise RuntimeError("read failure")
            return super().list_steward_cards(**kwargs)

    store = BrokenStore(review=[_review("mem_x")])
    queue = _steward(store).review_queue_list(project="neurons")

    assert queue["items"][0]["review_class"] == "competes_with_current"
    assert "current_index_unavailable" in queue["items"][0]["review_flags"]
    assert queue["review_summary"]["current_index_available"] is False
    assert queue["bulk_safe_ids"] == []


def test_scope_only_compares_within_the_requested_project():
    store = FakeStore(review=[_review("mem_a", project="neurons", title="Shared subject")],
                      current=[_current("mem_other", project="stocks", title="Shared subject")])
    queue = _steward(store).review_queue_list(project="neurons")

    assert queue["items"][0]["review_class"] == "additive"
    assert queue["bulk_safe_ids"] == ["mem_a"]


def test_conflict_after_one_hundred_current_cards_is_not_bulk_safe():
    existing = [_current(f"old-{i}", title=f"Other {i}") for i in range(100)]
    existing.append(_current("old-conflict", title="Exact conflicting subject"))
    queue = _steward(FakeStore([_review("new-conflict", title="Exact conflicting subject")], existing)).review_queue_list(project="neurons")
    assert queue["bulk_safe_ids"] == []
    assert queue["needs_eyes_ids"] == ["new-conflict"]


def test_unscoped_queue_does_not_compare_different_projects():
    queue = _steward(FakeStore([_review("new", project="neurons", title="Same")],
                              [_current("other", project="stocks", title="Same")])).review_queue_list()
    assert queue["bulk_safe_ids"] == ["new"]


def test_same_project_different_brain_or_scope_does_not_compete():
    pending = dict(_review("new", title="Same"), brain_id="/project/neurons", scope="project")
    old = dict(_current("old", title="Same"), brain_id="/other/neurons", scope="global")
    queue = _steward(FakeStore([pending], [old])).review_queue_list(project="neurons")
    assert queue["bulk_safe_ids"] == ["new"]


def test_duplicate_subjects_inside_one_review_queue_need_eyes():
    queue = _steward(FakeStore([_review("new-a", title="Same"), _review("new-b", title="Same")])).review_queue_list(project="neurons")
    assert queue["bulk_safe_ids"] == []
    assert queue["needs_eyes_ids"] == ["new-a", "new-b"]
    assert all("same_subject_in_review_bundle" in i["review_flags"] for i in queue["items"])

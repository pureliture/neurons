"""Read-only retry of an existing decided proposal, no authority overwrite."""
import copy

import pytest

from test_steward_intake_provenance import _candidate, _steward


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_identical_proposal_after_decision_is_verified_reuse(decision):
    service, store, db = _steward(allow_restricted=True)
    candidate = _candidate()
    proposal = service.candidate_create(source_span=candidate,mark_needs_review=True)
    memory_id = proposal["memory_id"]
    if decision == "approve":
        service.candidate_approve(candidate_memory_id=memory_id,approved_by="test-operator",decision_id="first-decision")
    else:
        service.candidate_reject(candidate_memory_id=memory_id,rejected_by="test-operator",decision_id="first-decision",reason="No approval")
    before = copy.deepcopy(db.rows[memory_id])
    audit = copy.deepcopy(db.decisions)
    outboxes = copy.deepcopy((db.embedding_jobs,db.graph_jobs))
    writes = [q for q,_ in db.executed if q.startswith(("UPDATE", "INSERT"))]
    response = service.candidate_create(source_span=candidate,mark_needs_review=True)
    assert response["memory_id"] == memory_id
    assert response["proposal_write_performed"] is False
    assert response["authoritative_memory_changed"] is False
    assert db.rows[memory_id] == before and db.decisions == audit
    assert (db.embedding_jobs,db.graph_jobs) == outboxes
    assert writes == [q for q,_ in db.executed if q.startswith(("UPDATE", "INSERT"))]


@pytest.mark.parametrize("field", ["typed_payload", "span_refs", "source_refs", "candidate_id"])
def test_committed_identity_cannot_hide_different_candidate_payload(field):
    service,store,db = _steward(allow_restricted=True)
    candidate = _candidate()
    memory_id = service.candidate_create(source_span=candidate,mark_needs_review=True)["memory_id"]
    service.candidate_approve(candidate_memory_id=memory_id,approved_by="test-operator",decision_id="first-decision")
    before=copy.deepcopy(db.rows[memory_id])
    changed=copy.deepcopy(candidate)
    if field == "typed_payload":changed[field]["decision"]="Different decision"
    elif field == "candidate_id":changed[field]="different-candidate"
    elif field == "source_refs":changed[field][0]["source_id"]="different-source"
    else:changed[field][0]["span_id"]="different-span"
    with pytest.raises(ValueError,match="collision"):
        service.candidate_create(source_span=changed,mark_needs_review=True)
    assert db.rows[memory_id]==before

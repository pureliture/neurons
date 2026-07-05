import pytest

from agent_knowledge.llm_brain_core.knowledge_objects import (
    AuthorityDecision,
    EvidenceRef,
    KnowledgeEdge,
    KnowledgeObjectEnvelope,
    ReviewProposal,
    memory_card_to_knowledge_object,
)


def test_knowledge_object_separates_authority_lane_from_verification_state():
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RuntimeTruth",
        natural_key="deploy:neurons:stable",
        scope={"project": "neurons"},
        title="Stable runtime truth",
        summary="Runtime evidence is not verified yet.",
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="runtime_unverified",
        review_state="needs_review",
        content_hash="sha256:" + "a" * 64,
        payload={"runtime_surface": "stable"},
    )

    data = obj.to_dict()
    assert data["authority_lane"] == "candidate"
    assert data["verification_state"] == "runtime_unverified"
    assert "authority_status" not in data


def test_knowledge_object_rejects_runtime_verified_as_authority_lane():
    with pytest.raises(ValueError, match="authority_lane"):
        KnowledgeObjectEnvelope.from_parts(
            object_type="RuntimeTruth",
            natural_key="deploy:neurons:stable",
            scope={"project": "neurons"},
            title="Bad runtime truth",
            summary="Bad state axis.",
            lifecycle_status="observed",
            authority_lane="runtime_verified",
            verification_state="unverified",
            review_state="needs_review",
            content_hash="sha256:" + "b" * 64,
        )


def test_knowledge_object_rejects_raw_private_payload():
    with pytest.raises(ValueError):
        KnowledgeObjectEnvelope.from_parts(
            object_type="RepoDocument",
            natural_key="private",
            scope={"project": "neurons"},
            title="Private path",
            summary="Leaky payload",
            lifecycle_status="observed",
            authority_lane="reference_only",
            verification_state="source_hash_verified",
            review_state="not_required",
            content_hash="sha256:" + "c" * 64,
            payload={"path_ref": "/Users/example/private.md"},
        )


def test_memory_card_adapter_maps_accepted_current_and_stale_cards():
    current = memory_card_to_knowledge_object(
        {
            "memory_id": "mem_current",
            "card_type": "decision",
            "project": "neurons",
            "title": "Current decision",
            "summary": "Use object substrate.",
            "content_hash": "sha256:" + "d" * 64,
            "lifecycle_state": "human_accepted",
            "approval_state": "approved",
            "currentness": "current",
            "confidence": 0.9,
        }
    ).to_dict()
    stale = memory_card_to_knowledge_object(
        {
            "memory_id": "mem_stale",
            "card_type": "status",
            "project": "neurons",
            "title": "Stale status",
            "summary": "Old status.",
            "content_hash": "sha256:" + "e" * 64,
            "lifecycle_state": "accepted",
            "approval_state": "approved",
            "currentness": "stale",
            "confidence": 0.6,
        }
    ).to_dict()

    assert current["lifecycle_status"] == "current"
    assert current["authority_lane"] == "accepted_current"
    assert current["review_state"] == "accepted"
    assert stale["lifecycle_status"] == "stale"
    assert stale["authority_lane"] == "accepted_non_current"


def test_edge_evidence_proposal_and_decision_are_public_safe():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/specs/example/design.md"},
        content_hash="sha256:" + "f" * 64,
        summary="Design source hash.",
    )
    edge = KnowledgeEdge.from_parts(
        edge_type="supersedes",
        from_object_id="ko:RepoDocument:old",
        to_object_id="ko:RepoDocument:new",
        evidence_refs=[evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="proposal_only",
        verification_state="unverified",
    )
    proposal = ReviewProposal.from_parts(
        proposal_type="propose_stale",
        target_object_id="ko:RepoDocument:old",
        reason="New design supersedes old doc.",
        evidence_refs=[evidence.evidence_id],
        proposer="codex",
    )
    decision = AuthorityDecision.from_parts(
        decision_type="commit_stale",
        target_object_id="ko:RepoDocument:old",
        previous_authority_lane="accepted_current",
        new_authority_lane="accepted_non_current",
        approved_by="human",
        evidence_refs=[evidence.evidence_id],
    )

    assert evidence.to_view()["locator_view"]["display_ref"] == "docs/specs/example/design.md"
    assert edge.to_dict()["edge_type"] == "supersedes"
    proposal_view = proposal.to_dict()
    assert proposal_view["proposal_preview_created"] is True
    assert proposal_view["proposal_write_performed"] is False
    assert proposal_view["authoritative_memory_changed"] is False
    decision_view = decision.to_dict()
    assert decision_view["authority_decision_preview_created"] is True
    assert decision_view["authority_write_performed"] is False
    assert decision_view["authoritative_memory_changed"] is False


def test_evidence_ref_view_redacts_opaque_locator_value():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "document_id", "value": "doc_opaque_internal_ref"},
        content_hash="sha256:" + "a" * 64,
        summary="Opaque source reference.",
    )

    view = evidence.to_view()

    assert view["locator_view"]["display_ref"] == "document_id:redacted"
    assert view["locator_view"]["display_ref_redacted"] is True
    assert view["locator_view"]["locator_digest"].startswith("sha256:")

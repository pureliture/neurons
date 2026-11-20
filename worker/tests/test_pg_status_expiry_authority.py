import hashlib
import json
from copy import deepcopy

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
from agent_knowledge.session_memory.memory_evaluation import (
    build_policy_version,
    evaluate_candidate_for_auto_policy,
)
from test_llm_brain_integration import _feedback_records, _suggested_accept_candidate

import pytest

from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.session_memory.brain_query import _normalize_query_memory_card
from agent_knowledge.session_memory.index_projection import _require_projectable_card
from agent_knowledge.session_memory.llm_brain_service import _build_pg_card
from agent_knowledge.session_memory.memory_card import validate_memory_card_envelope
from agent_knowledge.session_memory.memory_evaluation import classify_candidate_block_reason
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from test_brain_steward import PROJECT as STEWARD_PROJECT, _ledger, _span

PROJECT = "proof-status-authority"
FUTURE = "2999-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


def _envelope(mid, expires_at, *, project=PROJECT, auth="active", lifecycle="human_accepted",
              approval="approved"):
    return {
        "memory_id": mid, "project": project, "brain_id": f"/project/{project}",
        "scope": "project", "card_type": "status", "title": "Synthetic status",
        "summary": "Synthetic status summary", "typed_payload": {
            "status_value": "active", "observed_at": "2026-01-01T00:00:00Z",
            "expires_at": expires_at, "current_authority": True},
        "lifecycle_state": lifecycle, "approval_state": approval,
        "steward_proposal_kind": "approve", "authorization_status": auth,
        "currentness": "current", "confidence": 0.9, "confidence_basis": "synthetic",
        "valid_from": "2026-01-01T00:00:00+00:00", "valid_to": None,
        "content_hash": "sha256:" + hashlib.sha256(mid.encode()).hexdigest(),
        "source_refs": [], "provider": "hermes", "render_text": "Synthetic status render",
        "judgment_state": "none", "status": "accepted", "governance_tier": "medium",
        "freshness": "current", "evidence_refs": [], "evidence_hashes": [],
        "derived_from": [], "supersedes": [], "superseded_by": [], "conflicts": [],
        "active_until": None,
        "reason_capsule": {"rule_hits": [], "deterministic_signals": [], "evidence_gap": [],
                           "model_reason": "synthetic", "policy_version": "p",
                           "evaluator_version": "e", "review_block_reason": ""},
    }


def _seed(store, envelope):
    with store.transaction() as db:
        card = _build_pg_card(envelope, active=envelope["authorization_status"] == "active")
        store.insert_card(card, conn=db)
        with db.cursor() as cur:
            cur.execute("UPDATE memory_cards SET steward_envelope=%s::jsonb WHERE memory_id=%s",
                        (json.dumps(envelope), card.memory_id))


def test_malformed_expiry_is_readable_reviewable_but_not_authority_or_approvable(isolated_pg_store):
    store = isolated_pg_store
    malformed = _envelope("status_malformed", "until next deploy")
    valid = _envelope("status_valid", FUTURE)
    expired = _envelope("status_expired", PAST)
    disabled = _envelope("status_disabled", FUTURE, auth="disabled")
    other = _envelope("status_other_project", FUTURE, project="another-project")
    invalid_calendar = _envelope("status_invalid_calendar", "2026-02-30T12:00:00Z")
    invalid_fraction = _envelope(
        "status_invalid_fraction", "2999-01-01T12:00:00." + "0" * 200 + "Z"
    )
    invalid_combined = _envelope("status_invalid_combined", "2999-01-01T23:59:60.5Z")
    cards = (
        malformed, valid, expired, disabled, other,
        invalid_calendar, invalid_fraction, invalid_combined,
    )
    for card in cards:
        _seed(store, card)

    authorized = store.list_authorized_cards(
        project=PROJECT,
        memory_ids=[c["memory_id"] for c in cards],
        limit=10,
    )
    assert [c["memory_id"] for c in authorized] == [valid["memory_id"]]

    stored = store.get_steward_card(malformed["memory_id"])
    assert stored is not None
    assert stored["typed_payload"]["expires_at"] == "until next deploy"
    assert validate_memory_card_envelope(stored)["typed_payload"]["expires_at"] == "until next deploy"
    assert _normalize_query_memory_card(brain_id=f"/project/{PROJECT}", card=stored)
    assert _require_projectable_card(stored)
    with pytest.raises(ValueError, match="expires_at"):
        validate_memory_card_envelope(stored, strict_status_expiry=True)
    candidate = dict(stored, source_refs=[{"source_type": "session", "source_id": "synthetic"}])
    assert classify_candidate_block_reason(candidate) == "invalid_status_expires_at"


def test_service_auto_and_human_approval_block_malformed_status(isolated_pg_store, tmp_path):
    store = isolated_pg_store
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(
        ledger, pgvector_store=store, allow_restricted=True, allow_auto_accept=True
    )
    status_span = _span(
        card_type="status",
        title="Synthetic status",
        redacted_summary="Synthetic status summary",
        typed_payload={
            "status_value": "active",
            "observed_at": "2026-01-01T00:00:00Z",
            "expires_at": "until next deploy",
            "current_authority": True,
        },
    )
    created = steward.candidate_create(source_span=status_span)
    candidate_id = created["proposal"]["memory_id"]
    stored = store.get_steward_card(candidate_id)
    assert stored["typed_payload"]["expires_at"] == "until next deploy"
    assert validate_memory_card_envelope(stored)
    auto = steward.candidate_auto_accept(
        candidate_memory_id=candidate_id, evaluation={}, operator_approval_ref="op_auto"
    )
    # The service intentionally does not echo a blocked reason in this result.
    # The safe observable contract is: no canonical write + non-accepted card.
    assert auto.get("canonical_write_performed") is False
    assert store.get_steward_card(candidate_id)["lifecycle_state"] not in {
        "accepted", "human_accepted", "auto_accepted"
    }
    try:
        human = steward.candidate_approve(
            candidate_memory_id=candidate_id, approved_by="op", decision_id="d_human"
        )
    except ValueError as exc:
        assert "invalid_status_expires_at" in str(exc)
    else:
        assert human.get("canonical_write_performed") is False
    assert store.get_steward_card(candidate_id)["lifecycle_state"] not in {
        "accepted", "human_accepted", "auto_accepted"
    }
    assert store.list_authorized_cards(
        project=STEWARD_PROJECT, memory_ids=[candidate_id], limit=5
    ) == []
    assert candidate_id in [
        item["memory_id"] for item in steward.review_queue_list(project=STEWARD_PROJECT)["items"]
    ]


def test_auto_policy_revalidates_mutated_calendar_invalid_status(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)
    candidate = _suggested_accept_candidate(
        card_type="status",
        typed_payload={
            "status_value": "active",
            "observed_at": "2026-01-01T00:00:00Z",
            "expires_at": "2999-01-01T00:00:00Z",
            "current_authority": True,
        },
    )
    evaluation = evaluate_candidate_for_auto_policy(
        candidate,
        feedback_records=_feedback_records(candidate),
        policy=build_policy_version(min_feedback_count=5),
    )
    assert evaluation["decision"] == "auto_accept_eligible"
    valid = service.accept_auto_policy_candidate(
        candidate, evaluation, operator_approval_ref="op_valid"
    )
    assert valid["canonical_write_performed"] is True
    mutated = deepcopy(candidate)
    mutated["typed_payload"]["expires_at"] = "2026-02-30T12:00:00Z"
    blocked = service.accept_auto_policy_candidate(
        mutated, evaluation, operator_approval_ref="op_invalid"
    )
    assert blocked["canonical_write_performed"] is False
    assert blocked["application"]["status"] == "blocked_invalid_candidate"
    assert blocked["application"]["reason"] == "invalid_status_expires_at"
    stored = ledger.get_llm_brain_memory_card(candidate["memory_id"])
    assert stored["lifecycle_state"] == "auto_accepted"
    assert stored["typed_payload"]["expires_at"] == "2999-01-01T00:00:00Z"

from __future__ import annotations

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.brain_query import resolve_brain_ids, run_brain_query_v2
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
from agent_knowledge.session_memory.memory_evaluation import (
    apply_auto_acceptance_plan,
    build_policy_version,
    evaluate_candidate_for_auto_policy,
)
from agent_knowledge.session_memory.memory_promotion import build_feedback_record
from agent_knowledge.session_memory.memory_promotion import suggest_accept_from_evidence


PROJECT = "workspace-ragflow-advisor"


def _source_span(**overrides):
    span = {
        "source_ref": {"source_id": "src_integration"},
        "span_ref": {"span_id": "span_integration"},
        "content_hash": "sha256:integration",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "Current LLM-brain integration work",
        "redacted_summary": "Canonical ledger writes are now wired through LLMBrainMemoryService.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "Project accepted MemoryCards to the RAGFlow mirror.",
            "blocker": None,
            "owner_hint": "codex",
            "status": "integration_ready",
        },
        "confidence": 0.93,
        "confidence_basis": "operator-approved integration slice",
    }
    span.update(overrides)
    return span


def _candidate(**overrides):
    return build_memory_card_candidate_from_source_span(
        _source_span(**overrides), refresh_watermark="integration"
    )


def _suggested_accept_candidate(**overrides):
    candidate = _candidate(**overrides)
    return suggest_accept_from_evidence(
        candidate,
        evidence={
            "evidence_kind": "commit",
            "decision_id": "decision_integration_auto",
            "content_hash": "sha256:integration-auto-evidence",
            "source_ref": {"evidence_id": "commit_integration_auto"},
        },
        decision_id="decision_integration_auto",
    )["suggested_card"]


def _feedback_records(candidate, *, count=6):
    return [
        build_feedback_record(
            candidate=candidate,
            decision_id=f"decision_{index}",
            proposed_status="suggested_accept",
            final_status="accepted",
            user_action="approve",
            model_reason="feedback sample",
            confidence=0.95,
            conflict_state="none",
            timestamp=f"2026-06-13T00:0{index}:00+00:00",
        )
        for index in range(count)
    ]


def _projection_approval(job):
    return {
        "approved": True,
        "operation": "ragflow_projection_write",
        "idempotency_key": job["idempotency_key"],
        "dry_run_status": "dry_run",
        "approved_by": "ddalkak",
    }


def test_human_approved_candidate_is_canonical_in_ledger_and_queryable(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)

    candidate = _candidate()
    candidate["content_hash"] = "sha256:caller-controlled-stale-hash"
    committed = service.accept_human_approved_candidate(
        candidate,
        approved_by="ddalkak",
        decision_id="decision_integration",
        artifact_id="goal_state.md",
        timestamp="2026-06-13T00:00:00+00:00",
    )

    stored = ledger.get_llm_brain_memory_card(committed["accepted_card"]["memory_id"])
    assert committed["canonical_write_performed"] is True
    assert stored["lifecycle_state"] == "human_accepted"
    assert stored["content_hash"].startswith("sha256:")
    assert stored["content_hash"] != "sha256:caller-controlled-stale-hash"
    assert stored["card_hash"] == stored["content_hash"]
    assert ledger.list_llm_brain_feedback_records(memory_id=stored["memory_id"])[0]["user_action"] == "approve"

    result = run_brain_query_v2(
        read_model=LegacyLedgerBrainReadModel(ledger),
        brain_id=f"/project/{PROJECT}",
        query="현재 레포에서 진행중인 작업 알려줘",
        query_intent="current_work",
    )

    assert [item["memory_id"] for item in result["current"]] == [stored["memory_id"]]
    assert result["current"][0]["typed_payload"]["task_state"] == "active"

    resolved = resolve_brain_ids(read_model=LegacyLedgerBrainReadModel(ledger), query=PROJECT)
    assert resolved["candidates"] == [
        {"brain_id": f"/project/{PROJECT}", "kind": "project", "card_count": 1, "hint": ""}
    ]


def test_auto_policy_acceptance_requires_operator_ref_then_writes_canonical_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)
    candidate = _suggested_accept_candidate()
    evaluation = evaluate_candidate_for_auto_policy(
        candidate,
        feedback_records=_feedback_records(candidate),
        policy=build_policy_version(min_feedback_count=5),
    )

    missing_ref = apply_auto_acceptance_plan(candidate, evaluation, allow_auto_accept=True)
    assert missing_ref["status"] == "blocked_missing_operator_approval_ref"

    committed = service.accept_auto_policy_candidate(
        candidate,
        evaluation,
        operator_approval_ref="user-approved-all-stop-conditions",
    )

    assert committed["canonical_write_performed"] is True
    assert committed["accepted_card"]["lifecycle_state"] == "auto_accepted"
    assert ledger.get_llm_brain_memory_card(committed["accepted_card"]["memory_id"])["approval_state"] == "auto_accepted"


def test_projection_job_is_stored_and_fake_ragflow_write_updates_job_state(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)
    card = service.accept_human_approved_candidate(
        _candidate(),
        approved_by="ddalkak",
        decision_id="decision_projection",
    )["accepted_card"]
    queued = service.enqueue_projection_for_card(card)

    calls = {}

    class FakeProjectionClient:
        def upsert_memory_card(self, payload, *, idempotency_key):
            calls["memory_id"] = payload["memory_id"]
            calls["idempotency_key"] = idempotency_key
            return {"document_id": "doc_llm_brain"}

    executed = service.execute_projection_job(
        queued["job"],
        client=FakeProjectionClient(),
        allow_write=True,
        approval_record=_projection_approval(queued["job"]),
    )

    assert queued["projection_job_write_performed"] is True
    assert executed["projection_job_write_performed"] is True
    assert executed["result"]["status"] == "projected"
    assert executed["job"]["status"] == "projected"
    assert calls["memory_id"] == card["memory_id"]

from __future__ import annotations

from agent_knowledge.ledger import Ledger
from agent_knowledge.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.brain_query import run_brain_query_v2
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService


PROJECT = "neurons"


def _span(**overrides):
    span = {
        "source_ref": {"source_id": "src_old"},
        "span_ref": {"span_id": "span_old"},
        "content_hash": "sha256:old",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "auth approach",
        "redacted_summary": "Auth uses JWT.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "ship JWT login",
            "blocker": None,
            "owner_hint": "codex",
            "status": "active",
        },
        "confidence": 0.92,
        "confidence_basis": "operator-approved",
    }
    span.update(overrides)
    return span


def _candidate(**overrides):
    return build_memory_card_candidate_from_source_span(_span(**overrides), refresh_watermark="wm")


def test_supersede_accepted_card_demotes_old_out_of_current_and_accepted(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)

    old = service.accept_human_approved_candidate(
        _candidate(),
        approved_by="autopilot",
        decision_id="decision_old",
    )["accepted_card"]

    new_candidate = _candidate(
        source_ref={"source_id": "src_new"},
        span_ref={"span_id": "span_new"},
        content_hash="sha256:new",
        redacted_summary="Auth now uses OAuth.",
    )

    superseded = service.supersede_accepted_card(
        old_card=old,
        new_candidate=new_candidate,
        approved_by="autopilot",
        decision_id="decision_new",
    )
    new_card = superseded["new_card"]

    assert new_card["memory_id"] != old["memory_id"]

    stored_old = ledger.get_llm_brain_memory_card(old["memory_id"])
    assert stored_old["currentness"] == "superseded"
    assert stored_old["superseded_by"] == [new_card["memory_id"]]

    result = run_brain_query_v2(
        read_model=LegacyLedgerBrainReadModel(ledger),
        brain_id=f"/project/{PROJECT}",
        query="현재 인증 방식 알려줘",
        query_intent="current_work",
    )

    current_ids = [item["memory_id"] for item in result["current"]]
    accepted_ids = [item["memory_id"] for item in result["accepted"]]
    assert current_ids == [new_card["memory_id"]]
    assert old["memory_id"] not in current_ids
    assert old["memory_id"] not in accepted_ids
    assert new_card["memory_id"] in accepted_ids

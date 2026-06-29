from __future__ import annotations

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService


PROJECT = "neurons"


def _candidate(**overrides):
    span = {
        "source_ref": {"source_id": "src"},
        "span_ref": {"span_id": "span"},
        "content_hash": "sha256:x",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "auth approach",
        "redacted_summary": "Auth uses JWT.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "ship login",
            "blocker": None,
            "owner_hint": "codex",
            "status": "active",
        },
        "confidence": 0.92,
        "confidence_basis": "operator-approved",
    }
    span.update(overrides)
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="wm")


def test_accept_refuses_conflicted_candidate_and_writes_nothing(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)

    candidate = _candidate()
    candidate["conflicts"] = [{"memory_id": "mem_other", "reason": "contradicts"}]

    with pytest.raises(ValueError):
        service.accept_human_approved_candidate(
            candidate, approved_by="autopilot", decision_id="decision_block"
        )

    assert ledger.list_llm_brain_memory_cards(accepted_only=True) == []


def test_accept_allows_clean_candidate(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)

    committed = service.accept_human_approved_candidate(
        _candidate(), approved_by="autopilot", decision_id="decision_ok"
    )

    assert committed["canonical_write_performed"] is True
    assert len(ledger.list_llm_brain_memory_cards(accepted_only=True)) == 1

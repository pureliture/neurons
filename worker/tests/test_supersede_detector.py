from __future__ import annotations

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
from agent_knowledge.session_memory.supersede_detector import (
    build_index_judge_fn,
    build_supersede_detector,
)


PROJECT = "neurons"


def _candidate(summary: str, sid: str):
    span = {
        "source_ref": {"source_id": f"src_{sid}"},
        "span_ref": {"span_id": f"span_{sid}"},
        "content_hash": f"sha256:{sid}",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "auth",
        "redacted_summary": summary,
        "typed_payload": {"decision": summary, "rationale": "x", "alternatives": [], "consequence": "y", "authority_ref": "adr"},
        "confidence": 0.95,
        "confidence_basis": "operator",
    }
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="wm")


class _FakeRetiredIndexBridge:
    def __init__(self, hits):
        self._hits = hits

    def retrieve(self, question, dataset_ids, *, filters=None, similarity_threshold=0.2, top_n=8, **_):
        return list(self._hits)


def test_detector_returns_old_card_when_judge_says_supersede(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)
    old = service.accept_human_approved_candidate(
        _candidate("Auth uses JWT.", "old"), approved_by="autopilot", decision_id="d1"
    )["accepted_card"]

    retired_index_bridge = _FakeRetiredIndexBridge(hits=[{"memory_id": old["memory_id"], "summary": "Auth uses JWT.", "score": 0.9}])
    detector = build_supersede_detector(
        retired_index_bridge=retired_index_bridge, judge_fn=lambda cand, oldc: "supersede", dataset_id="derived-memory-items", project=PROJECT
    )

    new_candidate = _candidate("Auth now uses OAuth.", "new")
    result = detector(new_candidate, ledger)
    assert result is not None
    assert result["memory_id"] == old["memory_id"]


def test_detector_fails_closed_when_judge_says_distinct(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = LLMBrainMemoryService(ledger)
    old = service.accept_human_approved_candidate(
        _candidate("Auth uses JWT.", "old"), approved_by="autopilot", decision_id="d1"
    )["accepted_card"]

    retired_index_bridge = _FakeRetiredIndexBridge(hits=[{"memory_id": old["memory_id"], "summary": "Auth uses JWT.", "score": 0.9}])
    detector = build_supersede_detector(
        retired_index_bridge=retired_index_bridge, judge_fn=lambda cand, oldc: "distinct", dataset_id="derived-memory-items", project=PROJECT
    )

    assert detector(_candidate("Totally unrelated CI fix.", "new"), ledger) is None


class _FakeChatRetiredIndexBridge:
    def __init__(self, answer):
        self._answer = answer

    def chat_completion(self, messages, *, llm_id=""):
        return self._answer


def test_index_judge_maps_words_and_fails_closed():
    cand = {"summary": "Auth now uses OAuth."}
    old = {"summary": "Auth uses JWT."}
    assert build_index_judge_fn(_FakeChatRetiredIndexBridge("supersede"))(cand, old) == "supersede"
    assert build_index_judge_fn(_FakeChatRetiredIndexBridge("CONFLICT - both valid"))(cand, old) == "conflict"
    assert build_index_judge_fn(_FakeChatRetiredIndexBridge("garbage answer"))(cand, old) == "distinct"

from __future__ import annotations

from agent_knowledge.llm_brain_core import BrainReadService
from agent_knowledge.llm_brain_core.document_bridge import RetiredIndexBridgeDocumentBridge


def test_index_bridge_evidence_is_labeled_external_and_does_not_override_canonical_memory():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_task",
                "task",
                "Canonical graph task",
                {
                    "task_state": "Canonical LLM-Brain task",
                    "next_action": "Keep canonical memory as winner",
                    "status": "open",
                },
            ),
            _card(
                "mem_decision",
                "decision",
                "Canonical decision",
                {
                    "decision": "Session-memory artifact and MemoryCard ledger remain canonical.",
                    "rationale": "RetiredIndexBridge is an external document bridge.",
                },
            ),
        ],
        document_bridge=RetiredIndexBridgeDocumentBridge(
            retired_index_bridge=_FakeRetiredIndexBridge(
                [
                    {
                        "result_type": "index_document",
                        "title": "External PDF note",
                        "content": "RetiredIndexBridge document says it should become the brain store.",
                        "score": 0.98,
                        "metadata": {"source_ref_id": "src_external_pdf"},
                    }
                ]
            ),
            dataset_ids=["ds_docs"],
        ),
    )

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/llm-brain-core-design",
        current_files=[],
        current_request="brain store",
        project="neurons",
    ).to_dict()

    assert pack["current_task"] == "Canonical LLM-Brain task"
    assert pack["relevant_decisions"][0]["decision"] == "Session-memory artifact and MemoryCard ledger remain canonical."
    assert pack["bridge_status"] == {
        "status": "available",
        "authority": "external_document_bridge",
        "details": ["index_read_only_bridge"],
    }
    assert pack["bridge_evidence"][0]["authority"] == "external_document_bridge"
    assert pack["bridge_evidence"][0]["title"] == "External PDF note"


def test_index_bridge_unavailable_does_not_fail_core_contextpack():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_task",
                "task",
                "Canonical task",
                {
                    "task_state": "Canonical task survives bridge outage",
                    "next_action": "Report bridge gap only",
                    "status": "open",
                },
            )
        ],
        document_bridge=RetiredIndexBridgeDocumentBridge(retired_index_bridge=_BrokenRetiredIndexBridge(), dataset_ids=["ds_docs"]),
    )

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="anything",
        project="neurons",
    ).to_dict()

    assert pack["current_task"] == "Canonical task survives bridge outage"
    assert pack["bridge_status"]["status"] == "unavailable"
    assert pack["bridge_evidence"] == []


class _FakeRetiredIndexBridge:
    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    def retrieve(self, query, dataset_ids, *, filters=None, limit=5):
        _ = query
        _ = dataset_ids
        _ = filters
        return self._chunks[:limit]


class _BrokenRetiredIndexBridge:
    def retrieve(self, *args, **kwargs):
        _ = args
        _ = kwargs
        raise RuntimeError("bridge down")


def _card(memory_id, card_type, summary, typed_payload):
    return {
        "memory_id": memory_id,
        "brain_id": "/project/neurons",
        "card_type": card_type,
        "scope": "project",
        "project": "neurons",
        "provider": "codex",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "confidence": 0.9,
        "source_refs": [{"source_ref_id": "src_bridge_test", "content_hash": _h("source")}],
        "derived_from": ["evt_bridge_test"],
        "typed_payload": typed_payload,
    }


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

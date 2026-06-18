import json

import pytest

from agent_knowledge.llm_brain_core import BrainReadService, InMemorySessionMemoryArtifactStore
from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact


def test_core_contextpack_works_with_bridge_disabled_and_graph_unavailable():
    store = InMemorySessionMemoryArtifactStore(
        [
            SessionMemoryArtifact.from_summary(
                session_id_hash=_h("session-a"),
                project="neurons",
                provider="codex",
                summary="Graph core design stopped before choosing the derived index backend.",
                source_event_ids=["evt_session_a"],
                chunk_refs=["src_chunk_a"],
            )
        ]
    )
    service = BrainReadService(
        artifact_store=store,
        memory_cards=[
            _card(
                "mem_task",
                "task",
                "neurons",
                "Graph core design",
                {
                    "task_state": "Implement LLM-brain core contracts",
                    "next_action": "Add SourceRef and ContextPack contract tests",
                    "blocker": "No graph backend selected yet",
                    "owner_hint": "neurons",
                    "status": "open",
                },
            ),
            _card(
                "mem_decision",
                "decision",
                "neurons",
                "Keep the document bridge outside core",
                {
                    "decision": "Keep the document bridge outside core acceptance.",
                    "rationale": "Canonical memory must work without external corpus credentials.",
                    "alternatives": ["document corpus as core"],
                    "consequence": "Core ContextPack has a disabled bridge status.",
                    "authority_ref": "specs/llm-brain-core-v1/design.md",
                },
            ),
            _card(
                "mem_pref",
                "preference",
                "neurons",
                "Prefer architecture before code",
                {
                    "preference": "User prefers architecture before code.",
                    "explicitness": "explicit",
                    "repeated_count": 3,
                    "confirmation_status": "confirmed",
                    "applies_to": "global",
                },
            ),
        ],
    )

    pack = service.brain_context_resolve(
        repository="/Users/example/Projects/neurons",
        branch="codex/llm-brain-core-design",
        current_files=["worker/lib/agent_knowledge/llm_brain_core/context.py"],
        current_request="continue the core implementation",
        project="neurons",
    ).to_dict()

    assert pack["current_task"] == "Implement LLM-brain core contracts"
    assert "Add SourceRef" in pack["last_stopped_at"]
    assert pack["memory_status"]["status"] == "available"
    assert pack["graph_status"]["status"] == "unavailable"
    assert pack["bridge_status"] == {
        "status": "disabled",
        "authority": "external_document_bridge",
        "details": ["not_part_of_core_read_path"],
    }
    assert pack["relevant_decisions"][0]["decision"] == "Keep the document bridge outside core acceptance."
    assert pack["persona_constraints"][0]["preference"] == "User prefers architecture before code."


def test_core_artifact_rejects_external_index_identifiers():
    artifact = SessionMemoryArtifact.from_summary(
        session_id_hash=_h("session-b"),
        project="neurons",
        provider="codex",
        summary="Safe artifact",
        source_event_ids=["evt_session_b"],
    )
    record = artifact.to_dict()
    record["document_id"] = "external_doc"
    store = InMemorySessionMemoryArtifactStore()

    with pytest.raises(ValueError, match="document_id"):
        from agent_knowledge.llm_brain_core.artifact_store import _reject_external_index_fields

        _reject_external_index_fields(record)

    assert store.upsert(artifact) == "inserted"
    assert store.upsert(artifact) == "duplicate"
    assert "document_id" not in json.dumps(store.get(artifact.artifact_id).to_dict())


def _card(memory_id, card_type, project, summary, typed_payload):
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "card_type": card_type,
        "scope": "project",
        "project": project,
        "provider": "codex",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "active",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.9,
        "confidence_basis": "fixture",
        "source_refs": [{"source_ref_id": "src_session_a", "content_hash": _h("content-a")}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "typed_payload": typed_payload,
    }


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

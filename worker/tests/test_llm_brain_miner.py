from __future__ import annotations

from agent_knowledge.session_memory.llm_brain_miner import LlmBrainEnvelopeMiner


PROJECT = "neurons"


def _chunk():
    return {
        "redacted_text": "We switched auth from JWT to OAuth this week.",
        "knowledge_id": "k1",
        "content_hash": "sha256:c1",
        "project": PROJECT,
        "provider": "codex",
    }


def test_envelope_miner_emits_cycle_ready_memory_card_candidate():
    completion = (
        '[{"card_type": "decision", "title": "auth method", '
        '"statement": "Auth now uses OAuth.", '
        '"typed_payload": {"decision": "use OAuth", "rationale": "broader support", '
        '"alternatives": ["JWT"], "consequence": "migration", "authority_ref": "adr-auth"}}]'
    )
    miner = LlmBrainEnvelopeMiner(completion_fn=lambda messages: completion)

    candidates = miner.mine_chunk(_chunk(), refresh_watermark="wm")

    assert len(candidates) == 1
    card = candidates[0]
    assert card["card_type"] == "decision"
    assert card["lifecycle_state"] == "candidate"
    assert card["memory_id"]
    assert card["brain_id"] == f"/project/{PROJECT}"
    assert card["summary"]


def _real_ragflow_chunk():
    # Real list_transcript_memory_chunks shape (probed live 2026-06-14):
    # top-level {content, content_hash, metadata{...}}, NOT a flat redacted_text/knowledge_id dict.
    return {
        "content": "We switched auth from JWT to OAuth this week.",
        "content_hash": "sha256:c1",
        "metadata": {
            "knowledge_id": "kid-1",
            "project": "workspace-ragflow-advisor",
            "provider": "codex",
            "redaction_version": "v2",
        },
    }


def test_envelope_miner_handles_real_transcript_chunk_shape():
    completion = (
        '[{"card_type": "decision", "title": "auth", "statement": "Auth now uses OAuth.", '
        '"typed_payload": {"decision": "use OAuth", "rationale": "x", "alternatives": [], '
        '"consequence": "y", "authority_ref": "adr"}}]'
    )
    miner = LlmBrainEnvelopeMiner(completion_fn=lambda messages: completion)

    candidates = miner.mine_chunk(_real_ragflow_chunk(), refresh_watermark="wm")

    assert len(candidates) == 1
    assert candidates[0]["card_type"] == "decision"
    assert candidates[0]["project"] == "workspace-ragflow-advisor"
    assert candidates[0]["brain_id"] == "/project/workspace-ragflow-advisor"


def test_envelope_miner_skips_invalid_items_without_crashing():
    completion = (
        '[{"card_type": "not_a_real_type", "title": "x", "statement": "y", "typed_payload": {}}, '
        '{"card_type": "task", "title": "ship", "statement": "ship the login flow", '
        '"typed_payload": {"task_state": "active", "next_action": "merge PR", "blocker": null, '
        '"owner_hint": "codex", "status": "active"}}]'
    )
    miner = LlmBrainEnvelopeMiner(completion_fn=lambda messages: completion)

    candidates = miner.mine_chunk(_chunk(), refresh_watermark="wm")

    assert [c["card_type"] for c in candidates] == ["task"]

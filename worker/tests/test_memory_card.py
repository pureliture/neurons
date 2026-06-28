import json

from agent_knowledge.session_memory.memory_card import (
    CANDIDATE_TYPES,
    build_memory_candidate,
    build_memory_card,
)


PROJECT = "workspace-ragflow-advisor"


def test_memory_candidate_has_deterministic_id_hash_and_manual_approval_policy():
    first = build_memory_candidate(
        candidate_type="user_preference",
        statement="User prefers Korean natural-language responses.",
        project=PROJECT,
        provider="claude",
        evidence_refs=[{"knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}],
    )
    second = build_memory_candidate(
        candidate_type="user_preference",
        statement="User prefers Korean natural-language responses.",
        project=PROJECT,
        provider="claude",
        evidence_refs=[{"knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}],
    )

    assert "user_preference" in CANDIDATE_TYPES
    assert first["candidate_id"] == second["candidate_id"]
    assert first["content_hash"] == second["content_hash"]
    assert first["approval_state"] == "pending"
    assert first["requires_manual_approval"] is True
    assert first["evidence_refs"] == [{"knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}]
    assert "raw transcript" not in json.dumps(first, sort_keys=True).lower()


def test_approved_memory_card_uses_deterministic_memory_id_without_raw_evidence_body():
    candidate = build_memory_candidate(
        candidate_type="project_decision",
        statement="Keep RAGFlow core unmodified for the Knowledge Server program.",
        project=PROJECT,
        provider="claude",
        evidence_refs=[{"knowledge_id": "kn_decision", "content_hash": "sha256:decision"}],
    )

    card = build_memory_card(candidate, approved_by="ddalkak")

    assert card["memory_id"].startswith("mem_")
    assert card["memory_id"] == build_memory_card(candidate, approved_by="ddalkak")["memory_id"]
    assert card["state"] == "active"
    assert card["card_type"] == "project_decision"
    assert card["title"] == "Project decision"
    assert card["summary"] == "Keep RAGFlow core unmodified for the Knowledge Server program."
    assert "kn_decision" not in card["summary"]
    assert "raw_text" not in json.dumps(card, sort_keys=True)


def test_memory_candidate_redacts_secret_and_private_path_before_storage():
    candidate = build_memory_candidate(
        candidate_type="procedural_rule",
        statement="Rule: use RAGFLOW_TOKEN=live-secret from /Users/example/.openclaw/" + "private/runtime/x",
        project=PROJECT,
        provider="claude",
        evidence_refs=[{"knowledge_id": "kn_secret", "content_hash": "sha256:secret"}],
    )

    serialized = json.dumps(candidate, sort_keys=True)
    assert "live-secret" not in serialized
    assert "/Users/" not in serialized
    assert "<redacted:secret>" in candidate["statement"]
    assert "<redacted:private-path>" in candidate["statement"]

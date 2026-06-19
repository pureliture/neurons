from agent_knowledge.llm_brain_core import BrainReadService, FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def test_incident_search_returns_reusable_and_do_not_apply_lanes():
    service = BrainReadService(
        graph_adapter=FakeGraphMemoryAdapter(
            [
                _episode("Incident", "incident:nats-ack", {"incident_id": "incident:nats-ack", "symptom": "NATS ack pending grows"}),
                _episode("Attempt", "attempt:nats-ack", {"incident_id": "incident:nats-ack", "attempt": "Inspect consumer ack_pending counters"}),
                _episode("Fix", "fix:nats-ack", {"incident_id": "incident:nats-ack", "fix": "Remove broad natural-key scan before ack"}),
                _episode("Verification", "verification:nats-ack", {"incident_id": "incident:nats-ack", "verification": "Pending count returned to zero"}),
                _episode(
                    "Incident",
                    "incident:nats-auth",
                    {
                        "incident_id": "incident:nats-auth",
                        "symptom": "NATS auth failure has a similar startup error",
                        "do_not_apply": True,
                    },
                ),
                _episode("Fix", "fix:nats-auth", {"incident_id": "incident:nats-auth", "fix": "Do not change ack logic for auth failures"}),
            ]
        )
    )

    result = service.brain_incident_search(symptom="NATS ack pending grows", project="neurons")

    assert result["reusable_fixes"][0]["incident_id"] == "incident:nats-ack"
    assert result["reusable_fixes"][0]["fixes"] == ["Remove broad natural-key scan before ack"]
    assert result["reusable_fixes"][0]["verifications"] == ["Pending count returned to zero"]
    assert result["do_not_apply"][0]["incident_id"] == "incident:nats-auth"
    assert result["do_not_apply"][0]["do_not_apply"] is True


def test_drift_explain_tracks_prior_and_current_decisions():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_decision_old",
                "decision",
                "Use document corpus as core memory",
                {
                    "decision": "Use document corpus as core memory.",
                    "rationale": "It was already deployed.",
                    "alternatives": [],
                    "consequence": "Core depended on external corpus state.",
                    "authority_ref": "old-design",
                },
                currentness="superseded",
                superseded_by=["mem_decision_new"],
            ),
            _card(
                "mem_decision_new",
                "decision",
                "Use local artifact and MemoryCard as core memory",
                {
                    "decision": "Use local artifact and MemoryCard as core memory.",
                    "rationale": "Portable core must survive bridge downtime.",
                    "alternatives": ["external corpus as winner"],
                    "consequence": "External corpus becomes a bridge only.",
                    "authority_ref": "specs/llm-brain-core-v1/design.md",
                },
                supersedes=["mem_decision_old"],
            ),
            _card(
                "mem_drift",
                "drift",
                "Memory authority changed",
                {
                    "subject": "core memory authority",
                    "expected_state": "external corpus as winner",
                    "observed_state": "local artifact and MemoryCard as winner",
                    "drift_kind": "design_decision",
                    "severity": "medium",
                    "authority_lane": "design",
                    "source_precedence_rank": 0.9,
                    "resolution_action": "mark_superseded",
                    "suggested_action": "Keep bridge as non-canonical",
                    "basis_refs": ["src_design"],
                },
            ),
        ]
    )

    result = service.brain_drift_explain(subject="core memory authority", project="neurons")

    assert result["status"] == "explained"
    assert result["prior_decisions"][0]["memory_id"] == "mem_decision_old"
    assert result["current_decisions"][0]["memory_id"] == "mem_decision_new"
    assert result["drift_events"][0]["memory_id"] == "mem_drift"


def test_persona_check_states():
    aligned_service = BrainReadService(
        memory_cards=[
            _card(
                "mem_pref_arch",
                "preference",
                "User prefers architecture before code.",
                {
                    "preference": "User prefers architecture before code.",
                    "explicitness": "explicit",
                    "repeated_count": 4,
                    "confirmation_status": "confirmed",
                    "applies_to": "global",
                },
            )
        ]
    )

    assert aligned_service.brain_persona_check(plan="Finalize architecture before code.", project="neurons")["status"] == "aligned"
    conflict = aligned_service.brain_persona_check(plan="Go code first and implement before design.", project="neurons")
    assert conflict["status"] == "possible_conflict"
    assert conflict["conflicts"][0]["memory_id"] == "mem_pref_arch"
    assert BrainReadService().brain_persona_check(plan="unknown")["status"] == "insufficient_evidence"

    drift_service = BrainReadService(
        memory_cards=[
            _card(
                "mem_pref_old",
                "preference",
                "User prefers cloud only deployment.",
                {
                    "preference": "User prefers cloud only deployment.",
                    "explicitness": "inferred",
                    "repeated_count": 1,
                    "confirmation_status": "unconfirmed",
                    "applies_to": "global",
                },
                currentness="superseded",
                superseded_by=["mem_pref_new"],
            )
        ]
    )
    assert drift_service.brain_persona_check(plan="Use local first deployment.", project="neurons")["status"] == "persona_drift"


def _episode(entity_type, natural_id, payload):
    payload = dict(payload)
    payload.setdefault("brain_id", "/project/neurons")
    return OntologyEpisode.from_payload(
        event_id=f"evt_{natural_id.replace(':', '_')}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        source_ref_ids=["src_incident"],
    )


def _card(memory_id, card_type, summary, typed_payload, currentness="current", supersedes=None, superseded_by=None):
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
        "judgment_state": "none",
        "status": "active",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": currentness,
        "confidence": 0.9,
        "confidence_basis": "fixture",
        "source_refs": [{"source_ref_id": "src_design"}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": list(supersedes or []),
        "superseded_by": list(superseded_by or []),
        "conflicts": [],
        "active_until": "",
        "typed_payload": typed_payload,
    }


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

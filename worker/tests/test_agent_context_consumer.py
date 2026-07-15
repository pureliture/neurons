from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from agent_knowledge.llm_brain_core.objects.agent_context_consumer import (
    AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA,
    REQUIRED_POLICY_DECISIONS,
    build_agent_context_consumer_challenge,
    build_agent_context_consumer_startup_receipt,
    build_agent_context_startup_context_request,
    build_agent_context_startup_runtime_evidence,
    validate_agent_context_consumer_startup_receipt,
)
from agent_knowledge.public_safe_util import hash_payload


NOW = datetime(2026, 7, 15, 3, 0, 0, tzinfo=timezone.utc)
PROOF_KEY = b"p9-startup-proof-key-material-32b"


def _agent_context_product() -> dict:
    return {
        "schema_version": "agent_context_product_pack.v1",
        "consumer": "codex",
        "sections": {
            "current_authority": {
                "object_count": 1,
                "authority_lanes": ["accepted_current"],
                "items": [
                    {
                        "object_id": "ko:MemoryCard:current-authority",
                        "object_type": "MemoryCard:preference",
                        "title": "Current project authority",
                        "authority_lane": "accepted_current",
                        "recommended_action": "follow",
                    }
                ],
                "gaps": [],
            },
            "style_preference": {
                "object_count": 1,
                "authority_lanes": ["accepted_current"],
                "items": [
                    {
                        "object_id": "ko:ArtifactPreference:html-review",
                        "object_type": "ArtifactPreference",
                        "title": "Prefer evidence-first HTML review artifacts.",
                        "authority_lane": "accepted_current",
                        "recommended_action": "apply_preference",
                    }
                ],
                "gaps": [],
            },
            "active_work": {
                "object_count": 1,
                "authority_lanes": ["reference_only"],
                "items": [
                    {
                        "object_id": "ko:WorkUnit:p9",
                        "object_type": "WorkUnit",
                        "title": "Continue P9 startup activation",
                        "authority_lane": "reference_only",
                        "recommended_action": "resume",
                    }
                ],
                "gaps": [],
            },
            "required_verification": {
                "object_count": 1,
                "authority_lanes": ["reference_only"],
                "items": [
                    {
                        "object_id": "ko:Test:worker",
                        "object_type": "Test",
                        "title": "cd worker && uv run pytest -q",
                        "authority_lane": "reference_only",
                        "recommended_action": "review",
                    }
                ],
                "gaps": [],
                "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            },
        },
        "surface_policy": {
            "consumer": "codex",
            "read_only": True,
            "mutation_allowed": False,
            "allowed_actions": [
                "suggest_change",
                "run_verification",
                "request_missing_evidence",
            ],
            "property_omissions": [
                "raw_body",
                "raw_source",
                "private_deploy_value",
                "secret",
            ],
        },
        "degraded_mode": {
            "active": True,
            "gaps": ["runtime_evidence_unverified"],
        },
        "freshness": {
            "stale_evidence_visible": False,
            "stale_memory_count": 0,
            "no_recent_source": False,
        },
        "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
        "action_hints": [
            {
                "action": "request_missing_evidence",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["runtime_evidence_unverified"],
            },
            {
                "action": "promote_authority",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["approved_scope_required", "runtime_evidence_unverified"],
            },
        ],
        "tool_hints": [
            {
                "tool": "brain_objects_query",
                "suggest_allowed": True,
                "execute_allowed": False,
                "production_mutation_allowed": False,
                "safe_targets": ["read_only_object_pack"],
                "blocked_targets": ["authority_write", "production_mutation"],
            }
        ],
    }


def _context_pack() -> dict:
    return {
        "schema_version": "llm_brain_context_resolve.v1",
        "authority": {"agent_context_product": _agent_context_product()},
        "gaps": ["runtime_evidence_unverified"],
    }


def _route_smokes() -> list[dict]:
    routes = (
        "authority_archive_separation",
        "code_style_preference",
        "temporal_work_recall",
        "code_change_impact",
        "html_visualization_preference",
        "deployment_runtime_truth",
    )
    smokes = [
        {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "production_mutation_performed": False,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [],
                "lanes": {},
                "gaps": [],
                "production_mutation_performed": False,
            },
        }
        for route in routes
    ]
    for smoke in smokes:
        route = smoke["route"]
        smoke["semantic_payload_hash"] = hash_payload(
            {"route": route, "content_hash": f"sha256:{route}"}
        )
        smoke["source_payload_hash"] = hash_payload(
            {
                "route": route,
                "content_hash": f"sha256:{route}",
                "observed_at": "2026-07-15T03:00:00+00:00",
            }
        )
    return smokes


def _challenge() -> dict:
    return build_agent_context_consumer_challenge(
        consumer="codex",
        project="neurons",
        repository="pureliture/neurons",
        branch="main",
        expected_commit="a" * 40,
        endpoint_origin="https://mcp.invalid",
        now=NOW,
        nonce="bounded-test-nonce",
    )


def _receipt(**overrides) -> dict:
    receipt = build_agent_context_consumer_startup_receipt(
        challenge=_challenge(),
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
        process_instance_seed="bounded-test-process",
    )
    receipt.update(overrides)
    return receipt


def test_external_consumer_receipt_binds_actual_context_events_and_policy_decisions():
    challenge = _challenge()
    receipt = _receipt()
    consumed: set[str] = set()

    failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
        consumed_challenge_hashes=consumed,
    )

    assert failures == []
    assert receipt["schema_version"] == AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA
    assert receipt["issuer"]["kind"] == "external_consumer_process"
    assert receipt["issuer"]["consumer"] == "codex"
    assert [event["type"] for event in receipt["startup_events"]] == [
        "process_started",
        "context_requested",
        "context_loaded_before_task_dispatch",
    ]
    section_manifest = receipt["context_binding"]["section_manifest"]
    assert all(len(section_manifest[name]["item_hashes"]) == 1 for name in section_manifest)
    decisions = {item["request"]["capability"]: item for item in receipt["policy_decisions"]}
    assert set(decisions) == set(REQUIRED_POLICY_DECISIONS)
    assert decisions["context.execute_direct"]["decision"]["outcome"] == "deny"
    assert decisions["context.read_private_raw"]["decision"]["outcome"] == "deny"
    assert decisions["authority.promote_without_approval_scope"]["decision"]["outcome"] == "deny"
    assert decisions["context.suggest_change"]["decision"]["outcome"] == "allow"
    assert all(item["decision"]["executor_invoked"] is False for item in decisions.values())
    assert all(item["decision"]["side_effect_count"] == 0 for item in decisions.values())
    assert receipt["io_audit"] == {
        "brain_context_resolve_calls": 1,
        "brain_objects_query_calls": 6,
        "write_tool_calls": 0,
        "task_dispatch_count_before_load": 0,
        "observation_basis": "bounded_adapter_call_accounting",
    }
    scope = challenge["scope_binding"]
    assert scope["expected_commit_binding_kind"] == "requested_source_identity_only"
    assert scope["request_hash"] == hash_payload(
        build_agent_context_startup_context_request(
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
        )
    )
    assert set(scope["route_request_hashes"]) == {
        "authority_archive_separation",
        "code_style_preference",
        "temporal_work_recall",
        "code_change_impact",
        "html_visualization_preference",
        "deployment_runtime_truth",
    }
    assert consumed == {challenge["challenge_hash"]}


def test_receipt_v2_rejects_tampered_child_observed_source_payload_hash():
    challenge = _challenge()
    receipt = _receipt()
    route = "authority_archive_separation"
    binding = receipt["context_binding"]["route_manifest"][route]

    assert binding["schema_version"] == "agent_context_route_binding.v1"
    assert binding["observed_source_payload_hash"].startswith("sha256:")
    binding["observed_source_payload_hash"] = "sha256:" + "0" * 64

    failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
    )

    assert "agent_context_startup_receipt_hash_mismatch" in failures
    assert "agent_context_startup_proof_mismatch" in failures


def test_receipt_v2_rejects_duplicate_route_smokes_before_signing():
    route_smokes = _route_smokes()
    route_smokes.append(deepcopy(route_smokes[0]))

    with pytest.raises(ValueError, match="duplicate agent context startup route"):
        build_agent_context_consumer_startup_receipt(
            challenge=_challenge(),
            proof_key=PROOF_KEY,
            context_pack=_context_pack(),
            route_smokes=route_smokes,
            now=NOW,
            process_instance_seed="bounded-test-process",
        )


def test_receipt_validation_fails_closed_for_replay_tamper_and_server_issuer():
    challenge = _challenge()
    receipt = _receipt()
    consumed: set[str] = set()
    assert validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
        consumed_challenge_hashes=consumed,
    ) == []

    replay_failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
        consumed_challenge_hashes=consumed,
    )
    tampered = deepcopy(receipt)
    tampered["context_binding"]["product_hash"] = hash_payload("tampered")
    tamper_failures = validate_agent_context_consumer_startup_receipt(
        tampered,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
    )
    server_issued = deepcopy(receipt)
    server_issued["issuer"]["kind"] = "server_runtime"
    server_failures = validate_agent_context_consumer_startup_receipt(
        server_issued,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
    )

    assert "agent_context_startup_challenge_replayed" in replay_failures
    assert "agent_context_startup_product_hash_mismatch" in tamper_failures
    assert "agent_context_startup_receipt_hash_mismatch" in tamper_failures
    assert "agent_context_startup_proof_mismatch" in tamper_failures
    assert "agent_context_startup_issuer_not_external_consumer" in server_failures


def test_runtime_evidence_derives_counts_and_enforcement_from_verified_receipt():
    challenge = _challenge()
    receipt = _receipt()
    evidence = build_agent_context_startup_runtime_evidence(
        receipt=receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
    )

    assert evidence["schema_version"] == "agent_context_startup_runtime_evidence.v1"
    assert evidence["evidence_origin"] == "external_consumer_process"
    assert evidence["startup_context"]["loaded_on_startup"] is True
    assert evidence["startup_context"]["section_counts"] == {
        "current_authority": 1,
        "style_preference": 1,
        "active_work": 1,
        "required_verification": 1,
    }
    assert evidence["runtime_enforcement"] == {
        "evidence_kind": "context_pack_policy_projection",
        "runtime_interception_observed": False,
        "executor_invocation_count": 0,
        "direct_execution_allowed": False,
        "production_mutation_allowed": False,
        "raw_private_context_blocked": True,
        "approval_scope_blocker_enforced": True,
        "suggest_change_allowed": True,
        "stale_or_degraded_disclosure_present": True,
    }
    assert evidence["receipt_validation"]["status"] == "validated"
    assert evidence["production_mutation_performed"] is False


def test_receipt_validation_rejects_partial_rehashed_challenge():
    challenge = _challenge()
    challenge.pop("nonce")
    challenge["challenge_hash"] = hash_payload(
        {key: value for key, value in challenge.items() if key != "challenge_hash"}
    )
    receipt = build_agent_context_consumer_startup_receipt(
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
        process_instance_seed="bounded-test-process",
    )

    failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
    )

    assert "agent_context_startup_challenge_shape_mismatch" in failures
    assert "agent_context_startup_challenge_nonce_missing" in failures


@pytest.mark.parametrize(
    ("scope_field", "route"),
    [
        ("request_hash", ""),
        ("expected_commit", ""),
        ("route_request_hashes", "deployment_runtime_truth"),
    ],
)
def test_receipt_validation_rejects_partial_rehashed_scope_binding(
    scope_field,
    route,
):
    challenge = _challenge()
    scope = challenge["scope_binding"]
    if route:
        scope[scope_field].pop(route)
    else:
        scope.pop(scope_field)
    scope["scope_hash"] = hash_payload(
        {key: value for key, value in scope.items() if key != "scope_hash"}
    )
    challenge["challenge_id"] = (
        "challenge:"
        + hash_payload(
            [scope, challenge["nonce"], challenge["issued_at"]]
        ).split(":", 1)[1][:24]
    )
    challenge["challenge_hash"] = hash_payload(
        {key: value for key, value in challenge.items() if key != "challenge_hash"}
    )
    receipt = build_agent_context_consumer_startup_receipt(
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
        process_instance_seed="bounded-test-process",
    )

    failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=PROOF_KEY,
        context_pack=_context_pack(),
        route_smokes=_route_smokes(),
        now=NOW,
    )

    assert any(
        failure in {
            "agent_context_startup_scope_shape_mismatch",
            "agent_context_startup_scope_route_request_shape_mismatch",
        }
        for failure in failures
    )


def test_receipt_with_empty_required_section_cannot_be_promoted_to_runtime_proof():
    context_pack = _context_pack()
    context_pack["authority"]["agent_context_product"]["sections"]["active_work"] = {
        "object_count": 0,
        "items": [],
        "authority_lanes": [],
        "gaps": ["agent_context_active_work_missing"],
    }
    receipt = build_agent_context_consumer_startup_receipt(
        challenge=_challenge(),
        proof_key=PROOF_KEY,
        context_pack=context_pack,
        route_smokes=_route_smokes(),
        now=NOW,
        process_instance_seed="bounded-test-process",
    )
    failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=_challenge(),
        proof_key=PROOF_KEY,
        context_pack=context_pack,
        route_smokes=_route_smokes(),
        now=NOW,
    )

    assert "agent_context_startup_section_missing:active_work" in failures

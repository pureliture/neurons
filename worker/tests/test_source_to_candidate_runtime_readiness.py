from __future__ import annotations

import json

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_source_to_candidate_runtime_evidence_collection_plan,
    build_source_to_candidate_runtime_readiness_report,
)


def _sanitized_live_evidence(**overrides):
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "tool_hints": _safe_tool_hints(),
        },
        "brain_objects_query_smokes": [
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("temporal_work_recall"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
        "production_denials": {
            "brain_source_to_candidate_graph": {
                "status": "denied",
                "production_mutation_performed": False,
                "mutation_performed": False,
                "network_used": False,
            },
            "brain_approval_board_decide": {
                "permission": "denied",
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
            "brain_object_proposal_create": {
                "status": "denied",
                "production_mutation_performed": False,
                "proposal_write_performed": False,
                "authority_write_performed": False,
            },
            "brain_object_decision_commit": {
                "permission": "denied",
                "production_mutation_performed": False,
                "decision_write_performed": False,
                "authority_write_performed": False,
            },
        },
        "deployed_identity": {
            "contains_expected_commit": True,
            "identity_source": "redacted_live_runtime_evidence",
        },
        "tool_schemas": {
            "brain_object_proposal_create": _object_authority_tool_schema(),
            "brain_object_decision_commit": _object_authority_tool_schema(),
        },
        "production_authority_gate": {
            "runtime_flag": "--allow-object-authority-production-writes",
            "default_enabled": False,
            "per_call_gate_required": True,
            "production_mutation_performed": False,
        },
        "evidence_provenance": _evidence_provenance(
            collection_mode="local_test_replay",
            mutation_scope="bounded_production_authority_execution",
            network_used=False,
        ),
        "production_authority_execution": _production_authority_execution_evidence(),
    }
    evidence.update(overrides)
    return evidence


def _safe_tool_hints():
    return [dict(item) for item in object_native_review_tool_hints([])]


def _brain_objects_query_smoke(route: str, *, gaps: list[str] | None = None):
    return {
        "schema_version": "brain_objects_query.v1",
        "route": route,
        "production_mutation_performed": False,
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": route,
            "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
            "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
            "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "review"}],
            "gaps": list(gaps or []),
        },
    }


def _object_authority_tool_schema():
    return {
        "inputSchema": {
            "type": "object",
            "properties": {
                "ledger_scope": {"type": "string", "enum": ["local_test", "production"]},
                "production_gate": {
                    "type": "object",
                    "properties": {
                        "approved": {"type": "boolean"},
                        "approval_ref": {"type": "string"},
                        "scope": {"type": "string", "enum": ["single_project_single_object"]},
                        "project": {"type": "string"},
                        "max_objects": {"type": "integer", "maximum": 1},
                        "configured_deployed_mcp_identity_matches_source": {"type": "boolean"},
                        "read_after_write_smoke_plan": {"type": "boolean"},
                        "rollback_or_supersession_plan": {"type": "boolean"},
                        "no_raw_private_evidence": {"type": "boolean"},
                    },
                },
            },
        },
    }


def _production_authority_execution_evidence(**overrides):
    target_object_id = "ko:RepoDocument:production-gate-smoke"
    approval_ref_hash = "sha256:" + "a" * 24
    evidence = {
        "schema_version": "object_authority_bounded_execution_evidence.v1",
        "approval": {
            "approved": True,
            "approval_ref_hash": approval_ref_hash,
            "scope": "single_project_single_object",
            "project": "workspace-index-advisor",
            "max_objects": 1,
        },
        "proposal": {
            "proposal_write_performed": True,
            "proposal_write_target": "production_ledger",
            "authority_write_performed": False,
            "production_mutation_performed": True,
            "ledger_scope": "production",
            "target_object_id": target_object_id,
            "production_gate_ref_hash": approval_ref_hash,
        },
        "decision": {
            "authority_write_performed": True,
            "authoritative_memory_changed": True,
            "production_mutation_performed": True,
            "authority_write_scope": "production_ledger",
            "ledger_scope": "production",
            "target_object_id": target_object_id,
            "decision_id": "decision:production-gate-smoke",
            "production_gate_ref_hash": approval_ref_hash,
        },
        "read_after_write": {
            "status": "validated",
            "target_object_id": target_object_id,
            "authority_lane": "rejected",
            "decision_id": "decision:production-gate-smoke",
        },
        "rollback_or_supersession": {
            "status": "planned",
            "path": [
                "write_new_authority_decision_preserving_audit_history",
                "demote_prior_object_to_accepted_non_current_or_archive_only",
                "verify_brain_objects_query_read_after_write",
            ],
        },
        "postcheck": {
            "status": "validated",
            "review_queue_status": "rejected",
            "raw_private_evidence_returned": False,
        },
        "scope": {
            "project": "workspace-index-advisor",
            "object_ids": [target_object_id],
            "max_objects": 1,
            "allowed_object_classes": ["RepoDocument"],
        },
    }
    evidence.update(overrides)
    return evidence


def _evidence_provenance(**overrides):
    provenance = {
        "schema_version": "source_to_candidate_runtime_evidence_provenance.v1",
        "collection_mode": "post_deploy_read_only_smoke",
        "collector": "redacted_operator_or_agent",
        "network_used": True,
        "mutation_scope": "none",
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    provenance.update(overrides)
    return provenance


def test_runtime_readiness_evidence_collection_plan_is_public_safe_and_read_only():
    plan = build_source_to_candidate_runtime_evidence_collection_plan(
        expected_commit="7218cb2",
        repository="pureliture/neurons",
        branch="main",
        consumer="codex",
    )

    assert plan["schema_version"] == "source_to_candidate_runtime_evidence_collection_plan.v1"
    assert plan["status"] == "ready"
    assert plan["expected_commit"] == "7218cb2"
    assert plan["repository"] == "pureliture/neurons"
    assert plan["branch"] == "main"
    assert plan["consumer"] == "codex"
    assert plan["output_schema"] == "source_to_candidate_runtime_evidence.v1"
    assert plan["evidence_provenance_schema"] == EVIDENCE_PROVENANCE_SCHEMA
    assert plan["network_used"] is False
    assert plan["production_mutation_performed"] is False
    assert plan["mutation_allowed"] is False
    assert plan["collection_mode"] == "post_deploy_read_only_smoke"
    assert plan["required_tools"] == list(REQUIRED_RUNTIME_TOOL_NAMES)
    assert plan["required_routes"] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert plan["shadow_collection_requests"] == [
        {
            "schema_version": "source_to_candidate_runtime_shadow_collection_request.v1",
            "request_id": "shadow_brain_objects_query_route_smoke",
            "status": "requested",
            "trigger": "post_deploy_route_smoke",
            "target": "configured_deployed_mcp_read_path",
            "routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
            "required_evidence_fields": [
                "brain_objects_query_smokes",
                "deployed_identity",
                "evidence_provenance",
            ],
            "forbidden_gap": "object_pack_route_not_implemented",
            "expected_gap_if_not_collected": "shadow_route_smoke_collection_pending",
            "expected_gaps_if_not_collected": [
                f"shadow_route_smoke_collection_pending:{route}"
                for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
            ],
            "network_used": False,
            "mutation_allowed": False,
            "production_mutation_performed": False,
            "readiness_claim": "request_only_not_live_evidence",
        }
    ]
    assert plan["required_production_denials"] == [
        "brain_source_to_candidate_graph",
        "brain_approval_board_decide",
        "brain_object_proposal_create",
        "brain_object_decision_commit",
    ]
    assert plan["expected_readiness_outcomes"]["no_live_evidence"] == "PASS_WITH_GAPS"
    assert plan["expected_readiness_outcomes"]["complete_sanitized_packet"] == "PASS"
    assert plan["expected_readiness_outcomes"]["unsafe_or_incomplete_packet"] == "FAIL"
    assert {step["step_id"] for step in plan["collection_steps"]} == set(plan["required_steps"])
    assert all(step["mutation_allowed"] is False for step in plan["collection_steps"])
    assert all(step["production_mutation_performed"] is False for step in plan["collection_steps"])
    assert "raw_private_transcript" in plan["forbidden_outputs"]
    assert "secret_value" in plan["forbidden_outputs"]
    assert "host_topology" in plan["forbidden_outputs"]
    assert "raw_dataset_id" in plan["forbidden_outputs"]
    assert "raw_document_id" in plan["forbidden_outputs"]


def test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation():
    report = build_source_to_candidate_runtime_readiness_report(expected_commit="7218cb2")

    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    claims = {claim["claim_id"]: claim for claim in report["claims"]}

    assert claims["local.product_surface_checks"]["status"] == "validated"
    assert claims["live.mcp.review_tools_loaded"]["status"] == "not_validated"
    assert claims["live.agent_context.tool_hints"]["status"] == "not_validated"
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "not_validated"
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "not_validated"
    assert claims["live.production.source_to_candidate_denial"]["status"] == "not_validated"
    assert claims["live.production.object_proposal_denial"]["status"] == "not_validated"
    assert claims["live.production.object_decision_denial"]["status"] == "not_validated"
    assert claims["live.production.object_authority_gate_policy"]["status"] == "not_validated"
    assert claims["live.production.object_authority_bounded_execution"]["status"] == "not_validated"
    assert claims["live.evidence.provenance"]["status"] == "not_validated"
    assert "live_mcp_review_tools_unverified" in report["gaps"]
    assert "live_brain_objects_query_route_smokes_unverified" in report["gaps"]
    assert "live_deployed_identity_unverified" in report["gaps"]
    assert "live_object_authority_gate_policy_unverified" in report["gaps"]
    assert "bounded_production_authority_execution_unverified" in report["gaps"]
    assert "live_evidence_provenance_unverified" in report["gaps"]


def test_runtime_readiness_passes_with_sanitized_live_evidence():
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence(),
        expected_commit="7218cb2",
    )

    assert report["status"] == "PASS"
    assert report["gaps"] == []
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.mcp.review_tools_loaded"]["status"] == "validated"
    assert claims["live.agent_context.tool_hints"]["status"] == "validated"
    assert claims["live.agent_context.product_sections"]["status"] == "validated"
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "validated"
    assert "temporal_work_recall" in claims["live.brain_objects_query.route_smokes"]["required_routes"]
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "validated"
    assert claims["live.production.source_to_candidate_denial"]["status"] == "denied_as_expected"
    assert claims["live.production.approval_board_denial"]["status"] == "denied_as_expected"
    assert claims["live.production.object_proposal_denial"]["status"] == "denied_as_expected"
    assert claims["live.production.object_decision_denial"]["status"] == "denied_as_expected"
    assert claims["live.production.object_authority_gate_policy"]["status"] == "validated"
    assert claims["live.production.object_authority_bounded_execution"]["status"] == "validated"
    assert claims["live.production.object_authority_bounded_execution"]["production_mutation_performed"] is True
    assert claims["live.production.object_authority_bounded_execution"]["read_after_write_status"] == "validated"
    assert claims["live.evidence.provenance"]["status"] == "validated"
    assert claims["live.evidence.provenance"]["collection_mode"] == "local_test_replay"
    assert claims["live.evidence.provenance"]["mutation_scope"] == "bounded_production_authority_execution"
    assert report["network_used"] is False
    assert report["evidence_collection_network_used"] is False
    assert report["evidence_provenance"]["network_used_for_evidence"] is False


def test_runtime_readiness_fails_when_live_evidence_provenance_is_missing():
    evidence = _sanitized_live_evidence()
    evidence.pop("evidence_provenance")

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    provenance = claims["live.evidence.provenance"]
    assert provenance["status"] == "failed"
    assert "live_evidence_provenance_missing" in report["gaps"]


def test_runtime_readiness_fails_when_evidence_provenance_hides_bounded_mutation_scope():
    evidence = _sanitized_live_evidence(
        evidence_provenance=_evidence_provenance(
            collection_mode="local_test_replay",
            mutation_scope="none",
            network_used=False,
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    provenance = claims["live.evidence.provenance"]
    assert provenance["status"] == "failed"
    assert "live_evidence_provenance_mutation_scope_mismatch" in report["gaps"]


def test_runtime_readiness_fails_when_evidence_provenance_reports_private_or_topology_values():
    evidence = _sanitized_live_evidence(
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="bounded_production_authority_execution",
            network_used=True,
            raw_private_evidence_returned=True,
            secret_returned=True,
            host_topology_returned=True,
            raw_external_ids_returned=True,
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert report["evidence_collection_network_used"] is True
    assert "live_evidence_provenance_raw_private_evidence_returned" in report["gaps"]
    assert "live_evidence_provenance_secret_returned" in report["gaps"]
    assert "live_evidence_provenance_host_topology_returned" in report["gaps"]
    assert "live_evidence_provenance_raw_external_ids_returned" in report["gaps"]


def test_runtime_readiness_fails_when_bounded_production_execution_evidence_is_incomplete():
    evidence = _sanitized_live_evidence(
        production_authority_execution=_production_authority_execution_evidence(
            approval={"approved": True, "approval_ref_hash": "", "scope": "single_project_single_object", "max_objects": 2},
            read_after_write={"status": "missing"},
            postcheck={"status": "validated", "raw_private_evidence_returned": True},
            scope={
                "project": "workspace-index-advisor",
                "object_ids": [
                    "ko:RepoDocument:production-gate-smoke",
                    "ko:RepoDocument:second-object",
                ],
                "max_objects": 2,
                "allowed_object_classes": ["RepoDocument"],
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    execution = claims["live.production.object_authority_bounded_execution"]
    assert execution["status"] == "failed"
    assert "bounded_execution_approval_ref_hash_missing" in report["gaps"]
    assert "bounded_execution_max_objects_not_one" in report["gaps"]
    assert "bounded_execution_read_after_write_missing" in report["gaps"]
    assert "bounded_execution_raw_private_evidence_returned" in report["gaps"]


def test_runtime_readiness_reports_bounded_execution_gate_hash_mismatch():
    evidence = _sanitized_live_evidence(
        production_authority_execution=_production_authority_execution_evidence(
            proposal={
                "proposal_write_performed": True,
                "proposal_write_target": "production_ledger",
                "authority_write_performed": False,
                "production_mutation_performed": True,
                "ledger_scope": "production",
                "target_object_id": "ko:RepoDocument:production-gate-smoke",
                "production_gate_ref_hash": "sha256:" + "b" * 24,
            }
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    execution = claims["live.production.object_authority_bounded_execution"]
    assert execution["status"] == "failed"
    assert "bounded_execution_gate_hash_mismatch" in report["gaps"]


def test_runtime_readiness_keeps_fr8_route_out_of_required_live_smokes_until_deployed():
    assert "code_change_impact" not in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES


def test_runtime_readiness_fails_when_agent_context_tool_hint_allows_execution_or_mutation():
    tool_hints = _safe_tool_hints()
    for hint in tool_hints:
        if hint["tool"] == "brain_source_to_candidate_graph":
            hint["execute_allowed"] = True
            hint["production_mutation_allowed"] = True
            hint["safe_targets"] = []

    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
            "surface_policy": {"mutation_allowed": False},
            "tool_hints": tool_hints,
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    hint_claim = claims["live.agent_context.tool_hints"]
    assert hint_claim["status"] == "failed"
    assert "brain_source_to_candidate_graph_tool_hint_execute_allowed" in report["gaps"]
    assert "brain_source_to_candidate_graph_tool_hint_production_mutation_allowed" in report["gaps"]
    assert "brain_source_to_candidate_graph_tool_hint_safe_targets_missing" in report["gaps"]


def test_runtime_readiness_fails_when_approval_board_hint_lacks_approved_scope_blocker():
    tool_hints = _safe_tool_hints()
    for hint in tool_hints:
        if hint["tool"] == "brain_approval_board_decide":
            hint["blocked_by"] = []

    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
            "surface_policy": {"mutation_allowed": False},
            "tool_hints": tool_hints,
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert "brain_approval_board_decide_tool_hint_approved_scope_blocker_missing" in report["gaps"]


def test_runtime_readiness_fails_when_runtime_readiness_hint_omits_sanitized_target_policy():
    tool_hints = _safe_tool_hints()
    for hint in tool_hints:
        if hint["tool"] == "brain_source_to_candidate_runtime_readiness":
            hint["safe_targets"] = ["runtime_evidence"]
            hint["blocked_targets"] = ["production_mutation"]

    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "tool_hints": tool_hints,
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert (
        "brain_source_to_candidate_runtime_readiness_tool_hint_sanitized_evidence_target_missing"
        in report["gaps"]
    )
    assert (
        "brain_source_to_candidate_runtime_readiness_tool_hint_raw_private_blocker_missing"
        in report["gaps"]
    )


def test_runtime_readiness_fails_when_object_authority_gate_schema_is_missing():
    evidence = _sanitized_live_evidence(
        tool_schemas={
            "brain_object_proposal_create": {
                "inputSchema": {
                    "type": "object",
                    "properties": {"ledger_scope": {"type": "string"}},
                }
            },
            "brain_object_decision_commit": _object_authority_tool_schema(),
        }
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    gate_claim = claims["live.production.object_authority_gate_policy"]
    assert gate_claim["status"] == "failed"
    assert "brain_object_proposal_create_production_gate_schema_missing" in report["gaps"]


def test_runtime_readiness_fails_when_object_authority_gate_schema_is_partial():
    partial_schema = _object_authority_tool_schema()
    gate_properties = partial_schema["inputSchema"]["properties"]["production_gate"]["properties"]
    gate_properties.pop("approval_ref")
    evidence = _sanitized_live_evidence(
        tool_schemas={
            "brain_object_proposal_create": partial_schema,
            "brain_object_decision_commit": _object_authority_tool_schema(),
        }
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert "brain_object_proposal_create_production_gate_schema_missing" in report["gaps"]


def test_runtime_readiness_fails_when_object_authority_runtime_opt_in_is_unsafe():
    evidence = _sanitized_live_evidence(
        production_authority_gate={
            "runtime_flag": "--allow-object-authority-production-writes",
            "default_enabled": True,
            "per_call_gate_required": False,
            "production_mutation_performed": False,
        }
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    gate_claim = claims["live.production.object_authority_gate_policy"]
    assert gate_claim["status"] == "failed"
    assert "object_authority_production_runtime_default_enabled" in report["gaps"]
    assert "object_authority_production_per_call_gate_not_required" in report["gaps"]


def test_runtime_readiness_reports_schema_and_runtime_gate_failures_together():
    evidence = _sanitized_live_evidence(
        tool_schemas={
            "brain_object_proposal_create": {
                "inputSchema": {
                    "type": "object",
                    "properties": {"ledger_scope": {"type": "string"}},
                }
            },
            "brain_object_decision_commit": _object_authority_tool_schema(),
        },
        production_authority_gate={
            "runtime_flag": "--unexpected-flag",
            "default_enabled": True,
            "per_call_gate_required": False,
            "production_mutation_performed": True,
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert "brain_object_proposal_create_production_gate_schema_missing" in report["gaps"]
    assert "object_authority_production_runtime_flag_unverified" in report["gaps"]
    assert "object_authority_production_runtime_default_enabled" in report["gaps"]
    assert "object_authority_production_per_call_gate_not_required" in report["gaps"]
    assert "unexpected_production_mutation" in report["gaps"]


def test_runtime_readiness_fails_when_live_object_query_smoke_falls_back_to_unimplemented_route():
    evidence = _sanitized_live_evidence(
        brain_objects_query_smokes=[
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("temporal_work_recall"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["object_pack_route_not_implemented"]),
        ],
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    route_claim = claims["live.brain_objects_query.route_smokes"]
    assert route_claim["status"] == "failed"
    assert route_claim["unimplemented_routes"] == ["deployment_runtime_truth"]
    assert "brain_objects_query_route_unimplemented:deployment_runtime_truth" in report["gaps"]
    assert "shadow_route_smoke_not_implemented:deployment_runtime_truth" in report["gaps"]


def test_runtime_readiness_marks_current_session_unimplemented_route_as_gap_without_identity():
    evidence = _sanitized_live_evidence(
        brain_objects_query_smokes=[
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("temporal_work_recall"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["object_pack_route_not_implemented"]),
        ],
        deployed_identity={
            "contains_expected_commit": False,
            "identity_source": "redacted_current_session_mcp",
        },
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
        production_authority_execution={},
    )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit="f9ea751",
    )

    assert report["status"] == "PASS_WITH_GAPS"
    assert report["production_mutation_performed"] is False
    assert "live.brain_objects_query.route_smokes" not in report["failed_claims"]
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    route_claim = claims["live.brain_objects_query.route_smokes"]
    assert route_claim["status"] == "not_validated"
    assert route_claim["unimplemented_routes"] == ["deployment_runtime_truth"]
    assert "brain_objects_query_route_unimplemented:deployment_runtime_truth" in report["gaps"]
    assert "shadow_route_smoke_not_implemented:deployment_runtime_truth" in report["gaps"]
    assert "live_deployed_identity_expected_commit_unverified" in report["gaps"]
    assert "bounded_production_authority_execution_unverified" in report["gaps"]


def test_runtime_readiness_keeps_missing_route_gaps_visible_when_route_smoke_fails():
    evidence = _sanitized_live_evidence(
        brain_objects_query_smokes=[
            _brain_objects_query_smoke("authority_archive_separation", gaps=["object_pack_route_not_implemented"]),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    route_claim = claims["live.brain_objects_query.route_smokes"]
    assert route_claim["status"] == "failed"
    assert route_claim["missing_routes"] == ["code_style_preference", "temporal_work_recall"]
    assert "brain_objects_query_route_unimplemented:authority_archive_separation" in report["gaps"]
    assert "shadow_route_smoke_not_implemented:authority_archive_separation" in report["gaps"]
    assert "live_brain_objects_query_route_missing:code_style_preference" in report["gaps"]
    assert "live_brain_objects_query_route_missing:temporal_work_recall" in report["gaps"]


def test_runtime_readiness_requires_temporal_work_recall_live_smoke():
    evidence = _sanitized_live_evidence(
        brain_objects_query_smokes=[
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "PASS_WITH_GAPS"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    route_claim = claims["live.brain_objects_query.route_smokes"]
    assert route_claim["status"] == "not_validated"
    assert "temporal_work_recall" in route_claim["missing_routes"]
    assert "live_brain_objects_query_route_smokes_unverified" in report["gaps"]


def test_runtime_readiness_breaks_partial_live_evidence_into_actionable_gap_ids():
    evidence = _sanitized_live_evidence(
        tool_names=[
            "brain_objects_query",
            "brain_source_to_candidate_graph",
            "brain_approval_board_decide",
        ],
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "tool_hints": [
                hint
                for hint in _safe_tool_hints()
                if hint["tool"] not in {"brain_candidate_review_edit", "brain_source_to_candidate_runtime_readiness"}
            ],
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 0},
                "required_verification": {"object_count": 1},
            },
        },
        brain_objects_query_smokes=[
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
        production_denials={
            "brain_source_to_candidate_graph": {
                "status": "denied",
                "production_mutation_performed": False,
                "mutation_performed": False,
            },
            "brain_approval_board_decide": {
                "permission": "denied",
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
        },
        deployed_identity={
            "contains_expected_commit": False,
            "identity_source": "redacted_live_runtime_evidence",
        },
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
        production_authority_execution={},
    )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit="d8113d2",
    )

    assert report["status"] == "PASS_WITH_GAPS"
    assert "live_mcp_tool_missing:brain_candidate_review_edit" in report["gaps"]
    assert "live_mcp_tool_missing:brain_source_to_candidate_runtime_readiness" in report["gaps"]
    assert "live_agent_context_tool_hint_missing:brain_candidate_review_edit" in report["gaps"]
    assert "live_agent_context_tool_hint_missing:brain_source_to_candidate_runtime_readiness" in report["gaps"]
    assert "live_agent_context_section_missing:active_work" in report["gaps"]
    assert "live_brain_objects_query_route_missing:code_style_preference" in report["gaps"]
    assert "live_brain_objects_query_route_missing:temporal_work_recall" in report["gaps"]
    assert "live_deployed_identity_expected_commit_unverified" in report["gaps"]
    assert "brain_object_proposal_create_production_denial_unverified" in report["gaps"]
    assert "brain_object_decision_commit_production_denial_unverified" in report["gaps"]
    assert report["production_mutation_performed"] is False


def test_runtime_readiness_requires_live_agent_context_product_sections():
    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "tool_hints": _safe_tool_hints(),
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 0},
                "required_verification": {"object_count": 1},
            },
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "PASS_WITH_GAPS"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    section_claim = claims["live.agent_context.product_sections"]
    assert section_claim["status"] == "not_validated"
    assert section_claim["missing_sections"] == ["active_work"]
    assert "live_agent_context_product_sections_unverified" in report["gaps"]


def test_runtime_readiness_fails_when_live_agent_context_product_contract_is_incomplete():
    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "context_pack.v1",
            "consumer": "unknown-agent",
            "tool_hints": _safe_tool_hints(),
            "surface_policy": {"mutation_allowed": False},
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    section_claim = claims["live.agent_context.product_sections"]
    assert section_claim["status"] == "failed"
    assert "live_agent_context_product_schema_mismatch" in report["gaps"]
    assert "live_agent_context_consumer_unknown" in report["gaps"]
    assert "live_agent_context_degraded_gap_disclosure_missing" in report["gaps"]
    assert "live_agent_context_missing_evidence_before_promotion_missing" in report["gaps"]


def test_runtime_readiness_fails_when_live_agent_context_allows_mutation():
    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "tool_hints": _safe_tool_hints(),
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": True},
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    section_claim = claims["live.agent_context.product_sections"]
    assert section_claim["status"] == "failed"
    assert "live_agent_context_mutation_allowed" in report["gaps"]


def test_runtime_readiness_requires_proposal_and_decision_production_safety_smokes():
    evidence = _sanitized_live_evidence(
        production_denials={
            "brain_source_to_candidate_graph": {
                "status": "denied",
                "production_mutation_performed": False,
                "mutation_performed": False,
            },
            "brain_approval_board_decide": {
                "permission": "denied",
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
        }
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "PASS_WITH_GAPS"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.production.object_proposal_denial"]["status"] == "not_validated"
    assert claims["live.production.object_decision_denial"]["status"] == "not_validated"
    assert "brain_object_proposal_create_production_denial_unverified" in report["gaps"]
    assert "brain_object_decision_commit_production_denial_unverified" in report["gaps"]


def test_runtime_readiness_fails_on_unexpected_production_mutation():
    evidence = _sanitized_live_evidence(
        production_denials={
            "brain_source_to_candidate_graph": {
                "status": "allowed",
                "production_mutation_performed": True,
                "mutation_performed": True,
            },
            "brain_approval_board_decide": {
                "permission": "denied",
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
        }
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.production.source_to_candidate_denial"]["status"] == "failed"
    assert "unexpected_production_mutation" in report["gaps"]


def test_neuron_knowledge_runtime_readiness_cli_accepts_sanitized_evidence_file(tmp_path, capsys):
    evidence_file = tmp_path / "runtime-evidence.json"
    evidence_file.write_text(json.dumps(_sanitized_live_evidence()), encoding="utf-8")

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--live-evidence-file",
                str(evidence_file),
                "--expected-commit",
                "7218cb2",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS"
    assert report["production_mutation_performed"] is True


def test_neuron_knowledge_runtime_readiness_cli_outputs_evidence_collection_plan(capsys):
    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--evidence-collection-plan",
                "--expected-commit",
                "7218cb2",
                "--repository",
                "pureliture/neurons",
                "--branch",
                "main",
                "--consumer",
                "codex",
            ]
        )
        == 0
    )

    plan = json.loads(capsys.readouterr().out)
    assert plan["schema_version"] == "source_to_candidate_runtime_evidence_collection_plan.v1"
    assert plan["expected_commit"] == "7218cb2"
    assert plan["repository"] == "pureliture/neurons"
    assert plan["branch"] == "main"
    assert plan["consumer"] == "codex"
    assert plan["network_used"] is False
    assert plan["production_mutation_performed"] is False
    assert plan["mutation_allowed"] is False

from __future__ import annotations

import json

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_source_to_candidate_runtime_evidence_collection_plan,
    build_source_to_candidate_runtime_evidence_packet_template,
    build_source_to_candidate_runtime_collected_shadow_evidence_packet,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
    build_source_to_candidate_runtime_readiness_report,
    build_source_to_candidate_runtime_shadow_readiness_report,
    build_source_to_candidate_runtime_shadow_evidence_packet,
)


def _sanitized_live_evidence(**overrides):
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "current_authority": {"object_count": 1, "authority_lanes": ["accepted_current"]},
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
            _brain_objects_query_smoke("code_change_impact"),
            _brain_objects_query_smoke("html_visualization_preference"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
        "projection_join": _projection_join_runtime_evidence(),
        "source_to_candidate_review_loop": _source_to_candidate_review_loop_evidence(),
        "session_project_rollup_runtime": _session_project_rollup_runtime_evidence(),
        "preference_artifact_memory": _preference_artifact_memory_evidence(),
        "permission_sensitive_audit": _permission_sensitive_audit_evidence(),
        "agent_context_startup_runtime": _agent_context_startup_runtime_evidence(),
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
        "gitops_desired_state": {
            "schema_version": "gitops_desired_state_identity.v1",
            "images_include_expected_commit": True,
            "desired_state_source": "sanitized_ops_manifest_summary",
            "target_revision": "main",
            "production_mutation_performed": False,
        },
        "tool_schemas": {
            "brain_approval_board_decide": _object_authority_tool_schema(),
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
        "production_authority_replacement_current": _production_authority_replacement_current_evidence(),
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


def _source_to_candidate_review_loop_evidence(**overrides):
    evidence = {
        "schema_version": "source_to_candidate_review_loop_evidence.v1",
        "source_to_candidate_graph": {
            "schema_version": "source_to_candidate_graph_activation.v1",
            "status": "PASS_WITH_GAPS",
            "target_scope": "local_test",
            "pack_type": "candidate_graph_review",
            "candidate_count": 3,
            "accepted_count": 0,
            "quality_gate": {"source_to_candidate_graph": "PASS"},
            "production_mutation_performed": False,
            "mutation_performed": False,
        },
        "candidate_review_edit": {
            "schema_version": "candidate_review_edit_result.v1",
            "status": "PASS",
            "target_scope": "local_test",
            "mutation_mode": "no_mutation",
            "edited_candidate_count": 3,
            "rejected_edit_count": 0,
            "production_mutation_performed": False,
            "authority_write_performed": False,
        },
        "approval_board_decision": {
            "schema_version": "approval_board_decision_result.v1",
            "status": "PASS",
            "ledger_scope": "local_test",
            "authority_write_scope": "local_test",
            "decision_count": 1,
            "authority_write_performed": True,
            "production_mutation_performed": False,
        },
        "read_after_write": {
            "status": "validated",
            "object_pack_schema": "object_pack.v1",
            "route": "authority_archive_separation",
            "authority_lane": "accepted_current",
            "object_count": 1,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    evidence.update(overrides)
    return evidence


def _projection_join_runtime_evidence(**overrides):
    evidence = {
        "schema_version": "object_extraction_projection_join_preview.v1",
        "evidence_class": "runtime_projection_join",
        "status": "pass",
        "edge_count": 2,
        "production_mutation_performed": False,
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    evidence.update(overrides)
    return evidence


def _session_project_rollup_runtime_evidence(**overrides):
    evidence = {
        "schema_version": "session_project_rollup_runtime_evidence.v1",
        "rollup_preview": {
            "schema_version": "object_extraction_session_project_rollup_preview.v1",
            "status": "pass",
            "scope": "all_devices",
            "object_type_counts": {
                "Device": 2,
                "Session": 2,
                "Repository": 1,
                "Branch": 1,
                "WorkUnit": 1,
            },
            "edge_types": [
                "repository_has_branch",
                "session_on_device",
                "device_has_session",
                "session_in_repository",
                "repository_has_session",
                "session_on_branch",
                "branch_has_session",
                "part_of_work_unit",
                "work_unit_has_session",
            ],
            "object_count": 7,
            "edge_count": 12,
            "visible_session_count": 2,
            "all_device_session_count": 2,
            "device_count": 2,
            "production_mutation_performed": False,
        },
        "handoff_pack": {
            "schema_version": "session_project_handoff_pack.v1",
            "raw_return_capability": "denied",
            "visible_session_count": 2,
            "all_device_session_count": 2,
            "object_ref_counts": {"Session": 2, "WorkUnit": 1},
            "resume_context": {
                "schema_version": "session_project_resume_context.v1",
                "latest_session_ref_present": True,
                "work_unit_ref_count": 1,
                "production_mutation_performed": False,
            },
        },
        "read_after_write": {
            "status": "validated",
            "route": "temporal_work_recall",
            "object_pack_schema": "object_pack.v1",
            "object_types": ["WorkUnit"],
            "object_count": 1,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    evidence.update(overrides)
    return evidence


def _preference_artifact_memory_evidence(**overrides):
    accepted_object = {
        "object_id": "ko:ArtifactPreference:html-review-density",
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
    }
    proposal_object = {
        "object_id": "ko:ArtifactPreference:visualization-proposal",
        "object_type": "ArtifactPreference",
        "authority_lane": "proposal_only",
    }
    evidence = {
        "schema_version": "preference_artifact_memory_runtime_evidence.v1",
        "preference_object_pack": {
            "schema_version": "object_pack.v1",
            "route": "code_style_preference",
            "accepted_preference_count": 1,
            "proposal_preference_count": 1,
            "objects": [accepted_object, proposal_object],
            "lanes": {
                "accepted_current": [accepted_object],
                "proposal_only": [proposal_object],
            },
            "recommended_actions": [
                {"object_id": accepted_object["object_id"], "action": "apply_preference"},
                {"object_id": proposal_object["object_id"], "action": "review_inferred_preference"},
            ],
            "gaps": [],
            "production_mutation_performed": False,
        },
        "html_visualization_route_smoke": {
            "schema_version": "brain_objects_query.v1",
            "route": "html_visualization_preference",
            "production_mutation_performed": False,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": "html_visualization_preference",
                "objects": [accepted_object],
                "lanes": {"accepted_current": [accepted_object]},
                "recommended_actions": [
                    {"object_id": accepted_object["object_id"], "action": "apply_preference"}
                ],
                "gaps": [],
            },
        },
        "agent_context_preference_section": {
            "schema_version": "agent_context_product_pack.v1",
            "section": "style_preference",
            "object_count": 1,
            "accepted_preference_count": 1,
            "surface_policy": {"mutation_allowed": False},
        },
        "artifact_review_check": {
            "schema_version": "artifact_review_preference_check.v1",
            "status": "pass",
            "ui_required": False,
            "raw_artifact_body_returned": False,
            "assertions": ["accepted_html_preference_available"],
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    evidence.update(overrides)
    return evidence


def _permission_sensitive_audit_evidence(**overrides):
    event_base = {
        "schema_version": "runtime_permission_audit_event.v1",
        "event_type": "permission_sensitive_runtime_action",
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "actor_ref_hash": "sha256:" + "c" * 64,
        "request_hash": "sha256:" + "d" * 64,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    evidence = {
        "schema_version": "permission_sensitive_runtime_audit_evidence.v1",
        "audit_events": [
            {**event_base, "action": "brain_approval_board_decide"},
            {**event_base, "action": "brain_object_proposal_create"},
            {**event_base, "action": "brain_object_decision_commit"},
        ],
        "audit_store": {
            "status": "recorded",
            "event_count": 3,
            "production_mutation_performed": False,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    evidence.update(overrides)
    return evidence


def _agent_context_startup_runtime_evidence(**overrides):
    evidence = {
        "schema_version": "agent_context_startup_runtime_evidence.v1",
        "startup_context": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "loaded_on_startup": True,
            "section_counts": {
                "style_preference": 1,
                "active_work": 1,
                "required_verification": 1,
            },
            "surface_policy": {"mutation_allowed": False},
            "degraded_gap_disclosure_present": True,
            "missing_evidence_before_promotion_present": True,
        },
        "read_path_smoke": {
            "tool": "brain_objects_query",
            "read_only": True,
            "routes_checked": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
            "production_mutation_performed": False,
        },
        "runtime_enforcement": {
            "direct_execution_allowed": False,
            "production_mutation_allowed": False,
            "raw_private_context_blocked": True,
            "approval_scope_blocker_enforced": True,
            "stale_or_degraded_disclosure_present": True,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    evidence.update(overrides)
    return evidence


def _shadow_brain_objects_query_smoke(route: str):
    return {
        "schema_version": "brain_objects_query.v1",
        "route": route,
        "production_mutation_performed": False,
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": route,
            "objects": [],
            "edges": [],
            "evidence": [],
            "lanes": {},
            "recommended_actions": [],
            "gaps": ["object_pack_route_not_implemented"],
        },
    }


def _current_session_shadow_evidence_capture():
    return {
        "tool_names": ["brain_context_resolve", "brain_objects_query"],
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "style_preference": {"object_count": 0},
                "active_work": {"object_count": 0},
                "required_verification": {"object_count": 1},
            },
            "surface_policy": {"mutation_allowed": False},
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "tool_hints": [],
        },
        "brain_objects_query_smokes": [
            _shadow_brain_objects_query_smoke(route)
            for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        ],
        "deployed_identity": {
            "contains_expected_commit": False,
            "identity_source": "current_codex_session_configured_mcp_namespace",
        },
        "collection": {
            "collection_mode": "post_deploy_read_only_smoke",
            "network_used": True,
            "mutation_scope": "none",
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
    approval_ref_hash = "sha256:" + "a" * 64
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
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
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


def _production_authority_replacement_current_evidence(**overrides):
    approval_ref_hash = "sha256:" + "c" * 64
    evidence = {
        "schema_version": "object_authority_replacement_current_evidence.v1",
        "approval": {
            "approved": True,
            "approval_ref_hash": approval_ref_hash,
            "scope": "single_project_replacement_current",
            "project": "workspace-index-advisor",
            "max_objects": 2,
        },
        "prior_current": {
            "target_object_id": "ko:RepoDocument:replacement-prior-current",
            "proposal_write_performed": True,
            "proposal_write_target": "production_ledger",
            "decision_type": "commit_supersession",
            "authority_write_performed": True,
            "authoritative_memory_changed": True,
            "production_mutation_performed": True,
            "previous_authority_lane": "accepted_current",
            "new_authority_lane": "accepted_non_current",
            "ledger_scope": "production",
            "authority_write_scope": "production_ledger",
            "decision_id": "decision:p4-replacement-prior",
            "supersedes_decision_id": "decision:p4-replacement-successor",
            "production_gate_ref_hash": approval_ref_hash,
        },
        "successor_current": {
            "target_object_id": "ko:RepoDocument:replacement-successor-current",
            "proposal_write_performed": True,
            "proposal_write_target": "production_ledger",
            "decision_type": "accept_current",
            "authority_write_performed": True,
            "authoritative_memory_changed": True,
            "production_mutation_performed": True,
            "previous_authority_lane": "candidate",
            "new_authority_lane": "accepted_current",
            "ledger_scope": "production",
            "authority_write_scope": "production_ledger",
            "decision_id": "decision:p4-replacement-successor",
            "supersedes_decision_id": "decision:p4-replacement-prior",
            "production_gate_ref_hash": approval_ref_hash,
        },
        "read_after_write": {
            "status": "validated",
            "prior_authority_lane": "accepted_non_current",
            "successor_authority_lane": "accepted_current",
            "prior_decision_id": "decision:p4-replacement-prior",
            "successor_decision_id": "decision:p4-replacement-successor",
        },
        "replacement_path": [
            "demote_prior_object_to_accepted_non_current_or_archive_only",
            "promote_successor_object_to_accepted_current",
            "verify_brain_objects_query_read_after_write",
        ],
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "scope": {
            "project": "workspace-index-advisor",
            "object_ids": [
                "ko:RepoDocument:replacement-prior-current",
                "ko:RepoDocument:replacement-successor-current",
            ],
            "max_objects": 2,
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
    assert "probe_projection_join_runtime" in plan["required_steps"]
    assert plan["gap_mapping"]["probe_projection_join_runtime"] == "live_graph_qdrant_projection_join_unproven"
    assert "probe_source_to_candidate_review_loop" in plan["required_steps"]
    assert plan["gap_mapping"]["probe_source_to_candidate_review_loop"] == "live_source_to_candidate_review_loop_unverified"
    assert plan["shadow_collection_registration"] == {
        "schema_version": "source_to_candidate_runtime_shadow_collection_registration.v1",
        "registration_id": "shadow_route_smoke_post_deploy_registration",
        "status": "registration_ready",
        "registration_scope": "branch_local_request_artifact",
        "target": "external_post_deploy_runner",
        "collection_mode": "post_deploy_read_only_smoke",
        "request_ids": ["shadow_brain_objects_query_route_smoke"],
        "routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "output_schema": "source_to_candidate_runtime_evidence.v1",
        "evidence_provenance_schema": EVIDENCE_PROVENANCE_SCHEMA,
        "network_used": False,
        "mutation_allowed": False,
        "production_mutation_performed": False,
        "readiness_claim": "registration_only_not_runtime_evidence",
        "run_status": "not_run",
        "expected_gap_if_not_run": "shadow_collection_run_pending",
        "expected_gaps_if_not_run": [
            f"shadow_collection_run_pending:{route}"
            for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        ],
    }
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


def test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence():
    template = build_source_to_candidate_runtime_evidence_packet_template(
        expected_commit="7218cb2",
        repository="pureliture/neurons",
        branch="main",
        consumer="codex",
    )

    assert template["schema_version"] == "source_to_candidate_runtime_evidence_packet_template.v1"
    assert template["status"] == "template_ready"
    assert template["output_schema"] == "source_to_candidate_runtime_evidence.v1"
    assert template["expected_commit"] == "7218cb2"
    assert template["repository"] == "pureliture/neurons"
    assert template["branch"] == "main"
    assert template["consumer"] == "codex"
    assert template["network_used"] is False
    assert template["mutation_allowed"] is False
    assert template["production_mutation_performed"] is False
    assert template["readiness_claim"] == "template_only_not_runtime_evidence"
    assert template["collection_mode"] == "post_deploy_read_only_smoke"
    assert template["collection_plan_schema"] == "source_to_candidate_runtime_evidence_collection_plan.v1"
    assert template["shadow_collection_registration_id"] == "shadow_route_smoke_post_deploy_registration"
    assert template["required_tools"] == list(REQUIRED_RUNTIME_TOOL_NAMES)
    assert template["required_routes"] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert template["required_packet_fields"] == [
        "schema_version",
        "tool_names",
        "agent_context_product",
        "brain_objects_query_smokes",
        "projection_join",
        "source_to_candidate_review_loop",
        "session_project_rollup_runtime",
        "preference_artifact_memory",
        "permission_sensitive_audit",
        "agent_context_startup_runtime",
        "gitops_desired_state",
        "deployed_identity",
        "production_denials",
        "tool_schemas",
        "production_authority_gate",
        "evidence_provenance",
    ]
    assert template["packet_field_templates"]["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert "projection_join" in template["required_packet_fields"]
    assert "source_to_candidate_review_loop" in template["required_packet_fields"]
    assert "session_project_rollup_runtime" in template["required_packet_fields"]
    assert template["packet_field_templates"]["evidence_provenance"]["schema_version"] == EVIDENCE_PROVENANCE_SCHEMA
    assert (
        template["packet_field_templates"]["projection_join"]["schema_version"]
        == "object_extraction_projection_join_preview.v1"
    )
    assert template["packet_field_templates"]["projection_join"]["production_mutation_performed"] is False
    assert (
        template["packet_field_templates"]["source_to_candidate_review_loop"]["schema_version"]
        == "source_to_candidate_review_loop_evidence.v1"
    )
    assert (
        template["packet_field_templates"]["session_project_rollup_runtime"]["schema_version"]
        == "session_project_rollup_runtime_evidence.v1"
    )
    assert (
        template["packet_field_templates"]["preference_artifact_memory"]["schema_version"]
        == "preference_artifact_memory_runtime_evidence.v1"
    )
    assert (
        template["packet_field_templates"]["permission_sensitive_audit"]["schema_version"]
        == "permission_sensitive_runtime_audit_evidence.v1"
    )
    assert (
        template["packet_field_templates"]["agent_context_startup_runtime"]["schema_version"]
        == "agent_context_startup_runtime_evidence.v1"
    )
    assert template["packet_field_templates"]["evidence_provenance"]["mutation_scope"] == "none"
    assert template["packet_field_templates"]["evidence_provenance"]["network_used"] == "collector_sets_boolean"
    assert len(template["packet_field_templates"]["brain_objects_query_smokes"]) == len(
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )
    assert {
        item["route"] for item in template["packet_field_templates"]["brain_objects_query_smokes"]
    } == set(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(
        item["forbidden_gap"] == "object_pack_route_not_implemented"
        for item in template["packet_field_templates"]["brain_objects_query_smokes"]
    )
    assert all(
        item["production_mutation_performed"] is False
        for item in template["packet_field_templates"]["brain_objects_query_smokes"]
    )
    assert "raw_private_transcript" in template["forbidden_outputs"]
    assert "secret_value" in template["forbidden_outputs"]
    assert "host_topology" in template["forbidden_outputs"]
    assert "raw_dataset_id" in template["forbidden_outputs"]
    assert "raw_document_id" in template["forbidden_outputs"]


def test_runtime_evidence_contract_plan_template_and_shadow_routes_stay_in_sync():
    plan = build_source_to_candidate_runtime_evidence_collection_plan(
        expected_commit="7218cb2",
        repository="pureliture/neurons",
        branch="main",
        consumer="codex",
    )
    template = build_source_to_candidate_runtime_evidence_packet_template(
        expected_commit="7218cb2",
        repository="pureliture/neurons",
        branch="main",
        consumer="codex",
    )
    required_routes = list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)

    assert plan["required_routes"] == required_routes
    assert template["required_routes"] == required_routes
    assert plan["shadow_collection_requests"][0]["routes"] == required_routes
    assert plan["shadow_collection_registration"]["routes"] == required_routes
    assert [
        item["route"]
        for item in template["packet_field_templates"]["brain_objects_query_smokes"]
    ] == required_routes
    assert plan["shadow_collection_requests"][0]["expected_gaps_if_not_collected"] == [
        f"shadow_route_smoke_collection_pending:{route}"
        for route in required_routes
    ]
    assert plan["shadow_collection_registration"]["expected_gaps_if_not_run"] == [
        f"shadow_collection_run_pending:{route}"
        for route in required_routes
    ]


def test_runtime_readiness_shadow_evidence_normalizer_builds_public_safe_packet_without_mutation():
    packet = build_source_to_candidate_runtime_shadow_evidence_packet(
        captured_evidence=_current_session_shadow_evidence_capture()
    )

    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert packet["tool_names"] == ["brain_context_resolve", "brain_objects_query"]
    provenance = packet["evidence_provenance"]
    assert provenance["schema_version"] == EVIDENCE_PROVENANCE_SCHEMA
    assert provenance["collection_mode"] == "post_deploy_read_only_smoke"
    assert provenance["network_used"] is True
    assert provenance["mutation_scope"] == "none"
    assert provenance["raw_private_evidence_returned"] is False
    assert provenance["secret_returned"] is False
    assert provenance["host_topology_returned"] is False
    assert provenance["raw_external_ids_returned"] is False
    assert {item["route"] for item in packet["brain_objects_query_smokes"]} == set(
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )


def test_runtime_readiness_shadow_evidence_normalizer_preserves_projection_join_evidence():
    capture = _current_session_shadow_evidence_capture()
    capture["projection_join"] = _projection_join_runtime_evidence(edge_count=4)

    packet = build_source_to_candidate_runtime_shadow_evidence_packet(
        captured_evidence=capture
    )

    assert packet["projection_join"]["schema_version"] == "object_extraction_projection_join_preview.v1"
    assert packet["projection_join"]["edge_count"] == 4
    assert packet["projection_join"]["production_mutation_performed"] is False


def test_runtime_readiness_shadow_evidence_normalized_packet_evaluates_current_session_gaps():
    packet = build_source_to_candidate_runtime_shadow_evidence_packet(
        captured_evidence=_current_session_shadow_evidence_capture()
    )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit="c264b46",
    )

    assert report["status"] == "PASS_WITH_GAPS"
    assert report["failed_claims"] == []
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    assert report["evidence_collection_network_used"] is True
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        assert f"brain_objects_query_route_unimplemented:{route}" in report["gaps"]
        assert f"shadow_route_smoke_not_implemented:{route}" in report["gaps"]


def test_runtime_readiness_shadow_evidence_report_normalizes_and_evaluates_without_mutation():
    report = build_source_to_candidate_runtime_shadow_readiness_report(
        captured_evidence=_current_session_shadow_evidence_capture(),
        expected_commit="c264b46",
    )

    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["failed_claims"] == []
    assert report["live_evidence_provided"] is True
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    assert report["evidence_collection_network_used"] is True
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        assert f"brain_objects_query_route_unimplemented:{route}" in report["gaps"]
        assert f"shadow_route_smoke_not_implemented:{route}" in report["gaps"]


def test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation():
    report = build_source_to_candidate_runtime_readiness_report(expected_commit="7218cb2")

    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["evidence_is_live"] is False
    assert report["production_ready"] is False
    assert report["production_readiness"] == "not_ready"
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    claims = {claim["claim_id"]: claim for claim in report["claims"]}

    assert claims["local.product_surface_checks"]["status"] == "validated"
    assert claims["live.mcp.review_tools_loaded"]["status"] == "not_validated"
    assert claims["live.agent_context.tool_hints"]["status"] == "not_validated"
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "not_validated"
    assert claims["live.source_to_candidate.projection_join"]["status"] == "not_validated"
    assert claims["live.source_to_candidate.review_loop"]["status"] == "not_validated"
    assert claims["live.session_project.rollup"]["status"] == "not_validated"
    assert claims["live.preference_artifact.memory"]["status"] == "not_validated"
    assert claims["live.production.permission_sensitive_audit"]["status"] == "not_validated"
    assert claims["live.agent_context.startup_read_path"]["status"] == "not_validated"
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "not_validated"
    assert claims["live.production.source_to_candidate_denial"]["status"] == "not_validated"
    assert claims["live.production.object_proposal_denial"]["status"] == "not_validated"
    assert claims["live.production.object_decision_denial"]["status"] == "not_validated"
    assert claims["live.production.object_authority_gate_policy"]["status"] == "not_validated"
    assert claims["live.production.object_authority_bounded_execution"]["status"] == "not_validated"
    assert claims["live.evidence.provenance"]["status"] == "not_validated"
    assert "live_mcp_review_tools_unverified" in report["gaps"]
    assert "live_brain_objects_query_route_smokes_unverified" in report["gaps"]
    assert "live_graph_qdrant_projection_join_unproven" in report["gaps"]
    assert "live_source_to_candidate_review_loop_unverified" in report["gaps"]
    assert "live_session_project_rollup_unverified" in report["gaps"]
    assert "live_multi_device_rollup_unproven" in report["gaps"]
    assert "live_preference_artifact_memory_unverified" in report["gaps"]
    assert "accepted_preference_context_pack_live_unproven" in report["gaps"]
    assert "permission_sensitive_audit_unverified" in report["gaps"]
    assert "live_agent_context_startup_unverified" in report["gaps"]
    assert "production_startup_read_path_unproven" in report["gaps"]
    assert "live_deployed_identity_unverified" in report["gaps"]
    assert "live_object_authority_gate_policy_unverified" in report["gaps"]
    assert "bounded_production_authority_execution_unverified" in report["gaps"]
    assert "live_evidence_provenance_unverified" in report["gaps"]


def test_runtime_readiness_plan_requests_gitops_desired_state_separately_from_deployed_identity():
    plan = build_source_to_candidate_runtime_evidence_collection_plan(
        expected_commit="7218cb2",
        repository="pureliture/neurons",
        branch="main",
        consumer="codex",
    )
    template = build_source_to_candidate_runtime_evidence_packet_template(
        expected_commit="7218cb2",
        repository="pureliture/neurons",
        branch="main",
        consumer="codex",
    )

    assert "collect_gitops_desired_state" in plan["required_steps"]
    assert plan["gap_mapping"]["collect_gitops_desired_state"] == "gitops_desired_state_unverified"
    assert "gitops_desired_state" in template["required_packet_fields"]
    assert (
        template["packet_field_templates"]["gitops_desired_state"]["schema_version"]
        == "gitops_desired_state_identity.v1"
    )


def test_runtime_readiness_gitops_desired_state_does_not_replace_deployed_identity():
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "gitops_desired_state": {
            "schema_version": "gitops_desired_state_identity.v1",
            "images_include_expected_commit": True,
            "desired_state_source": "sanitized_ops_manifest_summary",
            "target_revision": "main",
            "production_mutation_performed": False,
        },
        "evidence_provenance": _evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
        "production_mutation_performed": False,
    }

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit="7218cb2",
    )

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert report["status"] == "PASS_WITH_GAPS"
    assert claims["ops.gitops_desired_state.includes_expected_commit"]["status"] == "validated"
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "not_validated"
    assert "gitops_desired_state_unverified" not in report["gaps"]
    assert "live_deployed_identity_unverified" in report["gaps"]
    assert report["production_mutation_performed"] is False


def test_runtime_readiness_fails_when_gitops_desired_state_mismatches_expected_commit():
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "gitops_desired_state": {
            "schema_version": "gitops_desired_state_identity.v1",
            "images_include_expected_commit": False,
            "desired_state_source": "sanitized_ops_manifest_summary",
            "target_revision": "main",
            "production_mutation_performed": False,
        },
        "evidence_provenance": _evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
        "production_mutation_performed": False,
    }

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit="7218cb2",
    )

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert report["status"] == "FAIL"
    assert "ops.gitops_desired_state.includes_expected_commit" in report["failed_claims"]
    assert claims["ops.gitops_desired_state.includes_expected_commit"]["status"] == "failed"
    assert "gitops_desired_state_expected_commit_mismatch" in report["gaps"]
    assert report["production_mutation_performed"] is False


def test_runtime_readiness_does_not_treat_deployed_identity_as_permission_audit():
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "deployed_identity": {
            "contains_expected_commit": True,
            "identity_source": "redacted_artifact_identity_summary",
        },
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke",
            "mutation_scope": "none",
            "network_used": True,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit="7218cb2",
    )

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["evidence_is_live"] is True
    assert report["production_ready"] is False
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["status"] == "not_validated"
    assert claims["live.evidence.provenance"]["status"] == "validated"
    assert "permission_sensitive_audit_unverified" in report["gaps"]
    assert "live_deployed_identity_unverified" not in report["gaps"]
    assert report["production_mutation_performed"] is False


def test_runtime_readiness_passes_with_sanitized_live_evidence():
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence(),
        expected_commit="7218cb2",
    )

    assert report["status"] == "PASS"
    assert report["evidence_is_live"] is False
    assert report["production_ready"] is False
    assert report["production_readiness"] == "not_ready_local_or_sanitized_evidence_only"
    assert report["gaps"] == []
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.mcp.review_tools_loaded"]["status"] == "validated"
    assert claims["live.agent_context.tool_hints"]["status"] == "validated"
    assert claims["live.agent_context.product_sections"]["status"] == "validated"
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "validated"
    assert claims["live.source_to_candidate.projection_join"]["status"] == "validated"
    assert claims["live.source_to_candidate.projection_join"]["edge_count"] == 2
    assert claims["live.source_to_candidate.review_loop"]["status"] == "validated"
    assert claims["live.source_to_candidate.review_loop"]["candidate_count"] == 3
    assert claims["live.source_to_candidate.review_loop"]["authority_write_scope"] == "local_test"
    assert claims["live.session_project.rollup"]["status"] == "validated"
    assert claims["live.session_project.rollup"]["device_count"] == 2
    assert claims["live.session_project.rollup"]["read_after_write_status"] == "validated"
    assert claims["live.preference_artifact.memory"]["status"] == "validated"
    assert claims["live.preference_artifact.memory"]["accepted_preference_count"] == 1
    assert claims["live.preference_artifact.memory"]["html_route_status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["event_count"] == 3
    assert claims["live.agent_context.startup_read_path"]["status"] == "validated"
    assert claims["live.agent_context.startup_read_path"]["startup_loaded"] is True
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


def test_runtime_readiness_fails_when_session_project_rollup_runtime_is_unsafe_or_incomplete():
    evidence = _sanitized_live_evidence(
        session_project_rollup_runtime=_session_project_rollup_runtime_evidence(
            rollup_preview={
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "status": "pass",
                "scope": "same_device",
                "object_type_counts": {"Session": 1, "WorkUnit": 1},
                "edge_types": ["part_of_work_unit"],
                "visible_session_count": 1,
                "all_device_session_count": 1,
                "device_count": 1,
                "production_mutation_performed": True,
            },
            handoff_pack={
                "schema_version": "session_project_handoff_pack.v1",
                "raw_return_capability": "allowed",
                "resume_context": {
                    "schema_version": "session_project_resume_context.v1",
                    "latest_session_ref_present": False,
                    "work_unit_ref_count": 0,
                    "production_mutation_performed": True,
                },
            },
            read_after_write={"status": "missing", "route": "temporal_work_recall"},
            postcheck={
                "status": "validated",
                "raw_private_evidence_returned": True,
                "secret_returned": True,
                "host_topology_returned": True,
                "raw_external_ids_returned": True,
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    rollup = claims["live.session_project.rollup"]
    assert rollup["status"] == "failed"
    assert rollup["production_mutation_performed"] is True
    assert "session_project_rollup_required_object_type_missing:Device" in report["gaps"]
    assert "session_project_rollup_required_edge_missing:session_on_device" in report["gaps"]
    assert "session_project_rollup_multi_device_unproven" in report["gaps"]
    assert "session_project_handoff_raw_return_not_denied" in report["gaps"]
    assert "session_project_resume_latest_session_missing" in report["gaps"]
    assert "session_project_rollup_read_after_write_missing" in report["gaps"]
    assert "session_project_rollup_raw_private_evidence_returned" in report["gaps"]
    assert "session_project_rollup_host_topology_returned" in report["gaps"]


def test_runtime_readiness_fails_when_session_project_rollup_handoff_counts_do_not_match_preview():
    rollup_evidence = _session_project_rollup_runtime_evidence()
    rollup_evidence["handoff_pack"] = {
        **rollup_evidence["handoff_pack"],
        "visible_session_count": 1,
        "object_ref_counts": {"Session": 1, "WorkUnit": 1},
    }
    evidence = _sanitized_live_evidence(session_project_rollup_runtime=rollup_evidence)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    rollup = claims["live.session_project.rollup"]
    assert rollup["status"] == "failed"
    assert "session_project_handoff_visible_session_count_mismatch" in report["gaps"]
    assert "session_project_handoff_session_ref_count_mismatch" in report["gaps"]


def test_runtime_readiness_fails_when_preference_artifact_memory_is_unsafe_or_incomplete():
    evidence = _sanitized_live_evidence(
        preference_artifact_memory=_preference_artifact_memory_evidence(
            preference_object_pack={
                "schema_version": "object_pack.v1",
                "route": "code_style_preference",
                "accepted_preference_count": 0,
                "proposal_preference_count": 0,
                "objects": [],
                "lanes": {},
                "recommended_actions": [],
                "gaps": ["accepted_artifact_preference_empty"],
                "production_mutation_performed": True,
            },
            html_visualization_route_smoke={
                "schema_version": "brain_objects_query.v1",
                "route": "html_visualization_preference",
                "production_mutation_performed": False,
                "object_pack": {
                    "schema_version": "object_pack.v1",
                    "route": "html_visualization_preference",
                    "objects": [],
                    "lanes": {},
                    "recommended_actions": [],
                    "gaps": ["object_pack_route_not_implemented", "accepted_html_preference_missing"],
                },
            },
            agent_context_preference_section={
                "schema_version": "agent_context_product_pack.v1",
                "section": "style_preference",
                "object_count": 0,
                "accepted_preference_count": 0,
                "surface_policy": {"mutation_allowed": True},
            },
            artifact_review_check={
                "schema_version": "artifact_review_preference_check.v1",
                "status": "fail",
                "ui_required": True,
                "raw_artifact_body_returned": True,
                "assertions": [],
            },
            postcheck={
                "status": "validated",
                "raw_private_evidence_returned": True,
                "secret_returned": True,
                "host_topology_returned": False,
                "raw_external_ids_returned": True,
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    preference = claims["live.preference_artifact.memory"]
    assert preference["status"] == "failed"
    assert preference["production_mutation_performed"] is True
    assert "preference_artifact_accepted_preference_missing" in report["gaps"]
    assert "preference_artifact_proposal_lane_missing" in report["gaps"]
    assert "preference_artifact_html_route_unimplemented" in report["gaps"]
    assert "preference_artifact_agent_context_missing" in report["gaps"]
    assert "preference_artifact_agent_context_mutation_allowed" in report["gaps"]
    assert "preference_artifact_review_check_failed" in report["gaps"]
    assert "preference_artifact_review_check_required_ui" in report["gaps"]
    assert "preference_artifact_raw_artifact_body_returned" in report["gaps"]
    assert "preference_artifact_raw_private_evidence_returned" in report["gaps"]
    assert "preference_artifact_secret_returned" in report["gaps"]
    assert "preference_artifact_raw_external_ids_returned" in report["gaps"]


def test_runtime_readiness_fails_when_permission_sensitive_audit_is_unsafe_or_incomplete():
    event = {
        "schema_version": "runtime_permission_audit_event.v1",
        "event_type": "permission_sensitive_runtime_action",
        "action": "brain_object_proposal_create",
        "ledger_scope": "production",
        "permission": "allowed",
        "authority_write_performed": True,
        "production_mutation_performed": True,
        "actor_ref_hash": "sha256:not-a-64-hex-digest",
        "request_hash": "sha256:" + "g" * 64,
        "protected_values_returned": True,
        "raw_private_evidence_returned": True,
        "secret_returned": True,
        "host_topology_returned": False,
        "raw_external_ids_returned": True,
    }
    evidence = _sanitized_live_evidence(
        permission_sensitive_audit=_permission_sensitive_audit_evidence(
            audit_events=[event],
            audit_store={
                "status": "missing",
                "event_count": 1,
                "production_mutation_performed": True,
            },
            postcheck={
                "status": "validated",
                "raw_private_evidence_returned": True,
                "secret_returned": True,
                "host_topology_returned": True,
                "raw_external_ids_returned": True,
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    audit = claims["live.production.permission_sensitive_audit"]
    assert audit["status"] == "failed"
    assert audit["production_mutation_performed"] is True
    assert "permission_sensitive_audit_missing_action:brain_object_decision_commit" in report["gaps"]
    assert "permission_sensitive_audit_event_not_denied:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_authority_write_performed:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_actor_hash_missing:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_request_hash_missing:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_protected_values_returned:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_raw_private_evidence_returned:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_secret_returned:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_raw_external_ids_returned:brain_object_proposal_create" in report["gaps"]
    assert "permission_sensitive_audit_store_not_recorded" in report["gaps"]
    assert "permission_sensitive_audit_raw_private_evidence_returned" in report["gaps"]
    assert "permission_sensitive_audit_host_topology_returned" in report["gaps"]


def test_runtime_readiness_fails_when_agent_context_startup_runtime_is_unsafe_or_incomplete():
    evidence = _sanitized_live_evidence(
        agent_context_startup_runtime=_agent_context_startup_runtime_evidence(
            startup_context={
                "schema_version": "agent_context_product_pack.v1",
                "consumer": "unknown-agent",
                "loaded_on_startup": False,
                "section_counts": {"style_preference": 0},
                "surface_policy": {"mutation_allowed": True},
                "degraded_gap_disclosure_present": False,
                "missing_evidence_before_promotion_present": False,
            },
            read_path_smoke={
                "tool": "brain_write",
                "read_only": False,
                "routes_checked": ["code_style_preference"],
                "production_mutation_performed": True,
            },
            runtime_enforcement={
                "direct_execution_allowed": True,
                "production_mutation_allowed": True,
                "raw_private_context_blocked": False,
                "approval_scope_blocker_enforced": False,
                "stale_or_degraded_disclosure_present": False,
            },
            postcheck={
                "status": "validated",
                "raw_private_evidence_returned": True,
                "secret_returned": False,
                "host_topology_returned": True,
                "raw_external_ids_returned": True,
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    startup = claims["live.agent_context.startup_read_path"]
    assert startup["status"] == "failed"
    assert startup["production_mutation_performed"] is True
    assert "agent_context_startup_consumer_unknown" in report["gaps"]
    assert "agent_context_startup_not_loaded" in report["gaps"]
    assert "agent_context_startup_section_missing:style_preference" in report["gaps"]
    assert "agent_context_startup_section_missing:active_work" in report["gaps"]
    assert "agent_context_startup_section_missing:required_verification" in report["gaps"]
    assert "agent_context_startup_mutation_allowed" in report["gaps"]
    assert "agent_context_startup_degraded_gap_disclosure_missing" in report["gaps"]
    assert "agent_context_startup_read_path_tool_mismatch" in report["gaps"]
    assert "agent_context_startup_read_path_not_read_only" in report["gaps"]
    assert "agent_context_startup_route_missing:authority_archive_separation" in report["gaps"]
    assert "agent_context_startup_direct_execution_allowed" in report["gaps"]
    assert "agent_context_startup_production_mutation_allowed" in report["gaps"]
    assert "agent_context_startup_raw_private_context_not_blocked" in report["gaps"]
    assert "agent_context_startup_approval_scope_blocker_missing" in report["gaps"]
    assert "agent_context_startup_raw_private_evidence_returned" in report["gaps"]
    assert "agent_context_startup_host_topology_returned" in report["gaps"]
    assert "agent_context_startup_raw_external_ids_returned" in report["gaps"]


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


def test_runtime_readiness_fails_when_read_only_provenance_claims_bounded_mutation_scope():
    evidence = _sanitized_live_evidence(
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="bounded_production_authority_execution",
            network_used=True,
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert report["production_ready"] is False
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    provenance = claims["live.evidence.provenance"]
    assert provenance["status"] == "failed"
    assert "live_evidence_provenance_read_only_mode_mutation_scope_mismatch" in report["gaps"]


def test_runtime_readiness_post_deploy_mode_without_network_does_not_claim_live_or_ready():
    evidence = _sanitized_live_evidence(
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=False,
        ),
        production_authority_execution={},
        production_authority_replacement_current={},
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "PASS_WITH_GAPS"
    assert report["evidence_is_live"] is False
    assert report["production_ready"] is False
    assert report["production_readiness"] == "not_ready"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    provenance = claims["live.evidence.provenance"]
    assert provenance["status"] == "not_validated"
    assert provenance["is_live"] is False
    assert "live_evidence_provenance_network_not_used_for_live_mode" in report["gaps"]


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


def test_runtime_readiness_fails_when_bounded_execution_postcheck_returns_forbidden_outputs():
    evidence = _sanitized_live_evidence(
        production_authority_execution=_production_authority_execution_evidence(
            postcheck={
                "status": "validated",
                "review_queue_status": "rejected",
                "raw_private_evidence_returned": False,
                "secret_returned": True,
                "host_topology_returned": True,
                "raw_external_ids_returned": True,
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    execution = claims["live.production.object_authority_bounded_execution"]
    assert execution["status"] == "failed"
    assert "bounded_execution_secret_returned" in report["gaps"]
    assert "bounded_execution_host_topology_returned" in report["gaps"]
    assert "bounded_execution_raw_external_ids_returned" in report["gaps"]


def test_runtime_readiness_requires_bounded_execution_demote_step():
    evidence = _sanitized_live_evidence(
        production_authority_execution=_production_authority_execution_evidence(
            rollback_or_supersession={
                "status": "planned",
                "path": [
                    "write_new_authority_decision_preserving_audit_history",
                    "verify_brain_objects_query_read_after_write",
                ],
            }
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    execution = claims["live.production.object_authority_bounded_execution"]
    assert execution["status"] == "failed"
    assert "bounded_execution_demote_prior_object_step_missing" in report["gaps"]


def test_runtime_readiness_validates_replacement_current_execution_evidence():
    evidence = _sanitized_live_evidence(
        production_authority_replacement_current=_production_authority_replacement_current_evidence(),
        evidence_provenance=_evidence_provenance(
            collection_mode="redacted_operator_packet",
            mutation_scope="bounded_production_authority_execution",
            network_used=True,
        ),
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    replacement = claims["live.production.object_authority_replacement_current"]
    assert replacement["status"] == "validated"
    assert replacement["production_mutation_performed"] is True
    assert replacement["prior_authority_lane"] == "accepted_non_current"
    assert replacement["successor_authority_lane"] == "accepted_current"
    assert "replacement_current_execution_unverified" not in report["gaps"]


def test_runtime_readiness_fails_when_replacement_current_skips_prior_demote():
    evidence = _sanitized_live_evidence(
        production_authority_replacement_current=_production_authority_replacement_current_evidence(
            prior_current={
                "target_object_id": "ko:RepoDocument:replacement-prior-current",
                "proposal_write_performed": True,
                "proposal_write_target": "production_ledger",
                "decision_type": "accept_current",
                "authority_write_performed": True,
                "authoritative_memory_changed": True,
                "production_mutation_performed": True,
                "previous_authority_lane": "accepted_current",
                "new_authority_lane": "accepted_current",
                "ledger_scope": "production",
                "authority_write_scope": "production_ledger",
                "decision_id": "decision:p4-replacement-prior",
                "production_gate_ref_hash": "sha256:" + "c" * 64,
            },
            read_after_write={
                "status": "validated",
                "prior_authority_lane": "accepted_current",
                "successor_authority_lane": "accepted_current",
                "prior_decision_id": "decision:p4-replacement-prior",
                "successor_decision_id": "decision:p4-replacement-successor",
            },
        ),
        evidence_provenance=_evidence_provenance(
            collection_mode="redacted_operator_packet",
            mutation_scope="bounded_production_authority_execution",
            network_used=True,
        ),
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    replacement = claims["live.production.object_authority_replacement_current"]
    assert report["status"] == "FAIL"
    assert replacement["status"] == "failed"
    assert "replacement_prior_not_demoted" in report["gaps"]
    assert "replacement_read_after_write_prior_not_demoted" in report["gaps"]


def test_runtime_readiness_fails_when_replacement_current_project_scope_mismatches():
    replacement = _production_authority_replacement_current_evidence()
    replacement["scope"] = {**replacement["scope"], "project": "other-project"}
    evidence = _sanitized_live_evidence(
        production_authority_replacement_current=replacement,
        evidence_provenance=_evidence_provenance(
            collection_mode="redacted_operator_packet",
            mutation_scope="bounded_production_authority_execution",
            network_used=True,
        ),
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    replacement_claim = claims["live.production.object_authority_replacement_current"]
    assert report["status"] == "FAIL"
    assert replacement_claim["status"] == "failed"
    assert "replacement_project_mismatch" in report["gaps"]


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


def test_runtime_readiness_requires_p5_p7_routes_for_post_deploy_live_smokes():
    assert "code_change_impact" in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    assert "html_visualization_preference" in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES


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


def test_runtime_readiness_fails_when_agent_context_tool_hint_targets_production():
    tool_hints = _safe_tool_hints()
    for hint in tool_hints:
        if hint["tool"] == "brain_approval_board_decide":
            hint["safe_targets"] = ["production"]
            hint["blocked_by"] = ["approved_scope_required"]

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
    assert "brain_approval_board_decide_tool_hint_safe_targets_not_allowed" in report["gaps"]


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
            "brain_approval_board_decide": _object_authority_tool_schema(),
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
            "brain_approval_board_decide": _object_authority_tool_schema(),
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
            "brain_approval_board_decide": _object_authority_tool_schema(),
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
    assert route_claim["route_fallback_interpretation"] == "fail_expected_deployed_identity"
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
        production_authority_replacement_current={},
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
    assert (
        route_claim["route_fallback_interpretation"]
        == "gap_until_deployed_identity_matches_expected_commit"
    )
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
    assert route_claim["route_fallback_interpretation"] == "fail_expected_deployed_identity"
    assert route_claim["missing_routes"] == [
        "code_style_preference",
        "temporal_work_recall",
        "code_change_impact",
        "html_visualization_preference",
    ]
    assert "brain_objects_query_route_unimplemented:authority_archive_separation" in report["gaps"]
    assert "shadow_route_smoke_not_implemented:authority_archive_separation" in report["gaps"]
    assert "live_brain_objects_query_route_missing:code_style_preference" in report["gaps"]
    assert "live_brain_objects_query_route_missing:temporal_work_recall" in report["gaps"]
    assert "live_brain_objects_query_route_missing:code_change_impact" in report["gaps"]
    assert "live_brain_objects_query_route_missing:html_visualization_preference" in report["gaps"]


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
        production_authority_replacement_current={},
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
                "current_authority": {"object_count": 1, "authority_lanes": ["accepted_current"]},
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


def test_runtime_readiness_requires_live_agent_context_current_authority_accepted_current():
    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "tool_hints": _safe_tool_hints(),
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "sections": {
                "current_authority": {"object_count": 1, "authority_lanes": ["reference_only"]},
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
        },
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "PASS_WITH_GAPS"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    section_claim = claims["live.agent_context.product_sections"]
    assert section_claim["status"] == "not_validated"
    assert section_claim["current_authority_object_count"] == 1
    assert section_claim["current_authority_authority_lanes"] == ["reference_only"]
    assert "live_agent_context_current_authority_accepted_current_missing" in section_claim["gaps"]
    assert "live_agent_context_current_authority_accepted_current_missing" in report["gaps"]


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


def test_runtime_readiness_fails_when_review_loop_smoke_mutates_production():
    review_loop = _source_to_candidate_review_loop_evidence(
        approval_board_decision={
            "schema_version": "approval_board_decision_result.v1",
            "status": "PASS",
            "ledger_scope": "production",
            "authority_write_scope": "production_ledger",
            "decision_count": 1,
            "authority_write_performed": True,
            "production_mutation_performed": True,
        },
    )
    evidence = _sanitized_live_evidence(source_to_candidate_review_loop=review_loop)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    review_claim = claims["live.source_to_candidate.review_loop"]
    assert review_claim["status"] == "failed"
    assert review_claim["production_mutation_performed"] is True
    assert "source_to_candidate_review_loop_production_mutation_performed" in report["gaps"]
    assert "source_to_candidate_review_loop_authority_scope_not_local_test" in report["gaps"]


def test_runtime_readiness_fails_when_review_loop_smoke_returns_private_or_incomplete_evidence():
    review_loop = _source_to_candidate_review_loop_evidence(
        candidate_review_edit={
            "schema_version": "candidate_review_edit_result.v1",
            "status": "PASS",
            "target_scope": "local_test",
            "mutation_mode": "authority_write",
            "edited_candidate_count": 3,
            "rejected_edit_count": 2,
            "production_mutation_performed": False,
            "authority_write_performed": True,
        },
        postcheck={
            "status": "validated",
            "raw_private_evidence_returned": True,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    )
    evidence = _sanitized_live_evidence(source_to_candidate_review_loop=review_loop)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    assert "source_to_candidate_review_loop_candidate_review_not_no_mutation" in report["gaps"]
    assert "source_to_candidate_review_loop_rejected_edits_present" in report["gaps"]
    assert "source_to_candidate_review_loop_raw_private_evidence_returned" in report["gaps"]


def test_runtime_readiness_fails_when_projection_join_evidence_is_unsafe_or_incomplete():
    evidence = _sanitized_live_evidence(
        projection_join=_projection_join_runtime_evidence(
            status="pass",
            edge_count=0,
            production_mutation_performed=True,
            postcheck={
                "status": "validated",
                "raw_private_evidence_returned": True,
                "secret_returned": True,
                "host_topology_returned": True,
                "raw_external_ids_returned": True,
            },
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    projection = claims["live.source_to_candidate.projection_join"]
    assert projection["status"] == "failed"
    assert projection["production_mutation_performed"] is True
    assert report["production_mutation_performed"] is True
    assert "projection_join_edge_count_missing" in report["gaps"]
    assert "projection_join_production_mutation_performed" in report["gaps"]
    assert "projection_join_raw_private_evidence_returned" in report["gaps"]
    assert "projection_join_secret_returned" in report["gaps"]
    assert "projection_join_host_topology_returned" in report["gaps"]
    assert "projection_join_raw_external_ids_returned" in report["gaps"]


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
    registration = plan["shadow_collection_registration"]
    assert registration["schema_version"] == "source_to_candidate_runtime_shadow_collection_registration.v1"
    assert registration["status"] == "registration_ready"
    assert registration["run_status"] == "not_run"
    assert registration["request_ids"] == ["shadow_brain_objects_query_route_smoke"]
    assert registration["network_used"] is False
    assert registration["mutation_allowed"] is False
    assert registration["production_mutation_performed"] is False
    assert registration["readiness_claim"] == "registration_only_not_runtime_evidence"


def test_neuron_knowledge_runtime_readiness_cli_outputs_evidence_packet_template(capsys):
    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--evidence-packet-template",
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

    template = json.loads(capsys.readouterr().out)
    assert template["schema_version"] == "source_to_candidate_runtime_evidence_packet_template.v1"
    assert template["status"] == "template_ready"
    assert template["output_schema"] == "source_to_candidate_runtime_evidence.v1"
    assert template["expected_commit"] == "7218cb2"
    assert template["repository"] == "pureliture/neurons"
    assert template["branch"] == "main"
    assert template["consumer"] == "codex"
    assert template["network_used"] is False
    assert template["mutation_allowed"] is False
    assert template["production_mutation_performed"] is False
    assert template["readiness_claim"] == "template_only_not_runtime_evidence"
    assert template["packet_field_templates"]["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert len(template["packet_field_templates"]["brain_objects_query_smokes"]) == len(
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )


def test_runtime_readiness_collector_builds_shadow_evidence_packet_without_mutation():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        tool_names=REQUIRED_RUNTIME_TOOL_NAMES,
    )

    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["collector"]["routes_collected"] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert packet["collector"]["projection_join_collected"] is True
    assert packet["collector"]["projection_join_schema"] == "object_extraction_projection_join_preview.v1"
    assert packet["collector"]["projection_join_edge_count"] >= 1
    assert packet["evidence_provenance"]["collection_mode"] == "local_test_replay"
    assert packet["evidence_provenance"]["network_used"] is False
    projection_join = packet["projection_join"]
    assert projection_join["schema_version"] == "object_extraction_projection_join_preview.v1"
    assert projection_join["evidence_class"] == "runtime_projection_join"
    assert projection_join["status"] == "pass"
    assert projection_join["edge_count"] >= 1
    assert projection_join["production_mutation_performed"] is False
    assert projection_join["postcheck"]["status"] == "validated"
    assert len(packet["brain_objects_query_smokes"]) == len(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(
        "object_pack_route_not_implemented" not in smoke.get("object_pack", {}).get("gaps", [])
        for smoke in packet["brain_objects_query_smokes"]
    )
    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    assert "live.brain_objects_query.route_smokes" not in report["failed_claims"]
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.source_to_candidate.projection_join"]["status"] == "validated"
    assert "live_graph_qdrant_projection_join_unproven" not in report["gaps"]


def test_runtime_readiness_collector_reports_projection_join_errors_public_safely():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    def broken_projection_join() -> dict:
        raise RuntimeError("raw private path should not be returned")

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        projection_join_runner=broken_projection_join,
    )

    projection_join = packet["projection_join"]
    assert projection_join["collector_error_type"] == "RuntimeError"
    assert projection_join["production_mutation_performed"] is False
    assert "raw private path" not in json.dumps(packet)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.source_to_candidate.projection_join"]["status"] == "failed"
    assert "projection_join_collector_error:RuntimeError" in report["gaps"]
    assert report["status"] == "FAIL"


def test_runtime_readiness_collector_includes_review_loop_shadow_evidence_without_live_claim():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        review_loop_runner=_source_to_candidate_review_loop_evidence,
    )

    loop = packet["source_to_candidate_review_loop"]
    assert loop["schema_version"] == "source_to_candidate_review_loop_evidence.v1"
    assert loop["source_to_candidate_graph"]["target_scope"] == "local_test"
    assert loop["candidate_review_edit"]["mutation_mode"] == "no_mutation"
    assert loop["approval_board_decision"]["authority_write_scope"] == "local_test"
    assert loop["postcheck"]["status"] == "validated"
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["evidence_provenance"]["collection_mode"] == "local_test_replay"
    assert packet["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.source_to_candidate.review_loop"]["status"] == "validated"
    assert "live_source_to_candidate_review_loop_unverified" not in report["gaps"]
    assert report["status"] == "PASS_WITH_GAPS"


def test_runtime_readiness_collector_includes_session_project_rollup_shadow_evidence():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        review_loop_runner=_source_to_candidate_review_loop_evidence,
        session_project_rollup_runner=_session_project_rollup_runtime_evidence,
    )

    rollup = packet["session_project_rollup_runtime"]
    assert rollup["schema_version"] == "session_project_rollup_runtime_evidence.v1"
    assert rollup["rollup_preview"]["scope"] == "all_devices"
    assert rollup["rollup_preview"]["device_count"] >= 2
    assert rollup["handoff_pack"]["raw_return_capability"] == "denied"
    assert rollup["read_after_write"]["route"] == "temporal_work_recall"
    assert rollup["postcheck"]["status"] == "validated"
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.session_project.rollup"]["status"] == "validated"
    assert "live_session_project_rollup_unverified" not in report["gaps"]
    assert "live_multi_device_rollup_unproven" not in report["gaps"]
    assert report["status"] == "PASS_WITH_GAPS"


def test_runtime_readiness_collector_reports_session_project_rollup_collector_errors_public_safely():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    def broken_session_rollup() -> dict:
        raise RuntimeError("sensitive path should not be returned")

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        review_loop_runner=_source_to_candidate_review_loop_evidence,
        session_project_rollup_runner=broken_session_rollup,
    )

    rollup = packet["session_project_rollup_runtime"]
    assert rollup["collector_error_type"] == "RuntimeError"
    assert "sensitive path" not in json.dumps(packet)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.session_project.rollup"]["status"] == "failed"
    assert "session_project_rollup_collector_error:RuntimeError" in report["gaps"]
    assert report["status"] == "FAIL"


def test_runtime_readiness_collector_includes_preference_artifact_memory_shadow_evidence():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        review_loop_runner=_source_to_candidate_review_loop_evidence,
        session_project_rollup_runner=_session_project_rollup_runtime_evidence,
        preference_artifact_memory_runner=_preference_artifact_memory_evidence,
    )

    preference = packet["preference_artifact_memory"]
    assert preference["schema_version"] == "preference_artifact_memory_runtime_evidence.v1"
    assert preference["preference_object_pack"]["accepted_preference_count"] >= 1
    assert preference["preference_object_pack"]["proposal_preference_count"] >= 1
    assert preference["html_visualization_route_smoke"]["route"] == "html_visualization_preference"
    assert preference["agent_context_preference_section"]["section"] == "style_preference"
    assert preference["artifact_review_check"]["status"] == "pass"
    assert preference["artifact_review_check"]["ui_required"] is False
    assert preference["artifact_review_check"]["raw_artifact_body_returned"] is False
    assert preference["postcheck"]["status"] == "validated"
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.preference_artifact.memory"]["status"] == "validated"
    assert "live_preference_artifact_memory_unverified" not in report["gaps"]
    assert "accepted_preference_context_pack_live_unproven" not in report["gaps"]
    assert report["status"] == "PASS_WITH_GAPS"


def test_runtime_readiness_collector_includes_permission_sensitive_audit_shadow_evidence():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        review_loop_runner=_source_to_candidate_review_loop_evidence,
        session_project_rollup_runner=_session_project_rollup_runtime_evidence,
        preference_artifact_memory_runner=_preference_artifact_memory_evidence,
        permission_sensitive_audit_runner=_permission_sensitive_audit_evidence,
    )

    audit = packet["permission_sensitive_audit"]
    assert audit["schema_version"] == "permission_sensitive_runtime_audit_evidence.v1"
    assert len(audit["audit_events"]) == 3
    assert {event["action"] for event in audit["audit_events"]} == {
        "brain_approval_board_decide",
        "brain_object_proposal_create",
        "brain_object_decision_commit",
    }
    assert all(event["ledger_scope"] == "production" for event in audit["audit_events"])
    assert all(event["permission"] == "denied" for event in audit["audit_events"])
    assert all(event["authority_write_performed"] is False for event in audit["audit_events"])
    assert all(event["production_mutation_performed"] is False for event in audit["audit_events"])
    assert all(
        event["actor_ref_hash"].startswith("sha256:") and len(event["actor_ref_hash"]) == 71
        for event in audit["audit_events"]
    )
    assert all(
        event["request_hash"].startswith("sha256:") and len(event["request_hash"]) == 71
        for event in audit["audit_events"]
    )
    assert audit["audit_store"]["status"] == "recorded"
    assert audit["postcheck"]["status"] == "validated"
    assert packet["collector"]["permission_sensitive_audit_schema"] == "permission_sensitive_runtime_audit_evidence.v1"
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.production.permission_sensitive_audit"]["status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["event_count"] == 3
    assert "permission_sensitive_audit_unverified" not in report["gaps"]
    assert report["status"] == "PASS_WITH_GAPS"


def test_runtime_readiness_collector_includes_agent_context_startup_shadow_evidence():
    def route_runner(route: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
                "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
                "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "request_evidence"}],
                "gaps": [],
            },
            "production_mutation_performed": False,
        }

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=route_runner,
        review_loop_runner=_source_to_candidate_review_loop_evidence,
        session_project_rollup_runner=_session_project_rollup_runtime_evidence,
        preference_artifact_memory_runner=_preference_artifact_memory_evidence,
        permission_sensitive_audit_runner=_permission_sensitive_audit_evidence,
        agent_context_startup_runner=_agent_context_startup_runtime_evidence,
    )

    startup = packet["agent_context_startup_runtime"]
    assert startup["schema_version"] == "agent_context_startup_runtime_evidence.v1"
    assert startup["startup_context"]["loaded_on_startup"] is True
    assert startup["startup_context"]["section_counts"]["style_preference"] >= 1
    assert startup["startup_context"]["section_counts"]["active_work"] >= 1
    assert startup["startup_context"]["surface_policy"]["mutation_allowed"] is False
    assert startup["read_path_smoke"]["tool"] == "brain_objects_query"
    assert startup["read_path_smoke"]["read_only"] is True
    assert set(startup["read_path_smoke"]["routes_checked"]) == set(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert startup["runtime_enforcement"]["direct_execution_allowed"] is False
    assert startup["runtime_enforcement"]["production_mutation_allowed"] is False
    assert startup["runtime_enforcement"]["raw_private_context_blocked"] is True
    assert startup["postcheck"]["status"] == "validated"
    assert packet["collector"]["agent_context_startup_schema"] == "agent_context_startup_runtime_evidence.v1"
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.agent_context.startup_read_path"]["status"] == "validated"
    assert "live_agent_context_startup_unverified" not in report["gaps"]
    assert "production_startup_read_path_unproven" not in report["gaps"]
    assert report["status"] == "PASS_WITH_GAPS"


def test_neuron_knowledge_runtime_readiness_cli_collects_shadow_evidence(capsys):
    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--collect-shadow-evidence",
                "--repository",
                "pureliture/neurons",
                "--branch",
                "codex/knowledge-object-review-flow-roadmap",
                "--consumer",
                "codex",
            ]
        )
        == 0
    )

    packet = json.loads(capsys.readouterr().out)
    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["evidence_provenance"]["collection_mode"] == "local_test_replay"
    assert packet["evidence_provenance"]["network_used"] is False
    assert packet["source_to_candidate_review_loop"]["schema_version"] == "source_to_candidate_review_loop_evidence.v1"
    assert packet["source_to_candidate_review_loop"]["candidate_review_edit"]["mutation_mode"] == "no_mutation"
    assert (
        packet["source_to_candidate_review_loop"]["approval_board_decision"]["authority_write_scope"]
        == "local_test"
    )
    assert packet["session_project_rollup_runtime"]["schema_version"] == "session_project_rollup_runtime_evidence.v1"
    assert packet["session_project_rollup_runtime"]["rollup_preview"]["device_count"] >= 2
    assert packet["preference_artifact_memory"]["schema_version"] == "preference_artifact_memory_runtime_evidence.v1"
    assert packet["preference_artifact_memory"]["preference_object_pack"]["accepted_preference_count"] >= 1
    assert packet["preference_artifact_memory"]["artifact_review_check"]["status"] == "pass"
    assert packet["permission_sensitive_audit"]["schema_version"] == "permission_sensitive_runtime_audit_evidence.v1"
    assert len(packet["permission_sensitive_audit"]["audit_events"]) == 3
    assert packet["permission_sensitive_audit"]["audit_store"]["status"] == "recorded"
    assert packet["agent_context_startup_runtime"]["schema_version"] == "agent_context_startup_runtime_evidence.v1"
    assert packet["agent_context_startup_runtime"]["startup_context"]["loaded_on_startup"] is True
    assert packet["agent_context_startup_runtime"]["read_path_smoke"]["tool"] == "brain_objects_query"
    assert packet["agent_context_startup_runtime"]["runtime_enforcement"]["production_mutation_allowed"] is False
    assert len(packet["brain_objects_query_smokes"]) == len(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(
        "object_pack_route_not_implemented" not in smoke.get("object_pack", {}).get("gaps", [])
        for smoke in packet["brain_objects_query_smokes"]
    )
    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    assert "live.brain_objects_query.route_smokes" not in report["failed_claims"]
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.source_to_candidate.review_loop"]["status"] == "validated"
    assert claims["live.session_project.rollup"]["status"] == "validated"
    assert claims["live.preference_artifact.memory"]["status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["status"] == "validated"
    assert claims["live.agent_context.startup_read_path"]["status"] == "validated"


def test_neuron_knowledge_runtime_readiness_cli_normalizes_shadow_evidence_file(tmp_path, capsys):
    capture_file = tmp_path / "shadow-evidence-capture.json"
    capture_file.write_text(
        json.dumps(_current_session_shadow_evidence_capture()),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--normalize-shadow-evidence-file",
                str(capture_file),
            ]
        )
        == 0
    )

    packet = json.loads(capsys.readouterr().out)
    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert packet["evidence_provenance"]["schema_version"] == EVIDENCE_PROVENANCE_SCHEMA
    assert packet["evidence_provenance"]["network_used"] is True
    assert len(packet["brain_objects_query_smokes"]) == len(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)


def test_neuron_knowledge_runtime_readiness_cli_normalizes_post_deploy_capture_file(tmp_path, capsys):
    capture_file = tmp_path / "post-deploy-capture.json"
    capture_file.write_text(
        json.dumps(_current_session_shadow_evidence_capture()),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--normalize-post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    packet = json.loads(capsys.readouterr().out)
    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert packet["evidence_provenance"]["schema_version"] == EVIDENCE_PROVENANCE_SCHEMA
    assert packet["evidence_provenance"]["collection_mode"] == "post_deploy_read_only_smoke"
    assert packet["evidence_provenance"]["network_used"] is True
    assert len(packet["brain_objects_query_smokes"]) == len(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)


def test_runtime_readiness_post_deploy_capture_preserves_top_level_mutation_report():
    capture = _current_session_shadow_evidence_capture()
    capture["production_mutation_performed"] = True

    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=capture,
    )

    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is True


def test_runtime_readiness_post_deploy_capture_fails_when_capture_reports_mutation():
    capture = _current_session_shadow_evidence_capture()
    capture["production_mutation_performed"] = True

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c264b46",
    )

    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "FAIL"
    assert report["production_mutation_performed"] is True
    assert "live.evidence.provenance" in report["failed_claims"]
    assert "live_evidence_provenance_mutation_scope_mismatch" in report["gaps"]


def test_runtime_readiness_post_deploy_capture_fails_empty_session_project_rollup_runtime():
    capture = _sanitized_live_evidence(session_project_rollup_runtime={})

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c264b46",
    )

    assert report["status"] == "FAIL"
    assert "live.session_project.rollup" in report["failed_claims"]
    assert "session_project_rollup_runtime_empty_or_invalid" in report["gaps"]


def test_neuron_knowledge_runtime_readiness_cli_evaluates_shadow_evidence_file(tmp_path, capsys):
    capture_file = tmp_path / "shadow-evidence-capture.json"
    capture_file.write_text(
        json.dumps(_current_session_shadow_evidence_capture()),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--shadow-evidence-file",
                str(capture_file),
                "--expected-commit",
                "c264b46",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["failed_claims"] == []
    assert report["live_evidence_provided"] is True
    assert report["production_mutation_performed"] is False
    assert report["evidence_collection_network_used"] is True
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        assert f"shadow_route_smoke_not_implemented:{route}" in report["gaps"]


def test_neuron_knowledge_runtime_readiness_cli_evaluates_post_deploy_capture_file(tmp_path, capsys):
    capture_file = tmp_path / "post-deploy-capture.json"
    capture_file.write_text(
        json.dumps(_current_session_shadow_evidence_capture()),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--post-deploy-capture-file",
                str(capture_file),
                "--expected-commit",
                "c264b46",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["failed_claims"] == []
    assert report["live_evidence_provided"] is True
    assert report["production_mutation_performed"] is False
    assert report["evidence_collection_network_used"] is True
    assert report["evidence_provenance"]["is_live"] is True
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        assert f"shadow_route_smoke_not_implemented:{route}" in report["gaps"]

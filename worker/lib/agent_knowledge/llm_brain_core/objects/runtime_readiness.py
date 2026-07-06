from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .._util import ensure_public_safe, public_safe_text
from .golden_query_eval import build_source_to_authority_quality_gate_report

REQUIRED_REVIEW_TOOL_NAMES = (
    "brain_objects_query",
    "brain_source_to_candidate_graph",
    "brain_candidate_review_edit",
    "brain_approval_board_decide",
)
REQUIRED_RUNTIME_TOOL_NAMES = (
    *REQUIRED_REVIEW_TOOL_NAMES,
    "brain_source_to_candidate_runtime_readiness",
)
REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES = (
    "authority_archive_separation",
    "code_style_preference",
    "temporal_work_recall",
    "deployment_runtime_truth",
)
REQUIRED_AGENT_CONTEXT_SECTIONS = (
    "style_preference",
    "active_work",
    "required_verification",
)
REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA = "agent_context_product_pack.v1"
ALLOWED_AGENT_CONTEXT_CONSUMERS = ("codex", "claude-code", "gemini", "hermes")
PRODUCTION_DENIAL_CLAIMS = (
    ("live.production.source_to_candidate_denial", "brain_source_to_candidate_graph"),
    ("live.production.approval_board_denial", "brain_approval_board_decide"),
    ("live.production.object_proposal_denial", "brain_object_proposal_create"),
    ("live.production.object_decision_denial", "brain_object_decision_commit"),
)
OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS = (
    "brain_object_proposal_create",
    "brain_object_decision_commit",
)
OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG = "--allow-object-authority-production-writes"
PERMISSION_SENSITIVE_AGENT_CONTEXT_TOOLS = ("brain_approval_board_decide",)
RUNTIME_READINESS_AGENT_CONTEXT_TOOL = "brain_source_to_candidate_runtime_readiness"
EVIDENCE_PROVENANCE_SCHEMA = "source_to_candidate_runtime_evidence_provenance.v1"
SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA = "session_project_rollup_runtime_evidence.v1"
SESSION_PROJECT_ROLLUP_PREVIEW_SCHEMA = "object_extraction_session_project_rollup_preview.v1"
SESSION_PROJECT_HANDOFF_SCHEMA = "session_project_handoff_pack.v1"
SESSION_PROJECT_RESUME_SCHEMA = "session_project_resume_context.v1"
PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA = "preference_artifact_memory_runtime_evidence.v1"
ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA = "artifact_review_preference_check.v1"
PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA = "permission_sensitive_runtime_audit_evidence.v1"
PERMISSION_AUDIT_EVENT_SCHEMA = "runtime_permission_audit_event.v1"
REQUIRED_SESSION_PROJECT_OBJECT_TYPES = ("Device", "Session", "Repository", "Branch", "WorkUnit")
REQUIRED_SESSION_PROJECT_EDGE_TYPES = (
    "repository_has_branch",
    "session_on_device",
    "device_has_session",
    "session_in_repository",
    "repository_has_session",
    "session_on_branch",
    "branch_has_session",
    "part_of_work_unit",
    "work_unit_has_session",
)
ALLOWED_EVIDENCE_COLLECTION_MODES = {
    "configured_mcp_read_path",
    "live_runtime_probe",
    "local_test_replay",
    "post_deploy_read_only_smoke",
    "redacted_operator_packet",
    "sanitized_file",
}
LIVE_EVIDENCE_COLLECTION_MODES = {
    "configured_mcp_read_path",
    "live_runtime_probe",
    "post_deploy_read_only_smoke",
    "redacted_operator_packet",
}
ALLOWED_EVIDENCE_MUTATION_SCOPES = {"none", "bounded_production_authority_execution"}


def build_source_to_candidate_runtime_evidence_collection_plan(
    *,
    expected_commit: str = "",
    repository: str = "",
    branch: str = "",
    consumer: str = "codex",
) -> dict[str, Any]:
    required_steps = [
        "collect_mcp_tool_inventory",
        "collect_agent_context_product",
        "probe_brain_objects_query_routes",
        "probe_source_to_candidate_review_loop",
        "probe_session_project_rollup_runtime",
        "probe_preference_artifact_memory_runtime",
        "collect_permission_sensitive_audit_runtime",
        "collect_deployed_identity",
        "probe_production_no_mutation_denials",
        "collect_object_authority_gate_policy",
        "collect_evidence_provenance",
    ]
    plan = {
        "schema_version": "source_to_candidate_runtime_evidence_collection_plan.v1",
        "status": "ready",
        "collection_mode": "post_deploy_read_only_smoke",
        "output_schema": "source_to_candidate_runtime_evidence.v1",
        "evidence_provenance_schema": EVIDENCE_PROVENANCE_SCHEMA,
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "repository": public_safe_text(str(repository or ""), max_chars=120),
        "branch": public_safe_text(str(branch or ""), max_chars=120),
        "consumer": public_safe_text(str(consumer or "codex"), max_chars=80),
        "network_used": False,
        "production_mutation_performed": False,
        "mutation_allowed": False,
        "required_steps": required_steps,
        "required_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "required_routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "required_agent_context": {
            "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
            "sections": list(REQUIRED_AGENT_CONTEXT_SECTIONS),
            "surface_policy": {"mutation_allowed": False},
            "consumer_allowlist": list(ALLOWED_AGENT_CONTEXT_CONSUMERS),
        },
        "required_production_denials": [tool_name for _, tool_name in PRODUCTION_DENIAL_CLAIMS],
        "required_tool_schema_gates": list(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS),
        "required_production_authority_gate": {
            "runtime_flag": OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG,
            "default_enabled": False,
            "per_call_gate_required": True,
            "production_mutation_performed": False,
        },
        "required_evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke",
            "mutation_scope": "none",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "collection_steps": _runtime_evidence_collection_steps(),
        "shadow_collection_registration": _shadow_collection_registration(),
        "shadow_collection_requests": [_shadow_brain_objects_query_route_smoke_request()],
        "forbidden_outputs": [
            "raw_private_transcript",
            "secret_value",
            "host_topology",
            "raw_dataset_id",
            "raw_document_id",
            "raw_private_runtime_evidence",
        ],
        "gap_mapping": {
            "collect_mcp_tool_inventory": "live_mcp_review_tools_unverified",
            "collect_agent_context_product": "live_agent_context_product_sections_unverified",
            "probe_brain_objects_query_routes": "live_brain_objects_query_route_smokes_unverified",
            "probe_source_to_candidate_review_loop": "live_source_to_candidate_review_loop_unverified",
            "probe_session_project_rollup_runtime": "live_session_project_rollup_unverified",
            "probe_preference_artifact_memory_runtime": "live_preference_artifact_memory_unverified",
            "collect_permission_sensitive_audit_runtime": "permission_sensitive_audit_unverified",
            "collect_deployed_identity": "live_deployed_identity_unverified",
            "probe_production_no_mutation_denials": "production_denial_smokes_unverified",
            "collect_object_authority_gate_policy": "live_object_authority_gate_policy_unverified",
            "collect_evidence_provenance": "live_evidence_provenance_unverified",
            "shadow_brain_objects_query_route_smoke": "shadow_route_smoke_collection_pending",
        },
        "expected_readiness_outcomes": {
            "no_live_evidence": "PASS_WITH_GAPS",
            "complete_sanitized_packet": "PASS",
            "unsafe_or_incomplete_packet": "FAIL",
        },
        "readiness_claim": "plan_only_not_runtime_evidence",
    }
    ensure_public_safe(plan, "SourceToCandidateRuntimeEvidenceCollectionPlan")
    return plan


def build_source_to_candidate_runtime_evidence_packet_template(
    *,
    expected_commit: str = "",
    repository: str = "",
    branch: str = "",
    consumer: str = "codex",
) -> dict[str, Any]:
    collection_plan = build_source_to_candidate_runtime_evidence_collection_plan(
        expected_commit=expected_commit,
        repository=repository,
        branch=branch,
        consumer=consumer,
    )
    registration = collection_plan.get("shadow_collection_registration")
    registration = registration if isinstance(registration, Mapping) else {}
    template = {
        "schema_version": "source_to_candidate_runtime_evidence_packet_template.v1",
        "status": "template_ready",
        "output_schema": "source_to_candidate_runtime_evidence.v1",
        "collection_plan_schema": str(collection_plan.get("schema_version") or ""),
        "shadow_collection_registration_id": public_safe_text(
            str(registration.get("registration_id") or ""),
            max_chars=120,
        ),
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "repository": public_safe_text(str(repository or ""), max_chars=120),
        "branch": public_safe_text(str(branch or ""), max_chars=120),
        "consumer": public_safe_text(str(consumer or "codex"), max_chars=80),
        "collection_mode": "post_deploy_read_only_smoke",
        "network_used": False,
        "mutation_allowed": False,
        "production_mutation_performed": False,
        "required_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "required_routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "required_packet_fields": [
            "schema_version",
            "tool_names",
            "agent_context_product",
            "brain_objects_query_smokes",
            "source_to_candidate_review_loop",
            "session_project_rollup_runtime",
            "preference_artifact_memory",
            "permission_sensitive_audit",
            "deployed_identity",
            "production_denials",
            "tool_schemas",
            "production_authority_gate",
            "evidence_provenance",
        ],
        "packet_field_templates": _runtime_evidence_packet_field_templates(),
        "forbidden_outputs": list(collection_plan.get("forbidden_outputs") or []),
        "readiness_claim": "template_only_not_runtime_evidence",
    }
    ensure_public_safe(template, "SourceToCandidateRuntimeEvidencePacketTemplate")
    return template


def build_source_to_candidate_runtime_shadow_evidence_packet(
    *,
    captured_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize a public-safe post-deploy shadow capture into evaluator input."""

    captured = captured_evidence if isinstance(captured_evidence, Mapping) else {}
    collection = captured.get("collection")
    collection = collection if isinstance(collection, Mapping) else {}
    provenance = captured.get("evidence_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else collection
    packet = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": _string_list(captured.get("tool_names")),
        "agent_context_product": _public_safe_mapping(captured.get("agent_context_product")),
        "brain_objects_query_smokes": _public_safe_mapping_list(captured.get("brain_objects_query_smokes")),
        "source_to_candidate_review_loop": _public_safe_mapping(captured.get("source_to_candidate_review_loop")),
        "session_project_rollup_runtime": _public_safe_mapping(captured.get("session_project_rollup_runtime")),
        "preference_artifact_memory": _public_safe_mapping(captured.get("preference_artifact_memory")),
        "permission_sensitive_audit": _public_safe_mapping(captured.get("permission_sensitive_audit")),
        "deployed_identity": _public_safe_mapping(captured.get("deployed_identity")),
        "production_denials": _public_safe_mapping(captured.get("production_denials")),
        "tool_schemas": _public_safe_mapping(captured.get("tool_schemas")),
        "production_authority_gate": _public_safe_mapping(captured.get("production_authority_gate")),
        "evidence_provenance": {
            "schema_version": public_safe_text(
                str(provenance.get("schema_version") or EVIDENCE_PROVENANCE_SCHEMA),
                max_chars=80,
            ),
            "collection_mode": public_safe_text(
                str(provenance.get("collection_mode") or "post_deploy_read_only_smoke"),
                max_chars=80,
            ),
            "network_used": provenance.get("network_used") is True,
            "mutation_scope": public_safe_text(
                str(provenance.get("mutation_scope") or "none"),
                max_chars=80,
            ),
            "raw_private_evidence_returned": _provenance_flag(provenance, "raw_private_evidence_returned"),
            "secret_returned": _provenance_flag(provenance, "secret_returned"),
            "host_topology_returned": _provenance_flag(provenance, "host_topology_returned"),
            "raw_external_ids_returned": _provenance_flag(provenance, "raw_external_ids_returned"),
        },
        "production_mutation_performed": False,
    }
    ensure_public_safe(packet, "SourceToCandidateRuntimeShadowEvidencePacket")
    return packet


def build_source_to_candidate_runtime_shadow_readiness_report(
    *,
    captured_evidence: Mapping[str, Any],
    expected_commit: str = "",
) -> dict[str, Any]:
    packet = build_source_to_candidate_runtime_shadow_evidence_packet(
        captured_evidence=captured_evidence,
    )
    return build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=expected_commit,
    )


def _runtime_evidence_packet_field_templates() -> dict[str, Any]:
    return {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": {
            "required_values": list(REQUIRED_RUNTIME_TOOL_NAMES),
            "source": "configured_deployed_mcp_tools_list",
        },
        "agent_context_product": {
            "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
            "required_sections": list(REQUIRED_AGENT_CONTEXT_SECTIONS),
            "surface_policy": {"mutation_allowed": False},
            "tool_hints_required": list(REQUIRED_RUNTIME_TOOL_NAMES),
        },
        "brain_objects_query_smokes": [
            {
                "schema_version": "brain_objects_query.v1",
                "route": route,
                "required_object_pack_schema": "object_pack.v1",
                "forbidden_gap": "object_pack_route_not_implemented",
                "production_mutation_performed": False,
            }
            for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        ],
        "source_to_candidate_review_loop": {
            "schema_version": "source_to_candidate_review_loop_evidence.v1",
            "source_to_candidate_graph": {
                "schema_version": "source_to_candidate_graph_activation.v1",
                "target_scope": "local_test",
                "pack_type": "candidate_graph_review",
                "production_mutation_performed": False,
            },
            "candidate_review_edit": {
                "schema_version": "candidate_review_edit_result.v1",
                "target_scope": "local_test",
                "mutation_mode": "no_mutation",
                "production_mutation_performed": False,
            },
            "approval_board_decision": {
                "schema_version": "approval_board_decision_result.v1",
                "ledger_scope": "local_test",
                "authority_write_scope": "local_test",
                "production_mutation_performed": False,
            },
            "read_after_write": {
                "status": "validated",
                "object_pack_schema": "object_pack.v1",
            },
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        },
        "session_project_rollup_runtime": {
            "schema_version": SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA,
            "rollup_preview": {
                "schema_version": SESSION_PROJECT_ROLLUP_PREVIEW_SCHEMA,
                "scope": "all_devices",
                "required_object_types": list(REQUIRED_SESSION_PROJECT_OBJECT_TYPES),
                "required_edge_types": list(REQUIRED_SESSION_PROJECT_EDGE_TYPES),
                "visible_session_count": "collector_sets_integer",
                "all_device_session_count": "collector_sets_integer",
                "device_count": "collector_sets_integer",
                "production_mutation_performed": False,
            },
            "handoff_pack": {
                "schema_version": SESSION_PROJECT_HANDOFF_SCHEMA,
                "raw_return_capability": "denied",
                "resume_context": {
                    "schema_version": SESSION_PROJECT_RESUME_SCHEMA,
                    "latest_session_ref_present": True,
                    "work_unit_ref_count": "collector_sets_integer",
                    "production_mutation_performed": False,
                },
            },
            "read_after_write": {
                "status": "validated",
                "route": "temporal_work_recall",
                "object_pack_schema": "object_pack.v1",
                "object_types": ["WorkUnit"],
            },
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        },
        "preference_artifact_memory": {
            "schema_version": PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA,
            "preference_object_pack": {
                "schema_version": "object_pack.v1",
                "route": "code_style_preference",
                "required_object_type": "ArtifactPreference",
                "accepted_preference_count": "collector_sets_integer",
                "proposal_preference_count": "collector_sets_integer",
                "production_mutation_performed": False,
            },
            "html_visualization_route_smoke": {
                "schema_version": "brain_objects_query.v1",
                "route": "html_visualization_preference",
                "required_object_pack_schema": "object_pack.v1",
                "required_object_type": "ArtifactPreference",
                "forbidden_gaps": [
                    "object_pack_route_not_implemented",
                    "accepted_html_preference_missing",
                    "visualization_preference_missing",
                ],
                "production_mutation_performed": False,
            },
            "agent_context_preference_section": {
                "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
                "section": "style_preference",
                "accepted_preference_count": "collector_sets_integer",
                "surface_policy": {"mutation_allowed": False},
            },
            "artifact_review_check": {
                "schema_version": ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA,
                "status": "pass",
                "ui_required": False,
                "raw_artifact_body_returned": False,
            },
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        },
        "permission_sensitive_audit": {
            "schema_version": PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA,
            "audit_events": [
                {
                    "schema_version": PERMISSION_AUDIT_EVENT_SCHEMA,
                    "event_type": "permission_sensitive_runtime_action",
                    "action": tool_name,
                    "ledger_scope": "production",
                    "permission": "denied",
                    "authority_write_performed": False,
                    "production_mutation_performed": False,
                    "actor_ref_hash": "collector_sets_sha256",
                    "request_hash": "collector_sets_sha256",
                    "protected_values_returned": False,
                    "raw_private_evidence_returned": False,
                    "secret_returned": False,
                    "host_topology_returned": False,
                    "raw_external_ids_returned": False,
                }
                for tool_name in OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS
            ],
            "audit_store": {
                "status": "recorded",
                "event_count": len(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS),
                "production_mutation_performed": False,
            },
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        },
        "deployed_identity": {
            "contains_expected_commit": "collector_sets_boolean",
            "identity_source": "redacted_artifact_identity_summary",
        },
        "production_denials": {
            tool_name: {
                "expected_result": "denied_no_mutation",
                "production_mutation_performed": False,
            }
            for _, tool_name in PRODUCTION_DENIAL_CLAIMS
        },
        "tool_schemas": {
            tool_name: {
                "must_include_production_gate": True,
                "production_mutation_performed": False,
            }
            for tool_name in OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS
        },
        "production_authority_gate": {
            "runtime_flag": OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG,
            "default_enabled": False,
            "per_call_gate_required": True,
            "production_mutation_performed": False,
        },
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke",
            "network_used": "collector_sets_boolean",
            "mutation_scope": "none",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _shadow_brain_objects_query_route_smoke_request() -> dict[str, Any]:
    return {
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


def _shadow_collection_registration() -> dict[str, Any]:
    return {
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


def _runtime_evidence_collection_steps() -> list[dict[str, Any]]:
    return [
        {
            "step_id": "collect_mcp_tool_inventory",
            "evidence_field": "tool_names",
            "required_values": list(REQUIRED_RUNTIME_TOOL_NAMES),
            "safe_target": "configured_deployed_mcp_read_path",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_agent_context_product",
            "evidence_field": "agent_context_product",
            "required_values": list(REQUIRED_AGENT_CONTEXT_SECTIONS),
            "safe_target": "sanitized_agent_context_product",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "probe_brain_objects_query_routes",
            "evidence_field": "brain_objects_query_smokes",
            "required_values": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
            "safe_target": "object_native_read_path",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "probe_source_to_candidate_review_loop",
            "evidence_field": "source_to_candidate_review_loop",
            "required_values": [
                "source_to_candidate_graph_activation.v1",
                "candidate_review_edit_result.v1",
                "approval_board_decision_result.v1",
                "object_pack.v1",
            ],
            "safe_target": "local_test_source_to_candidate_review_loop_smoke",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "probe_session_project_rollup_runtime",
            "evidence_field": "session_project_rollup_runtime",
            "required_values": [
                SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA,
                SESSION_PROJECT_ROLLUP_PREVIEW_SCHEMA,
                SESSION_PROJECT_HANDOFF_SCHEMA,
                SESSION_PROJECT_RESUME_SCHEMA,
                "temporal_work_recall",
                "object_pack.v1",
            ],
            "safe_target": "sanitized_session_project_rollup_runtime_smoke",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "probe_preference_artifact_memory_runtime",
            "evidence_field": "preference_artifact_memory",
            "required_values": [
                PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA,
                "code_style_preference",
                "html_visualization_preference",
                "ArtifactPreference",
                ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA,
            ],
            "safe_target": "sanitized_preference_artifact_memory_runtime_smoke",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_permission_sensitive_audit_runtime",
            "evidence_field": "permission_sensitive_audit",
            "required_values": [
                PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA,
                PERMISSION_AUDIT_EVENT_SCHEMA,
                *OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS,
                "permission=denied",
                "protected_values_returned=false",
            ],
            "safe_target": "sanitized_permission_sensitive_audit_runtime_evidence",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_deployed_identity",
            "evidence_field": "deployed_identity",
            "required_values": ["contains_expected_commit"],
            "safe_target": "redacted_artifact_identity_summary",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "probe_production_no_mutation_denials",
            "evidence_field": "production_denials",
            "required_values": [tool_name for _, tool_name in PRODUCTION_DENIAL_CLAIMS],
            "safe_target": "denied_no_mutation_smoke_results",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_object_authority_gate_policy",
            "evidence_field": "production_authority_gate",
            "required_values": [OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG, *OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS],
            "safe_target": "redacted_runtime_gate_policy",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_evidence_provenance",
            "evidence_field": "evidence_provenance",
            "required_values": [EVIDENCE_PROVENANCE_SCHEMA, "post_deploy_read_only_smoke", "none"],
            "safe_target": "sanitized_evidence_provenance",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
    ]


def build_source_to_candidate_runtime_readiness_report(
    *,
    live_evidence: Mapping[str, Any] | None = None,
    expected_commit: str = "",
) -> dict[str, Any]:
    evidence = live_evidence if isinstance(live_evidence, Mapping) else {}
    local_gate = build_source_to_authority_quality_gate_report()
    claims = [
        _local_product_surface_claim(local_gate),
        _live_evidence_provenance_claim(evidence),
        _live_tools_claim(evidence),
        _live_agent_context_tool_hints_claim(evidence),
        _live_agent_context_product_sections_claim(evidence),
        _live_brain_objects_query_route_smokes_claim(evidence),
        _live_source_to_candidate_review_loop_claim(evidence),
        _live_session_project_rollup_claim(evidence),
        _live_preference_artifact_memory_claim(evidence),
        _live_permission_sensitive_audit_claim(evidence),
        _live_deployed_identity_claim(evidence, expected_commit=expected_commit),
        _live_object_authority_production_gate_policy_claim(evidence),
        _live_object_authority_bounded_execution_claim(evidence),
        *[
            _production_denial_claim(evidence, claim_id=claim_id, tool_name=tool_name)
            for claim_id, tool_name in PRODUCTION_DENIAL_CLAIMS
        ],
    ]
    gaps = _dedupe(
        gap
        for claim in claims
        for gap in claim.get("gaps", [])
        if isinstance(gap, str) and gap
    )
    failed = [claim["claim_id"] for claim in claims if claim["status"] == "failed"]
    provenance_claim = next(
        claim for claim in claims if claim["claim_id"] == "live.evidence.provenance"
    )
    report = {
        "schema_version": "source_to_candidate_runtime_readiness.v1",
        "status": "FAIL" if failed else ("PASS_WITH_GAPS" if gaps else "PASS"),
        "claims": claims,
        "failed_claims": failed,
        "gaps": gaps,
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "live_evidence_provided": bool(evidence),
        "production_mutation_performed": any(_claim_reports_mutation(claim) for claim in claims),
        "network_used": False,
        "evidence_collection_network_used": provenance_claim.get("network_used_for_evidence") is True,
        "evidence_provenance": _report_evidence_provenance(provenance_claim),
        "local_gate_status": local_gate["status"],
        "release_quality_gate": "not_green" if gaps else "green",
    }
    ensure_public_safe(report, "SourceToCandidateRuntimeReadiness")
    return report


def _local_product_surface_claim(local_gate: Mapping[str, Any]) -> dict[str, Any]:
    checks = local_gate.get("product_surface_checks")
    product_checks = checks if isinstance(checks, list) else []
    failed = [
        str(item.get("id") or "")
        for item in product_checks
        if isinstance(item, Mapping) and item.get("result") != "PASS"
    ]
    return {
        "claim_id": "local.product_surface_checks",
        "evidence_class": "local_test",
        "status": "failed" if failed else "validated",
        "result": "FAIL" if failed else "PASS",
        "covered_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "gaps": ["local_product_surface_checks_failed"] if failed else [],
        "failed_checks": failed,
    }


def _live_evidence_provenance_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not evidence:
        return {
            "claim_id": "live.evidence.provenance",
            "evidence_class": "runtime_evidence_provenance",
            "status": "not_validated",
            "schema_version": "",
            "collection_mode": "missing",
            "is_live": False,
            "network_used_for_evidence": False,
            "mutation_scope": "none",
            "redaction_check": "missing",
            "gaps": ["live_evidence_provenance_unverified"],
        }
    provenance = evidence.get("evidence_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    if not provenance:
        return {
            "claim_id": "live.evidence.provenance",
            "evidence_class": "runtime_evidence_provenance",
            "status": "failed",
            "schema_version": "",
            "collection_mode": "missing",
            "is_live": False,
            "network_used_for_evidence": False,
            "mutation_scope": "unknown",
            "redaction_check": "missing",
            "gaps": ["live_evidence_provenance_missing"],
        }
    collection_mode = public_safe_text(str(provenance.get("collection_mode") or ""), max_chars=80)
    mutation_scope = public_safe_text(str(provenance.get("mutation_scope") or ""), max_chars=80)
    failures = _evidence_provenance_failures(
        provenance=provenance,
        collection_mode=collection_mode,
        mutation_scope=mutation_scope,
        execution_reports_mutation=_evidence_execution_reports_mutation(evidence),
    )
    redaction_check = "forbidden_fields_present" if any(
        gap
        in failures
        for gap in (
            "live_evidence_provenance_raw_private_evidence_returned",
            "live_evidence_provenance_secret_returned",
            "live_evidence_provenance_host_topology_returned",
            "live_evidence_provenance_raw_external_ids_returned",
        )
    ) else "redacted_only"
    return {
        "claim_id": "live.evidence.provenance",
        "evidence_class": "runtime_evidence_provenance",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(provenance.get("schema_version") or ""), max_chars=80),
        "collection_mode": collection_mode,
        "source": collection_mode,
        "is_live": collection_mode in LIVE_EVIDENCE_COLLECTION_MODES,
        "network_used_for_evidence": provenance.get("network_used") is True,
        "mutation_scope": mutation_scope,
        "redaction_check": redaction_check,
        "gaps": failures,
    }


def _evidence_provenance_failures(
    *,
    provenance: Mapping[str, Any],
    collection_mode: str,
    mutation_scope: str,
    execution_reports_mutation: bool,
) -> list[str]:
    failures: list[str] = []
    if provenance.get("schema_version") != EVIDENCE_PROVENANCE_SCHEMA:
        failures.append("live_evidence_provenance_schema_mismatch")
    if collection_mode not in ALLOWED_EVIDENCE_COLLECTION_MODES:
        failures.append("live_evidence_provenance_source_unknown")
    if mutation_scope not in ALLOWED_EVIDENCE_MUTATION_SCOPES:
        failures.append("live_evidence_provenance_mutation_scope_unknown")
    if execution_reports_mutation and mutation_scope != "bounded_production_authority_execution":
        failures.append("live_evidence_provenance_mutation_scope_mismatch")
    if not execution_reports_mutation and mutation_scope != "none":
        failures.append("live_evidence_provenance_unexpected_mutation_scope")
    if provenance.get("raw_private_evidence_returned") is not False:
        failures.append("live_evidence_provenance_raw_private_evidence_returned")
    if provenance.get("secret_returned") is not False:
        failures.append("live_evidence_provenance_secret_returned")
    if provenance.get("host_topology_returned") is not False:
        failures.append("live_evidence_provenance_host_topology_returned")
    if provenance.get("raw_external_ids_returned") is not False:
        failures.append("live_evidence_provenance_raw_external_ids_returned")
    return _dedupe(failures)


def _evidence_execution_reports_mutation(evidence: Mapping[str, Any]) -> bool:
    execution = evidence.get("production_authority_execution")
    execution = execution if isinstance(execution, Mapping) else {}
    proposal = execution.get("proposal") if isinstance(execution.get("proposal"), Mapping) else {}
    decision = execution.get("decision") if isinstance(execution.get("decision"), Mapping) else {}
    return _bounded_execution_reports_mutation(proposal, decision)


def _report_evidence_provenance(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": public_safe_text(str(claim.get("source") or claim.get("collection_mode") or ""), max_chars=80),
        "is_live": claim.get("is_live") is True,
        "network_used_for_evidence": claim.get("network_used_for_evidence") is True,
        "mutation_scope": public_safe_text(str(claim.get("mutation_scope") or ""), max_chars=80),
        "redaction_check": public_safe_text(str(claim.get("redaction_check") or ""), max_chars=80),
    }


def _live_tools_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    tool_names = set(_string_list(evidence.get("tool_names")))
    missing = [name for name in REQUIRED_RUNTIME_TOOL_NAMES if name not in tool_names]
    return {
        "claim_id": "live.mcp.review_tools_loaded",
        "evidence_class": "runtime_read_path",
        "status": "not_validated" if missing else "validated",
        "required_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "missing_tools": missing,
        "gaps": ["live_mcp_review_tools_unverified", *_named_gaps("live_mcp_tool_missing", missing)] if missing else [],
    }


def _live_agent_context_tool_hints_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    tool_hints = _agent_context_tool_hints(evidence)
    hints_by_tool = {
        str(item.get("tool") or ""): item
        for item in tool_hints
        if isinstance(item, Mapping) and str(item.get("tool") or "")
    }
    hinted_tools = set(hints_by_tool)
    missing = [name for name in REQUIRED_RUNTIME_TOOL_NAMES if name not in hinted_tools]
    safety_failures = [
        failure
        for name in REQUIRED_RUNTIME_TOOL_NAMES
        if name in hints_by_tool
        for failure in _agent_context_tool_hint_safety_failures(name, hints_by_tool[name])
    ]
    base = {
        "claim_id": "live.agent_context.tool_hints",
        "evidence_class": "runtime_read_path",
        "required_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "missing_tools": missing,
        "unsafe_tool_hints": safety_failures,
    }
    if safety_failures:
        return {
            **base,
            "status": "failed",
            "gaps": [
                *safety_failures,
                *(["live_agent_context_tool_hints_unverified"] if missing else []),
                *_named_gaps("live_agent_context_tool_hint_missing", missing),
            ],
        }
    return {
        **base,
        "status": "not_validated" if missing else "validated",
        "gaps": [
            "live_agent_context_tool_hints_unverified",
            *_named_gaps("live_agent_context_tool_hint_missing", missing),
        ]
        if missing
        else [],
    }


def _live_agent_context_product_sections_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    product = _agent_context_product(evidence)
    sections = product.get("sections") if isinstance(product.get("sections"), Mapping) else {}
    missing = [
        name
        for name in REQUIRED_AGENT_CONTEXT_SECTIONS
        if _section_object_count(sections.get(name)) < 1
    ]
    mutation_allowed = (
        product.get("surface_policy") if isinstance(product.get("surface_policy"), Mapping) else {}
    ).get("mutation_allowed")
    contract_failures = _agent_context_product_contract_failures(product)
    base = {
        "claim_id": "live.agent_context.product_sections",
        "evidence_class": "runtime_read_path",
        "schema_version": public_safe_text(str(product.get("schema_version") or ""), max_chars=80),
        "consumer": public_safe_text(str(product.get("consumer") or ""), max_chars=80),
        "required_sections": list(REQUIRED_AGENT_CONTEXT_SECTIONS),
        "missing_sections": missing,
        "mutation_allowed": bool(mutation_allowed),
    }
    if contract_failures:
        return {
            **base,
            "status": "failed",
            "gaps": contract_failures,
        }
    if bool(mutation_allowed):
        return {
            **base,
            "status": "failed",
            "gaps": ["live_agent_context_mutation_allowed"],
        }
    return {
        **base,
        "status": "not_validated" if missing else "validated",
        "gaps": [
            "live_agent_context_product_sections_unverified",
            *_named_gaps("live_agent_context_section_missing", missing),
        ]
        if missing
        else [],
    }


def _agent_context_product_contract_failures(product: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if not product:
        return failures
    if product.get("schema_version") != REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA:
        failures.append("live_agent_context_product_schema_mismatch")
    if str(product.get("consumer") or "") not in ALLOWED_AGENT_CONTEXT_CONSUMERS:
        failures.append("live_agent_context_consumer_unknown")
    degraded = product.get("degraded_mode")
    degraded_gaps = degraded.get("gaps") if isinstance(degraded, Mapping) else None
    if not isinstance(degraded_gaps, list):
        failures.append("live_agent_context_degraded_gap_disclosure_missing")
    missing_before_promotion = product.get("missing_evidence_before_promotion")
    if not isinstance(missing_before_promotion, list):
        failures.append("live_agent_context_missing_evidence_before_promotion_missing")
    return failures


def _live_brain_objects_query_route_smokes_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    smokes = evidence.get("brain_objects_query_smokes")
    smoke_items = [dict(item) for item in smokes if isinstance(item, Mapping)] if isinstance(smokes, list) else []
    by_route = {
        str(item.get("route") or (item.get("object_pack") or {}).get("route") or ""): item
        for item in smoke_items
    }
    missing = [route for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES if route not in by_route]
    unimplemented_routes = [
        route
        for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        if route in by_route and _brain_objects_query_route_unimplemented(by_route[route])
    ]
    failures = [
        failure
        for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        if route in by_route
        for failure in _brain_objects_query_smoke_failures(route, by_route[route])
    ]
    identity = evidence.get("deployed_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    deployed_identity_matches_expected = identity.get("contains_expected_commit") is True
    if unimplemented_routes and not deployed_identity_matches_expected:
        failures = [
            failure
            for failure in failures
            if not failure.startswith("brain_objects_query_route_unimplemented:")
        ]
    missing_gaps = (
        [
            "live_brain_objects_query_route_smokes_unverified",
            *_named_gaps("live_brain_objects_query_route_missing", missing),
        ]
        if missing
        else []
    )
    unimplemented_gaps = [
        *_named_gaps("brain_objects_query_route_unimplemented", unimplemented_routes),
        *_named_gaps("shadow_route_smoke_not_implemented", unimplemented_routes),
    ]
    base = {
        "claim_id": "live.brain_objects_query.route_smokes",
        "evidence_class": "runtime_read_path",
        "required_routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "validated_routes": sorted(route for route in by_route if route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "missing_routes": missing,
        "unimplemented_routes": unimplemented_routes,
        "production_mutation_performed": _object_query_smokes_report_mutation(smoke_items),
    }
    if failures:
        return {
            **base,
            "status": "failed",
            "gaps": _dedupe([*failures, *unimplemented_gaps, *missing_gaps]),
        }
    if unimplemented_routes:
        return {
            **base,
            "status": "not_validated",
            "gaps": _dedupe(
                [
                    "live_brain_objects_query_route_smokes_unverified",
                    *unimplemented_gaps,
                    *missing_gaps,
                ]
            ),
        }
    return {
        **base,
        "status": "not_validated" if missing else "validated",
        "gaps": missing_gaps if missing else [],
    }


def _live_source_to_candidate_review_loop_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    loop = evidence.get("source_to_candidate_review_loop")
    loop = loop if isinstance(loop, Mapping) else {}
    if not loop:
        return {
            "claim_id": "live.source_to_candidate.review_loop",
            "evidence_class": "runtime_read_path",
            "status": "not_validated",
            "candidate_count": 0,
            "edited_candidate_count": 0,
            "decision_count": 0,
            "authority_write_scope": "",
            "production_mutation_performed": False,
            "gaps": ["live_source_to_candidate_review_loop_unverified"],
        }
    graph = loop.get("source_to_candidate_graph") if isinstance(loop.get("source_to_candidate_graph"), Mapping) else {}
    review = loop.get("candidate_review_edit") if isinstance(loop.get("candidate_review_edit"), Mapping) else {}
    decision = loop.get("approval_board_decision") if isinstance(loop.get("approval_board_decision"), Mapping) else {}
    read_after_write = loop.get("read_after_write") if isinstance(loop.get("read_after_write"), Mapping) else {}
    postcheck = loop.get("postcheck") if isinstance(loop.get("postcheck"), Mapping) else {}
    failures = _source_to_candidate_review_loop_failures(
        loop=loop,
        graph=graph,
        review=review,
        decision=decision,
        read_after_write=read_after_write,
        postcheck=postcheck,
    )
    mutation_performed = _source_to_candidate_review_loop_reports_mutation(
        graph=graph,
        review=review,
        decision=decision,
    )
    return {
        "claim_id": "live.source_to_candidate.review_loop",
        "evidence_class": "runtime_read_path",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(loop.get("schema_version") or ""), max_chars=80),
        "candidate_count": _int_value(graph.get("candidate_count")),
        "edited_candidate_count": _int_value(review.get("edited_candidate_count")),
        "decision_count": _int_value(decision.get("decision_count")),
        "authority_write_scope": public_safe_text(str(decision.get("authority_write_scope") or ""), max_chars=120),
        "read_after_write_status": public_safe_text(str(read_after_write.get("status") or ""), max_chars=80),
        "postcheck_status": public_safe_text(str(postcheck.get("status") or ""), max_chars=80),
        "production_mutation_performed": mutation_performed,
        "gaps": failures,
    }


def _source_to_candidate_review_loop_failures(
    *,
    loop: Mapping[str, Any],
    graph: Mapping[str, Any],
    review: Mapping[str, Any],
    decision: Mapping[str, Any],
    read_after_write: Mapping[str, Any],
    postcheck: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    if loop.get("schema_version") != "source_to_candidate_review_loop_evidence.v1":
        failures.append("source_to_candidate_review_loop_schema_mismatch")
    if graph.get("schema_version") != "source_to_candidate_graph_activation.v1":
        failures.append("source_to_candidate_review_loop_graph_schema_mismatch")
    if str(graph.get("target_scope") or "") != "local_test":
        failures.append("source_to_candidate_review_loop_graph_scope_not_local_test")
    if graph.get("pack_type") != "candidate_graph_review":
        failures.append("source_to_candidate_review_loop_pack_type_mismatch")
    if _int_value(graph.get("candidate_count")) < 1:
        failures.append("source_to_candidate_review_loop_candidate_count_missing")
    if graph.get("quality_gate"):
        quality_gate = graph.get("quality_gate") if isinstance(graph.get("quality_gate"), Mapping) else {}
        if quality_gate.get("source_to_candidate_graph") != "PASS":
            failures.append("source_to_candidate_review_loop_quality_gate_failed")
    if review.get("schema_version") != "candidate_review_edit_result.v1":
        failures.append("source_to_candidate_review_loop_candidate_review_schema_mismatch")
    if str(review.get("target_scope") or "") != "local_test":
        failures.append("source_to_candidate_review_loop_candidate_review_scope_not_local_test")
    if review.get("mutation_mode") != "no_mutation" or review.get("authority_write_performed") is True:
        failures.append("source_to_candidate_review_loop_candidate_review_not_no_mutation")
    if _int_value(review.get("edited_candidate_count")) < 1:
        failures.append("source_to_candidate_review_loop_candidate_review_missing")
    if _int_value(review.get("rejected_edit_count")) > 0:
        failures.append("source_to_candidate_review_loop_rejected_edits_present")
    if decision.get("schema_version") != "approval_board_decision_result.v1":
        failures.append("source_to_candidate_review_loop_approval_schema_mismatch")
    if decision.get("ledger_scope") != "local_test" or decision.get("authority_write_scope") != "local_test":
        failures.append("source_to_candidate_review_loop_authority_scope_not_local_test")
    if decision.get("authority_write_performed") is not True:
        failures.append("source_to_candidate_review_loop_authority_write_missing")
    if _int_value(decision.get("decision_count")) < 1:
        failures.append("source_to_candidate_review_loop_decision_count_missing")
    if read_after_write.get("status") != "validated":
        failures.append("source_to_candidate_review_loop_read_after_write_missing")
    if read_after_write.get("object_pack_schema") != "object_pack.v1":
        failures.append("source_to_candidate_review_loop_object_pack_schema_mismatch")
    if _source_to_candidate_review_loop_reports_mutation(graph=graph, review=review, decision=decision):
        failures.append("source_to_candidate_review_loop_production_mutation_performed")
    if postcheck.get("status") != "validated":
        failures.append("source_to_candidate_review_loop_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "source_to_candidate_review_loop_raw_private_evidence_returned"),
        ("secret_returned", "source_to_candidate_review_loop_secret_returned"),
        ("host_topology_returned", "source_to_candidate_review_loop_host_topology_returned"),
        ("raw_external_ids_returned", "source_to_candidate_review_loop_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _source_to_candidate_review_loop_reports_mutation(
    *,
    graph: Mapping[str, Any],
    review: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> bool:
    return (
        graph.get("production_mutation_performed") is True
        or graph.get("mutation_performed") is True
        or review.get("production_mutation_performed") is True
        or decision.get("production_mutation_performed") is True
        or decision.get("ledger_scope") == "production"
        or decision.get("authority_write_scope") == "production_ledger"
    )


def _live_session_project_rollup_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    rollup = evidence.get("session_project_rollup_runtime")
    rollup = rollup if isinstance(rollup, Mapping) else {}
    if not rollup:
        return {
            "claim_id": "live.session_project.rollup",
            "evidence_class": "runtime_read_path",
            "status": "not_validated",
            "schema_version": "",
            "device_count": 0,
            "visible_session_count": 0,
            "all_device_session_count": 0,
            "read_after_write_status": "",
            "production_mutation_performed": False,
            "gaps": ["live_session_project_rollup_unverified", "live_multi_device_rollup_unproven"],
        }
    preview = rollup.get("rollup_preview") if isinstance(rollup.get("rollup_preview"), Mapping) else {}
    handoff = rollup.get("handoff_pack") if isinstance(rollup.get("handoff_pack"), Mapping) else {}
    resume = handoff.get("resume_context") if isinstance(handoff.get("resume_context"), Mapping) else {}
    read_after_write = (
        rollup.get("read_after_write") if isinstance(rollup.get("read_after_write"), Mapping) else {}
    )
    postcheck = rollup.get("postcheck") if isinstance(rollup.get("postcheck"), Mapping) else {}
    object_type_counts = (
        preview.get("object_type_counts") if isinstance(preview.get("object_type_counts"), Mapping) else {}
    )
    edge_types = _string_list(preview.get("edge_types"))
    failures = _session_project_rollup_failures(
        rollup=rollup,
        preview=preview,
        handoff=handoff,
        resume=resume,
        read_after_write=read_after_write,
        postcheck=postcheck,
        object_type_counts=object_type_counts,
        edge_types=edge_types,
    )
    return {
        "claim_id": "live.session_project.rollup",
        "evidence_class": "runtime_read_path",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(rollup.get("schema_version") or ""), max_chars=80),
        "rollup_preview_schema": public_safe_text(str(preview.get("schema_version") or ""), max_chars=80),
        "handoff_pack_schema": public_safe_text(str(handoff.get("schema_version") or ""), max_chars=80),
        "resume_context_schema": public_safe_text(str(resume.get("schema_version") or ""), max_chars=80),
        "scope": public_safe_text(str(preview.get("scope") or ""), max_chars=80),
        "device_count": _int_value(preview.get("device_count")),
        "visible_session_count": _int_value(preview.get("visible_session_count")),
        "all_device_session_count": _int_value(preview.get("all_device_session_count")),
        "edge_count": _int_value(preview.get("edge_count")),
        "read_after_write_status": public_safe_text(str(read_after_write.get("status") or ""), max_chars=80),
        "raw_return_capability": public_safe_text(str(handoff.get("raw_return_capability") or ""), max_chars=80),
        "production_mutation_performed": _session_project_rollup_reports_mutation(
            rollup=rollup,
            preview=preview,
            resume=resume,
        ),
        "gaps": failures,
    }


def _session_project_rollup_failures(
    *,
    rollup: Mapping[str, Any],
    preview: Mapping[str, Any],
    handoff: Mapping[str, Any],
    resume: Mapping[str, Any],
    read_after_write: Mapping[str, Any],
    postcheck: Mapping[str, Any],
    object_type_counts: Mapping[str, Any],
    edge_types: list[str],
) -> list[str]:
    failures: list[str] = []
    if rollup.get("schema_version") != SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA:
        failures.append("session_project_rollup_schema_mismatch")
    if preview.get("schema_version") != SESSION_PROJECT_ROLLUP_PREVIEW_SCHEMA:
        failures.append("session_project_rollup_preview_schema_mismatch")
    if str(preview.get("scope") or "") != "all_devices":
        failures.append("session_project_rollup_scope_not_all_devices")
    if _int_value(preview.get("visible_session_count")) < 1:
        failures.append("session_project_rollup_visible_session_missing")
    if _int_value(preview.get("all_device_session_count")) < _int_value(preview.get("visible_session_count")):
        failures.append("session_project_rollup_all_device_count_inconsistent")
    if _int_value(preview.get("device_count")) < 2:
        failures.append("session_project_rollup_multi_device_unproven")
    missing_object_types = [
        object_type
        for object_type in REQUIRED_SESSION_PROJECT_OBJECT_TYPES
        if _int_value(object_type_counts.get(object_type)) < 1
    ]
    failures.extend(_named_gaps("session_project_rollup_required_object_type_missing", missing_object_types))
    missing_edge_types = [
        edge_type for edge_type in REQUIRED_SESSION_PROJECT_EDGE_TYPES if edge_type not in set(edge_types)
    ]
    failures.extend(_named_gaps("session_project_rollup_required_edge_missing", missing_edge_types))
    if handoff.get("schema_version") != SESSION_PROJECT_HANDOFF_SCHEMA:
        failures.append("session_project_handoff_schema_mismatch")
    if handoff.get("raw_return_capability") != "denied":
        failures.append("session_project_handoff_raw_return_not_denied")
    if resume.get("schema_version") != SESSION_PROJECT_RESUME_SCHEMA:
        failures.append("session_project_resume_schema_mismatch")
    if resume.get("latest_session_ref_present") is not True:
        failures.append("session_project_resume_latest_session_missing")
    if _int_value(resume.get("work_unit_ref_count")) < 1:
        failures.append("session_project_resume_work_unit_missing")
    if read_after_write.get("status") != "validated":
        failures.append("session_project_rollup_read_after_write_missing")
    if read_after_write.get("route") != "temporal_work_recall":
        failures.append("session_project_rollup_read_after_write_route_mismatch")
    if read_after_write.get("object_pack_schema") != "object_pack.v1":
        failures.append("session_project_rollup_object_pack_schema_mismatch")
    if "WorkUnit" not in _string_list(read_after_write.get("object_types")):
        failures.append("session_project_rollup_work_unit_read_missing")
    if _session_project_rollup_reports_mutation(rollup=rollup, preview=preview, resume=resume):
        failures.append("session_project_rollup_production_mutation_performed")
    if postcheck.get("status") != "validated":
        failures.append("session_project_rollup_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "session_project_rollup_raw_private_evidence_returned"),
        ("secret_returned", "session_project_rollup_secret_returned"),
        ("host_topology_returned", "session_project_rollup_host_topology_returned"),
        ("raw_external_ids_returned", "session_project_rollup_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _session_project_rollup_reports_mutation(
    *,
    rollup: Mapping[str, Any],
    preview: Mapping[str, Any],
    resume: Mapping[str, Any],
) -> bool:
    return (
        rollup.get("production_mutation_performed") is True
        or preview.get("production_mutation_performed") is True
        or resume.get("production_mutation_performed") is True
    )


def _live_preference_artifact_memory_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    preference = evidence.get("preference_artifact_memory")
    preference = preference if isinstance(preference, Mapping) else {}
    if not preference:
        return {
            "claim_id": "live.preference_artifact.memory",
            "evidence_class": "runtime_read_path",
            "status": "not_validated",
            "schema_version": "",
            "accepted_preference_count": 0,
            "proposal_preference_count": 0,
            "html_route_status": "not_validated",
            "production_mutation_performed": False,
            "gaps": [
                "live_preference_artifact_memory_unverified",
                "accepted_preference_context_pack_live_unproven",
            ],
        }
    pack = (
        preference.get("preference_object_pack")
        if isinstance(preference.get("preference_object_pack"), Mapping)
        else {}
    )
    html_smoke = (
        preference.get("html_visualization_route_smoke")
        if isinstance(preference.get("html_visualization_route_smoke"), Mapping)
        else {}
    )
    html_pack = html_smoke.get("object_pack") if isinstance(html_smoke.get("object_pack"), Mapping) else {}
    context = (
        preference.get("agent_context_preference_section")
        if isinstance(preference.get("agent_context_preference_section"), Mapping)
        else {}
    )
    artifact_check = (
        preference.get("artifact_review_check")
        if isinstance(preference.get("artifact_review_check"), Mapping)
        else {}
    )
    postcheck = preference.get("postcheck") if isinstance(preference.get("postcheck"), Mapping) else {}
    failures = _preference_artifact_memory_failures(
        preference=preference,
        pack=pack,
        html_smoke=html_smoke,
        html_pack=html_pack,
        context=context,
        artifact_check=artifact_check,
        postcheck=postcheck,
    )
    return {
        "claim_id": "live.preference_artifact.memory",
        "evidence_class": "runtime_read_path",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(preference.get("schema_version") or ""), max_chars=80),
        "preference_pack_schema": public_safe_text(str(pack.get("schema_version") or ""), max_chars=80),
        "accepted_preference_count": _int_value(pack.get("accepted_preference_count")),
        "proposal_preference_count": _int_value(pack.get("proposal_preference_count")),
        "html_route_status": "failed" if _html_preference_route_unimplemented(html_smoke, html_pack) else "validated",
        "agent_context_object_count": _int_value(context.get("object_count")),
        "artifact_review_check_status": public_safe_text(str(artifact_check.get("status") or ""), max_chars=80),
        "production_mutation_performed": _preference_artifact_memory_reports_mutation(
            preference=preference,
            pack=pack,
            html_smoke=html_smoke,
        ),
        "gaps": failures,
    }


def _preference_artifact_memory_failures(
    *,
    preference: Mapping[str, Any],
    pack: Mapping[str, Any],
    html_smoke: Mapping[str, Any],
    html_pack: Mapping[str, Any],
    context: Mapping[str, Any],
    artifact_check: Mapping[str, Any],
    postcheck: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    if preference.get("schema_version") != PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA:
        failures.append("preference_artifact_memory_schema_mismatch")
    if pack.get("schema_version") != "object_pack.v1":
        failures.append("preference_artifact_pack_schema_mismatch")
    if pack.get("route") != "code_style_preference":
        failures.append("preference_artifact_pack_route_mismatch")
    if _int_value(pack.get("accepted_preference_count")) < 1:
        failures.append("preference_artifact_accepted_preference_missing")
    if _int_value(pack.get("proposal_preference_count")) < 1:
        failures.append("preference_artifact_proposal_lane_missing")
    if not _pack_contains_object_type(pack, "ArtifactPreference"):
        failures.append("preference_artifact_object_missing")
    if not isinstance(pack.get("recommended_actions"), list):
        failures.append("preference_artifact_recommended_actions_missing")
    if _html_preference_route_unimplemented(html_smoke, html_pack):
        failures.append("preference_artifact_html_route_unimplemented")
    if html_smoke.get("schema_version") != "brain_objects_query.v1":
        failures.append("preference_artifact_html_route_schema_mismatch")
    if html_smoke.get("route") != "html_visualization_preference":
        failures.append("preference_artifact_html_route_mismatch")
    if html_pack.get("schema_version") != "object_pack.v1":
        failures.append("preference_artifact_html_object_pack_schema_mismatch")
    if html_pack.get("route") != "html_visualization_preference":
        failures.append("preference_artifact_html_object_pack_route_mismatch")
    if not _pack_contains_object_type(html_pack, "ArtifactPreference"):
        failures.append("preference_artifact_html_preference_missing")
    if context.get("schema_version") != REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA:
        failures.append("preference_artifact_agent_context_schema_mismatch")
    if context.get("section") != "style_preference":
        failures.append("preference_artifact_agent_context_section_mismatch")
    if _int_value(context.get("object_count")) < 1 or _int_value(context.get("accepted_preference_count")) < 1:
        failures.append("preference_artifact_agent_context_missing")
    policy = context.get("surface_policy") if isinstance(context.get("surface_policy"), Mapping) else {}
    if policy.get("mutation_allowed") is not False:
        failures.append("preference_artifact_agent_context_mutation_allowed")
    if artifact_check.get("schema_version") != ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA:
        failures.append("preference_artifact_review_check_schema_mismatch")
    if artifact_check.get("status") != "pass":
        failures.append("preference_artifact_review_check_failed")
    if artifact_check.get("ui_required") is not False:
        failures.append("preference_artifact_review_check_required_ui")
    if artifact_check.get("raw_artifact_body_returned") is not False:
        failures.append("preference_artifact_raw_artifact_body_returned")
    if _preference_artifact_memory_reports_mutation(
        preference=preference,
        pack=pack,
        html_smoke=html_smoke,
    ):
        failures.append("preference_artifact_production_mutation_performed")
    if postcheck.get("status") != "validated":
        failures.append("preference_artifact_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "preference_artifact_raw_private_evidence_returned"),
        ("secret_returned", "preference_artifact_secret_returned"),
        ("host_topology_returned", "preference_artifact_host_topology_returned"),
        ("raw_external_ids_returned", "preference_artifact_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _html_preference_route_unimplemented(html_smoke: Mapping[str, Any], html_pack: Mapping[str, Any]) -> bool:
    gaps = [str(gap) for gap in html_pack.get("gaps", []) if str(gap or "")]
    return (
        html_smoke.get("production_mutation_performed") is True
        or "object_pack_route_not_implemented" in gaps
        or "accepted_html_preference_missing" in gaps
        or "visualization_preference_missing" in gaps
    )


def _pack_contains_object_type(pack: Mapping[str, Any], object_type: str) -> bool:
    objects = pack.get("objects") if isinstance(pack.get("objects"), list) else []
    return any(isinstance(obj, Mapping) and obj.get("object_type") == object_type for obj in objects)


def _preference_artifact_memory_reports_mutation(
    *,
    preference: Mapping[str, Any],
    pack: Mapping[str, Any],
    html_smoke: Mapping[str, Any],
) -> bool:
    return (
        preference.get("production_mutation_performed") is True
        or pack.get("production_mutation_performed") is True
        or html_smoke.get("production_mutation_performed") is True
    )


def _live_permission_sensitive_audit_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    audit = evidence.get("permission_sensitive_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    if not audit:
        return {
            "claim_id": "live.production.permission_sensitive_audit",
            "evidence_class": "runtime_safety_audit",
            "status": "not_validated",
            "schema_version": "",
            "event_count": 0,
            "production_mutation_performed": False,
            "gaps": ["permission_sensitive_audit_unverified"],
        }
    events_raw = audit.get("audit_events")
    events = [dict(item) for item in events_raw if isinstance(item, Mapping)] if isinstance(events_raw, list) else []
    by_action = {public_safe_text(str(item.get("action") or ""), max_chars=120): item for item in events}
    store = audit.get("audit_store") if isinstance(audit.get("audit_store"), Mapping) else {}
    postcheck = audit.get("postcheck") if isinstance(audit.get("postcheck"), Mapping) else {}
    failures = _permission_sensitive_audit_failures(
        audit=audit,
        events=events,
        by_action=by_action,
        store=store,
        postcheck=postcheck,
    )
    return {
        "claim_id": "live.production.permission_sensitive_audit",
        "evidence_class": "runtime_safety_audit",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(audit.get("schema_version") or ""), max_chars=80),
        "event_count": len(events),
        "required_actions": list(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS),
        "recorded_actions": sorted(action for action in by_action if action),
        "audit_store_status": public_safe_text(str(store.get("status") or ""), max_chars=80),
        "production_mutation_performed": _permission_sensitive_audit_reports_mutation(
            audit=audit,
            events=events,
            store=store,
        ),
        "gaps": failures,
    }


def _permission_sensitive_audit_failures(
    *,
    audit: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    by_action: Mapping[str, Mapping[str, Any]],
    store: Mapping[str, Any],
    postcheck: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    if audit.get("schema_version") != PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA:
        failures.append("permission_sensitive_audit_schema_mismatch")
    missing_actions = [tool_name for tool_name in OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS if tool_name not in by_action]
    failures.extend(_named_gaps("permission_sensitive_audit_missing_action", missing_actions))
    for action, event in by_action.items():
        if action not in OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS:
            continue
        failures.extend(_permission_audit_event_failures(action, event))
    if store.get("status") != "recorded":
        failures.append("permission_sensitive_audit_store_not_recorded")
    if _int_value(store.get("event_count")) < len(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS):
        failures.append("permission_sensitive_audit_event_count_incomplete")
    if _permission_sensitive_audit_reports_mutation(audit=audit, events=events, store=store):
        failures.append("permission_sensitive_audit_production_mutation_performed")
    if postcheck.get("status") != "validated":
        failures.append("permission_sensitive_audit_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "permission_sensitive_audit_raw_private_evidence_returned"),
        ("secret_returned", "permission_sensitive_audit_secret_returned"),
        ("host_topology_returned", "permission_sensitive_audit_host_topology_returned"),
        ("raw_external_ids_returned", "permission_sensitive_audit_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _permission_audit_event_failures(action: str, event: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if event.get("schema_version") != PERMISSION_AUDIT_EVENT_SCHEMA:
        failures.append(f"permission_sensitive_audit_event_schema_mismatch:{action}")
    if event.get("event_type") != "permission_sensitive_runtime_action":
        failures.append(f"permission_sensitive_audit_event_type_mismatch:{action}")
    if event.get("ledger_scope") != "production":
        failures.append(f"permission_sensitive_audit_ledger_scope_mismatch:{action}")
    if str(event.get("permission") or "") != "denied":
        failures.append(f"permission_sensitive_audit_event_not_denied:{action}")
    if event.get("authority_write_performed") is not False:
        failures.append(f"permission_sensitive_audit_authority_write_performed:{action}")
    if event.get("production_mutation_performed") is True:
        failures.append(f"permission_sensitive_audit_event_mutation_performed:{action}")
    actor_hash = public_safe_text(str(event.get("actor_ref_hash") or ""), max_chars=120)
    request_hash = public_safe_text(str(event.get("request_hash") or ""), max_chars=120)
    if not actor_hash.startswith("sha256:"):
        failures.append(f"permission_sensitive_audit_actor_hash_missing:{action}")
    if not request_hash.startswith("sha256:"):
        failures.append(f"permission_sensitive_audit_request_hash_missing:{action}")
    for field, gap in (
        ("protected_values_returned", "permission_sensitive_audit_protected_values_returned"),
        ("raw_private_evidence_returned", "permission_sensitive_audit_raw_private_evidence_returned"),
        ("secret_returned", "permission_sensitive_audit_secret_returned"),
        ("host_topology_returned", "permission_sensitive_audit_host_topology_returned"),
        ("raw_external_ids_returned", "permission_sensitive_audit_raw_external_ids_returned"),
    ):
        if event.get(field) is not False:
            failures.append(f"{gap}:{action}")
    return failures


def _permission_sensitive_audit_reports_mutation(
    *,
    audit: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    store: Mapping[str, Any],
) -> bool:
    return (
        audit.get("production_mutation_performed") is True
        or store.get("production_mutation_performed") is True
        or any(
            event.get("production_mutation_performed") is True
            or event.get("authority_write_performed") is True
            for event in events
        )
    )


def _live_deployed_identity_claim(evidence: Mapping[str, Any], *, expected_commit: str) -> dict[str, Any]:
    identity = evidence.get("deployed_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    contains_expected = identity.get("contains_expected_commit") is True
    gaps = [] if contains_expected else ["live_deployed_identity_unverified"]
    if identity and not contains_expected:
        gaps.append("live_deployed_identity_expected_commit_unverified")
    return {
        "claim_id": "live.deployed_identity.includes_expected_commit",
        "evidence_class": "runtime_artifact_identity",
        "status": "validated" if contains_expected else "not_validated",
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "identity_source": public_safe_text(str(identity.get("identity_source") or ""), max_chars=160),
        "gaps": gaps,
    }


def _live_object_authority_production_gate_policy_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    tool_schemas = evidence.get("tool_schemas")
    tool_schemas = tool_schemas if isinstance(tool_schemas, Mapping) else {}
    runtime_gate = evidence.get("production_authority_gate")
    runtime_gate = runtime_gate if isinstance(runtime_gate, Mapping) else {}
    missing_schemas = [
        tool_name
        for tool_name in OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS
        if not _tool_schema_has_production_gate(tool_schemas.get(tool_name))
    ]
    has_runtime_policy = bool(runtime_gate)
    base = {
        "claim_id": "live.production.object_authority_gate_policy",
        "evidence_class": "runtime_safety_gate",
        "tools": list(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS),
        "missing_gate_schemas": missing_schemas,
        "runtime_flag": public_safe_text(str(runtime_gate.get("runtime_flag") or ""), max_chars=120),
        "default_enabled": bool(runtime_gate.get("default_enabled")),
        "per_call_gate_required": runtime_gate.get("per_call_gate_required") is True,
        "production_mutation_performed": runtime_gate.get("production_mutation_performed") is True,
    }
    missing_evidence = []
    if missing_schemas:
        missing_evidence.extend(f"{tool_name}_production_gate_schema_missing" for tool_name in missing_schemas)
    if not has_runtime_policy:
        missing_evidence.append("object_authority_production_runtime_policy_unverified")
    runtime_failures = (
        _object_authority_runtime_gate_policy_failures(runtime_gate) if has_runtime_policy else []
    )
    if not missing_schemas and has_runtime_policy:
        if runtime_failures:
            return {
                **base,
                "status": "failed",
                "gaps": runtime_failures,
            }
        return {
            **base,
            "status": "validated",
            "gaps": [],
        }
    if runtime_failures:
        missing_evidence.extend(runtime_failures)
    if tool_schemas or runtime_gate:
        return {
            **base,
            "status": "failed",
            "gaps": missing_evidence,
        }
    return {
        **base,
        "status": "not_validated",
        "gaps": ["live_object_authority_gate_policy_unverified"],
    }


def _tool_schema_has_production_gate(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    input_schema = schema.get("inputSchema") if isinstance(schema.get("inputSchema"), Mapping) else schema
    properties = input_schema.get("properties") if isinstance(input_schema.get("properties"), Mapping) else {}
    gate = properties.get("production_gate")
    if not isinstance(gate, Mapping):
        return False
    gate_properties = gate.get("properties") if isinstance(gate.get("properties"), Mapping) else {}
    required = {
        "approved",
        "approval_ref",
        "scope",
        "project",
        "max_objects",
        "configured_deployed_mcp_identity_matches_source",
        "read_after_write_smoke_plan",
        "rollback_or_supersession_plan",
        "no_raw_private_evidence",
    }
    return required.issubset(set(str(key) for key in gate_properties))


def _object_authority_runtime_gate_policy_failures(runtime_gate: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if str(runtime_gate.get("runtime_flag") or "") != OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG:
        failures.append("object_authority_production_runtime_flag_unverified")
    if runtime_gate.get("default_enabled") is True:
        failures.append("object_authority_production_runtime_default_enabled")
    if runtime_gate.get("per_call_gate_required") is not True:
        failures.append("object_authority_production_per_call_gate_not_required")
    if runtime_gate.get("production_mutation_performed") is True:
        failures.append("unexpected_production_mutation")
    return failures


def _live_object_authority_bounded_execution_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    execution = evidence.get("production_authority_execution")
    execution = execution if isinstance(execution, Mapping) else {}
    if not execution:
        return {
            "claim_id": "live.production.object_authority_bounded_execution",
            "evidence_class": "runtime_safety_gate",
            "status": "not_validated",
            "production_mutation_performed": False,
            "gaps": ["bounded_production_authority_execution_unverified"],
        }
    approval = execution.get("approval") if isinstance(execution.get("approval"), Mapping) else {}
    proposal = execution.get("proposal") if isinstance(execution.get("proposal"), Mapping) else {}
    decision = execution.get("decision") if isinstance(execution.get("decision"), Mapping) else {}
    read_after_write = (
        execution.get("read_after_write") if isinstance(execution.get("read_after_write"), Mapping) else {}
    )
    rollback = (
        execution.get("rollback_or_supersession")
        if isinstance(execution.get("rollback_or_supersession"), Mapping)
        else {}
    )
    postcheck = execution.get("postcheck") if isinstance(execution.get("postcheck"), Mapping) else {}
    scope = execution.get("scope") if isinstance(execution.get("scope"), Mapping) else {}
    proposal_target = public_safe_text(str(proposal.get("target_object_id") or ""), max_chars=180)
    decision_target = public_safe_text(str(decision.get("target_object_id") or ""), max_chars=180)
    read_target = public_safe_text(str(read_after_write.get("target_object_id") or ""), max_chars=180)
    decision_id = public_safe_text(str(decision.get("decision_id") or ""), max_chars=180)
    approval_ref_hash = public_safe_text(str(approval.get("approval_ref_hash") or ""), max_chars=120)
    proposal_gate_hash = public_safe_text(str(proposal.get("production_gate_ref_hash") or ""), max_chars=120)
    decision_gate_hash = public_safe_text(str(decision.get("production_gate_ref_hash") or ""), max_chars=120)
    object_ids = _string_list(scope.get("object_ids"))
    allowed_object_classes = set(_string_list(scope.get("allowed_object_classes")))
    failures = _bounded_execution_failures(
        execution=execution,
        approval=approval,
        proposal=proposal,
        decision=decision,
        read_after_write=read_after_write,
        rollback=rollback,
        postcheck=postcheck,
        scope=scope,
        proposal_target=proposal_target,
        decision_target=decision_target,
        read_target=read_target,
        decision_id=decision_id,
        approval_ref_hash=approval_ref_hash,
        proposal_gate_hash=proposal_gate_hash,
        decision_gate_hash=decision_gate_hash,
        object_ids=object_ids,
        allowed_object_classes=allowed_object_classes,
    )
    return {
        "claim_id": "live.production.object_authority_bounded_execution",
        "evidence_class": "runtime_safety_gate",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(execution.get("schema_version") or ""), max_chars=80),
        "target_object_id": proposal_target,
        "decision_id": decision_id,
        "approval_ref_hash_present": bool(approval_ref_hash),
        "read_after_write_status": public_safe_text(str(read_after_write.get("status") or ""), max_chars=80),
        "rollback_or_supersession_status": public_safe_text(str(rollback.get("status") or ""), max_chars=80),
        "postcheck_status": public_safe_text(str(postcheck.get("status") or ""), max_chars=80),
        "object_count": len(object_ids),
        "production_mutation_performed": _bounded_execution_reports_mutation(proposal, decision),
        "gaps": failures,
    }


def _bounded_execution_failures(
    *,
    execution: Mapping[str, Any],
    approval: Mapping[str, Any],
    proposal: Mapping[str, Any],
    decision: Mapping[str, Any],
    read_after_write: Mapping[str, Any],
    rollback: Mapping[str, Any],
    postcheck: Mapping[str, Any],
    scope: Mapping[str, Any],
    proposal_target: str,
    decision_target: str,
    read_target: str,
    decision_id: str,
    approval_ref_hash: str,
    proposal_gate_hash: str,
    decision_gate_hash: str,
    object_ids: list[str],
    allowed_object_classes: set[str],
) -> list[str]:
    failures: list[str] = []
    if execution.get("schema_version") != "object_authority_bounded_execution_evidence.v1":
        failures.append("bounded_execution_schema_mismatch")
    if approval.get("approved") is not True:
        failures.append("bounded_execution_approval_missing")
    if not approval_ref_hash.startswith("sha256:"):
        failures.append("bounded_execution_approval_ref_hash_missing")
    if str(approval.get("scope") or "") != "single_project_single_object":
        failures.append("bounded_execution_scope_not_single_project_single_object")
    if _int_value(approval.get("max_objects")) != 1 or _int_value(scope.get("max_objects")) != 1:
        failures.append("bounded_execution_max_objects_not_one")
    if len(object_ids) != 1:
        failures.append("bounded_execution_object_count_not_one")
    if not proposal_target or proposal_target != decision_target or proposal_target != read_target:
        failures.append("bounded_execution_target_object_mismatch")
    if proposal_target and not proposal_target.startswith("ko:RepoDocument:"):
        failures.append("bounded_execution_object_class_not_allowed")
    if "RepoDocument" not in allowed_object_classes:
        failures.append("bounded_execution_allowed_object_class_missing")
    if proposal.get("proposal_write_performed") is not True:
        failures.append("bounded_execution_proposal_write_missing")
    if proposal.get("proposal_write_target") != "production_ledger":
        failures.append("bounded_execution_proposal_target_not_production")
    if proposal.get("authority_write_performed") is True:
        failures.append("bounded_execution_proposal_changed_authority")
    if proposal.get("ledger_scope") != "production" or decision.get("ledger_scope") != "production":
        failures.append("bounded_execution_ledger_scope_not_production")
    if proposal_gate_hash != approval_ref_hash or decision_gate_hash != approval_ref_hash:
        failures.append("bounded_execution_gate_hash_mismatch")
    if decision.get("authority_write_performed") is not True:
        failures.append("bounded_execution_decision_write_missing")
    if decision.get("authoritative_memory_changed") is not True:
        failures.append("bounded_execution_authoritative_memory_not_changed")
    if decision.get("authority_write_scope") != "production_ledger":
        failures.append("bounded_execution_decision_scope_not_production")
    if read_after_write.get("status") != "validated" or not decision_id:
        failures.append("bounded_execution_read_after_write_missing")
    if public_safe_text(str(read_after_write.get("decision_id") or ""), max_chars=180) != decision_id:
        failures.append("bounded_execution_read_after_write_decision_mismatch")
    if str(rollback.get("status") or "") not in {"planned", "validated"} or not _string_list(rollback.get("path")):
        failures.append("bounded_execution_rollback_or_supersession_missing")
    if postcheck.get("status") != "validated":
        failures.append("bounded_execution_postcheck_missing")
    if postcheck.get("raw_private_evidence_returned") is not False:
        failures.append("bounded_execution_raw_private_evidence_returned")
    return _dedupe(failures)


def _bounded_execution_reports_mutation(proposal: Mapping[str, Any], decision: Mapping[str, Any]) -> bool:
    return (
        proposal.get("production_mutation_performed") is True
        or proposal.get("proposal_write_performed") is True
        or decision.get("production_mutation_performed") is True
        or decision.get("authority_write_performed") is True
    )


def _production_denial_claim(
    evidence: Mapping[str, Any],
    *,
    claim_id: str,
    tool_name: str,
) -> dict[str, Any]:
    denials = evidence.get("production_denials")
    denials = denials if isinstance(denials, Mapping) else {}
    payload = denials.get(tool_name)
    if not isinstance(payload, Mapping):
        return {
            "claim_id": claim_id,
            "evidence_class": "runtime_safety_denial",
            "tool": tool_name,
            "status": "not_validated",
            "production_mutation_performed": False,
            "gaps": [f"{tool_name}_production_denial_unverified"],
        }
    mutation_performed = (
        payload.get("production_mutation_performed") is True
        or payload.get("mutation_performed") is True
        or payload.get("authority_write_performed") is True
        or payload.get("proposal_write_performed") is True
        or payload.get("decision_write_performed") is True
    )
    denied = str(payload.get("status") or payload.get("permission") or "").lower() == "denied"
    if mutation_performed or not denied:
        return {
            "claim_id": claim_id,
            "evidence_class": "runtime_safety_denial",
            "tool": tool_name,
            "status": "failed",
            "production_mutation_performed": bool(mutation_performed),
            "gaps": ["unexpected_production_mutation"],
        }
    return {
        "claim_id": claim_id,
        "evidence_class": "runtime_safety_denial",
        "tool": tool_name,
        "status": "denied_as_expected",
        "production_mutation_performed": False,
        "gaps": [],
    }


def _agent_context_tool_hints(evidence: Mapping[str, Any]) -> list[Any]:
    product = _agent_context_product(evidence)
    hints = product.get("tool_hints") if isinstance(product, Mapping) else []
    return list(hints) if isinstance(hints, list) else []


def _agent_context_tool_hint_safety_failures(tool_name: str, hint: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    safe_targets = _string_list(hint.get("safe_targets"))
    blocked_targets = _string_list(hint.get("blocked_targets"))
    if hint.get("suggest_allowed") is not True:
        failures.append(f"{tool_name}_tool_hint_suggest_not_allowed")
    if hint.get("execute_allowed") is not False:
        failures.append(f"{tool_name}_tool_hint_execute_allowed")
    if hint.get("production_mutation_allowed") is not False:
        failures.append(f"{tool_name}_tool_hint_production_mutation_allowed")
    if not safe_targets:
        failures.append(f"{tool_name}_tool_hint_safe_targets_missing")
    if tool_name in PERMISSION_SENSITIVE_AGENT_CONTEXT_TOOLS and "approved_scope_required" not in _string_list(
        hint.get("blocked_by")
    ):
        failures.append(f"{tool_name}_tool_hint_approved_scope_blocker_missing")
    if tool_name == RUNTIME_READINESS_AGENT_CONTEXT_TOOL:
        if "sanitized_evidence_packet" not in safe_targets:
            failures.append(f"{tool_name}_tool_hint_sanitized_evidence_target_missing")
        if "raw_private_runtime_evidence" not in blocked_targets:
            failures.append(f"{tool_name}_tool_hint_raw_private_blocker_missing")
    return failures


def _agent_context_product(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    product = evidence.get("agent_context_product")
    if isinstance(product, Mapping):
        return product
    context_pack = evidence.get("context_pack")
    authority = context_pack.get("authority") if isinstance(context_pack, Mapping) else {}
    product = authority.get("agent_context_product") if isinstance(authority, Mapping) else {}
    return product if isinstance(product, Mapping) else {}


def _section_object_count(section: Any) -> int:
    if not isinstance(section, Mapping):
        return 0
    try:
        return int(section.get("object_count") or 0)
    except (TypeError, ValueError):
        return 0


def _brain_objects_query_smoke_failures(route: str, smoke: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    object_pack = smoke.get("object_pack") if isinstance(smoke.get("object_pack"), Mapping) else {}
    if smoke.get("schema_version") != "brain_objects_query.v1":
        failures.append(f"brain_objects_query_schema_mismatch:{route}")
    if object_pack.get("schema_version") != "object_pack.v1":
        failures.append(f"brain_objects_query_object_pack_schema_mismatch:{route}")
    if str(smoke.get("route") or object_pack.get("route") or "") != route:
        failures.append(f"brain_objects_query_route_mismatch:{route}")
    if _brain_objects_query_route_unimplemented(smoke):
        failures.append(f"brain_objects_query_route_unimplemented:{route}")
    if bool(smoke.get("production_mutation_performed")) or bool(smoke.get("mutation_performed")):
        failures.append(f"brain_objects_query_mutation_performed:{route}")
    if not isinstance(object_pack.get("recommended_actions"), list):
        failures.append(f"brain_objects_query_recommended_actions_missing:{route}")
    if not isinstance(object_pack.get("lanes"), Mapping):
        failures.append(f"brain_objects_query_lanes_missing:{route}")
    return failures


def _brain_objects_query_route_unimplemented(smoke: Mapping[str, Any]) -> bool:
    object_pack = smoke.get("object_pack") if isinstance(smoke.get("object_pack"), Mapping) else {}
    gaps = [str(gap) for gap in object_pack.get("gaps", []) if str(gap or "")]
    return "object_pack_route_not_implemented" in gaps


def _object_query_smokes_report_mutation(smoke_items: list[Mapping[str, Any]]) -> bool:
    return any(
        bool(item.get("production_mutation_performed")) or bool(item.get("mutation_performed"))
        for item in smoke_items
    )


def _public_safe_mapping(value: Any) -> dict[str, Any]:
    return _public_safe_json_value(value) if isinstance(value, Mapping) else {}


def _public_safe_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_public_safe_mapping(item) for item in value if isinstance(item, Mapping)]


def _public_safe_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            public_safe_text(str(key), max_chars=160): _public_safe_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_safe_json_value(item) for item in value]
    if isinstance(value, str):
        return public_safe_text(value, max_chars=2048)
    return value


def _provenance_flag(provenance: Mapping[str, Any], name: str) -> Any:
    if name in provenance:
        return provenance.get(name)
    return False


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [public_safe_text(str(item), max_chars=160) for item in value if str(item or "")]


def _claim_reports_mutation(claim: Mapping[str, Any]) -> bool:
    return claim.get("production_mutation_performed") is True


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _named_gaps(prefix: str, values: list[str]) -> list[str]:
    return [f"{prefix}:{public_safe_text(str(value), max_chars=120)}" for value in values if str(value or "")]

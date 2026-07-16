from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import unquote

from .._util import ensure_public_safe, hash_payload, public_safe_text, require_sha256
from .agent_context_consumer import (
    AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA,
    AGENT_CONTEXT_ROUTE_BINDING_SCHEMA,
    CODEX_BOUNDED_ACTIVATION_SCOPE,
    CODEX_CONTEXT_ADAPTER,
    REQUIRED_POLICY_DECISIONS,
)
from .authority_policy import (
    knowledge_object_class_from_id,
    is_allowed_object_target,
)
from .artifact_preference_evaluator import (
    ARTIFACT_PREFERENCE_COLLECTOR_ATTESTATION_SCHEMA,
    ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
    artifact_preference_application_receipt_is_valid,
)
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
    "code_change_impact",
    "html_visualization_preference",
    "deployment_runtime_truth",
)
REQUIRED_AGENT_CONTEXT_SECTIONS = (
    "style_preference",
    "active_work",
    "required_verification",
)
REQUIRED_AGENT_CONTEXT_AUTHORITY_SECTION = "current_authority"
REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE = "accepted_current"
REQUIRED_AGENT_CONTEXT_STYLE_PREFERENCE_SECTION = "style_preference"
REQUIRED_AGENT_CONTEXT_STARTUP_SECTIONS = (
    REQUIRED_AGENT_CONTEXT_AUTHORITY_SECTION,
    *REQUIRED_AGENT_CONTEXT_SECTIONS,
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
    "brain_approval_board_decide",
    "brain_object_proposal_create",
    "brain_object_decision_commit",
)
OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG = "--allow-object-authority-production-writes"
PERMISSION_SENSITIVE_AGENT_CONTEXT_TOOLS = ("brain_approval_board_decide",)
RUNTIME_READINESS_AGENT_CONTEXT_TOOL = "brain_source_to_candidate_runtime_readiness"
ALLOWED_AGENT_CONTEXT_TOOL_SAFE_TARGETS = {
    "brain_objects_query": frozenset({"read_only_object_pack"}),
    "brain_source_to_candidate_graph": frozenset({"local_test"}),
    "brain_candidate_review_edit": frozenset({"local_test_pack"}),
    "brain_approval_board_decide": frozenset({"local_test"}),
    "brain_source_to_candidate_runtime_readiness": frozenset({"sanitized_evidence_packet"}),
}
EVIDENCE_PROVENANCE_SCHEMA = "source_to_candidate_runtime_evidence_provenance.v1"
GITOPS_DESIRED_STATE_SCHEMA = "gitops_desired_state_identity.v1"
ARGO_RECONCILIATION_SCHEMA = "argo_reconciliation_identity.v1"
DEPLOYMENT_EVIDENCE_BINDING_SCHEMA = "deployment_evidence_binding.v1"
PROJECTION_JOIN_RUNTIME_SCHEMA = "object_extraction_projection_join_preview.v1"
SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA = "session_project_rollup_runtime_evidence.v1"
SESSION_PROJECT_ROLLUP_PREVIEW_SCHEMA = "object_extraction_session_project_rollup_preview.v1"
SESSION_PROJECT_HANDOFF_SCHEMA = "session_project_handoff_pack.v1"
SESSION_PROJECT_RESUME_SCHEMA = "session_project_resume_context.v1"
TEMPORAL_RECALL_CORRECTIVE_CHECKPOINT_SCHEMA = "temporal_recall_corrective_checkpoint.v1"
TEMPORAL_RECALL_CORRECTIVE_CHECKPOINT_READINESS_SCHEMA = (
    "temporal_recall_corrective_checkpoint_readiness.v1"
)
TEMPORAL_CORRECTNESS_RUNTIME_AGGREGATE_SCHEMA = "temporal_correctness_runtime_aggregate.v1"
TEMPORAL_CORRECTNESS_RUNTIME_POSTCHECK_RECEIPT_SCHEMA = (
    "temporal_correctness_runtime_postcheck_receipt.v1"
)
TEMPORAL_SEMANTIC_RESULT_MIN_SCORE = 0.75
PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA = "preference_artifact_memory_runtime_evidence.v1"
ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA = "artifact_review_preference_check.v1"
_COLLECTOR_CAPABILITY = object()
_COLLECTOR_ATTESTABLE_FIELDS = frozenset(
    {
        "agent_context_startup_runtime",
        "preference_artifact_memory",
    }
)

_GITOPS_DESIRED_STATE_KEYS = frozenset(
    {
        "schema_version",
        "images_include_expected_commit",
        "desired_state_source",
        "target_revision",
        "source_commit",
        "desired_image_set_hash",
        "ops_revision",
        "expected_image_ref_count",
        "production_mutation_performed",
    }
)
_ARGO_RECONCILIATION_KEYS = frozenset(
    {
        "schema_version",
        "reconciliation_source",
        "reconciled_ops_revision",
        "sync_status",
        "health_status",
        "production_mutation_performed",
    }
)
_DEPLOYED_IDENTITY_KEYS = frozenset(
    {
        "contains_expected_commit",
        "identity_source",
        "source_commit",
        "live_image_set_hash",
        "stale_image_ref_count",
        "production_mutation_performed",
    }
)
_DEPLOYMENT_EVIDENCE_BINDING_KEYS = frozenset({"schema_version", "canonical_tuple_hash"})
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PUBLIC_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@-]{0,119}$")
_MALFORMED_EVIDENCE_TYPE_FIELD = "malformed_evidence_type"


class _CollectorAttestedEvidence(dict[str, Any]):
    __slots__ = ("_collector_capability", "_collector_field_hashes")

    def __init__(
        self,
        value: Mapping[str, Any],
        *,
        capability: object,
        attested_fields: Iterable[str],
    ) -> None:
        if capability is not _COLLECTOR_CAPABILITY:
            raise TypeError("collector-attested evidence can only be minted in-process")
        normalized_fields = frozenset(str(field) for field in attested_fields)
        unknown_fields = normalized_fields - _COLLECTOR_ATTESTABLE_FIELDS
        if unknown_fields:
            raise ValueError("collector-attested evidence contains an unsupported field")
        missing_fields = normalized_fields - value.keys()
        if missing_fields:
            raise ValueError("collector-attested evidence field is missing")
        super().__init__(value)
        self._collector_capability = capability
        self._collector_field_hashes = tuple(
            (field, hash_payload(value[field]))
            for field in sorted(normalized_fields)
        )


def _mint_collector_attested_evidence(
    value: Mapping[str, Any],
    *,
    attested_fields: Iterable[str],
) -> dict[str, Any]:
    return _CollectorAttestedEvidence(
        value,
        capability=_COLLECTOR_CAPABILITY,
        attested_fields=attested_fields,
    )


def _collector_attested_fields(value: Mapping[str, Any]) -> frozenset[str]:
    if not (
        isinstance(value, _CollectorAttestedEvidence)
        and value._collector_capability is _COLLECTOR_CAPABILITY
    ):
        return frozenset()
    attested_fields: set[str] = set()
    for field, expected_hash in value._collector_field_hashes:
        if field not in value:
            continue
        try:
            current_hash = hash_payload(value[field])
        except (TypeError, ValueError):
            continue
        if current_hash == expected_hash:
            attested_fields.add(field)
    return frozenset(attested_fields)


def _has_collector_attestation_capability(
    value: Mapping[str, Any],
    field: str,
) -> bool:
    return field in _collector_attested_fields(value)


_RUNTIME_EVIDENCE_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "body",
        "dataset_id",
        "document_id",
        "endpoint_url",
        "host",
        "hostname",
        "host_topology",
        "ip_address",
        "image",
        "image_ref",
        "manifest",
        "manifest_path",
        "raw_manifest",
        "docker_image",
        "password",
        "private",
        "private_path",
        "raw",
        "raw_body",
        "raw_content",
        "raw_source",
        "raw_text",
        "secret",
        "token",
    }
)
_RUNTIME_EVIDENCE_FORBIDDEN_COMPACT_KEYS = frozenset(
    key.replace("_", "") for key in _RUNTIME_EVIDENCE_FORBIDDEN_KEYS
)
_RAW_EXTERNAL_REF_MARKERS = (
    "dataset:",
    "dataset_id:",
    "document:",
    "document_id:",
    "ragflow_dataset:",
    "ragflow_document:",
)
_ARTIFACT_REF_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_RAW_EXTERNAL_REF_SUFFIX_RE = re.compile(
    r"^(?:ragflow[._-])?(?:dataset|document)(?:[._-]|$)",
    re.IGNORECASE,
)
PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA = "permission_sensitive_runtime_audit_evidence.v1"
PERMISSION_AUDIT_EVENT_SCHEMA = "runtime_permission_audit_event.v1"
AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA = "agent_context_startup_runtime_evidence.v1"
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
    project: str = "",
    consumer: str = "codex",
) -> dict[str, Any]:
    required_steps = [
        "collect_mcp_tool_inventory",
        "collect_agent_context_product",
        "probe_brain_objects_query_routes",
        "probe_temporal_recall_corrective_checkpoint",
        "probe_projection_join_runtime",
        "probe_source_to_candidate_review_loop",
        "probe_session_project_rollup_runtime",
        "probe_preference_artifact_memory_runtime",
        "collect_permission_sensitive_audit_runtime",
        "probe_agent_context_startup_runtime",
        "collect_gitops_desired_state",
        "collect_argo_reconciliation",
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
        "project": public_safe_text(str(project or ""), max_chars=120),
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
            "raw_gitops_manifest",
            "raw_image_reference",
        ],
        "gap_mapping": {
            "collect_mcp_tool_inventory": "live_mcp_review_tools_unverified",
            "collect_agent_context_product": "live_agent_context_product_sections_unverified",
            "probe_brain_objects_query_routes": "live_brain_objects_query_route_smokes_unverified",
            "probe_temporal_recall_corrective_checkpoint": "live_temporal_recall_corrective_checkpoint_unverified",
            "probe_projection_join_runtime": "live_graph_qdrant_projection_join_unproven",
            "probe_source_to_candidate_review_loop": "live_source_to_candidate_review_loop_unverified",
            "probe_session_project_rollup_runtime": "live_session_project_rollup_unverified",
            "probe_preference_artifact_memory_runtime": "live_preference_artifact_memory_unverified",
            "collect_permission_sensitive_audit_runtime": "permission_sensitive_audit_unverified",
            "probe_agent_context_startup_runtime": "live_agent_context_startup_unverified",
            "collect_gitops_desired_state": "gitops_desired_state_unverified",
            "collect_argo_reconciliation": "argo_reconciliation_unverified",
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
    project: str = "",
    consumer: str = "codex",
) -> dict[str, Any]:
    collection_plan = build_source_to_candidate_runtime_evidence_collection_plan(
        expected_commit=expected_commit,
        repository=repository,
        branch=branch,
        project=project,
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
        "project": public_safe_text(str(project or ""), max_chars=120),
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
            "temporal_recall_corrective_checkpoint",
            "projection_join",
            "source_to_candidate_review_loop",
            "session_project_rollup_runtime",
            "preference_artifact_memory",
            "permission_sensitive_audit",
            "agent_context_startup_runtime",
            "gitops_desired_state",
            "argo_reconciliation",
            "deployment_evidence_binding",
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
    gitops_desired_state = captured.get("gitops_desired_state")
    argo_reconciliation = captured.get("argo_reconciliation")
    deployed_identity = captured.get("deployed_identity")
    _reject_forbidden_runtime_evidence_keys(gitops_desired_state)
    _reject_forbidden_runtime_evidence_keys(argo_reconciliation)
    _reject_forbidden_runtime_evidence_keys(deployed_identity)
    safe_expected_commit = public_safe_text(
        str(captured.get("expected_commit") or ""), max_chars=80
    )
    safe_desired_state = _public_safe_mapping(
        _deployment_evidence_layer(captured, "gitops_desired_state")
    )
    safe_argo_reconciliation = _public_safe_mapping(
        _deployment_evidence_layer(captured, "argo_reconciliation")
    )
    safe_deployed_identity = _public_safe_mapping(
        _deployment_evidence_layer(captured, "deployed_identity")
    )
    supplied_binding = captured.get("deployment_evidence_binding")
    _reject_forbidden_runtime_evidence_keys(supplied_binding)
    binding = _public_safe_mapping(
        _deployment_evidence_layer(captured, "deployment_evidence_binding")
    )
    collection = captured.get("collection")
    collection = collection if isinstance(collection, Mapping) else {}
    provenance = captured.get("evidence_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else collection
    packet = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": _string_list(captured.get("tool_names")),
        "agent_context_product": _public_safe_mapping(captured.get("agent_context_product")),
        "brain_objects_query_smokes": _public_safe_mapping_list(captured.get("brain_objects_query_smokes")),
        "temporal_recall_corrective_checkpoint": _public_safe_mapping(
            captured.get("temporal_recall_corrective_checkpoint")
        ),
        "temporal_correctness_runtime": _public_safe_mapping(
            captured.get("temporal_correctness_runtime")
        ),
        "projection_join": _public_safe_mapping(captured.get("projection_join")),
        "source_to_candidate_review_loop": _public_safe_mapping(captured.get("source_to_candidate_review_loop")),
        "session_project_rollup_runtime": _public_safe_mapping(captured.get("session_project_rollup_runtime")),
        "session_project_rollup_runtime_present": "session_project_rollup_runtime" in captured,
        "preference_artifact_memory": _public_safe_mapping(captured.get("preference_artifact_memory")),
        "permission_sensitive_audit": _public_safe_mapping(captured.get("permission_sensitive_audit")),
        "agent_context_startup_runtime": _public_safe_mapping(captured.get("agent_context_startup_runtime")),
        "expected_commit": safe_expected_commit,
        "gitops_desired_state": safe_desired_state,
        "argo_reconciliation": safe_argo_reconciliation,
        "deployment_evidence_binding": binding,
        "deployed_identity": safe_deployed_identity,
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
        "production_mutation_performed": captured.get("production_mutation_performed") is True
        or captured.get("mutation_performed") is True,
    }
    ensure_public_safe(packet, "SourceToCandidateRuntimeShadowEvidencePacket")
    attested_fields = _collector_attested_fields(captured)
    if attested_fields:
        return _mint_collector_attested_evidence(
            packet,
            attested_fields=attested_fields,
        )
    return packet


def build_deployment_evidence_binding(
    *,
    expected_commit: str,
    gitops_desired_state: Mapping[str, Any],
    argo_reconciliation: Mapping[str, Any],
    deployed_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Hash a public consistency binding across desired, Argo, and live identity."""

    canonical_tuple = _deployment_evidence_binding_tuple(
        expected_commit=expected_commit,
        gitops_desired_state=gitops_desired_state,
        argo_reconciliation=argo_reconciliation,
        deployed_identity=deployed_identity,
    )
    return {
        "schema_version": DEPLOYMENT_EVIDENCE_BINDING_SCHEMA,
        "canonical_tuple_hash": hash_payload(canonical_tuple),
    }


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


def build_source_to_candidate_runtime_post_deploy_capture_packet(
    *,
    captured_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize a sanitized post-deploy capture into evaluator input."""

    return build_source_to_candidate_runtime_shadow_evidence_packet(
        captured_evidence=captured_evidence,
    )


def build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
    *,
    captured_evidence: Mapping[str, Any],
    expected_commit: str = "",
) -> dict[str, Any]:
    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=captured_evidence,
    )
    return build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=expected_commit,
    )


def build_source_to_candidate_runtime_collected_shadow_evidence_packet(
    *,
    repository: str = "",
    branch: str = "",
    project: str = "",
    consumer: str = "codex",
    expected_commit: str = "",
    route_runner: Callable[[str], Mapping[str, Any]],
    projection_join_runner: Callable[[], Mapping[str, Any]] | None = None,
    review_loop_runner: Callable[[], Mapping[str, Any]] | None = None,
    session_project_rollup_runner: Callable[[], Mapping[str, Any]] | None = None,
    preference_artifact_memory_runner: Callable[[], Mapping[str, Any]] | None = None,
    temporal_correctness_runtime_runner: Callable[[], Mapping[str, Any]] | None = None,
    permission_sensitive_audit_runner: Callable[[], Mapping[str, Any]] | None = None,
    agent_context_startup_runner: Callable[[], Mapping[str, Any]] | None = None,
    tool_names: Any = None,
    collection_mode: str = "local_test_replay",
    network_used: bool = False,
) -> dict[str, Any]:
    """Run read-only route smokes and return evaluator-ready public-safe evidence."""

    smokes = [
        _collect_brain_objects_query_route_smoke(route_runner, route)
        for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    ]
    projection_join = _collect_projection_join_shadow(
        projection_join_runner,
        repository=repository,
    )
    review_loop = _collect_source_to_candidate_review_loop_shadow(review_loop_runner)
    session_project_rollup = _collect_session_project_rollup_shadow(
        session_project_rollup_runner,
        repository=repository,
        branch=branch,
    )
    preference_artifact_memory = _collect_preference_artifact_memory_shadow(
        preference_artifact_memory_runner,
        repository=repository,
    )
    temporal_correctness_runtime = _collect_temporal_correctness_runtime_shadow(
        temporal_correctness_runtime_runner,
    )
    permission_sensitive_audit = _collect_permission_sensitive_audit_shadow(
        permission_sensitive_audit_runner,
    )
    agent_context_startup = _collect_agent_context_startup_shadow(
        agent_context_startup_runner,
        consumer=consumer,
    )
    safe_collection_mode = public_safe_text(str(collection_mode or "local_test_replay"), max_chars=80)
    packet_is_runtime_evidence = safe_collection_mode in LIVE_EVIDENCE_COLLECTION_MODES and network_used is True
    readiness_claim = (
        "runtime_read_path_evidence"
        if packet_is_runtime_evidence
        else "collector_packet_not_live_evidence"
    )
    capture = {
        "schema_version": "source_to_candidate_runtime_shadow_capture.v1",
        "tool_names": _string_list(tool_names) or list(REQUIRED_RUNTIME_TOOL_NAMES),
        "brain_objects_query_smokes": smokes,
        "projection_join": projection_join,
        "source_to_candidate_review_loop": review_loop,
        "session_project_rollup_runtime": session_project_rollup,
        "preference_artifact_memory": preference_artifact_memory,
        "temporal_correctness_runtime": temporal_correctness_runtime,
        "permission_sensitive_audit": permission_sensitive_audit,
        "agent_context_startup_runtime": agent_context_startup,
        "deployed_identity": {
            "contains_expected_commit": False,
            "identity_source": "collector_not_deployed_identity_proof",
        },
        "collector": {
            "schema_version": "source_to_candidate_runtime_evidence_collector.v1",
            "status": "completed_with_gaps",
            "repository": public_safe_text(str(repository or ""), max_chars=120),
            "branch": public_safe_text(str(branch or ""), max_chars=120),
            "project": public_safe_text(str(project or ""), max_chars=120),
            "consumer": public_safe_text(str(consumer or "codex"), max_chars=80),
            "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
            "routes_collected": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
            "route_failure_count": sum(1 for smoke in smokes if "collector_route_smoke_failed" in _smoke_gaps(smoke)),
            "projection_join_collected": bool(projection_join),
            "projection_join_schema": public_safe_text(str(projection_join.get("schema_version") or ""), max_chars=80),
            "projection_join_edge_count": _int_value(projection_join.get("edge_count")),
            "review_loop_collected": bool(review_loop),
            "review_loop_schema": public_safe_text(str(review_loop.get("schema_version") or ""), max_chars=80),
            "session_project_rollup_collected": bool(session_project_rollup),
            "session_project_rollup_schema": public_safe_text(
                str(session_project_rollup.get("schema_version") or ""),
                max_chars=80,
            ),
            "preference_artifact_memory_collected": bool(preference_artifact_memory),
            "preference_artifact_memory_schema": public_safe_text(
                str(preference_artifact_memory.get("schema_version") or ""),
                max_chars=80,
            ),
            "temporal_correctness_runtime_collected": bool(
                temporal_correctness_runtime
            ),
            "permission_sensitive_audit_collected": bool(permission_sensitive_audit),
            "permission_sensitive_audit_schema": public_safe_text(
                str(permission_sensitive_audit.get("schema_version") or ""),
                max_chars=80,
            ),
            "agent_context_startup_collected": bool(agent_context_startup),
            "agent_context_startup_schema": public_safe_text(
                str(agent_context_startup.get("schema_version") or ""),
                max_chars=80,
            ),
            "network_used": network_used is True,
            "mutation_allowed": False,
            "production_mutation_performed": False,
            "readiness_claim": readiness_claim,
        },
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": safe_collection_mode,
            "network_used": network_used is True,
            "mutation_scope": "none",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    packet = build_source_to_candidate_runtime_shadow_evidence_packet(captured_evidence=capture)
    packet["collector"] = capture["collector"]
    ensure_public_safe(packet, "SourceToCandidateRuntimeCollectedShadowEvidencePacket")
    return packet


def build_source_to_candidate_projection_join_shadow_evidence(
    *,
    repository: str = "neurons",
) -> dict[str, Any]:
    """Build branch-local projection join evidence without graph/search mutation."""

    from .extraction_pipeline import run_graph_search_projection_join_preview

    target_object_id = "ko:RepoDocument:projection-join-shadow-target"
    preview = run_graph_search_projection_join_preview(
        objects=[
            {
                "object_id": target_object_id,
                "object_type": "RepoDocument",
                "title": "Projection join shadow target",
                "summary": "Public-safe source-to-candidate projection join target.",
                "authority_lane": "candidate",
                "verification_state": "source_hash_verified",
                "review_state": "needs_review",
            }
        ],
        projection_hits=[
            {
                "hit_id": "projection-hit:graph-shadow",
                "source": "graph",
                "object_ref": target_object_id,
                "summary": "Derived graph projection hit for the shadow target.",
                "score": 0.86,
            },
            {
                "hit_id": "projection-hit:qdrant-shadow",
                "source": "search",
                "object_ref": target_object_id,
                "summary": "Derived search projection hit for the shadow target.",
                "score": 0.82,
            },
        ],
        repository=repository or "neurons",
    )
    evidence = {
        **preview,
        "evidence_class": "runtime_projection_join",
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    ensure_public_safe(evidence, "SourceToCandidateProjectionJoinShadowEvidence")
    return evidence


def _collect_projection_join_shadow(
    projection_join_runner: Callable[[], Mapping[str, Any]] | None,
    *,
    repository: str = "neurons",
) -> dict[str, Any]:
    try:
        raw = (
            projection_join_runner()
            if projection_join_runner is not None
            else build_source_to_candidate_projection_join_shadow_evidence(
                repository=repository or "neurons",
            )
        )
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": PROJECTION_JOIN_RUNTIME_SCHEMA,
            "evidence_class": "runtime_projection_join",
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "status": "pass_with_gaps",
            "edge_count": 0,
            "production_mutation_performed": False,
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        }
    evidence = _public_safe_mapping(raw)
    ensure_public_safe(evidence, "CollectedProjectionJoinShadowEvidence")
    return evidence


def build_source_to_candidate_review_loop_shadow_evidence(
    *,
    project: str = "neurons",
    consumer: str = "codex",
) -> dict[str, Any]:
    """Build a branch-local local_test source->candidate->review->approval smoke summary."""

    from .extraction_pipeline import run_source_to_candidate_graph_activation_preview
    from .object_packs import apply_approval_board_decisions, apply_candidate_review_edits

    corpus_status = _source_to_candidate_shadow_corpus_status(project=project)
    graph = run_source_to_candidate_graph_activation_preview(
        corpus_status=corpus_status,
        project=project,
        consumer=consumer,
    )
    pack = graph.get("candidate_graph_review_pack") if isinstance(graph.get("candidate_graph_review_pack"), Mapping) else {}
    candidates = pack.get("lanes", {}).get("candidate") if isinstance(pack.get("lanes"), Mapping) else []
    candidates = candidates if isinstance(candidates, list) else []
    candidate_id = public_safe_text(
        str(candidates[0].get("object_id") if candidates and isinstance(candidates[0], Mapping) else ""),
        max_chars=180,
    )
    edit_result = apply_candidate_review_edits(
        pack,
        edits=[
            {
                "action": "update_object",
                "object_id": candidate_id,
                "fields": {
                    "summary": "Reviewer clarified branch-local source-to-candidate shadow evidence.",
                    "recommended_action": "promote",
                },
            }
        ],
        reviewer={"id": "runtime-shadow-reviewer"},
        target_scope="local_test",
        mutation_mode="no_mutation",
    )
    edited_pack = edit_result.get("updated_pack") if isinstance(edit_result.get("updated_pack"), Mapping) else pack
    decision_result = apply_approval_board_decisions(
        edited_pack,
        decisions=[
            {
                "action": "promote",
                "object_id": candidate_id,
                "reason": "Branch-local source-to-candidate shadow approval smoke.",
                "approved_by": "runtime-shadow-reviewer",
            }
        ],
        reviewer={"id": "runtime-shadow-reviewer"},
        ledger_scope="local_test",
    )
    decided_pack = (
        decision_result.get("updated_pack")
        if isinstance(decision_result.get("updated_pack"), Mapping)
        else edited_pack
    )
    accepted_current = (
        decided_pack.get("lanes", {}).get("accepted_current")
        if isinstance(decided_pack.get("lanes"), Mapping)
        else []
    )
    accepted_current = accepted_current if isinstance(accepted_current, list) else []
    evidence = {
        "schema_version": "source_to_candidate_review_loop_evidence.v1",
        "source_to_candidate_graph": {
            "schema_version": public_safe_text(str(graph.get("schema_version") or ""), max_chars=80),
            "status": public_safe_text(str(graph.get("status") or ""), max_chars=80),
            "target_scope": "local_test",
            "pack_type": public_safe_text(str(pack.get("route") or "candidate_graph_review"), max_chars=80),
            "candidate_count": len(candidates),
            "accepted_count": len(accepted_current),
            "quality_gate": _public_safe_mapping(graph.get("quality_gate")),
            "production_mutation_performed": graph.get("production_mutation_performed") is True,
            "mutation_performed": False,
        },
        "candidate_review_edit": {
            "schema_version": public_safe_text(str(edit_result.get("schema_version") or ""), max_chars=80),
            "status": "PASS" if edit_result.get("permission") == "allowed" else "FAIL",
            "target_scope": public_safe_text(str(edit_result.get("target_scope") or ""), max_chars=80),
            "mutation_mode": public_safe_text(str(edit_result.get("mutation_mode") or ""), max_chars=80),
            "edited_candidate_count": len(edit_result.get("accepted_edits") or []),
            "rejected_edit_count": len(edit_result.get("rejected_edits") or []),
            "production_mutation_performed": edit_result.get("production_mutation_performed") is True,
            "authority_write_performed": edit_result.get("authority_write_performed") is True,
        },
        "approval_board_decision": {
            "schema_version": public_safe_text(str(decision_result.get("schema_version") or ""), max_chars=80),
            "status": "PASS" if decision_result.get("permission") == "allowed" else "FAIL",
            "ledger_scope": public_safe_text(str(decision_result.get("ledger_scope") or ""), max_chars=80),
            "authority_write_scope": public_safe_text(
                str(decision_result.get("authority_write_scope") or ""),
                max_chars=80,
            ),
            "decision_count": _int_value(decision_result.get("decision_count")),
            "authority_write_performed": decision_result.get("authority_write_performed") is True,
            "production_mutation_performed": decision_result.get("production_mutation_performed") is True,
        },
        "read_after_write": {
            "status": "validated" if accepted_current else "missing",
            "object_pack_schema": public_safe_text(str(decided_pack.get("schema_version") or "object_pack.v1"), max_chars=80),
            "route": public_safe_text(str(decided_pack.get("route") or "candidate_graph_review"), max_chars=80),
            "authority_lane": "accepted_current",
            "object_count": len(accepted_current),
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    ensure_public_safe(evidence, "SourceToCandidateReviewLoopShadowEvidence")
    return evidence


def _collect_source_to_candidate_review_loop_shadow(
    review_loop_runner: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    try:
        raw = review_loop_runner() if review_loop_runner is not None else build_source_to_candidate_review_loop_shadow_evidence()
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": "source_to_candidate_review_loop_evidence.v1",
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "source_to_candidate_graph": {
                "schema_version": "",
                "target_scope": "local_test",
                "pack_type": "candidate_graph_review",
                "candidate_count": 0,
                "production_mutation_performed": False,
                "mutation_performed": False,
            },
            "candidate_review_edit": {
                "schema_version": "",
                "target_scope": "local_test",
                "mutation_mode": "no_mutation",
                "edited_candidate_count": 0,
                "rejected_edit_count": 0,
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
            "approval_board_decision": {
                "schema_version": "",
                "ledger_scope": "local_test",
                "authority_write_scope": "",
                "decision_count": 0,
                "authority_write_performed": False,
                "production_mutation_performed": False,
            },
            "read_after_write": {"status": "missing", "object_pack_schema": ""},
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        }
    evidence = _public_safe_mapping(raw)
    ensure_public_safe(evidence, "CollectedSourceToCandidateReviewLoopShadowEvidence")
    return evidence


def _source_to_candidate_shadow_corpus_status(*, project: str) -> dict[str, Any]:
    safe_project = public_safe_text(str(project or "neurons"), max_chars=120)
    return {
        "schema_version": "brain_corpus_status.v1",
        "project": safe_project,
        "corpus_id": "local-test-shadow-corpus",
        "source_count": 1,
        "reference_object_count": 1,
        "document_source_count": 1,
        "extraction_run_count": 1,
        "storage_modes": {"managed_snapshot": 1},
        "manifest_hashes": ["sha256:" + "1" * 64],
        "document_sources": [
            {
                "source_id": "local-test-shadow-source",
                "title": "Branch-local source-to-candidate shadow source",
                "content_hash": "sha256:" + "2" * 64,
                "verification_state": "source_hash_verified",
                "source_url_status": "verified",
                "normalized_path_ref": "docs/specs/redacted-shadow-source.md",
            }
        ],
        "freshness_gaps": [],
        "gaps": [],
    }


def build_session_project_rollup_shadow_evidence(
    *,
    repository: str = "neurons",
    branch: str = "codex/knowledge-object-review-flow-roadmap",
    project: str = "neurons",
) -> dict[str, Any]:
    """Build a branch-local local_test P6 session/project/work-unit rollup summary."""

    from .extraction_pipeline import run_session_project_rollup_preview

    report = run_session_project_rollup_preview(
        sessions=[
            {
                "session_id_hash": "session:p6-shadow-a",
                "device_id_hash": "device:p6-shadow-this",
                "provider": "codex",
                "summary": "P6 shadow rollup visible session.",
                "work_unit_id": "work:p6-shadow",
                "evidence_refs": ["ev:p6-shadow:session-a"],
            },
            {
                "session_id_hash": "session:p6-shadow-b",
                "device_id_hash": "device:p6-shadow-other",
                "provider": "codex",
                "summary": "P6 shadow rollup other-device session.",
                "work_unit_id": "work:p6-shadow",
                "evidence_refs": ["ev:p6-shadow:session-b"],
            },
        ],
        repository=repository,
        branch=branch,
        project=project,
        specs=[{"spec_ref": "docs/specs/p6/design.md", "work_unit_id": "work:p6-shadow"}],
        pull_requests=[{"pr_id": "pr:95", "number": 95, "work_unit_id": "work:p6-shadow"}],
        commits=[{"commit_id": "commit:p6-shadow", "pull_request_id": "pr:95", "work_unit_id": "work:p6-shadow"}],
        requesting_device_id_hash="device:p6-shadow-this",
        scope="all_devices",
    )
    handoff = report.get("handoff_pack") if isinstance(report.get("handoff_pack"), Mapping) else {}
    resume = handoff.get("resume_context") if isinstance(handoff.get("resume_context"), Mapping) else {}
    object_refs = handoff.get("object_refs") if isinstance(handoff.get("object_refs"), Mapping) else {}
    objects = report.get("objects") if isinstance(report.get("objects"), list) else []
    edges = report.get("edges") if isinstance(report.get("edges"), list) else []
    object_type_counts = _object_type_counts(objects)
    evidence = {
        "schema_version": SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA,
        "rollup_preview": {
            "schema_version": public_safe_text(str(report.get("schema_version") or ""), max_chars=80),
            "status": public_safe_text(str(report.get("status") or ""), max_chars=80),
            "scope": public_safe_text(str(report.get("scope") or ""), max_chars=80),
            "object_type_counts": object_type_counts,
            "edge_types": _edge_types(edges),
            "object_count": _int_value(report.get("object_count")),
            "edge_count": _int_value(report.get("edge_count")),
            "visible_session_count": _int_value(report.get("visible_session_count")),
            "all_device_session_count": _int_value(report.get("all_device_session_count")),
            "device_count": _int_value(report.get("device_count")),
            "production_mutation_performed": report.get("production_mutation_performed") is True,
        },
        "handoff_pack": {
            "schema_version": public_safe_text(str(handoff.get("schema_version") or ""), max_chars=80),
            "raw_return_capability": public_safe_text(str(handoff.get("raw_return_capability") or ""), max_chars=80),
            "visible_session_count": _int_value(handoff.get("visible_session_count")),
            "all_device_session_count": _int_value(handoff.get("all_device_session_count")),
            "object_ref_counts": _object_ref_counts(object_refs),
            "resume_context": {
                "schema_version": public_safe_text(str(resume.get("schema_version") or ""), max_chars=80),
                "latest_session_ref_present": isinstance(resume.get("latest_session"), Mapping),
                "work_unit_ref_count": len(resume.get("work_unit_refs") or []),
                "production_mutation_performed": resume.get("production_mutation_performed") is True,
            },
        },
        "read_after_write": {
            "status": "validated",
            "route": "temporal_work_recall",
            "object_pack_schema": "object_pack.v1",
            "object_types": ["WorkUnit"],
            "object_count": _int_value(object_type_counts.get("WorkUnit")),
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    ensure_public_safe(evidence, "SessionProjectRollupShadowEvidence")
    return evidence


def _collect_session_project_rollup_shadow(
    session_project_rollup_runner: Callable[[], Mapping[str, Any]] | None,
    *,
    repository: str = "neurons",
    branch: str = "codex/knowledge-object-review-flow-roadmap",
) -> dict[str, Any]:
    try:
        raw = (
            session_project_rollup_runner()
            if session_project_rollup_runner is not None
            else build_session_project_rollup_shadow_evidence(
                repository=repository or "neurons",
                branch=branch or "codex/knowledge-object-review-flow-roadmap",
            )
        )
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": SESSION_PROJECT_ROLLUP_RUNTIME_SCHEMA,
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "rollup_preview": {
                "schema_version": "",
                "scope": "all_devices",
                "object_type_counts": {},
                "edge_types": [],
                "visible_session_count": 0,
                "all_device_session_count": 0,
                "device_count": 0,
                "production_mutation_performed": False,
            },
            "handoff_pack": {
                "schema_version": "",
                "raw_return_capability": "denied",
                "resume_context": {
                    "schema_version": "",
                    "latest_session_ref_present": False,
                    "work_unit_ref_count": 0,
                    "production_mutation_performed": False,
                },
            },
            "read_after_write": {
                "status": "missing",
                "route": "temporal_work_recall",
                "object_pack_schema": "",
                "object_types": [],
                "object_count": 0,
            },
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        }
    evidence = _public_safe_mapping(raw)
    ensure_public_safe(evidence, "CollectedSessionProjectRollupShadowEvidence")
    return evidence


def _object_type_counts(objects: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(objects, list):
        return counts
    for obj in objects:
        if not isinstance(obj, Mapping):
            continue
        object_type = public_safe_text(str(obj.get("object_type") or ""), max_chars=80)
        if object_type:
            counts[object_type] = counts.get(object_type, 0) + 1
    return counts


def _object_ref_counts(object_refs: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for object_type, refs in object_refs.items():
        if not isinstance(refs, list):
            continue
        safe_type = public_safe_text(str(object_type or ""), max_chars=80)
        if safe_type:
            counts[safe_type] = len(refs)
    return counts


def _edge_types(edges: Any) -> list[str]:
    if not isinstance(edges, list):
        return []
    return sorted(
        {
            public_safe_text(str(edge.get("edge_type") or ""), max_chars=120)
            for edge in edges
            if isinstance(edge, Mapping) and edge.get("edge_type")
        }
    )


def build_preference_artifact_memory_shadow_evidence(
    *,
    repository: str = "neurons",
) -> dict[str, Any]:
    """Build a branch-local local_test P7 preference/artifact memory summary."""

    from .extraction_pipeline import run_preference_style_extraction_preview

    report = run_preference_style_extraction_preview(
        memory_cards=[
            {
                "memory_id": "mem:p7-shadow-html-review-accepted",
                "card_type": "preference",
                "summary": "Accepted HTML artifact preference",
                "confidence": 0.94,
                "currentness": "current",
                "review_state": "accepted",
                "lifecycle_state": "accepted",
                "approval_state": "approved",
                "project": "neurons",
                "content_hash": "sha256:" + "a" * 64,
                "typed_payload": {
                    "preference": "HTML review artifacts should be information dense.",
                    "applies_to": "html review artifact",
                    "reason": "Accepted local_test preference evidence.",
                    "source_object_type": "ArtifactPreference",
                    "target_object_id": "ko:ArtifactPreference:p7-shadow-html-review",
                    "source_content_hash": "sha256:" + "b" * 64,
                    "authority_proposal_id": "proposal:p7-shadow-html-review",
                    "authority_decision_id": "decision:p7-shadow-html-review",
                },
                "source_refs": [{"source_ref_id": "ev:p7-shadow:html-review"}],
            },
            {
                "memory_id": "mem:p7-shadow-visualization-proposal",
                "card_type": "preference",
                "summary": "Proposed visualization preference",
                "confidence": 0.61,
                "currentness": "inferred",
                "project": "neurons",
                "content_hash": "sha256:" + "c" * 64,
                "typed_payload": {
                    "preference": "Visualization artifacts should use motion only when it clarifies state.",
                    "applies_to": "visualization artifact",
                    "reason": "Observed local_test preference candidate requiring review.",
                    "source_object_type": "ArtifactPreference",
                    "target_object_id": "ko:ArtifactPreference:p7-shadow-visualization",
                    "source_content_hash": "sha256:" + "d" * 64,
                    "authority_proposal_id": "proposal:p7-shadow-visualization",
                },
                "source_refs": [{"source_ref_id": "ev:p7-shadow:visualization"}],
            },
        ],
        repository=repository,
        current_request="review HTML visualization artifact",
        current_files=[],
        artifact_review={
            "artifact_type": "html_review",
            "summary": "Dense review output with prioritized findings and evidence links.",
            "text_metrics": {
                "finding_count": 3,
                "evidence_ref_count": 3,
                "word_count": 640,
            },
            "body": "redacted-local-test-body-not-returned",
        },
    )
    pack = report.get("artifact_preference_pack") if isinstance(report.get("artifact_preference_pack"), Mapping) else {}
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    accepted = [dict(item) for item in lanes.get("accepted_current", []) if isinstance(item, Mapping)]
    proposals = [dict(item) for item in lanes.get("proposal_only", []) if isinstance(item, Mapping)]
    preference_objects = [*accepted, *proposals]
    recommended_actions = pack.get("recommended_actions") if isinstance(pack.get("recommended_actions"), list) else []
    artifact_check = (
        report.get("artifact_review_check") if isinstance(report.get("artifact_review_check"), Mapping) else {}
    )
    safe_artifact_check = _public_safe_mapping(artifact_check)
    safe_artifact_check["schema_version"] = public_safe_text(
        str(safe_artifact_check.get("schema_version") or ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA),
        max_chars=80,
    )
    safe_artifact_check["raw_artifact_body_returned"] = False
    evidence = {
        "schema_version": PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA,
        "preference_object_pack": {
            "schema_version": "object_pack.v1",
            "route": "code_style_preference",
            "accepted_preference_count": len(accepted),
            "proposal_preference_count": len(proposals),
            "objects": preference_objects,
            "lanes": {
                "accepted_current": accepted,
                "proposal_only": proposals,
            },
            "recommended_actions": recommended_actions,
            "gaps": list(pack.get("gaps") or []),
            "production_mutation_performed": False,
        },
        "html_visualization_route_smoke": {
            "schema_version": "brain_objects_query.v1",
            "route": "html_visualization_preference",
            "production_mutation_performed": False,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": "html_visualization_preference",
                "objects": accepted,
                "lanes": {"accepted_current": accepted},
                "recommended_actions": [
                    {"object_id": str(obj.get("object_id") or ""), "action": "apply_preference"}
                    for obj in accepted
                    if str(obj.get("object_id") or "")
                ],
                "gaps": [] if accepted else ["accepted_html_preference_missing"],
            },
        },
        "agent_context_preference_section": {
            "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
            "section": "style_preference",
            "object_count": len(accepted),
            "accepted_preference_count": len(accepted),
            "authority_lanes": [REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE] if accepted else [],
            "surface_policy": {"mutation_allowed": False},
        },
        "artifact_review_check": safe_artifact_check,
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }
    ensure_public_safe(evidence, "PreferenceArtifactMemoryShadowEvidence")
    return evidence


def build_preference_artifact_memory_runtime_evidence(
    *,
    preference_route: Mapping[str, Any],
    html_route: Mapping[str, Any],
    context_pack: Mapping[str, Any],
    artifact_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Build P7 evidence only from actual public-safe runtime read surfaces."""

    for raw_input in (preference_route, html_route, context_pack, artifact_summary):
        _reject_forbidden_runtime_evidence_keys(raw_input)
    safe_preference_route = _public_safe_mapping(preference_route)
    safe_html_route = _public_safe_mapping(html_route)
    safe_context = _public_safe_mapping(context_pack)
    preference_pack = (
        safe_preference_route.get("object_pack")
        if isinstance(safe_preference_route.get("object_pack"), Mapping)
        else {}
    )
    html_pack = (
        safe_html_route.get("object_pack")
        if isinstance(safe_html_route.get("object_pack"), Mapping)
        else {}
    )
    authority = safe_context.get("authority") if isinstance(safe_context.get("authority"), Mapping) else {}
    product = (
        authority.get("agent_context_product")
        if isinstance(authority.get("agent_context_product"), Mapping)
        else {}
    )
    sections = product.get("sections") if isinstance(product.get("sections"), Mapping) else {}
    style_section = sections.get("style_preference") if isinstance(sections.get("style_preference"), Mapping) else {}

    code_objects = _accepted_artifact_preferences(preference_pack)
    html_objects = _accepted_artifact_preferences(html_pack)
    context_items = style_section.get("items") if isinstance(style_section.get("items"), list) else []
    context_objects = [
        _artifact_preference_object_view(item)
        for item in context_items
        if isinstance(item, Mapping)
        and item.get("object_type") == "ArtifactPreference"
        and item.get("authority_lane") == REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE
        and knowledge_object_class_from_id(str(item.get("object_id") or ""))
        == "ArtifactPreference"
    ]
    code_ids = sorted(_object_ids(code_objects))
    html_ids = sorted(_object_ids(html_objects))
    context_ids = sorted(_object_ids(context_objects))
    aligned_ids = sorted(set(code_ids).intersection(html_ids, context_ids))
    code_by_id = {str(item.get("object_id") or ""): item for item in code_objects}
    html_by_id = {str(item.get("object_id") or ""): item for item in html_objects}
    context_by_id = {str(item.get("object_id") or ""): item for item in context_objects}
    target_object_id = ""
    continuity: tuple[str, str, str, str, str, str] = ("", "", "", "", "", "")
    for candidate_id in aligned_ids:
        surface_continuity = [
            _artifact_preference_continuity(view)
            for view in (
                code_by_id[candidate_id],
                html_by_id[candidate_id],
                context_by_id[candidate_id],
            )
        ]
        if surface_continuity[0] != ("", "", "", "", "", "") and len(set(surface_continuity)) == 1:
            target_object_id = candidate_id
            continuity = surface_continuity[0]
            break
    alignment_status = "validated" if target_object_id else "failed"

    safe_summary = public_safe_text(str(artifact_summary.get("summary") or ""), max_chars=360)
    artifact_type = public_safe_text(str(artifact_summary.get("artifact_type") or ""), max_chars=80)
    consumer_provenance = (
        artifact_summary.get("consumer_provenance")
        if isinstance(artifact_summary.get("consumer_provenance"), Mapping)
        else {}
    )
    safe_consumer_provenance = {
        "consumer": public_safe_text(str(consumer_provenance.get("consumer") or ""), max_chars=120),
        "workflow": public_safe_text(str(consumer_provenance.get("workflow") or ""), max_chars=120),
        "evidence_kind": public_safe_text(
            str(consumer_provenance.get("evidence_kind") or ""),
            max_chars=80,
        ),
    }
    artifact_fingerprint = ""
    supplied_fingerprint = str(artifact_summary.get("artifact_fingerprint") or "")
    try:
        artifact_fingerprint = require_sha256(supplied_fingerprint, "artifact_fingerprint")
    except ValueError:
        pass
    finding_refs = _public_safe_artifact_refs(
        artifact_summary.get("finding_refs"),
        required_prefix="finding:",
    )
    evidence_refs = _public_safe_artifact_refs(
        artifact_summary.get("evidence_refs"),
        required_prefix="evidence:",
    )
    finding_count = len(finding_refs)
    evidence_ref_count = len(evidence_refs)
    word_count = _int_value(artifact_summary.get("word_count"))
    artifact_failures: list[str] = []
    if artifact_type != "html_review" or not safe_summary:
        artifact_failures.append("html_artifact_summary_missing")
    if not target_object_id:
        artifact_failures.append("accepted_html_preference_missing")
    if (
        safe_consumer_provenance["evidence_kind"] != "actual_consumer_output"
        or not safe_consumer_provenance["consumer"]
        or not safe_consumer_provenance["workflow"]
    ):
        artifact_failures.append("actual_artifact_consumer_provenance_missing")
    if not artifact_fingerprint:
        artifact_failures.append("artifact_fingerprint_missing")
    if finding_count < 1 or evidence_ref_count < 1:
        artifact_failures.append("information_density_evidence_missing")
    if _int_value(artifact_summary.get("finding_count")) != finding_count:
        artifact_failures.append("artifact_finding_count_mismatch")
    if _int_value(artifact_summary.get("evidence_ref_count")) != evidence_ref_count:
        artifact_failures.append("artifact_evidence_ref_count_mismatch")
    gaps: list[str] = []
    if not code_ids:
        gaps.append("accepted_current_artifact_preference_missing")
    if not target_object_id:
        gaps.append("preference_read_surface_target_mismatch")
    if aligned_ids and not target_object_id:
        gaps.append("preference_read_surface_metadata_mismatch")
    if any(
        failure
        in {
            "actual_artifact_consumer_provenance_missing",
            "artifact_fingerprint_missing",
            "information_density_evidence_missing",
            "artifact_finding_count_mismatch",
            "artifact_evidence_ref_count_mismatch",
        }
        for failure in artifact_failures
    ):
        gaps.append("artifact_consumer_evidence_missing")

    raw_proposal_lane = (
        preference_pack.get("lanes", {}).get("proposal_only", [])
        if isinstance(preference_pack.get("lanes"), Mapping)
        else []
    )
    proposal_lane = [
        _artifact_preference_object_view(item)
        for item in raw_proposal_lane
        if isinstance(item, Mapping)
        and item.get("object_type") == "ArtifactPreference"
        and knowledge_object_class_from_id(str(item.get("object_id") or ""))
        == "ArtifactPreference"
    ]
    evidence = {
        "schema_version": PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA,
        "attestation_state": "unattested_runtime_read",
        "read_surface_alignment": {
            "status": alignment_status,
            "target_object_id": target_object_id,
            "memory_id": continuity[0],
            "card_content_hash": continuity[1],
            "authority_proposal_id": continuity[2],
            "authority_decision_id": continuity[3],
            "project": continuity[4],
            "source_content_hash": continuity[5],
            "code_style_preference_object_ids": code_ids,
            "html_visualization_preference_object_ids": html_ids,
            "style_preference_context_object_ids": context_ids,
        },
        "preference_object_pack": {
            "schema_version": public_safe_text(str(preference_pack.get("schema_version") or ""), max_chars=80),
            "route": public_safe_text(str(preference_pack.get("route") or ""), max_chars=80),
            "accepted_preference_count": len(code_objects),
            "proposal_preference_count": len(proposal_lane),
            "objects": [*code_objects, *proposal_lane],
            "lanes": {
                "accepted_current": code_objects,
                "proposal_only": proposal_lane,
            },
            "recommended_actions": _artifact_preference_action_views(
                preference_pack.get("recommended_actions")
            ),
            "gaps": _string_list(preference_pack.get("gaps")),
            "production_mutation_performed": False,
        },
        "html_visualization_route_smoke": {
            "schema_version": public_safe_text(str(safe_html_route.get("schema_version") or ""), max_chars=80),
            "route": public_safe_text(str(safe_html_route.get("route") or ""), max_chars=80),
            "production_mutation_performed": False,
            "object_pack": {
                "schema_version": public_safe_text(
                    str(html_pack.get("schema_version") or ""),
                    max_chars=80,
                ),
                "route": public_safe_text(str(html_pack.get("route") or ""), max_chars=80),
                "objects": html_objects,
                "lanes": {"accepted_current": html_objects},
                "recommended_actions": _artifact_preference_action_views(
                    html_pack.get("recommended_actions")
                ),
                "gaps": _string_list(html_pack.get("gaps")),
            },
        },
        "agent_context_preference_section": {
            "schema_version": public_safe_text(str(product.get("schema_version") or ""), max_chars=80),
            "section": "style_preference",
            "object_count": len(context_objects),
            "accepted_preference_count": len(context_objects),
            "authority_lanes": list(style_section.get("authority_lanes") or []),
            "items": context_objects,
            "surface_policy": {
                "mutation_allowed": (
                    product.get("surface_policy", {}).get("mutation_allowed")
                    if isinstance(product.get("surface_policy"), Mapping)
                    else None
                )
            },
        },
        "artifact_consumer_evidence": {
            "status": "validated" if not artifact_failures else "failed",
            "consumer_provenance": safe_consumer_provenance,
            "artifact_fingerprint": artifact_fingerprint,
            "finding_refs": finding_refs,
            "evidence_refs": evidence_refs,
            "finding_count": finding_count,
            "evidence_ref_count": evidence_ref_count,
        },
        "artifact_review_check": {
            "schema_version": ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA,
            "status": "pass" if not artifact_failures else "failed",
            "ui_required": False,
            "artifact_type": artifact_type,
            "artifact_summary": safe_summary,
            "artifact_metrics": {
                "finding_count": finding_count,
                "evidence_ref_count": evidence_ref_count,
                "word_count": word_count,
            },
            "matched_preference_object_ids": [target_object_id] if target_object_id else [],
            "failures": artifact_failures,
            "raw_artifact_body_returned": False,
        },
        "gaps": gaps,
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    ensure_public_safe(evidence, "PreferenceArtifactMemoryRuntimeEvidence")
    return evidence


def _public_safe_artifact_refs(value: Any, *, required_prefix: str) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        ensure_public_safe(item, "artifact_consumer_ref")
        safe = public_safe_text(item, max_chars=180)
        if not safe:
            return []
        normalized = safe.casefold()
        decoded = _fully_unquote(safe)
        suffix = safe[len(required_prefix) :] if normalized.startswith(required_prefix) else ""
        if (
            decoded != safe
            or not suffix
            or not _ARTIFACT_REF_SUFFIX_RE.fullmatch(suffix)
            or _RAW_EXTERNAL_REF_SUFFIX_RE.match(suffix)
            or any(marker in normalized for marker in _RAW_EXTERNAL_REF_MARKERS)
        ):
            raise ValueError("public-safe artifact refs must use internal finding/evidence refs")
        refs.append(safe)
    return list(dict.fromkeys(refs))


def _accepted_artifact_preferences(pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    accepted = lanes.get(REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE)
    return [
        _artifact_preference_object_view(item)
        for item in accepted or []
        if isinstance(item, Mapping)
        and item.get("object_type") == "ArtifactPreference"
        and item.get("authority_lane") == REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE
        and knowledge_object_class_from_id(str(item.get("object_id") or ""))
        == "ArtifactPreference"
    ]


def _artifact_preference_object_view(obj: Mapping[str, Any]) -> dict[str, Any]:
    scope = obj.get("scope") if isinstance(obj.get("scope"), Mapping) else {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), Mapping) else {}
    content_hash = public_safe_text(str(obj.get("content_hash") or ""), max_chars=80)
    source_content_hash = public_safe_text(
        str(payload.get("source_content_hash") or content_hash),
        max_chars=80,
    )
    return {
        "object_id": public_safe_text(str(obj.get("object_id") or ""), max_chars=180),
        "object_type": public_safe_text(str(obj.get("object_type") or ""), max_chars=80),
        "authority_lane": public_safe_text(str(obj.get("authority_lane") or ""), max_chars=80),
        "title": public_safe_text(str(obj.get("title") or ""), max_chars=240),
        "memory_id": public_safe_text(
            str(obj.get("memory_id") or payload.get("memory_id") or ""),
            max_chars=180,
        ),
        "card_content_hash": public_safe_text(
            str(obj.get("card_content_hash") or payload.get("card_content_hash") or ""),
            max_chars=80,
        ),
        "authority_proposal_id": public_safe_text(
            str(
                obj.get("authority_proposal_id")
                or payload.get("authority_proposal_id")
                or ""
            ),
            max_chars=180,
        ),
        "project": public_safe_text(
            str(scope.get("project") or payload.get("project") or ""),
            max_chars=120,
        ),
        "content_hash": content_hash,
        "source_content_hash": source_content_hash,
        "authority_decision_id": public_safe_text(
            str(payload.get("authority_decision_id") or ""),
            max_chars=180,
        ),
    }


def _artifact_preference_action_views(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "object_id": public_safe_text(str(item.get("object_id") or ""), max_chars=180),
            "action": public_safe_text(str(item.get("action") or ""), max_chars=120),
        }
        for item in value
        if isinstance(item, Mapping)
    ]


def _artifact_preference_continuity(
    obj: Mapping[str, Any],
) -> tuple[str, str, str, str, str, str]:
    scope = obj.get("scope") if isinstance(obj.get("scope"), Mapping) else {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), Mapping) else {}
    memory_id = public_safe_text(
        str(obj.get("memory_id") or payload.get("memory_id") or ""),
        max_chars=180,
    )
    card_content_hash = public_safe_text(
        str(obj.get("card_content_hash") or payload.get("card_content_hash") or ""),
        max_chars=80,
    )
    authority_proposal_id = public_safe_text(
        str(obj.get("authority_proposal_id") or payload.get("authority_proposal_id") or ""),
        max_chars=180,
    )
    authority_decision_id = public_safe_text(
        str(obj.get("authority_decision_id") or payload.get("authority_decision_id") or ""),
        max_chars=180,
    )
    project = public_safe_text(
        str(obj.get("project") or scope.get("project") or payload.get("project") or ""),
        max_chars=120,
    )
    content_hash = public_safe_text(
        str(
            obj.get("source_content_hash")
            or obj.get("content_hash")
            or payload.get("source_content_hash")
            or ""
        ),
        max_chars=80,
    )
    try:
        require_sha256(card_content_hash, "card_content_hash")
        require_sha256(content_hash, "source_content_hash")
    except ValueError:
        return "", "", "", "", "", ""
    if not all(
        (
            memory_id,
            authority_proposal_id,
            authority_decision_id,
            project,
        )
    ):
        return "", "", "", "", "", ""
    return (
        memory_id,
        card_content_hash,
        authority_proposal_id,
        authority_decision_id,
        project,
        content_hash,
    )


def _object_ids(objects: list[Mapping[str, Any]]) -> set[str]:
    return {
        public_safe_text(str(item.get("object_id") or ""), max_chars=180)
        for item in objects
        if str(item.get("object_id") or "")
    }


def _collect_preference_artifact_memory_shadow(
    preference_artifact_memory_runner: Callable[[], Mapping[str, Any]] | None,
    *,
    repository: str = "neurons",
) -> dict[str, Any]:
    try:
        raw = (
            preference_artifact_memory_runner()
            if preference_artifact_memory_runner is not None
            else build_preference_artifact_memory_shadow_evidence(repository=repository or "neurons")
        )
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA,
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "preference_object_pack": {
                "schema_version": "object_pack.v1",
                "route": "code_style_preference",
                "accepted_preference_count": 0,
                "proposal_preference_count": 0,
                "objects": [],
                "lanes": {"accepted_current": [], "proposal_only": []},
                "recommended_actions": [],
                "gaps": ["preference_artifact_collector_failed"],
                "production_mutation_performed": False,
            },
            "html_visualization_route_smoke": {
                "schema_version": "brain_objects_query.v1",
                "route": "html_visualization_preference",
                "production_mutation_performed": False,
                "object_pack": {
                    "schema_version": "object_pack.v1",
                    "route": "html_visualization_preference",
                    "objects": [],
                    "lanes": {"accepted_current": []},
                    "recommended_actions": [],
                    "gaps": ["accepted_html_preference_missing"],
                },
            },
            "agent_context_preference_section": {
                "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
                "section": "style_preference",
                "object_count": 0,
                "accepted_preference_count": 0,
                "authority_lanes": [],
                "surface_policy": {"mutation_allowed": False},
            },
            "artifact_review_check": {
                "schema_version": ARTIFACT_REVIEW_PREFERENCE_CHECK_SCHEMA,
                "status": "failed",
                "ui_required": False,
                "raw_artifact_body_returned": False,
            },
            "postcheck": {
                "status": "failed",
            },
        }
    evidence = _public_safe_mapping(raw)
    ensure_public_safe(evidence, "CollectedPreferenceArtifactMemoryShadowEvidence")
    return evidence


def _collect_temporal_correctness_runtime_shadow(
    runner: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Collect only the public-safe aggregate produced by the deployed read path."""

    if runner is None:
        return {}
    try:
        raw = runner()
    except Exception:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    safe = _public_safe_mapping(raw)
    if safe.get("schema_version") != TEMPORAL_CORRECTNESS_RUNTIME_AGGREGATE_SCHEMA:
        return {}
    if safe.get("production_mutation_performed") is not False:
        return {}
    ensure_public_safe(safe, "TemporalCorrectnessRuntimeReadPathEvidence")
    return safe


def build_permission_sensitive_audit_shadow_evidence() -> dict[str, Any]:
    """Build a branch-local local_test P8 denial/audit summary without mutation."""

    event_base = {
        "schema_version": PERMISSION_AUDIT_EVENT_SCHEMA,
        "event_type": "permission_sensitive_runtime_action",
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    events = [
        {
            **event_base,
            "action": tool_name,
            "actor_ref_hash": "sha256:" + "a" * 64,
            "request_hash": "sha256:" + str(index) * 64,
        }
        for index, tool_name in enumerate(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS, start=1)
    ]
    evidence = {
        "schema_version": PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA,
        "audit_events": events,
        "audit_store": {
            "status": "recorded",
            "event_count": len(events),
            "production_mutation_performed": False,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    ensure_public_safe(evidence, "PermissionSensitiveAuditShadowEvidence")
    return evidence


def _collect_permission_sensitive_audit_shadow(
    permission_sensitive_audit_runner: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    try:
        raw = (
            permission_sensitive_audit_runner()
            if permission_sensitive_audit_runner is not None
            else build_permission_sensitive_audit_shadow_evidence()
        )
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA,
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "audit_events": [],
            "audit_store": {
                "status": "failed",
                "event_count": 0,
                "production_mutation_performed": False,
            },
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
            "production_mutation_performed": False,
        }
    evidence = _public_safe_mapping(raw)
    ensure_public_safe(evidence, "CollectedPermissionSensitiveAuditShadowEvidence")
    return evidence


def build_agent_context_startup_shadow_evidence(
    *,
    consumer: str = "codex",
) -> dict[str, Any]:
    """Build a branch-local local_test P9 startup/read-path summary without mutation."""

    safe_consumer = public_safe_text(str(consumer or "codex"), max_chars=80)
    if safe_consumer not in ALLOWED_AGENT_CONTEXT_CONSUMERS:
        safe_consumer = "codex"
    evidence = {
        "schema_version": AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
        "startup_context": {
            "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
            "consumer": safe_consumer,
            "loaded_on_startup": True,
            "section_counts": {
                "current_authority": 1,
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
        "production_mutation_performed": False,
    }
    ensure_public_safe(evidence, "AgentContextStartupShadowEvidence")
    return evidence


def _collect_agent_context_startup_shadow(
    agent_context_startup_runner: Callable[[], Mapping[str, Any]] | None,
    *,
    consumer: str = "codex",
) -> dict[str, Any]:
    try:
        raw = (
            agent_context_startup_runner()
            if agent_context_startup_runner is not None
            else build_agent_context_startup_shadow_evidence(consumer=consumer or "codex")
        )
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "startup_context": {
                "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
                "consumer": public_safe_text(str(consumer or "codex"), max_chars=80),
                "loaded_on_startup": False,
                "section_counts": {},
                "surface_policy": {"mutation_allowed": False},
                "degraded_gap_disclosure_present": True,
                "missing_evidence_before_promotion_present": True,
            },
            "read_path_smoke": {
                "tool": "brain_objects_query",
                "read_only": True,
                "routes_checked": [],
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
            "production_mutation_performed": False,
        }
    evidence = _public_safe_mapping(raw)
    ensure_public_safe(evidence, "CollectedAgentContextStartupShadowEvidence")
    return evidence


def _collect_brain_objects_query_route_smoke(
    route_runner: Callable[[str], Mapping[str, Any]],
    route: str,
) -> dict[str, Any]:
    try:
        raw = route_runner(route)
        _reject_forbidden_runtime_evidence_keys(raw)
    except Exception as exc:  # pragma: no cover - defensive public-safe guard
        raw = {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [],
                "edges": [],
                "evidence": [],
                "gaps": ["collector_route_smoke_failed"],
            },
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
        }
    smoke = _public_safe_mapping(raw)
    smoke["schema_version"] = public_safe_text(
        str(smoke.get("schema_version") or "brain_objects_query.v1"),
        max_chars=80,
    )
    smoke["route"] = public_safe_text(str(smoke.get("route") or route), max_chars=120)
    smoke["production_mutation_performed"] = False
    object_pack = smoke.get("object_pack") if isinstance(smoke.get("object_pack"), Mapping) else {}
    if not object_pack:
        object_pack = {
            "schema_version": "object_pack.v1",
            "route": route,
            "objects": [],
            "edges": [],
            "evidence": [],
            "gaps": ["collector_route_smoke_missing_object_pack"],
        }
    else:
        object_pack = _public_safe_mapping(object_pack)
        object_pack["schema_version"] = public_safe_text(
            str(object_pack.get("schema_version") or "object_pack.v1"),
            max_chars=80,
        )
        object_pack["route"] = public_safe_text(str(object_pack.get("route") or route), max_chars=120)
    smoke["object_pack"] = object_pack
    ensure_public_safe(smoke, "CollectedBrainObjectsQueryRouteSmoke")
    return smoke


def _smoke_gaps(smoke: Mapping[str, Any]) -> list[str]:
    object_pack = smoke.get("object_pack") if isinstance(smoke.get("object_pack"), Mapping) else {}
    return _string_list(object_pack.get("gaps")) if isinstance(object_pack, Mapping) else []


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
        "temporal_recall_corrective_checkpoint": {
            "schema_version": TEMPORAL_RECALL_CORRECTIVE_CHECKPOINT_SCHEMA,
            "evidence_class": "runtime_semantic_acceptance",
            "required_probes": [
                "date_a",
                "date_b",
                "range_boundary",
                "mismatch",
                "nonsense_query",
                "semantic_query",
            ],
            "semantic_result_minimum_score": TEMPORAL_SEMANTIC_RESULT_MIN_SCORE,
            "runtime_aggregate_schema": TEMPORAL_CORRECTNESS_RUNTIME_AGGREGATE_SCHEMA,
            "production_mutation_performed": False,
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        },
        "projection_join": {
            "schema_version": PROJECTION_JOIN_RUNTIME_SCHEMA,
            "evidence_class": "runtime_projection_join",
            "status": "pass",
            "edge_count": "collector_sets_integer",
            "production_mutation_performed": False,
            "postcheck": {
                "status": "validated",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
        },
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
                "visible_session_count": "collector_sets_integer",
                "all_device_session_count": "collector_sets_integer",
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
                "required_authority_lane": REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE,
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
        "agent_context_startup_runtime": {
            "schema_version": AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
            "startup_context": {
                "schema_version": REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
                "consumer": "collector_sets_allowed_consumer",
                "loaded_on_startup": True,
                "required_sections": list(REQUIRED_AGENT_CONTEXT_STARTUP_SECTIONS),
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
        },
        "gitops_desired_state": {
            "schema_version": GITOPS_DESIRED_STATE_SCHEMA,
            "images_include_expected_commit": "collector_sets_boolean",
            "desired_state_source": "sanitized_ops_manifest_summary",
            "target_revision": "collector_sets_public_ref",
            "source_commit": "collector_sets_expected_commit",
            "desired_image_set_hash": "sha256:<64-hex>",
            "ops_revision": "collector_sets_public_ref",
            "expected_image_ref_count": "collector_sets_positive_integer",
            "production_mutation_performed": False,
        },
        "argo_reconciliation": {
            "schema_version": ARGO_RECONCILIATION_SCHEMA,
            "reconciliation_source": "sanitized_argo_application_summary",
            "reconciled_ops_revision": "collector_sets_public_ref",
            "sync_status": "Synced",
            "health_status": "Healthy",
            "production_mutation_performed": False,
        },
        "deployment_evidence_binding": {
            "schema_version": DEPLOYMENT_EVIDENCE_BINDING_SCHEMA,
            "canonical_tuple_hash": "sha256:<64-hex>",
        },
        "deployed_identity": {
            "contains_expected_commit": "collector_sets_boolean",
            "identity_source": "redacted_live_runtime_evidence",
            "source_commit": "collector_sets_expected_commit",
            "live_image_set_hash": "sha256:<64-hex>",
            "stale_image_ref_count": 0,
            "production_mutation_performed": False,
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
            "step_id": "probe_temporal_recall_corrective_checkpoint",
            "evidence_field": "temporal_recall_corrective_checkpoint",
            "required_values": [
                TEMPORAL_RECALL_CORRECTIVE_CHECKPOINT_SCHEMA,
                "date_a_fingerprint_matches",
                "date_b_fingerprint_matches",
                "date_ab_distinct",
                "mismatch_empty_with_gap_and_zero_confidence",
                "nonsense_query_empty",
                "semantic_ranker_bound_and_used",
                "qdrant_semantic_result_lane_used",
                "projection_hash_current",
                "entity_aggregate_improved",
            ],
            "safe_target": "sanitized_temporal_semantic_acceptance",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "probe_projection_join_runtime",
            "evidence_field": "projection_join",
            "required_values": [
                PROJECTION_JOIN_RUNTIME_SCHEMA,
                "runtime_projection_join",
                "edge_count>0",
                "redacted_postcheck",
            ],
            "safe_target": "sanitized_graph_qdrant_projection_join_read_path",
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
            "step_id": "probe_agent_context_startup_runtime",
            "evidence_field": "agent_context_startup_runtime",
            "required_values": [
                AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
                REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA,
                "brain_objects_query",
                "read_only=true",
                "mutation_allowed=false",
                "raw_private_context_blocked=true",
            ],
            "safe_target": "sanitized_agent_context_startup_runtime_smoke",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_gitops_desired_state",
            "evidence_field": "gitops_desired_state",
            "required_values": [GITOPS_DESIRED_STATE_SCHEMA, "images_include_expected_commit"],
            "safe_target": "sanitized_ops_gitops_desired_state_summary",
            "mutation_allowed": False,
            "production_mutation_performed": False,
        },
        {
            "step_id": "collect_argo_reconciliation",
            "evidence_field": "argo_reconciliation",
            "required_values": [ARGO_RECONCILIATION_SCHEMA, "Synced", "Healthy"],
            "safe_target": "sanitized_argo_application_summary",
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
        _live_temporal_recall_corrective_checkpoint_claim(evidence),
        _live_brain_objects_query_route_smokes_claim(evidence),
        _live_source_to_candidate_projection_join_claim(evidence),
        _live_source_to_candidate_review_loop_claim(evidence),
        _live_session_project_rollup_claim(evidence),
        _live_preference_artifact_memory_claim(evidence),
        _live_permission_sensitive_audit_claim(evidence),
        _live_agent_context_startup_claim(evidence),
        _gitops_desired_state_claim(evidence, expected_commit=expected_commit),
        _argo_reconciliation_claim(evidence),
        _live_deployed_identity_claim(evidence, expected_commit=expected_commit),
        _deployment_evidence_binding_claim(evidence, expected_commit=expected_commit),
        _live_object_authority_production_gate_policy_claim(evidence),
        _live_object_authority_bounded_execution_claim(evidence),
        _live_object_authority_replacement_current_claim(evidence),
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
    status = "FAIL" if failed else ("PASS_WITH_GAPS" if gaps else "PASS")
    evidence_is_live = provenance_claim.get("is_live") is True
    production_ready = status == "PASS" and evidence_is_live
    report = {
        "schema_version": "source_to_candidate_runtime_readiness.v1",
        "status": status,
        "claims": claims,
        "failed_claims": failed,
        "gaps": gaps,
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "live_evidence_provided": bool(evidence),
        "evidence_is_live": evidence_is_live,
        "production_ready": production_ready,
        "production_readiness": (
            "ready"
            if production_ready
            else ("not_ready_local_or_sanitized_evidence_only" if status == "PASS" else "not_ready")
        ),
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
    execution_reports_mutation = _evidence_execution_reports_mutation(evidence)
    failures = _evidence_provenance_failures(
        provenance=provenance,
        collection_mode=collection_mode,
        mutation_scope=mutation_scope,
        execution_reports_mutation=execution_reports_mutation,
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
    network_used_for_evidence = provenance.get("network_used") is True
    live_mode = collection_mode in LIVE_EVIDENCE_COLLECTION_MODES
    live_mode_gaps = (
        ["live_evidence_provenance_network_not_used_for_live_mode"]
        if live_mode and not network_used_for_evidence
        else []
    )
    gaps = _dedupe([*failures, *live_mode_gaps])
    return {
        "claim_id": "live.evidence.provenance",
        "evidence_class": "runtime_evidence_provenance",
        "status": "failed" if failures else ("not_validated" if live_mode_gaps else "validated"),
        "schema_version": public_safe_text(str(provenance.get("schema_version") or ""), max_chars=80),
        "collection_mode": collection_mode,
        "source": collection_mode,
        "is_live": live_mode and network_used_for_evidence,
        "network_used_for_evidence": network_used_for_evidence,
        "mutation_scope": mutation_scope,
        "production_mutation_performed": execution_reports_mutation,
        "redaction_check": redaction_check,
        "gaps": gaps,
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
    if collection_mode == "post_deploy_read_only_smoke" and mutation_scope != "none":
        failures.append("live_evidence_provenance_read_only_mode_mutation_scope_mismatch")
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
    replacement = evidence.get("production_authority_replacement_current")
    replacement = replacement if isinstance(replacement, Mapping) else {}
    return (
        evidence.get("production_mutation_performed") is True
        or evidence.get("mutation_performed") is True
        or _bounded_execution_reports_mutation(proposal, decision)
        or _replacement_current_reports_mutation(replacement)
    )


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
    current_authority = sections.get(REQUIRED_AGENT_CONTEXT_AUTHORITY_SECTION)
    current_authority_object_count = _section_object_count(current_authority)
    current_authority_authority_lanes = _section_authority_lanes(current_authority)
    current_authority_gaps: list[str] = []
    if current_authority_object_count < 1:
        current_authority_gaps.append("live_agent_context_current_authority_missing")
    elif set(current_authority_authority_lanes) != {REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE}:
        current_authority_gaps.append(
            "live_agent_context_current_authority_accepted_current_missing"
        )
    style_preference = sections.get(REQUIRED_AGENT_CONTEXT_STYLE_PREFERENCE_SECTION)
    style_preference_object_count = _section_object_count(style_preference)
    style_preference_authority_lanes = _section_authority_lanes(style_preference)
    style_preference_gaps: list[str] = []
    if (
        style_preference_object_count >= 1
        and set(style_preference_authority_lanes) != {REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE}
    ):
        style_preference_gaps.append(
            "live_agent_context_style_preference_accepted_current_missing"
        )
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
        "required_authority_section": REQUIRED_AGENT_CONTEXT_AUTHORITY_SECTION,
        "required_authority_lane": REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE,
        "current_authority_object_count": current_authority_object_count,
        "current_authority_authority_lanes": current_authority_authority_lanes,
        "style_preference_object_count": style_preference_object_count,
        "style_preference_authority_lanes": style_preference_authority_lanes,
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
        "status": "not_validated"
        if missing or current_authority_gaps or style_preference_gaps
        else "validated",
        "gaps": _dedupe(
            [
                *(
                    [
                        "live_agent_context_product_sections_unverified",
                        *_named_gaps("live_agent_context_section_missing", missing),
                    ]
                    if missing
                    else []
                ),
                *current_authority_gaps,
                *style_preference_gaps,
            ]
        ),
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


def _live_temporal_recall_corrective_checkpoint_claim(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = evidence.get("temporal_recall_corrective_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    base = {
        "claim_id": "live.temporal_recall.corrective_checkpoint",
        "evidence_class": "runtime_semantic_acceptance",
        "schema_version": public_safe_text(
            str(checkpoint.get("schema_version") or ""),
            max_chars=80,
        ),
        "production_mutation_performed": checkpoint.get("production_mutation_performed") is True,
    }
    if not checkpoint:
        return {
            **base,
            "status": "not_validated",
            "date_ab_distinct": False,
            "hash_currentness_validated": False,
            "entity_aggregate_improved": False,
            "gaps": ["live_temporal_recall_corrective_checkpoint_unverified"],
        }

    selector = (
        checkpoint.get("selector_contract")
        if isinstance(checkpoint.get("selector_contract"), Mapping)
        else {}
    )
    temporal_query_hash = str(checkpoint.get("temporal_query_hash") or "")
    date_a = checkpoint.get("date_a") if isinstance(checkpoint.get("date_a"), Mapping) else {}
    date_b = checkpoint.get("date_b") if isinstance(checkpoint.get("date_b"), Mapping) else {}
    boundary = (
        checkpoint.get("range_boundary")
        if isinstance(checkpoint.get("range_boundary"), Mapping)
        else {}
    )
    mismatch = (
        checkpoint.get("mismatch")
        if isinstance(checkpoint.get("mismatch"), Mapping)
        else {}
    )
    nonsense = (
        checkpoint.get("nonsense_query")
        if isinstance(checkpoint.get("nonsense_query"), Mapping)
        else {}
    )
    semantic = (
        checkpoint.get("semantic_query")
        if isinstance(checkpoint.get("semantic_query"), Mapping)
        else {}
    )
    runtime = (
        checkpoint.get("runtime_aggregate")
        if isinstance(checkpoint.get("runtime_aggregate"), Mapping)
        else {}
    )
    runtime_aggregate_source = str(checkpoint.get("runtime_aggregate_source") or "")
    runtime_postcheck_receipt_hash = str(
        checkpoint.get("runtime_postcheck_receipt_hash") or ""
    )
    currentness = (
        runtime.get("projection_currentness")
        if isinstance(runtime.get("projection_currentness"), Mapping)
        else {}
    )
    entity = (
        runtime.get("entity_projection")
        if isinstance(runtime.get("entity_projection"), Mapping)
        else {}
    )
    postcheck = (
        checkpoint.get("postcheck")
        if isinstance(checkpoint.get("postcheck"), Mapping)
        else {}
    )

    expected_a = str(date_a.get("expected_object_fingerprint") or "")
    observed_a = str(date_a.get("observed_object_fingerprint") or "")
    expected_b = str(date_b.get("expected_object_fingerprint") or "")
    observed_b = str(date_b.get("observed_object_fingerprint") or "")
    expected_identity_a = str(
        date_a.get("expected_object_identity_fingerprint") or ""
    )
    observed_identity_a = str(
        date_a.get("observed_object_identity_fingerprint") or ""
    )
    expected_identity_b = str(
        date_b.get("expected_object_identity_fingerprint") or ""
    )
    observed_identity_b = str(
        date_b.get("observed_object_identity_fingerprint") or ""
    )
    selector_a = str(date_a.get("selector_hash") or "")
    selector_b = str(date_b.get("selector_hash") or "")
    date_ab_selectors_distinct = bool(
        _is_sha256_hash_ref(selector_a)
        and _is_sha256_hash_ref(selector_b)
        and selector_a != selector_b
    )
    date_ab_object_fingerprints_distinct = bool(
        expected_a
        and expected_b
        and observed_a
        and observed_b
        and expected_a != expected_b
        and observed_a != observed_b
    )
    date_ab_identity_distinct = bool(
        _is_sha256_hash_ref(expected_identity_a)
        and _is_sha256_hash_ref(observed_identity_a)
        and _is_sha256_hash_ref(expected_identity_b)
        and _is_sha256_hash_ref(observed_identity_b)
        and expected_identity_a == observed_identity_a
        and expected_identity_b == observed_identity_b
        and expected_identity_a != expected_identity_b
        and observed_identity_a != observed_identity_b
    )
    date_ab_distinct = bool(
        date_ab_object_fingerprints_distinct and date_ab_identity_distinct
    )
    source_session_count = _strict_int_or_none(
        currentness.get("source_session_count")
    )
    minimum_source_session_count = _strict_int_or_none(
        currentness.get("minimum_source_session_count")
    )
    session_memory_current_count = _strict_int_or_none(
        currentness.get("session_memory_projection_current_count")
    )
    graph_current_count = _strict_int_or_none(
        currentness.get("graph_projection_current_count")
    )
    state_digest_validated = all(
        _is_sha256_hash_ref(str(currentness.get(field) or ""))
        for field in (
            "source_state_digest",
            "graph_projection_state_digest",
            "session_memory_projection_state_digest",
            "source_projection_state_digest",
        )
    )
    hash_currentness_validated = (
        currentness.get("source_hash_match") is True
        and _strict_int_or_none(currentness.get("source_hash_mismatch_count")) == 0
        and _strict_int_or_none(currentness.get("stale_projected_session_count")) == 0
        and source_session_count is not None
        and minimum_source_session_count is not None
        and source_session_count >= minimum_source_session_count > 0
        and graph_current_count == source_session_count
        and _strict_int_or_none(currentness.get("graph_projection_noncurrent_count"))
        == 0
        and session_memory_current_count == source_session_count
        and _strict_int_or_none(
            currentness.get("session_memory_projection_noncurrent_count")
        )
        == 0
        and _strict_int_or_none(
            currentness.get("session_memory_source_hash_mismatch_count")
        )
        == 0
        and _strict_int_or_none(
            currentness.get("session_memory_stale_projected_session_count")
        )
        == 0
        and _strict_int_or_none(
            currentness.get("artifact_missing_session_count")
        )
        == 0
        and _strict_int_or_none(currentness.get("artifact_age_unknown_count"))
        == 0
        and _strict_int_or_none(
            currentness.get("artifact_source_hash_mismatch_count")
        )
        == 0
        and currentness.get("artifact_current") is True
        and currentness.get("graph_run_scope_match") is True
        and currentness.get("graph_run_fresh") is True
        and _strict_int_or_none(
            currentness.get("graph_run_completed_age_seconds")
        )
        is not None
        and _strict_int_or_none(currentness.get("graph_run_max_age_seconds"))
        is not None
        and int(currentness.get("graph_run_completed_age_seconds"))
        <= int(currentness.get("graph_run_max_age_seconds"))
    )
    baseline_coverage = _strict_int_or_none(entity.get("baseline_coverage_count"))
    coverage = _strict_int_or_none(entity.get("coverage_count"))
    baseline_backlog = _strict_int_or_none(entity.get("baseline_backlog_count"))
    backlog = _strict_int_or_none(entity.get("backlog_count"))
    entity_error_count = _strict_int_or_none(entity.get("error_count"))
    valid_source_count = _strict_int_or_none(entity.get("valid_source_count"))
    minimum_valid_source_count = _strict_int_or_none(
        entity.get("minimum_valid_source_count")
    )
    entity_counts_valid = all(
        value is not None and value >= 0
        for value in (
            baseline_coverage,
            coverage,
            baseline_backlog,
            backlog,
            entity_error_count,
            valid_source_count,
            minimum_valid_source_count,
        )
    )
    entity_aggregate_improved = bool(
        entity_counts_valid
        and entity_error_count == 0
        and minimum_valid_source_count > 0
        and valid_source_count >= minimum_valid_source_count
        and coverage + backlog == valid_source_count
        and baseline_coverage + baseline_backlog == minimum_valid_source_count
        and (coverage > baseline_coverage or backlog < baseline_backlog)
    )

    failures: list[str] = []
    if checkpoint.get("schema_version") != TEMPORAL_RECALL_CORRECTIVE_CHECKPOINT_SCHEMA:
        failures.append("temporal_corrective_checkpoint_schema_mismatch")
    if checkpoint.get("evidence_class") != "runtime_semantic_acceptance":
        failures.append("temporal_corrective_evidence_class_mismatch")
    if not _is_sha256_hash_ref(temporal_query_hash):
        failures.append("temporal_corrective_query_hash_invalid")
    if not all(
        selector.get(field) is True
        for field in ("as_of_supported", "date_range_supported", "invalid_range_rejected")
    ):
        failures.append("temporal_corrective_selector_contract_incomplete")
    if (
        isinstance(selector.get("invalid_range_error_code"), bool)
        or selector.get("invalid_range_error_code") != -32602
    ):
        failures.append("temporal_corrective_invalid_range_error_code_unexpected")
    for label, probe in (("date_a", date_a), ("date_b", date_b)):
        if not _is_sha256_hash_ref(str(probe.get("selector_hash") or "")):
            failures.append(f"temporal_corrective_{label}_selector_hash_invalid")
        if not _is_sha256_hash_ref(str(probe.get("expected_object_fingerprint") or "")):
            failures.append(f"temporal_corrective_{label}_expected_fingerprint_invalid")
        if not _is_sha256_hash_ref(str(probe.get("observed_object_fingerprint") or "")):
            failures.append(f"temporal_corrective_{label}_observed_fingerprint_invalid")
        if probe.get("expected_object_fingerprint") != probe.get("observed_object_fingerprint"):
            failures.append(f"temporal_corrective_{label}_fingerprint_mismatch")
        if not _is_sha256_hash_ref(
            str(probe.get("expected_object_identity_fingerprint") or "")
        ):
            failures.append(
                f"temporal_corrective_{label}_expected_identity_fingerprint_invalid"
            )
        if not _is_sha256_hash_ref(
            str(probe.get("observed_object_identity_fingerprint") or "")
        ):
            failures.append(
                f"temporal_corrective_{label}_observed_identity_fingerprint_invalid"
            )
        if probe.get("expected_object_identity_fingerprint") != probe.get(
            "observed_object_identity_fingerprint"
        ):
            failures.append(
                f"temporal_corrective_{label}_identity_fingerprint_mismatch"
            )
        if _strict_int_or_none(probe.get("work_unit_count")) != 1:
            failures.append(f"temporal_corrective_{label}_work_unit_count_invalid")
        confidence_score = probe.get("confidence_score")
        if (
            _strict_int_or_none(probe.get("gap_count")) != 0
            or isinstance(confidence_score, bool)
            or not isinstance(confidence_score, (int, float))
            or not math.isfinite(float(confidence_score))
            or not 0.0 < float(confidence_score) <= 1.0
        ):
            failures.append(f"temporal_corrective_{label}_not_fail_closed")
    if not date_ab_distinct:
        failures.append("temporal_corrective_date_fingerprints_not_distinct")
    if not date_ab_identity_distinct:
        failures.append("temporal_corrective_date_identities_not_distinct")
    if not date_ab_selectors_distinct:
        failures.append("temporal_corrective_date_selectors_not_distinct")
    if (
        not _is_sha256_hash_ref(str(boundary.get("selector_hash") or ""))
        or not _is_sha256_hash_ref(
            str(boundary.get("expected_object_fingerprint") or "")
        )
        or not _is_sha256_hash_ref(
            str(boundary.get("observed_object_fingerprint") or "")
        )
        or boundary.get("expected_object_fingerprint")
        != boundary.get("observed_object_fingerprint")
        or not _is_sha256_hash_ref(
            str(boundary.get("expected_object_identity_fingerprint") or "")
        )
        or not _is_sha256_hash_ref(
            str(boundary.get("observed_object_identity_fingerprint") or "")
        )
        or boundary.get("expected_object_identity_fingerprint")
        != boundary.get("observed_object_identity_fingerprint")
        or _strict_int_or_none(boundary.get("work_unit_count")) != 1
        or _strict_int_or_none(boundary.get("gap_count")) != 0
        or isinstance(boundary.get("confidence_score"), bool)
        or not isinstance(boundary.get("confidence_score"), (int, float))
        or not math.isfinite(float(boundary.get("confidence_score")))
        or not 0.0 < float(boundary.get("confidence_score")) <= 1.0
    ):
        failures.append("temporal_corrective_range_boundary_failed")
    confidence_score = mismatch.get("confidence_score")
    if (
        not _is_sha256_hash_ref(str(mismatch.get("selector_hash") or ""))
        or _strict_int_or_none(mismatch.get("object_count")) != 0
        or (_strict_int_or_none(mismatch.get("gap_count")) or 0) <= 0
        or isinstance(confidence_score, bool)
        or not isinstance(confidence_score, (int, float))
        or float(confidence_score) != 0.0
    ):
        failures.append("temporal_corrective_mismatch_not_fail_closed")
    if (
        not _is_sha256_hash_ref(str(nonsense.get("query_hash") or ""))
        or any(
            _strict_int_or_none(nonsense.get(field)) != 0
            for field in ("result_count", "current_count", "accepted_count")
        )
    ):
        failures.append("temporal_corrective_nonsense_query_not_empty")
    if (
        nonsense.get("semantic_ranker_bound") is not True
        or nonsense.get("semantic_ranker_used") is not True
    ):
        failures.append("temporal_corrective_semantic_ranker_not_used")
    if not semantic:
        failures.append("temporal_corrective_semantic_query_missing")
    if not _is_sha256_hash_ref(str(semantic.get("query_hash") or "")):
        failures.append("temporal_corrective_semantic_query_hash_invalid")
    expected_semantic_fingerprint = str(
        semantic.get("expected_result_fingerprint") or ""
    )
    observed_semantic_fingerprint = str(
        semantic.get("observed_result_fingerprint") or ""
    )
    if (
        not _is_sha256_hash_ref(expected_semantic_fingerprint)
        or not _is_sha256_hash_ref(observed_semantic_fingerprint)
        or expected_semantic_fingerprint != observed_semantic_fingerprint
    ):
        failures.append("temporal_corrective_semantic_result_fingerprint_mismatch")
    if _strict_int_or_none(semantic.get("result_count")) != 1:
        failures.append("temporal_corrective_semantic_result_count_invalid")
    if semantic.get("why_retrieved_semantic_match") is not True:
        failures.append("temporal_corrective_semantic_result_reason_invalid")
    semantic_score = semantic.get("score")
    semantic_minimum_score = semantic.get("minimum_score")
    if (
        isinstance(semantic_score, bool)
        or not isinstance(semantic_score, (int, float))
        or not math.isfinite(float(semantic_score))
        or float(semantic_score) < TEMPORAL_SEMANTIC_RESULT_MIN_SCORE
        or isinstance(semantic_minimum_score, bool)
        or not isinstance(semantic_minimum_score, (int, float))
        or float(semantic_minimum_score) != TEMPORAL_SEMANTIC_RESULT_MIN_SCORE
    ):
        failures.append("temporal_corrective_semantic_result_score_below_threshold")
    if (
        semantic.get("semantic_ranker_bound") is not True
        or semantic.get("semantic_ranker_used") is not True
    ):
        failures.append("temporal_corrective_semantic_query_ranker_not_used")
    if semantic.get("qdrant_semantic_result_lane_used") is not True:
        failures.append("temporal_corrective_qdrant_semantic_result_lane_not_used")
    if runtime.get("schema_version") != TEMPORAL_CORRECTNESS_RUNTIME_AGGREGATE_SCHEMA:
        failures.append("temporal_corrective_runtime_aggregate_schema_mismatch")
    if runtime_aggregate_source != "live_mcp_runtime_packet":
        failures.append("temporal_corrective_runtime_aggregate_source_untrusted")
    if runtime_postcheck_receipt_hash:
        failures.append("temporal_corrective_runtime_postcheck_receipt_not_allowed")
    if not hash_currentness_validated:
        failures.append("temporal_corrective_projection_hash_not_current")
    if not state_digest_validated:
        failures.append("temporal_corrective_projection_state_digest_invalid")
    if not entity_aggregate_improved:
        failures.append("temporal_corrective_entity_aggregate_not_improved")
    if checkpoint.get("production_mutation_performed") is True:
        failures.append("temporal_corrective_unexpected_production_mutation")
    if postcheck.get("status") != "validated" or any(
        postcheck.get(field) is not False
        for field in (
            "raw_private_evidence_returned",
            "secret_returned",
            "host_topology_returned",
            "raw_external_ids_returned",
        )
    ):
        failures.append("temporal_corrective_postcheck_failed")
    return {
        **base,
        "status": "failed" if failures else "validated",
        "date_ab_selectors_distinct": date_ab_selectors_distinct,
        "date_ab_distinct": date_ab_distinct,
        "date_ab_identity_distinct": date_ab_identity_distinct,
        "hash_currentness_validated": hash_currentness_validated,
        "state_digest_validated": state_digest_validated,
        "entity_aggregate_improved": entity_aggregate_improved,
        "semantic_ranker_bound": nonsense.get("semantic_ranker_bound") is True,
        "semantic_ranker_used": nonsense.get("semantic_ranker_used") is True,
        "semantic_query_ranker_bound": semantic.get("semantic_ranker_bound") is True,
        "semantic_query_ranker_used": semantic.get("semantic_ranker_used") is True,
        "qdrant_semantic_result_lane_used": (
            semantic.get("qdrant_semantic_result_lane_used") is True
        ),
        "semantic_result_count": _strict_int_or_none(semantic.get("result_count")),
        "semantic_result_score": (
            float(semantic_score)
            if isinstance(semantic_score, (int, float))
            and not isinstance(semantic_score, bool)
            and math.isfinite(float(semantic_score))
            else None
        ),
        "semantic_result_minimum_score": TEMPORAL_SEMANTIC_RESULT_MIN_SCORE,
        "nonsense_result_count": _strict_int_or_none(nonsense.get("result_count")),
        "entity_coverage_count": coverage,
        "entity_backlog_count": backlog,
        "gaps": _dedupe(failures),
    }


def build_temporal_recall_corrective_checkpoint_readiness_report(
    *,
    checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate only the temporal corrective checkpoint, without deployment binding."""

    safe_checkpoint = _public_safe_mapping(checkpoint)
    claim = _live_temporal_recall_corrective_checkpoint_claim(
        {"temporal_recall_corrective_checkpoint": safe_checkpoint}
    )
    validated = claim.get("status") == "validated"
    report = {
        "schema_version": TEMPORAL_RECALL_CORRECTIVE_CHECKPOINT_READINESS_SCHEMA,
        "status": "PASS" if validated else "FAIL",
        "production_mutation_performed": (
            claim.get("production_mutation_performed") is True
        ),
        "failed_claims": [] if validated else [str(claim["claim_id"])],
        "gaps": _dedupe(claim.get("gaps", [])),
        "claim": claim,
    }
    ensure_public_safe(report, "TemporalRecallCorrectiveCheckpointReadiness")
    return report


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
    temporal_checkpoint = _live_temporal_recall_corrective_checkpoint_claim(evidence)
    temporal_semantics_validated = temporal_checkpoint.get("status") == "validated"
    identity = evidence.get("deployed_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    deployed_identity_matches_expected = identity.get("contains_expected_commit") is True
    route_fallback_interpretation = (
        "fail_expected_deployed_identity"
        if unimplemented_routes and deployed_identity_matches_expected
        else (
            "gap_until_deployed_identity_matches_expected_commit"
            if unimplemented_routes
            else "not_applicable"
        )
    )
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
    syntactic_routes = sorted(
        route for route in by_route if route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )
    validated_routes = [
        route
        for route in syntactic_routes
        if route != "temporal_work_recall" or temporal_semantics_validated
    ]
    base = {
        "claim_id": "live.brain_objects_query.route_smokes",
        "evidence_class": "runtime_read_path",
        "required_routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "syntactic_routes": syntactic_routes,
        "validated_routes": validated_routes,
        "missing_routes": missing,
        "unimplemented_routes": unimplemented_routes,
        "route_fallback_interpretation": route_fallback_interpretation,
        "temporal_semantic_acceptance_status": temporal_checkpoint.get("status"),
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
    if not temporal_semantics_validated:
        return {
            **base,
            "status": "not_validated",
            "gaps": _dedupe(
                [
                    *missing_gaps,
                    *(
                        temporal_checkpoint.get("gaps")
                        if isinstance(temporal_checkpoint.get("gaps"), list)
                        else []
                    ),
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


def _live_source_to_candidate_projection_join_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    projection = evidence.get("projection_join")
    projection = projection if isinstance(projection, Mapping) else {}
    if not projection:
        return {
            "claim_id": "live.source_to_candidate.projection_join",
            "evidence_class": "runtime_read_path",
            "status": "not_validated",
            "schema_version": "",
            "edge_count": 0,
            "production_mutation_performed": False,
            "gaps": ["live_graph_qdrant_projection_join_unproven"],
        }
    postcheck = projection.get("postcheck") if isinstance(projection.get("postcheck"), Mapping) else {}
    failures = _projection_join_failures(projection=projection, postcheck=postcheck)
    mutation_performed = _projection_join_reports_mutation(projection)
    return {
        "claim_id": "live.source_to_candidate.projection_join",
        "evidence_class": "runtime_read_path",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(projection.get("schema_version") or ""), max_chars=80),
        "evidence_class_observed": public_safe_text(str(projection.get("evidence_class") or ""), max_chars=80),
        "runtime_status": public_safe_text(str(projection.get("status") or ""), max_chars=80),
        "edge_count": _int_value(projection.get("edge_count")),
        "postcheck_status": public_safe_text(str(postcheck.get("status") or ""), max_chars=80),
        "production_mutation_performed": mutation_performed,
        "gaps": failures,
    }


def _projection_join_failures(
    *,
    projection: Mapping[str, Any],
    postcheck: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    collector_error_type = public_safe_text(str(projection.get("collector_error_type") or ""), max_chars=80)
    if collector_error_type:
        failures.append(f"projection_join_collector_error:{collector_error_type}")
    if projection.get("schema_version") != PROJECTION_JOIN_RUNTIME_SCHEMA:
        failures.append("projection_join_schema_mismatch")
    if projection.get("evidence_class") != "runtime_projection_join":
        failures.append("projection_join_evidence_class_mismatch")
    if projection.get("status") != "pass":
        failures.append("projection_join_status_not_pass")
    if _int_value(projection.get("edge_count")) < 1:
        failures.append("projection_join_edge_count_missing")
    if _projection_join_reports_mutation(projection):
        failures.append("projection_join_production_mutation_performed")
    if postcheck.get("status") != "validated":
        failures.append("projection_join_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "projection_join_raw_private_evidence_returned"),
        ("secret_returned", "projection_join_secret_returned"),
        ("host_topology_returned", "projection_join_host_topology_returned"),
        ("raw_external_ids_returned", "projection_join_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _projection_join_reports_mutation(projection: Mapping[str, Any]) -> bool:
    return (
        projection.get("production_mutation_performed") is True
        or projection.get("mutation_performed") is True
    )


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
    rollup_present = _runtime_evidence_field_present(
        evidence,
        "session_project_rollup_runtime",
        "session_project_rollup_runtime_present",
    )
    rollup = evidence.get("session_project_rollup_runtime")
    rollup = rollup if isinstance(rollup, Mapping) else {}
    if not rollup:
        if rollup_present:
            return {
                "claim_id": "live.session_project.rollup",
                "evidence_class": "runtime_read_path",
                "status": "failed",
                "schema_version": "",
                "device_count": 0,
                "visible_session_count": 0,
                "all_device_session_count": 0,
                "read_after_write_status": "",
                "production_mutation_performed": False,
                "gaps": [
                    "session_project_rollup_runtime_empty_or_invalid",
                    "live_multi_device_rollup_unproven",
                ],
            }
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
        "handoff_visible_session_count": _int_value(handoff.get("visible_session_count")),
        "handoff_all_device_session_count": _int_value(handoff.get("all_device_session_count")),
        "handoff_session_ref_count": _int_value(
            (
                handoff.get("object_ref_counts")
                if isinstance(handoff.get("object_ref_counts"), Mapping)
                else {}
            ).get("Session")
        ),
        "read_after_write_status": public_safe_text(str(read_after_write.get("status") or ""), max_chars=80),
        "raw_return_capability": public_safe_text(str(handoff.get("raw_return_capability") or ""), max_chars=80),
        "production_mutation_performed": _session_project_rollup_reports_mutation(
            rollup=rollup,
            preview=preview,
            resume=resume,
        ),
        "gaps": failures,
    }


def _runtime_evidence_field_present(
    evidence: Mapping[str, Any],
    field_name: str,
    marker_name: str,
) -> bool:
    marker = evidence.get(marker_name)
    if isinstance(marker, bool):
        return marker
    return field_name in evidence


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
    collector_error_type = public_safe_text(str(rollup.get("collector_error_type") or ""), max_chars=80)
    if collector_error_type:
        failures.append(f"session_project_rollup_collector_error:{collector_error_type}")
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
    handoff_object_ref_counts = (
        handoff.get("object_ref_counts") if isinstance(handoff.get("object_ref_counts"), Mapping) else {}
    )
    preview_visible_session_count = _int_value(preview.get("visible_session_count"))
    preview_all_device_session_count = _int_value(preview.get("all_device_session_count"))
    if _int_value(handoff.get("visible_session_count")) != preview_visible_session_count:
        failures.append("session_project_handoff_visible_session_count_mismatch")
    if _int_value(handoff.get("all_device_session_count")) != preview_all_device_session_count:
        failures.append("session_project_handoff_all_device_session_count_mismatch")
    if _int_value(handoff_object_ref_counts.get("Session")) < preview_visible_session_count:
        failures.append("session_project_handoff_session_ref_count_mismatch")
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
    if _int_value(handoff_object_ref_counts.get("WorkUnit")) < _int_value(resume.get("work_unit_ref_count")):
        failures.append("session_project_handoff_work_unit_ref_count_mismatch")
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
        collector_capability_present=_has_collector_attestation_capability(
            evidence,
            "preference_artifact_memory",
        ),
    )
    collector_proof_gaps = {
        "preference_artifact_collector_capability_missing",
        "preference_artifact_collector_attestation_missing",
    }
    hard_failures = [
        failure
        for failure in failures
        if failure not in collector_proof_gaps
    ]
    return {
        "claim_id": "live.preference_artifact.memory",
        "evidence_class": "runtime_read_path",
        "status": (
            "failed"
            if hard_failures
            else (
                "not_validated"
                if collector_proof_gaps.intersection(failures)
                else "validated"
            )
        ),
        "schema_version": public_safe_text(str(preference.get("schema_version") or ""), max_chars=80),
        "preference_pack_schema": public_safe_text(str(pack.get("schema_version") or ""), max_chars=80),
        "accepted_preference_count": _int_value(pack.get("accepted_preference_count")),
        "accepted_current_lane_count": _pack_lane_object_count(
            pack,
            REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE,
        ),
        "proposal_preference_count": _int_value(pack.get("proposal_preference_count")),
        "html_route_status": "failed" if _html_preference_route_unimplemented(html_smoke, html_pack) else "validated",
        "agent_context_object_count": _int_value(context.get("object_count")),
        "agent_context_authority_lanes": _section_authority_lanes(context),
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
    collector_capability_present: bool = False,
) -> list[str]:
    failures: list[str] = []
    collector_error_type = public_safe_text(str(preference.get("collector_error_type") or ""), max_chars=80)
    if collector_error_type:
        failures.append(f"preference_artifact_memory_collector_error:{collector_error_type}")
    if preference.get("schema_version") != PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA:
        failures.append("preference_artifact_memory_schema_mismatch")
    if not collector_capability_present:
        failures.append("preference_artifact_collector_capability_missing")
    elif not _collector_preference_attestation_valid(preference):
        failures.append("preference_artifact_collector_attestation_missing")
    if pack.get("schema_version") != "object_pack.v1":
        failures.append("preference_artifact_pack_schema_mismatch")
    if pack.get("route") != "code_style_preference":
        failures.append("preference_artifact_pack_route_mismatch")
    if _int_value(pack.get("accepted_preference_count")) < 1:
        failures.append("preference_artifact_accepted_preference_missing")
    if not _pack_lane_contains_object_type(
        pack,
        REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE,
        "ArtifactPreference",
    ):
        failures.append("preference_artifact_accepted_current_lane_missing")
    actual_runtime_read = _is_actual_preference_runtime_read(preference)
    if not actual_runtime_read and _int_value(pack.get("proposal_preference_count")) < 1:
        failures.append("preference_artifact_proposal_lane_missing")
    if actual_runtime_read:
        alignment = (
            preference.get("read_surface_alignment")
            if isinstance(preference.get("read_surface_alignment"), Mapping)
            else {}
        )
        if not _preference_artifact_alignment_valid(preference, alignment):
            failures.append("preference_artifact_read_surface_alignment_failed")
        if not _runtime_artifact_consumer_evidence_valid(preference):
            failures.append("preference_artifact_consumer_evidence_missing")
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
    if REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE not in _section_authority_lanes(context):
        failures.append("preference_artifact_agent_context_accepted_current_missing")
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


def _is_actual_preference_runtime_read(preference: Mapping[str, Any]) -> bool:
    return (
        preference.get("schema_version") == PREFERENCE_ARTIFACT_MEMORY_RUNTIME_SCHEMA
        and preference.get("attestation_state")
        in {"unattested_runtime_read", "attested_post_deploy_streamable_http"}
        and isinstance(preference.get("read_surface_alignment"), Mapping)
        and isinstance(preference.get("artifact_consumer_evidence"), Mapping)
    )


def _runtime_artifact_consumer_evidence_valid(preference: Mapping[str, Any]) -> bool:
    consumer = (
        preference.get("artifact_consumer_evidence")
        if isinstance(preference.get("artifact_consumer_evidence"), Mapping)
        else {}
    )
    return artifact_preference_application_receipt_is_valid(consumer)


def _collector_preference_attestation_valid(preference: Mapping[str, Any]) -> bool:
    attestation = (
        preference.get("attestation_provenance")
        if isinstance(preference.get("attestation_provenance"), Mapping)
        else {}
    )
    receipt = (
        preference.get("artifact_consumer_evidence")
        if isinstance(preference.get("artifact_consumer_evidence"), Mapping)
        else {}
    )
    if set(attestation) != {
        "schema_version",
        "collector",
        "transport",
        "named_tool",
        "receipt_hash",
        "read_surface_recheck",
    }:
        return False
    try:
        receipt_hash = require_sha256(
            str(attestation.get("receipt_hash") or ""),
            "attestation_provenance.receipt_hash",
        )
    except ValueError:
        return False
    return (
        preference.get("attestation_state") == "attested_post_deploy_streamable_http"
        and preference.get("evidence_class") == "runtime_preference_artifact_memory"
        and preference.get("evidence_source") == "actual_live_read_surfaces"
        and attestation.get("schema_version")
        == ARTIFACT_PREFERENCE_COLLECTOR_ATTESTATION_SCHEMA
        and attestation.get("collector")
        == "source_to_candidate_post_deploy_mcp_capture"
        and attestation.get("transport") == "streamable_http"
        and attestation.get("named_tool") == ARTIFACT_PREFERENCE_EVALUATOR_TOOL
        and attestation.get("read_surface_recheck") == "validated"
        and receipt_hash == str(receipt.get("receipt_hash") or "")
        and artifact_preference_application_receipt_is_valid(receipt)
    )


def _preference_artifact_alignment_valid(
    preference: Mapping[str, Any],
    alignment: Mapping[str, Any],
) -> bool:
    target_object_id = public_safe_text(str(alignment.get("target_object_id") or ""), max_chars=180)
    if (
        alignment.get("status") != "validated"
        or knowledge_object_class_from_id(target_object_id) != "ArtifactPreference"
    ):
        return False
    pack = preference.get("preference_object_pack") if isinstance(
        preference.get("preference_object_pack"), Mapping
    ) else {}
    html_smoke = preference.get("html_visualization_route_smoke") if isinstance(
        preference.get("html_visualization_route_smoke"), Mapping
    ) else {}
    html_pack = html_smoke.get("object_pack") if isinstance(html_smoke.get("object_pack"), Mapping) else {}
    context = preference.get("agent_context_preference_section") if isinstance(
        preference.get("agent_context_preference_section"), Mapping
    ) else {}
    surfaces = [
        pack.get("lanes", {}).get(REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE, [])
        if isinstance(pack.get("lanes"), Mapping)
        else [],
        html_pack.get("lanes", {}).get(REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE, [])
        if isinstance(html_pack.get("lanes"), Mapping)
        else [],
        context.get("items", []) if isinstance(context.get("items"), list) else [],
    ]
    continuity: list[tuple[str, str, str, str, str, str]] = []
    for items in surfaces:
        obj = next(
            (
                item
                for item in items
                if isinstance(item, Mapping) and str(item.get("object_id") or "") == target_object_id
            ),
            None,
        )
        if obj is None:
            return False
        continuity.append(_artifact_preference_continuity(obj))
    expected = (
        public_safe_text(str(alignment.get("memory_id") or ""), max_chars=180),
        public_safe_text(str(alignment.get("card_content_hash") or ""), max_chars=80),
        public_safe_text(str(alignment.get("authority_proposal_id") or ""), max_chars=180),
        public_safe_text(str(alignment.get("authority_decision_id") or ""), max_chars=180),
        public_safe_text(str(alignment.get("project") or ""), max_chars=120),
        public_safe_text(str(alignment.get("source_content_hash") or ""), max_chars=80),
    )
    return (
        expected != ("", "", "", "", "", "")
        and len(set(continuity)) == 1
        and continuity[0] == expected
    )


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
    return any(_object_matches_type(obj, object_type) for obj in objects)


def _pack_lane_object_count(pack: Mapping[str, Any], lane: str) -> int:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    objects = lanes.get(lane) if isinstance(lanes.get(lane), list) else []
    return sum(1 for obj in objects if isinstance(obj, Mapping))


def _pack_lane_contains_object_type(pack: Mapping[str, Any], lane: str, object_type: str) -> bool:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    objects = lanes.get(lane) if isinstance(lanes.get(lane), list) else []
    return any(
        _object_matches_type(obj, object_type)
        and obj.get("authority_lane") == lane
        for obj in objects
    )


def _object_matches_type(value: Any, object_type: str) -> bool:
    if not isinstance(value, Mapping) or value.get("object_type") != object_type:
        return False
    return (
        object_type != "ArtifactPreference"
        or knowledge_object_class_from_id(str(value.get("object_id") or ""))
        == "ArtifactPreference"
    )


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
    collector_error_type = public_safe_text(str(audit.get("collector_error_type") or ""), max_chars=80)
    if collector_error_type:
        failures.append(f"permission_sensitive_audit_collector_error:{collector_error_type}")
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
    if not _is_sha256_hash_ref(actor_hash):
        failures.append(f"permission_sensitive_audit_actor_hash_missing:{action}")
    if not _is_sha256_hash_ref(request_hash):
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


def _is_sha256_hash_ref(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(char in "0123456789abcdefABCDEF" for char in digest)


def _duplicate_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


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


def _live_agent_context_startup_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    startup = evidence.get("agent_context_startup_runtime")
    startup = startup if isinstance(startup, Mapping) else {}
    if not startup:
        return {
            "claim_id": "live.agent_context.startup_read_path",
            "evidence_class": "runtime_startup_read_path",
            "status": "not_validated",
            "schema_version": "",
            "startup_loaded": False,
            "production_mutation_performed": False,
            "gaps": ["live_agent_context_startup_unverified", "production_startup_read_path_unproven"],
        }
    context = startup.get("startup_context") if isinstance(startup.get("startup_context"), Mapping) else {}
    read_path = startup.get("read_path_smoke") if isinstance(startup.get("read_path_smoke"), Mapping) else {}
    enforcement = (
        startup.get("runtime_enforcement") if isinstance(startup.get("runtime_enforcement"), Mapping) else {}
    )
    postcheck = startup.get("postcheck") if isinstance(startup.get("postcheck"), Mapping) else {}
    provenance = (
        evidence.get("evidence_provenance")
        if isinstance(evidence.get("evidence_provenance"), Mapping)
        else {}
    )
    collection_mode = str(provenance.get("collection_mode") or "")
    require_external_receipt = collection_mode in LIVE_EVIDENCE_COLLECTION_MODES
    captured_product = _agent_context_product(evidence)
    captured_route_smokes = (
        [item for item in evidence.get("brain_objects_query_smokes", []) if isinstance(item, Mapping)]
        if isinstance(evidence.get("brain_objects_query_smokes"), list)
        else []
    )
    failures = _agent_context_startup_failures(
        startup=startup,
        context=context,
        read_path=read_path,
        enforcement=enforcement,
        postcheck=postcheck,
        captured_product=captured_product,
        captured_route_smokes=captured_route_smokes,
        require_external_receipt=require_external_receipt,
        collector_capability_present=_has_collector_attestation_capability(
            evidence,
            "agent_context_startup_runtime",
        ),
    )
    collector_proof_gaps = {
        "agent_context_startup_collector_capability_missing",
    }
    hard_failures = [
        failure
        for failure in failures
        if failure not in collector_proof_gaps
    ]
    collector_proof_missing = bool(collector_proof_gaps.intersection(failures))
    consumer_statuses = (
        startup.get("consumer_statuses")
        if isinstance(startup.get("consumer_statuses"), Mapping)
        else {}
    )
    unvalidated_consumers = [
        consumer
        for consumer in ALLOWED_AGENT_CONTEXT_CONSUMERS
        if consumer != "codex"
        and str(
            (
                consumer_statuses.get(consumer)
                if isinstance(consumer_statuses.get(consumer), Mapping)
                else {}
            ).get("status")
            or "not_validated"
        )
        != "validated"
    ]
    bounded_gaps = (
        [
            f"agent_context_consumer_startup_unvalidated:{consumer}"
            for consumer in unvalidated_consumers
        ]
        if require_external_receipt and not hard_failures and not collector_proof_missing
        else []
    )
    if (
        require_external_receipt
        and not hard_failures
        and not collector_proof_missing
        and enforcement.get("runtime_interception_observed") is not True
    ):
        bounded_gaps.append(
            "agent_context_action_surface_runtime_interception_unvalidated"
        )
    if require_external_receipt and not hard_failures and not collector_proof_missing:
        bounded_gaps.append("agent_context_codex_host_startup_hook_unvalidated")
    bounded_adapter_status = (
        "failed"
        if hard_failures
        else "not_validated"
        if collector_proof_missing
        else "validated"
    )
    return {
        "claim_id": "live.agent_context.startup_read_path",
        "evidence_class": "runtime_startup_read_path",
        "status": (
            "failed"
            if hard_failures
            else "not_validated"
            if collector_proof_missing or bounded_gaps
            else "validated"
        ),
        "bounded_adapter_status": bounded_adapter_status,
        "host_startup_hook_status": (
            "failed" if hard_failures else "not_validated"
        ),
        "schema_version": public_safe_text(str(startup.get("schema_version") or ""), max_chars=80),
        "consumer": public_safe_text(str(context.get("consumer") or ""), max_chars=80),
        "activation_scope": public_safe_text(
            str(startup.get("activation_scope") or ""),
            max_chars=120,
        ),
        "evidence_origin": public_safe_text(
            str(startup.get("evidence_origin") or ""),
            max_chars=120,
        ),
        "startup_loaded": context.get("loaded_on_startup") is True,
        "read_path_tool": public_safe_text(str(read_path.get("tool") or ""), max_chars=120),
        "routes_checked": _string_list(read_path.get("routes_checked")),
        "validated_consumers": [
            consumer
            for consumer in ALLOWED_AGENT_CONTEXT_CONSUMERS
            if str(
                (
                    consumer_statuses.get(consumer)
                    if isinstance(consumer_statuses.get(consumer), Mapping)
                    else {}
                ).get("status")
                or ""
            )
            == "validated"
        ],
        "unvalidated_consumers": unvalidated_consumers,
        "production_mutation_performed": _agent_context_startup_reports_mutation(
            startup=startup,
            read_path=read_path,
            enforcement=enforcement,
        ),
        "gaps": [*failures, *bounded_gaps],
    }


def _agent_context_startup_failures(
    *,
    startup: Mapping[str, Any],
    context: Mapping[str, Any],
    read_path: Mapping[str, Any],
    enforcement: Mapping[str, Any],
    postcheck: Mapping[str, Any],
    captured_product: Mapping[str, Any],
    captured_route_smokes: list[Mapping[str, Any]],
    require_external_receipt: bool = False,
    collector_capability_present: bool = False,
) -> list[str]:
    failures: list[str] = []
    collector_error_type = public_safe_text(str(startup.get("collector_error_type") or ""), max_chars=80)
    if collector_error_type:
        failures.append(f"agent_context_startup_collector_error:{collector_error_type}")
    if startup.get("schema_version") != AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA:
        failures.append("agent_context_startup_schema_mismatch")
    if context.get("schema_version") != REQUIRED_AGENT_CONTEXT_PRODUCT_SCHEMA:
        failures.append("agent_context_startup_product_schema_mismatch")
    if str(context.get("consumer") or "") not in ALLOWED_AGENT_CONTEXT_CONSUMERS:
        failures.append("agent_context_startup_consumer_unknown")
    if context.get("loaded_on_startup") is not True:
        failures.append("agent_context_startup_not_loaded")
    section_counts = context.get("section_counts") if isinstance(context.get("section_counts"), Mapping) else {}
    missing_sections = [
        section
        for section in REQUIRED_AGENT_CONTEXT_STARTUP_SECTIONS
        if _int_value(section_counts.get(section)) < 1
    ]
    failures.extend(_named_gaps("agent_context_startup_section_missing", missing_sections))
    policy = context.get("surface_policy") if isinstance(context.get("surface_policy"), Mapping) else {}
    if policy.get("mutation_allowed") is not False:
        failures.append("agent_context_startup_mutation_allowed")
    if context.get("degraded_gap_disclosure_present") is not True:
        failures.append("agent_context_startup_degraded_gap_disclosure_missing")
    if context.get("missing_evidence_before_promotion_present") is not True:
        failures.append("agent_context_startup_missing_evidence_before_promotion_missing")
    if read_path.get("tool") != "brain_objects_query":
        failures.append("agent_context_startup_read_path_tool_mismatch")
    if read_path.get("read_only") is not True:
        failures.append("agent_context_startup_read_path_not_read_only")
    routes_checked = set(_string_list(read_path.get("routes_checked")))
    missing_routes = [route for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES if route not in routes_checked]
    failures.extend(_named_gaps("agent_context_startup_route_missing", missing_routes))
    if read_path.get("production_mutation_performed") is True:
        failures.append("agent_context_startup_read_path_mutation_performed")
    if enforcement.get("direct_execution_allowed") is not False:
        failures.append("agent_context_startup_direct_execution_allowed")
    if require_external_receipt and enforcement.get("evidence_kind") != (
        "context_pack_policy_projection"
    ):
        failures.append("agent_context_startup_policy_projection_missing")
    if enforcement.get("production_mutation_allowed") is not False:
        failures.append("agent_context_startup_production_mutation_allowed")
    if enforcement.get("raw_private_context_blocked") is not True:
        failures.append("agent_context_startup_raw_private_context_not_blocked")
    if enforcement.get("approval_scope_blocker_enforced") is not True:
        failures.append("agent_context_startup_approval_scope_blocker_missing")
    if enforcement.get("stale_or_degraded_disclosure_present") is not True:
        failures.append("agent_context_startup_stale_or_degraded_disclosure_missing")
    if _agent_context_startup_reports_mutation(
        startup=startup,
        read_path=read_path,
        enforcement=enforcement,
    ):
        failures.append("agent_context_startup_production_mutation_performed")
    if postcheck.get("status") != "validated":
        failures.append("agent_context_startup_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "agent_context_startup_raw_private_evidence_returned"),
        ("secret_returned", "agent_context_startup_secret_returned"),
        ("host_topology_returned", "agent_context_startup_host_topology_returned"),
        ("raw_external_ids_returned", "agent_context_startup_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    if require_external_receipt:
        failures.extend(
            _external_agent_context_startup_receipt_failures(
                startup=startup,
                context=context,
                read_path=read_path,
                enforcement=enforcement,
                captured_product=captured_product,
                captured_route_smokes=captured_route_smokes,
                collector_capability_present=collector_capability_present,
            )
        )
    return _dedupe(failures)


def _external_agent_context_startup_receipt_failures(
    *,
    startup: Mapping[str, Any],
    context: Mapping[str, Any],
    read_path: Mapping[str, Any],
    enforcement: Mapping[str, Any],
    captured_product: Mapping[str, Any],
    captured_route_smokes: list[Mapping[str, Any]],
    collector_capability_present: bool,
) -> list[str]:
    failures: list[str] = []
    if not collector_capability_present:
        failures.append("agent_context_startup_collector_capability_missing")
    collector_execution = (
        startup.get("collector_execution")
        if isinstance(startup.get("collector_execution"), Mapping)
        else {}
    )
    if (
        collector_execution.get("runner_kind") != "default_external_subprocess"
        or collector_execution.get("subprocess_attested") is not True
    ):
        failures.append("agent_context_startup_external_subprocess_unattested")
    if startup.get("evidence_origin") != "external_consumer_process":
        failures.append("agent_context_startup_external_consumer_receipt_missing")
    if startup.get("activation_scope") != CODEX_BOUNDED_ACTIVATION_SCOPE:
        failures.append("agent_context_startup_activation_scope_mismatch")
    validation = (
        startup.get("receipt_validation")
        if isinstance(startup.get("receipt_validation"), Mapping)
        else {}
    )
    validation_failures = _string_list(validation.get("failures"))
    if validation.get("status") != "validated" or validation_failures:
        failures.append("agent_context_startup_receipt_not_verified")
        failures.extend(validation_failures)

    receipt = (
        startup.get("startup_receipt")
        if isinstance(startup.get("startup_receipt"), Mapping)
        else {}
    )
    if receipt.get("schema_version") != AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA:
        failures.append("agent_context_startup_receipt_schema_mismatch")
    issuer = receipt.get("issuer") if isinstance(receipt.get("issuer"), Mapping) else {}
    if issuer.get("kind") != "external_consumer_process":
        failures.append("agent_context_startup_issuer_not_external_consumer")
    if issuer.get("consumer") != "codex":
        failures.append("agent_context_startup_consumer_mismatch")
    if issuer.get("implementation") != CODEX_CONTEXT_ADAPTER:
        failures.append("agent_context_startup_adapter_mismatch")

    receipt_core = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_hash", "proof"}
    }
    receipt_hash = str(receipt.get("receipt_hash") or "")
    if receipt_hash != hash_payload(receipt_core):
        failures.append("agent_context_startup_receipt_hash_mismatch")
    proof = receipt.get("proof") if isinstance(receipt.get("proof"), Mapping) else {}
    if proof.get("algorithm") != "HMAC-SHA-256" or not _is_sha256_hash_ref(
        str(proof.get("tag") or "")
    ):
        failures.append("agent_context_startup_proof_missing")

    events = [
        item
        for item in receipt.get("startup_events", [])
        if isinstance(item, Mapping)
    ] if isinstance(receipt.get("startup_events"), list) else []
    expected_event_types = (
        "process_started",
        "context_requested",
        "context_loaded_before_task_dispatch",
    )
    if len(events) != len(expected_event_types):
        failures.append("agent_context_startup_event_sequence_incomplete")
    previous_hash = ""
    for index, expected_type in enumerate(expected_event_types, start=1):
        event = events[index - 1] if len(events) >= index else {}
        if event.get("seq") != index or event.get("type") != expected_type:
            failures.append(f"agent_context_startup_event_order_mismatch:{index}")
        if event.get("prev_hash") != previous_hash:
            failures.append(f"agent_context_startup_event_chain_mismatch:{index}")
        expected_hash = hash_payload(
            {key: value for key, value in event.items() if key != "event_hash"}
        )
        event_hash = str(event.get("event_hash") or "")
        if event_hash != expected_hash:
            failures.append(f"agent_context_startup_event_hash_mismatch:{index}")
        previous_hash = event_hash

    context_binding = (
        receipt.get("context_binding")
        if isinstance(receipt.get("context_binding"), Mapping)
        else {}
    )
    section_manifest = (
        context_binding.get("section_manifest")
        if isinstance(context_binding.get("section_manifest"), Mapping)
        else {}
    )
    section_counts = (
        context.get("section_counts")
        if isinstance(context.get("section_counts"), Mapping)
        else {}
    )
    section_authority_lanes = (
        context.get("section_authority_lanes")
        if isinstance(context.get("section_authority_lanes"), Mapping)
        else {}
    )
    for section in REQUIRED_AGENT_CONTEXT_STARTUP_SECTIONS:
        manifest = (
            section_manifest.get(section)
            if isinstance(section_manifest.get(section), Mapping)
            else {}
        )
        item_hashes = _string_list(manifest.get("item_hashes"))
        if any(not _is_sha256_hash_ref(item_hash) for item_hash in item_hashes):
            failures.append(f"agent_context_startup_item_hash_invalid:{section}")
        if _int_value(section_counts.get(section)) != len(item_hashes):
            failures.append(f"agent_context_startup_section_count_mismatch:{section}")
        manifest_lanes = _string_list(manifest.get("authority_lanes"))
        context_lanes = _string_list(section_authority_lanes.get(section))
        if context_lanes != manifest_lanes:
            failures.append(f"agent_context_startup_section_lane_mismatch:{section}")
        if section in {
            REQUIRED_AGENT_CONTEXT_AUTHORITY_SECTION,
            REQUIRED_AGENT_CONTEXT_STYLE_PREFERENCE_SECTION,
        } and set(manifest_lanes) != {REQUIRED_AGENT_CONTEXT_AUTHORITY_LANE}:
            failures.append(f"agent_context_startup_authority_lane_mismatch:{section}")

    route_manifest = (
        context_binding.get("route_manifest")
        if isinstance(context_binding.get("route_manifest"), Mapping)
        else {}
    )
    checked_route_list = _string_list(read_path.get("routes_checked"))
    checked_routes = set(checked_route_list)
    for route in _duplicate_strings(checked_route_list):
        failures.append(f"agent_context_startup_read_path_route_duplicate:{route}")
    if set(route_manifest) != checked_routes:
        failures.append("agent_context_startup_route_manifest_mismatch")
    receipt_scope = (
        receipt.get("scope_binding")
        if isinstance(receipt.get("scope_binding"), Mapping)
        else {}
    )
    route_request_hashes = (
        receipt_scope.get("route_request_hashes")
        if isinstance(receipt_scope.get("route_request_hashes"), Mapping)
        else {}
    )
    route_binding_keys = {
        "schema_version",
        "route",
        "route_request_hash",
        "semantic_projection_hash",
        "observed_source_payload_hash",
    }
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        if route not in route_manifest:
            failures.append(f"agent_context_startup_route_missing:{route}")
            continue
        binding = (
            route_manifest.get(route)
            if isinstance(route_manifest.get(route), Mapping)
            else {}
        )
        if set(binding) != route_binding_keys:
            failures.append(f"agent_context_startup_route_binding_shape_mismatch:{route}")
        if binding.get("schema_version") != AGENT_CONTEXT_ROUTE_BINDING_SCHEMA:
            failures.append(f"agent_context_startup_route_binding_schema_mismatch:{route}")
        if binding.get("route") != route:
            failures.append(f"agent_context_startup_route_binding_route_mismatch:{route}")
        if binding.get("route_request_hash") != route_request_hashes.get(route):
            failures.append(f"agent_context_startup_route_request_binding_mismatch:{route}")
        if not _is_sha256_hash_ref(str(binding.get("semantic_projection_hash") or "")):
            failures.append(f"agent_context_startup_route_semantic_hash_invalid:{route}")
        if not _is_sha256_hash_ref(str(binding.get("observed_source_payload_hash") or "")):
            failures.append(f"agent_context_startup_route_observed_hash_invalid:{route}")

    bundle_binding = (
        startup.get("capture_bundle_binding")
        if isinstance(startup.get("capture_bundle_binding"), Mapping)
        else {}
    )
    if bundle_binding.get("schema_version") != "agent_context_capture_bundle_binding.v1":
        failures.append("agent_context_startup_capture_bundle_schema_mismatch")
    source_product_hash = str(captured_product.get("source_payload_hash") or "")
    if (
        not _is_sha256_hash_ref(source_product_hash)
        or source_product_hash != str(context_binding.get("product_hash") or "")
        or source_product_hash != str(bundle_binding.get("source_product_hash") or "")
    ):
        failures.append("agent_context_startup_product_capture_binding_mismatch")
    if bundle_binding.get("agent_context_product_projection_hash") != hash_payload(
        captured_product
    ):
        failures.append("agent_context_startup_product_projection_binding_mismatch")
    captured_route_names = [
        str(smoke.get("route") or "")
        for smoke in captured_route_smokes
        if str(smoke.get("route") or "")
    ]
    for route in _duplicate_strings(captured_route_names):
        failures.append(f"agent_context_startup_route_capture_duplicate:{route}")
    captured_smokes_by_route = {
        str(smoke.get("route") or ""): smoke
        for smoke in captured_route_smokes
        if str(smoke.get("route") or "")
    }
    bundle_route_hashes = (
        bundle_binding.get("route_smoke_projection_hashes")
        if isinstance(bundle_binding.get("route_smoke_projection_hashes"), Mapping)
        else {}
    )
    if (
        set(captured_smokes_by_route) != set(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
        or set(bundle_route_hashes) != set(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    ):
        failures.append("agent_context_startup_route_capture_binding_shape_mismatch")
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        captured_smoke = captured_smokes_by_route.get(route, {})
        captured_hash = hash_payload(captured_smoke)
        binding = (
            route_manifest.get(route)
            if isinstance(route_manifest.get(route), Mapping)
            else {}
        )
        captured_semantic_hash = str(captured_smoke.get("semantic_payload_hash") or "")
        if str(binding.get("semantic_projection_hash") or "") != captured_semantic_hash:
            failures.append(f"agent_context_startup_route_semantic_binding_mismatch:{route}")
        if str(bundle_route_hashes.get(route) or "") != captured_hash:
            failures.append(f"agent_context_startup_route_capture_binding_mismatch:{route}")

    decisions = [
        item
        for item in receipt.get("policy_decisions", [])
        if isinstance(item, Mapping)
    ] if isinstance(receipt.get("policy_decisions"), list) else []
    by_capability = {
        str(
            (
                item.get("request")
                if isinstance(item.get("request"), Mapping)
                else {}
            ).get("capability")
            or ""
        ): item
        for item in decisions
    }
    for capability, (expected_outcome, expected_reason) in REQUIRED_POLICY_DECISIONS.items():
        decision_receipt = by_capability.get(capability)
        if not isinstance(decision_receipt, Mapping):
            failures.append(f"agent_context_startup_policy_decision_missing:{capability}")
            continue
        decision = (
            decision_receipt.get("decision")
            if isinstance(decision_receipt.get("decision"), Mapping)
            else {}
        )
        if (
            decision.get("outcome") != expected_outcome
            or decision.get("reason_code") != expected_reason
        ):
            failures.append(f"agent_context_startup_policy_outcome_mismatch:{capability}")
        if decision.get("executor_invoked") is not False or decision.get("side_effect_count") != 0:
            failures.append(f"agent_context_startup_policy_side_effect:{capability}")
        expected_decision_hash = hash_payload(
            {
                key: value
                for key, value in decision_receipt.items()
                if key != "decision_hash"
            }
        )
        if decision_receipt.get("decision_hash") != expected_decision_hash:
            failures.append(f"agent_context_startup_policy_hash_mismatch:{capability}")
    decision_hashes = [str(item.get("decision_hash") or "") for item in decisions]
    if receipt.get("policy_decision_hashes") != decision_hashes:
        failures.append("agent_context_startup_policy_decision_hashes_mismatch")

    io_audit = receipt.get("io_audit") if isinstance(receipt.get("io_audit"), Mapping) else {}
    if io_audit.get("brain_context_resolve_calls") != 1:
        failures.append("agent_context_startup_context_read_count_mismatch")
    if io_audit.get("brain_objects_query_calls") != len(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES):
        failures.append("agent_context_startup_object_query_count_mismatch")
    if io_audit.get("write_tool_calls") != 0:
        failures.append("agent_context_startup_write_tool_called")
    if io_audit.get("task_dispatch_count_before_load") != 0:
        failures.append("agent_context_startup_task_dispatched_before_load")
    if enforcement.get("suggest_change_allowed") is not True:
        failures.append("agent_context_startup_suggest_change_positive_control_missing")

    consumer_statuses = (
        startup.get("consumer_statuses")
        if isinstance(startup.get("consumer_statuses"), Mapping)
        else {}
    )
    codex_status = (
        consumer_statuses.get("codex")
        if isinstance(consumer_statuses.get("codex"), Mapping)
        else {}
    )
    if codex_status.get("status") != "validated" or codex_status.get("scope") != CODEX_BOUNDED_ACTIVATION_SCOPE:
        failures.append("agent_context_startup_codex_bounded_status_mismatch")
    for consumer in ("claude-code", "gemini", "hermes"):
        status = (
            consumer_statuses.get(consumer)
            if isinstance(consumer_statuses.get(consumer), Mapping)
            else {}
        )
        if status.get("status") != "not_validated":
            failures.append(f"agent_context_startup_consumer_overclaimed:{consumer}")
    return _dedupe(failures)


def _agent_context_startup_reports_mutation(
    *,
    startup: Mapping[str, Any],
    read_path: Mapping[str, Any],
    enforcement: Mapping[str, Any],
) -> bool:
    return (
        startup.get("production_mutation_performed") is True
        or read_path.get("production_mutation_performed") is True
        or enforcement.get("production_mutation_allowed") is True
    )


def _strict_int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_public_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_PUBLIC_REF_RE.fullmatch(value))


def _is_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_COMMIT_SHA_RE.fullmatch(value))


def _is_sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_DIGEST_RE.fullmatch(value))


def _unknown_keys(value: Mapping[str, Any], allowed: frozenset[str]) -> list[str]:
    return ["unexpected_field" for key in value if key not in allowed]


def _deployment_evidence_layer(
    evidence: Mapping[str, Any], key: str
) -> Mapping[str, Any]:
    if key not in evidence:
        return {}
    value = evidence.get(key)
    if isinstance(value, Mapping):
        return value
    return {_MALFORMED_EVIDENCE_TYPE_FIELD: True}


def _gitops_desired_state_errors(value: Mapping[str, Any]) -> list[str]:
    if not value:
        return ["gitops_desired_state_unverified"]
    errors = _unknown_keys(value, _GITOPS_DESIRED_STATE_KEYS)
    if value.get("schema_version") != GITOPS_DESIRED_STATE_SCHEMA:
        errors.append("gitops_desired_state_schema_mismatch")
    if value.get("images_include_expected_commit") is not True:
        errors.append("gitops_desired_state_expected_commit_mismatch")
    if value.get("desired_state_source") != "sanitized_ops_manifest_summary":
        errors.append("gitops_desired_state_source_invalid")
    if value.get("target_revision") != "main":
        errors.append("gitops_desired_state_target_revision_invalid")
    if not _is_commit_sha(value.get("source_commit")):
        errors.append("gitops_desired_state_source_commit_invalid")
    if not _is_sha256_digest(value.get("desired_image_set_hash")):
        errors.append("gitops_desired_state_image_set_hash_invalid")
    if not _is_public_ref(value.get("ops_revision")):
        errors.append("gitops_desired_state_ops_revision_invalid")
    count = _strict_int_or_none(value.get("expected_image_ref_count"))
    if count is None or count <= 0:
        errors.append("gitops_desired_state_expected_image_ref_count_invalid")
    if value.get("production_mutation_performed") is not False:
        errors.append("gitops_desired_state_mutation_invalid")
    return _dedupe(errors)


def _argo_reconciliation_errors(value: Mapping[str, Any]) -> list[str]:
    if not value:
        return ["argo_reconciliation_unverified"]
    errors = _unknown_keys(value, _ARGO_RECONCILIATION_KEYS)
    if value.get("schema_version") != ARGO_RECONCILIATION_SCHEMA:
        errors.append("argo_reconciliation_schema_mismatch")
    if value.get("reconciliation_source") != "sanitized_argo_application_summary":
        errors.append("argo_reconciliation_source_invalid")
    if not _is_public_ref(value.get("reconciled_ops_revision")):
        errors.append("argo_reconciliation_revision_invalid")
    if value.get("sync_status") != "Synced":
        errors.append("argo_reconciliation_sync_status_invalid")
    if value.get("health_status") != "Healthy":
        errors.append("argo_reconciliation_health_status_invalid")
    if value.get("production_mutation_performed") is not False:
        errors.append("argo_reconciliation_mutation_invalid")
    return _dedupe(errors)


def _deployed_identity_errors(value: Mapping[str, Any]) -> list[str]:
    if not value:
        return ["live_deployed_identity_unverified"]
    errors = _unknown_keys(value, _DEPLOYED_IDENTITY_KEYS)
    if value.get("contains_expected_commit") is not True:
        errors.append("live_deployed_identity_expected_commit_unverified")
    if value.get("identity_source") != "redacted_live_runtime_evidence":
        errors.append("live_deployed_identity_source_invalid")
    if not _is_commit_sha(value.get("source_commit")):
        errors.append("live_deployed_identity_source_commit_invalid")
    if not _is_sha256_digest(value.get("live_image_set_hash")):
        errors.append("live_deployed_identity_image_set_hash_invalid")
    if _strict_int_or_none(value.get("stale_image_ref_count")) != 0:
        errors.append("live_deployed_identity_stale_image_ref_count_invalid")
    if value.get("production_mutation_performed") is not False:
        errors.append("live_deployed_identity_mutation_invalid")
    return _dedupe(errors)


def _binding_errors(value: Mapping[str, Any]) -> list[str]:
    errors = _unknown_keys(value, _DEPLOYMENT_EVIDENCE_BINDING_KEYS)
    if value.get("schema_version") != DEPLOYMENT_EVIDENCE_BINDING_SCHEMA:
        errors.append("gitops_deployment_evidence_binding_schema_mismatch")
    if not _is_sha256_digest(value.get("canonical_tuple_hash")):
        errors.append("gitops_deployment_evidence_binding_hash_invalid")
    return _dedupe(errors)


def _argo_reconciliation_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    argo = _deployment_evidence_layer(evidence, "argo_reconciliation")
    errors = _argo_reconciliation_errors(argo)
    return {
        "claim_id": "ops.argo_reconciliation.application_status",
        "evidence_class": "argo_reconciliation_identity",
        "status": "failed" if argo and errors else ("validated" if argo else "not_validated"),
        "production_mutation_performed": argo.get("production_mutation_performed") is True,
        "gaps": [] if argo and not errors else errors,
    }


def _gitops_desired_state_claim(evidence: Mapping[str, Any], *, expected_commit: str) -> dict[str, Any]:
    desired = _deployment_evidence_layer(evidence, "gitops_desired_state")
    errors = _gitops_desired_state_errors(desired)
    strict_fields_present = any(
        key in desired
        for key in (
            "source_commit",
            "desired_image_set_hash",
            "ops_revision",
            "expected_image_ref_count",
        )
    )
    external_commit_mismatch = bool(expected_commit) and (
        "source_commit" in desired and desired.get("source_commit") != expected_commit
    )
    if external_commit_mismatch:
        errors = _dedupe([*errors, "gitops_desired_state_external_commit_mismatch"])
    has_expected = desired.get("images_include_expected_commit") is True
    mutation_performed = desired.get("production_mutation_performed") is True
    explicitly_invalid = bool(desired) and (
        bool(_unknown_keys(desired, _GITOPS_DESIRED_STATE_KEYS))
        or (
            "schema_version" in desired
            and desired.get("schema_version") != GITOPS_DESIRED_STATE_SCHEMA
        )
        or (
            "images_include_expected_commit" in desired
            and desired.get("images_include_expected_commit") is not True
        )
        or (
            "production_mutation_performed" in desired
            and desired.get("production_mutation_performed") is not False
        )
        or external_commit_mismatch
        or (strict_fields_present and bool(errors))
    )
    gaps = [] if desired and not errors else (["gitops_desired_state_unverified"] if not desired else errors)
    return {
        "claim_id": "ops.gitops_desired_state.includes_expected_commit",
        "evidence_class": "gitops_desired_state_identity",
        "status": (
            "failed"
            if explicitly_invalid
            else ("validated" if desired and not errors and has_expected else "not_validated")
        ),
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "desired_state_source": public_safe_text(
            str(desired.get("desired_state_source") or ""),
            max_chars=160,
        ),
        "target_revision": public_safe_text(str(desired.get("target_revision") or ""), max_chars=120),
        "images_include_expected_commit": has_expected,
        "production_mutation_performed": mutation_performed,
        "gaps": gaps,
    }


def _live_deployed_identity_claim(evidence: Mapping[str, Any], *, expected_commit: str) -> dict[str, Any]:
    identity = _deployment_evidence_layer(evidence, "deployed_identity")
    contains_expected = identity.get("contains_expected_commit") is True
    errors = _deployed_identity_errors(identity)
    strict_fields_present = any(
        key in identity
        for key in (
            "source_commit",
            "live_image_set_hash",
            "stale_image_ref_count",
        )
    )
    external_commit_mismatch = bool(expected_commit) and (
        "source_commit" in identity and identity.get("source_commit") != expected_commit
    )
    if external_commit_mismatch:
        errors = _dedupe([*errors, "live_deployed_identity_external_commit_mismatch"])
    explicitly_invalid = bool(identity) and (
        bool(_unknown_keys(identity, _DEPLOYED_IDENTITY_KEYS))
        or (
            "production_mutation_performed" in identity
            and identity.get("production_mutation_performed") is not False
        )
        or external_commit_mismatch
        or (strict_fields_present and bool(errors))
    )
    gaps = [] if identity and not errors else (["live_deployed_identity_unverified"] if not identity else errors)
    return {
        "claim_id": "live.deployed_identity.includes_expected_commit",
        "evidence_class": "runtime_artifact_identity",
        "status": (
            "failed"
            if explicitly_invalid
            else ("validated" if identity and not errors and contains_expected else "not_validated")
        ),
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "identity_source": public_safe_text(str(identity.get("identity_source") or ""), max_chars=160),
        "production_mutation_performed": identity.get("production_mutation_performed") is True,
        "gaps": gaps,
    }


def _deployment_evidence_binding_tuple(
    *,
    expected_commit: str,
    gitops_desired_state: Mapping[str, Any],
    argo_reconciliation: Mapping[str, Any],
    deployed_identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "desired_source_commit": public_safe_text(
            str(gitops_desired_state.get("source_commit") or ""), max_chars=80
        ),
        "deployed_source_commit": public_safe_text(
            str(deployed_identity.get("source_commit") or ""), max_chars=80
        ),
        "desired_image_set_hash": public_safe_text(
            str(gitops_desired_state.get("desired_image_set_hash") or ""), max_chars=80
        ),
        "live_image_set_hash": public_safe_text(
            str(deployed_identity.get("live_image_set_hash") or ""), max_chars=80
        ),
        "ops_revision": public_safe_text(
            str(gitops_desired_state.get("ops_revision") or ""), max_chars=120
        ),
        "reconciled_ops_revision": public_safe_text(
            str(argo_reconciliation.get("reconciled_ops_revision") or ""), max_chars=120
        ),
        "sync_status": public_safe_text(
            str(argo_reconciliation.get("sync_status") or ""), max_chars=40
        ),
        "health_status": public_safe_text(
            str(argo_reconciliation.get("health_status") or ""), max_chars=40
        ),
        "expected_image_ref_count": _strict_int_or_none(gitops_desired_state.get("expected_image_ref_count")),
        "stale_image_ref_count": _strict_int_or_none(deployed_identity.get("stale_image_ref_count")),
        "desired_production_mutation_performed": (
            gitops_desired_state.get("production_mutation_performed") is True
        ),
        "argo_production_mutation_performed": (
            argo_reconciliation.get("production_mutation_performed") is True
        ),
        "deployed_production_mutation_performed": (
            deployed_identity.get("production_mutation_performed") is True
        ),
    }


def _deployment_evidence_binding_claim(
    evidence: Mapping[str, Any], *, expected_commit: str
) -> dict[str, Any]:
    binding = _deployment_evidence_layer(evidence, "deployment_evidence_binding")
    desired = _deployment_evidence_layer(evidence, "gitops_desired_state")
    argo = _deployment_evidence_layer(evidence, "argo_reconciliation")
    deployed = _deployment_evidence_layer(evidence, "deployed_identity")
    packet_expected_commit = evidence.get("expected_commit")
    external_expected_commit = expected_commit
    effective_expected_commit = public_safe_text(
        str(external_expected_commit or packet_expected_commit or ""), max_chars=80
    )
    failures: list[str] = []
    if (
        desired.get("source_commit")
        and deployed.get("source_commit")
        and desired.get("source_commit") != deployed.get("source_commit")
    ):
        failures.append("gitops_deployment_evidence_binding_source_commit_mismatch")
    if (
        desired.get("desired_image_set_hash")
        and deployed.get("live_image_set_hash")
        and desired.get("desired_image_set_hash") != deployed.get("live_image_set_hash")
    ):
        failures.append("gitops_deployment_evidence_binding_image_set_hash_mismatch")
    if (
        desired.get("ops_revision")
        and argo.get("reconciled_ops_revision")
        and desired.get("ops_revision") != argo.get("reconciled_ops_revision")
    ):
        failures.append("gitops_deployment_evidence_binding_ops_revision_mismatch")
    if binding:
        failures.extend(_binding_errors(binding))
        failures.extend(_gitops_desired_state_errors(desired))
        failures.extend(_argo_reconciliation_errors(argo))
        failures.extend(_deployed_identity_errors(deployed))
        if not _is_commit_sha(packet_expected_commit):
            failures.append("gitops_deployment_evidence_binding_packet_expected_commit_invalid")
        canonical_tuple = _deployment_evidence_binding_tuple(
            expected_commit=effective_expected_commit,
            gitops_desired_state=desired,
            argo_reconciliation=argo,
            deployed_identity=deployed,
        )
        if binding.get("canonical_tuple_hash") != hash_payload(canonical_tuple):
            failures.append("gitops_deployment_evidence_binding_hash_mismatch")
        if desired.get("source_commit") != packet_expected_commit:
            failures.append("gitops_deployment_evidence_binding_desired_commit_mismatch")
        if deployed.get("source_commit") != packet_expected_commit:
            failures.append("gitops_deployment_evidence_binding_deployed_commit_mismatch")
        if external_expected_commit and (
            not _is_commit_sha(external_expected_commit)
            or packet_expected_commit != external_expected_commit
            or desired.get("source_commit") != external_expected_commit
            or deployed.get("source_commit") != external_expected_commit
        ):
            failures.append("gitops_deployment_evidence_binding_external_expected_commit_mismatch")
        if desired.get("desired_image_set_hash") != deployed.get("live_image_set_hash"):
            failures.append("gitops_deployment_evidence_binding_image_set_hash_mismatch")
        if desired.get("ops_revision") != argo.get("reconciled_ops_revision"):
            failures.append("gitops_deployment_evidence_binding_ops_revision_mismatch")
        if argo.get("sync_status") != "Synced":
            failures.append("gitops_deployment_evidence_binding_sync_status_mismatch")
        if argo.get("health_status") != "Healthy":
            failures.append("gitops_deployment_evidence_binding_health_status_mismatch")
        expected_image_ref_count = _strict_int_or_none(desired.get("expected_image_ref_count"))
        if expected_image_ref_count is None or expected_image_ref_count <= 0:
            failures.append("gitops_deployment_evidence_binding_expected_image_ref_count_invalid")
        if _strict_int_or_none(deployed.get("stale_image_ref_count")) != 0:
            failures.append("gitops_deployment_evidence_binding_stale_image_ref_count_mismatch")
        if desired.get("production_mutation_performed") is True:
            failures.append("gitops_deployment_evidence_binding_desired_state_mutation")
        if argo.get("production_mutation_performed") is True:
            failures.append("gitops_deployment_evidence_binding_argo_reconciliation_mutation")
        if deployed.get("production_mutation_performed") is True:
            failures.append("gitops_deployment_evidence_binding_deployed_identity_mutation")
    failures = _dedupe(failures)
    gaps = list(failures)
    if binding and not failures and not external_expected_commit:
        gaps.append("external_expected_commit_anchor_unverified")
    return {
        "claim_id": "ops.gitops_deployment_evidence_binding",
        "evidence_class": "gitops_deployment_evidence_binding",
        "status": (
            "failed"
            if failures
            else ("validated" if binding and external_expected_commit else "not_validated")
        ),
        "expected_commit": effective_expected_commit,
        "binding_present": bool(binding),
        "production_mutation_performed": (
            desired.get("production_mutation_performed") is True
            or argo.get("production_mutation_performed") is True
            or deployed.get("production_mutation_performed") is True
        ),
        "gaps": (
            _dedupe([*gaps, "gitops_deployment_evidence_binding_unverified"])
            if not binding
            else gaps
        ),
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
    if not _is_sha256_hash_ref(approval_ref_hash):
        failures.append("bounded_execution_approval_ref_hash_missing")
    if str(approval.get("scope") or "") != "single_project_single_object":
        failures.append("bounded_execution_scope_not_single_project_single_object")
    if _int_value(approval.get("max_objects")) != 1 or _int_value(scope.get("max_objects")) != 1:
        failures.append("bounded_execution_max_objects_not_one")
    if len(object_ids) != 1:
        failures.append("bounded_execution_object_count_not_one")
    if not proposal_target or proposal_target != decision_target or proposal_target != read_target:
        failures.append("bounded_execution_target_object_mismatch")
    approval_project = public_safe_text(str(approval.get("project") or ""), max_chars=120)
    proposal_project = public_safe_text(str(proposal.get("project") or ""), max_chars=120)
    decision_project = public_safe_text(str(decision.get("project") or ""), max_chars=120)
    scope_project = public_safe_text(str(scope.get("project") or ""), max_chars=120)
    projects = (approval_project, proposal_project, decision_project, scope_project)
    if not all(projects):
        failures.append("bounded_execution_project_missing")
    elif len(set(projects)) != 1:
        failures.append("bounded_execution_project_mismatch")
    if proposal_target and proposal_target not in object_ids:
        failures.append("bounded_execution_target_not_in_scope")
    target_object_class = knowledge_object_class_from_id(proposal_target)
    if proposal_target and not is_allowed_object_target(proposal_target):
        failures.append("bounded_execution_object_class_not_allowed")
    if not target_object_class or target_object_class not in allowed_object_classes:
        failures.append("bounded_execution_allowed_object_class_missing")
    if target_object_class == "ArtifactPreference" and (
        proposal.get("proposal_type") != "propose_current"
        or decision.get("decision_type") != "accept_current"
        or decision.get("new_authority_lane") != "accepted_current"
        or read_after_write.get("authority_lane") != "accepted_current"
        or postcheck.get("review_queue_status") != "accepted"
    ):
        failures.append("bounded_execution_artifact_preference_not_accepted_current")
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
    rollback_path = _string_list(rollback.get("path"))
    if str(rollback.get("status") or "") not in {"planned", "validated"} or not rollback_path:
        failures.append("bounded_execution_rollback_or_supersession_missing")
    elif "demote_prior_object_to_accepted_non_current_or_archive_only" not in rollback_path:
        failures.append("bounded_execution_demote_prior_object_step_missing")
    if postcheck.get("status") != "validated":
        failures.append("bounded_execution_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "bounded_execution_raw_private_evidence_returned"),
        ("secret_returned", "bounded_execution_secret_returned"),
        ("host_topology_returned", "bounded_execution_host_topology_returned"),
        ("raw_external_ids_returned", "bounded_execution_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _bounded_execution_reports_mutation(proposal: Mapping[str, Any], decision: Mapping[str, Any]) -> bool:
    return (
        proposal.get("production_mutation_performed") is True
        or proposal.get("proposal_write_performed") is True
        or decision.get("production_mutation_performed") is True
        or decision.get("authority_write_performed") is True
    )


def _live_object_authority_replacement_current_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    replacement = evidence.get("production_authority_replacement_current")
    replacement = replacement if isinstance(replacement, Mapping) else {}
    if not replacement:
        return {
            "claim_id": "live.production.object_authority_replacement_current",
            "evidence_class": "runtime_safety_gate",
            "status": "not_validated",
            "production_mutation_performed": False,
            "gaps": [],
        }
    approval = replacement.get("approval") if isinstance(replacement.get("approval"), Mapping) else {}
    prior = replacement.get("prior_current") if isinstance(replacement.get("prior_current"), Mapping) else {}
    successor = (
        replacement.get("successor_current")
        if isinstance(replacement.get("successor_current"), Mapping)
        else {}
    )
    read_after_write = (
        replacement.get("read_after_write")
        if isinstance(replacement.get("read_after_write"), Mapping)
        else {}
    )
    postcheck = replacement.get("postcheck") if isinstance(replacement.get("postcheck"), Mapping) else {}
    scope = replacement.get("scope") if isinstance(replacement.get("scope"), Mapping) else {}
    approval_ref_hash = public_safe_text(str(approval.get("approval_ref_hash") or ""), max_chars=120)
    prior_target = public_safe_text(str(prior.get("target_object_id") or ""), max_chars=180)
    successor_target = public_safe_text(str(successor.get("target_object_id") or ""), max_chars=180)
    prior_decision_id = public_safe_text(str(prior.get("decision_id") or ""), max_chars=180)
    successor_decision_id = public_safe_text(str(successor.get("decision_id") or ""), max_chars=180)
    object_ids = _string_list(scope.get("object_ids"))
    replacement_path = _string_list(replacement.get("replacement_path"))
    allowed_object_classes = set(_string_list(scope.get("allowed_object_classes")))
    failures = _replacement_current_failures(
        replacement=replacement,
        approval=approval,
        prior=prior,
        successor=successor,
        read_after_write=read_after_write,
        postcheck=postcheck,
        scope=scope,
        approval_ref_hash=approval_ref_hash,
        prior_target=prior_target,
        successor_target=successor_target,
        prior_decision_id=prior_decision_id,
        successor_decision_id=successor_decision_id,
        object_ids=object_ids,
        replacement_path=replacement_path,
        allowed_object_classes=allowed_object_classes,
    )
    return {
        "claim_id": "live.production.object_authority_replacement_current",
        "evidence_class": "runtime_safety_gate",
        "status": "failed" if failures else "validated",
        "schema_version": public_safe_text(str(replacement.get("schema_version") or ""), max_chars=80),
        "prior_target_object_id": prior_target,
        "successor_target_object_id": successor_target,
        "prior_authority_lane": public_safe_text(str(prior.get("new_authority_lane") or ""), max_chars=80),
        "successor_authority_lane": public_safe_text(str(successor.get("new_authority_lane") or ""), max_chars=80),
        "read_after_write_status": public_safe_text(str(read_after_write.get("status") or ""), max_chars=80),
        "postcheck_status": public_safe_text(str(postcheck.get("status") or ""), max_chars=80),
        "object_count": len(object_ids),
        "production_mutation_performed": _replacement_current_reports_mutation(replacement),
        "gaps": failures,
    }


def _replacement_current_failures(
    *,
    replacement: Mapping[str, Any],
    approval: Mapping[str, Any],
    prior: Mapping[str, Any],
    successor: Mapping[str, Any],
    read_after_write: Mapping[str, Any],
    postcheck: Mapping[str, Any],
    scope: Mapping[str, Any],
    approval_ref_hash: str,
    prior_target: str,
    successor_target: str,
    prior_decision_id: str,
    successor_decision_id: str,
    object_ids: list[str],
    replacement_path: list[str],
    allowed_object_classes: set[str],
) -> list[str]:
    failures: list[str] = []
    if replacement.get("schema_version") != "object_authority_replacement_current_evidence.v1":
        failures.append("replacement_current_schema_mismatch")
    if approval.get("approved") is not True:
        failures.append("replacement_approval_missing")
    if not _is_sha256_hash_ref(approval_ref_hash):
        failures.append("replacement_approval_ref_hash_missing")
    if str(approval.get("scope") or "") != "single_project_replacement_current":
        failures.append("replacement_scope_not_single_project_replacement_current")
    approval_project = str(approval.get("project") or "")
    scope_project = str(scope.get("project") or "")
    if not approval_project:
        failures.append("replacement_approval_project_missing")
    if not scope_project:
        failures.append("replacement_scope_project_missing")
    elif approval_project != scope_project:
        failures.append("replacement_project_mismatch")
    if _int_value(approval.get("max_objects")) != 2 or _int_value(scope.get("max_objects")) != 2:
        failures.append("replacement_max_objects_not_two")
    if len(object_ids) != 2:
        failures.append("replacement_object_count_not_two")
    if not prior_target or not successor_target or prior_target == successor_target:
        failures.append("replacement_target_pair_invalid")
    if prior_target and prior_target not in object_ids:
        failures.append("replacement_prior_target_not_in_scope")
    if successor_target and successor_target not in object_ids:
        failures.append("replacement_successor_target_not_in_scope")
    if any(target and not target.startswith("ko:RepoDocument:") for target in (prior_target, successor_target)):
        failures.append("replacement_object_class_not_allowed")
    if "RepoDocument" not in allowed_object_classes:
        failures.append("replacement_allowed_object_class_missing")
    if prior.get("proposal_write_performed") is not True or successor.get("proposal_write_performed") is not True:
        failures.append("replacement_proposal_write_missing")
    if prior.get("proposal_write_target") != "production_ledger" or successor.get("proposal_write_target") != "production_ledger":
        failures.append("replacement_proposal_target_not_production")
    if prior.get("ledger_scope") != "production" or successor.get("ledger_scope") != "production":
        failures.append("replacement_ledger_scope_not_production")
    if prior.get("authority_write_scope") != "production_ledger" or successor.get("authority_write_scope") != "production_ledger":
        failures.append("replacement_decision_scope_not_production")
    if prior.get("production_gate_ref_hash") != approval_ref_hash or successor.get("production_gate_ref_hash") != approval_ref_hash:
        failures.append("replacement_gate_hash_mismatch")
    if prior.get("decision_type") != "commit_supersession":
        failures.append("replacement_prior_decision_not_supersession")
    if prior.get("previous_authority_lane") != "accepted_current" or prior.get("new_authority_lane") not in {
        "accepted_non_current",
        "archive_only",
    }:
        failures.append("replacement_prior_not_demoted")
    if successor.get("decision_type") != "accept_current":
        failures.append("replacement_successor_decision_not_accept_current")
    if successor.get("new_authority_lane") != "accepted_current":
        failures.append("replacement_successor_not_current")
    lineage_valid = (
        successor.get("supersedes_decision_id") == prior_decision_id
        or prior.get("supersedes_decision_id") == successor_decision_id
    )
    if not lineage_valid:
        failures.append("replacement_successor_lineage_missing")
    if prior.get("authority_write_performed") is not True or successor.get("authority_write_performed") is not True:
        failures.append("replacement_decision_write_missing")
    if prior.get("authoritative_memory_changed") is not True or successor.get("authoritative_memory_changed") is not True:
        failures.append("replacement_authoritative_memory_not_changed")
    if read_after_write.get("status") != "validated":
        failures.append("replacement_read_after_write_missing")
    if read_after_write.get("prior_decision_id") != prior_decision_id or read_after_write.get("successor_decision_id") != successor_decision_id:
        failures.append("replacement_read_after_write_decision_mismatch")
    if read_after_write.get("prior_authority_lane") not in {"accepted_non_current", "archive_only"}:
        failures.append("replacement_read_after_write_prior_not_demoted")
    if read_after_write.get("successor_authority_lane") != "accepted_current":
        failures.append("replacement_read_after_write_successor_not_current")
    if "demote_prior_object_to_accepted_non_current_or_archive_only" not in replacement_path:
        failures.append("replacement_demote_prior_object_step_missing")
    if "promote_successor_object_to_accepted_current" not in replacement_path:
        failures.append("replacement_promote_successor_step_missing")
    if postcheck.get("status") != "validated":
        failures.append("replacement_postcheck_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "replacement_raw_private_evidence_returned"),
        ("secret_returned", "replacement_secret_returned"),
        ("host_topology_returned", "replacement_host_topology_returned"),
        ("raw_external_ids_returned", "replacement_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return _dedupe(failures)


def _replacement_current_reports_mutation(replacement: Mapping[str, Any]) -> bool:
    prior = replacement.get("prior_current") if isinstance(replacement.get("prior_current"), Mapping) else {}
    successor = (
        replacement.get("successor_current")
        if isinstance(replacement.get("successor_current"), Mapping)
        else {}
    )
    return (
        prior.get("production_mutation_performed") is True
        or prior.get("proposal_write_performed") is True
        or prior.get("authority_write_performed") is True
        or successor.get("production_mutation_performed") is True
        or successor.get("proposal_write_performed") is True
        or successor.get("authority_write_performed") is True
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
    allowed_safe_targets = ALLOWED_AGENT_CONTEXT_TOOL_SAFE_TARGETS.get(tool_name, frozenset())
    if allowed_safe_targets and any(target not in allowed_safe_targets for target in safe_targets):
        failures.append(f"{tool_name}_tool_hint_safe_targets_not_allowed")
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


def _section_authority_lanes(section: Any) -> list[str]:
    if not isinstance(section, Mapping):
        return []
    return _string_list(section.get("authority_lanes"))


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


def _reject_forbidden_runtime_evidence_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_sensitive_key(key)
            compact = normalized.replace("_", "")
            if (
                normalized in _RUNTIME_EVIDENCE_FORBIDDEN_KEYS
                or compact in _RUNTIME_EVIDENCE_FORBIDDEN_COMPACT_KEYS
                or (
                    normalized.endswith("s")
                    and (
                        normalized[:-1] in _RUNTIME_EVIDENCE_FORBIDDEN_KEYS
                        or compact[:-1] in _RUNTIME_EVIDENCE_FORBIDDEN_COMPACT_KEYS
                    )
                )
            ):
                raise ValueError("runtime evidence contains a forbidden field")
            _reject_forbidden_runtime_evidence_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_forbidden_runtime_evidence_keys(child)


def _normalized_sensitive_key(value: Any) -> str:
    decoded = _fully_unquote(str(value).strip())
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", decoded)
    return re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").casefold()


def _fully_unquote(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


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

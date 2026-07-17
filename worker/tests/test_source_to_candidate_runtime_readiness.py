from __future__ import annotations

import copy
import json

import pytest

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_deployment_evidence_binding,
    build_source_to_candidate_runtime_evidence_collection_plan,
    build_source_to_candidate_runtime_evidence_packet_template,
    build_source_to_candidate_runtime_collected_shadow_evidence_packet,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
    build_source_to_candidate_runtime_readiness_report,
    build_source_to_candidate_runtime_shadow_readiness_report,
    build_source_to_candidate_runtime_shadow_evidence_packet,
    build_preference_artifact_memory_runtime_evidence,
    build_temporal_recall_corrective_checkpoint_readiness_report,
)
from agent_knowledge.public_safe_util import hash_payload
from agent_knowledge.permission_audit_contract import (
    build_permission_audit_operation_hash,
)


_BOUND_SOURCE_COMMIT = "c" * 40


def _sanitized_live_evidence(**overrides):
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "current_authority": {"object_count": 1, "authority_lanes": ["accepted_current"]},
                "style_preference": {"object_count": 1, "authority_lanes": ["accepted_current"]},
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
        "temporal_recall_corrective_checkpoint": _temporal_recall_corrective_checkpoint(),
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


def _gitops_bound_live_evidence(**overrides):
    expected_commit = _BOUND_SOURCE_COMMIT
    evidence = _sanitized_live_evidence(
        expected_commit=expected_commit,
        gitops_desired_state={
            "schema_version": "gitops_desired_state_identity.v1",
            "images_include_expected_commit": True,
            "desired_state_source": "sanitized_ops_manifest_summary",
            "target_revision": "main",
            "source_commit": expected_commit,
            "desired_image_set_hash": "sha256:" + "a" * 64,
            "ops_revision": "a" * 40,
            "expected_image_ref_count": 1,
            "production_mutation_performed": False,
        },
        deployed_identity={
            "contains_expected_commit": True,
            "identity_source": "redacted_live_runtime_evidence",
            "source_commit": expected_commit,
            "live_image_set_hash": "sha256:" + "a" * 64,
            "stale_image_ref_count": 0,
            "production_mutation_performed": False,
        },
        argo_reconciliation={
            "schema_version": "argo_reconciliation_identity.v1",
            "reconciliation_source": "sanitized_argo_application_summary",
            "reconciled_ops_revision": "a" * 40,
            "sync_status": "Synced",
            "health_status": "Healthy",
            "production_mutation_performed": False,
        },
    )
    evidence.update(overrides)
    evidence.setdefault(
        "deployment_evidence_binding",
        build_deployment_evidence_binding(
            expected_commit=expected_commit,
            gitops_desired_state=evidence["gitops_desired_state"],
            argo_reconciliation=evidence["argo_reconciliation"],
            deployed_identity=evidence["deployed_identity"],
        ),
    )
    return evidence


def test_runtime_readiness_normalizes_and_validates_gitops_deployment_evidence_binding():
    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=_gitops_bound_live_evidence(),
    )

    binding = packet["deployment_evidence_binding"]
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}

    assert packet["expected_commit"] == _BOUND_SOURCE_COMMIT
    assert binding["schema_version"] == "deployment_evidence_binding.v1"
    assert binding["canonical_tuple_hash"].startswith("sha256:")
    assert claims["ops.gitops_deployment_evidence_binding"]["status"] == "validated"


def test_deployment_evidence_binding_v1_preserves_golden_tuple_hash_and_claim_id():
    evidence = _gitops_bound_live_evidence()
    canonical_tuple = {
        "expected_commit": _BOUND_SOURCE_COMMIT,
        "desired_source_commit": _BOUND_SOURCE_COMMIT,
        "deployed_source_commit": _BOUND_SOURCE_COMMIT,
        "desired_image_set_hash": "sha256:" + "a" * 64,
        "live_image_set_hash": "sha256:" + "a" * 64,
        "ops_revision": "a" * 40,
        "reconciled_ops_revision": "a" * 40,
        "sync_status": "Synced",
        "health_status": "Healthy",
        "expected_image_ref_count": 1,
        "stale_image_ref_count": 0,
        "desired_production_mutation_performed": False,
        "argo_production_mutation_performed": False,
        "deployed_production_mutation_performed": False,
    }

    assert set(canonical_tuple) == {
        "expected_commit",
        "desired_source_commit",
        "deployed_source_commit",
        "desired_image_set_hash",
        "live_image_set_hash",
        "ops_revision",
        "reconciled_ops_revision",
        "sync_status",
        "health_status",
        "expected_image_ref_count",
        "stale_image_ref_count",
        "desired_production_mutation_performed",
        "argo_production_mutation_performed",
        "deployed_production_mutation_performed",
    }
    assert hash_payload(canonical_tuple) == (
        "sha256:dc12138cb1402b37a74a8efca884c229541ab47ca3954c0dbf907a203bebf0ba"
    )
    assert evidence["deployment_evidence_binding"] == {
        "schema_version": "deployment_evidence_binding.v1",
        "canonical_tuple_hash": (
            "sha256:dc12138cb1402b37a74a8efca884c229541ab47ca3954c0dbf907a203bebf0ba"
        ),
    }

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )
    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "ops.gitops_deployment_evidence_binding"
    )
    assert claim["claim_id"] == "ops.gitops_deployment_evidence_binding"
    assert claim["status"] == "validated"


def test_runtime_readiness_normalizer_does_not_mint_missing_deployment_binding():
    capture = _gitops_bound_live_evidence()
    capture.pop("deployment_evidence_binding")

    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=capture,
    )

    assert packet["deployment_evidence_binding"] == {}


@pytest.mark.parametrize(
    ("layer", "field", "value", "expected_gap"),
    [
        ("gitops_desired_state", "desired_image_set_hash", "", "gitops_desired_state_image_set_hash_invalid"),
        ("gitops_desired_state", "desired_image_set_hash", "sha256:not-a-digest", "gitops_desired_state_image_set_hash_invalid"),
        ("argo_reconciliation", "reconciled_ops_revision", "", "argo_reconciliation_revision_invalid"),
        ("gitops_desired_state", "expected_image_ref_count", None, "gitops_desired_state_expected_image_ref_count_invalid"),
        ("gitops_desired_state", "expected_image_ref_count", "1", "gitops_desired_state_expected_image_ref_count_invalid"),
        ("gitops_desired_state", "expected_image_ref_count", True, "gitops_desired_state_expected_image_ref_count_invalid"),
        ("deployed_identity", "stale_image_ref_count", None, "live_deployed_identity_stale_image_ref_count_invalid"),
        ("deployed_identity", "stale_image_ref_count", "0", "live_deployed_identity_stale_image_ref_count_invalid"),
        ("deployed_identity", "stale_image_ref_count", False, "live_deployed_identity_stale_image_ref_count_invalid"),
        ("gitops_desired_state", "production_mutation_performed", None, "gitops_desired_state_mutation_invalid"),
        ("argo_reconciliation", "production_mutation_performed", 1, "argo_reconciliation_mutation_invalid"),
        ("deployed_identity", "production_mutation_performed", "true", "live_deployed_identity_mutation_invalid"),
    ],
)
def test_runtime_readiness_fails_closed_for_malformed_bound_layer_values(
    layer, field, value, expected_gap
):
    evidence = _gitops_bound_live_evidence()
    evidence[layer][field] = value
    evidence["deployment_evidence_binding"] = build_deployment_evidence_binding(
        expected_commit=evidence["expected_commit"],
        gitops_desired_state=evidence["gitops_desired_state"],
        argo_reconciliation=evidence["argo_reconciliation"],
        deployed_identity=evidence["deployed_identity"],
    )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert "ops.gitops_deployment_evidence_binding" in report["failed_claims"]
    assert expected_gap in report["gaps"]


@pytest.mark.parametrize("key", ["image", "images", "image_ref", "image_refs", "manifest_path", "raw_manifest", "docker_image", "unknown_field"])
def test_runtime_readiness_rejects_unknown_or_raw_binding_layer_keys(key):
    evidence = _gitops_bound_live_evidence()
    evidence["gitops_desired_state"][key] = "redacted"

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert "ops.gitops_deployment_evidence_binding" in report["failed_claims"]


@pytest.mark.parametrize(
    "key",
    ["image", "images", "image_ref", "image_refs", "manifest_path", "raw_manifest", "docker_image"],
)
def test_runtime_readiness_normalizer_rejects_raw_deployment_layer_keys(key):
    capture = _gitops_bound_live_evidence()
    capture.pop("deployment_evidence_binding")
    capture["gitops_desired_state"][key] = "private/image:tag"

    with pytest.raises(ValueError, match="forbidden field"):
        build_source_to_candidate_runtime_post_deploy_capture_packet(
            captured_evidence=capture,
        )


@pytest.mark.parametrize(
    ("field", "value", "failed_claim"),
    [
        ("sync_status", "OutOfSync", "ops.argo_reconciliation.application_status"),
        ("health_status", "Degraded", "ops.argo_reconciliation.application_status"),
        ("production_mutation_performed", 1, "ops.argo_reconciliation.application_status"),
    ],
)
def test_runtime_readiness_fails_for_supplied_invalid_argo_without_binding(
    field, value, failed_claim
):
    evidence = _gitops_bound_live_evidence()
    evidence.pop("deployment_evidence_binding")
    evidence["argo_reconciliation"][field] = value

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert failed_claim in report["failed_claims"]


def test_runtime_readiness_requires_external_expected_commit_anchor_for_binding_validation():
    evidence = _gitops_bound_live_evidence()

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "PASS_WITH_GAPS"
    claim = next(item for item in report["claims"] if item["claim_id"] == "ops.gitops_deployment_evidence_binding")
    assert claim["status"] == "not_validated"
    assert "external_expected_commit_anchor_unverified" in claim["gaps"]


def test_runtime_readiness_rejects_mismatched_external_expected_commit_anchor():
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(),
        expected_commit="b" * 40,
    )

    assert report["status"] == "FAIL"
    assert "gitops_deployment_evidence_binding_external_expected_commit_mismatch" in report["gaps"]


def test_runtime_readiness_rejects_all_zero_production_commit_anchor():
    zero_commit = "0" * 40
    evidence = _gitops_bound_live_evidence()
    evidence["expected_commit"] = zero_commit
    evidence["gitops_desired_state"]["source_commit"] = zero_commit
    evidence["deployed_identity"]["source_commit"] = zero_commit
    evidence["deployment_evidence_binding"] = build_deployment_evidence_binding(
        expected_commit=zero_commit,
        gitops_desired_state=evidence["gitops_desired_state"],
        argo_reconciliation=evidence["argo_reconciliation"],
        deployed_identity=evidence["deployed_identity"],
    )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=zero_commit,
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "ops.gitops_deployment_evidence_binding"
    )
    assert report["status"] == "FAIL"
    assert claim["status"] == "failed"
    assert "external_expected_commit_anchor_invalid" in claim["gaps"]
    assert (
        "gitops_deployment_evidence_binding_packet_expected_commit_invalid"
        in claim["gaps"]
    )


@pytest.mark.parametrize(
    ("layer", "field", "value", "failed_claim"),
    [
        ("gitops_desired_state", "source_commit", "b" * 40, "ops.gitops_desired_state.includes_expected_commit"),
        ("deployed_identity", "source_commit", "b" * 40, "live.deployed_identity.includes_expected_commit"),
        ("deployed_identity", "live_image_set_hash", "sha256:" + "b" * 64, "ops.gitops_deployment_evidence_binding"),
        ("argo_reconciliation", "reconciled_ops_revision", "b" * 40, "ops.gitops_deployment_evidence_binding"),
    ],
)
def test_runtime_readiness_fails_for_supplied_cross_layer_mismatch_without_binding(
    layer, field, value, failed_claim
):
    evidence = _gitops_bound_live_evidence()
    evidence.pop("deployment_evidence_binding")
    evidence[layer][field] = value

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert failed_claim in report["failed_claims"]


def test_runtime_readiness_rejects_gitops_raw_manifest_and_image_refs():
    capture = _gitops_bound_live_evidence(
        gitops_desired_state={
            **_gitops_bound_live_evidence()["gitops_desired_state"],
            "manifest": "private manifest",
            "image_refs": ["private/image:tag"],
        }
    )

    with pytest.raises(ValueError, match="forbidden field"):
        build_source_to_candidate_runtime_post_deploy_capture_packet(
            captured_evidence=capture,
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_gap"),
    [
        ("desired_commit", "other-commit", "gitops_deployment_evidence_binding_desired_commit_mismatch"),
        ("image_set", "sha256:" + "b" * 64, "gitops_deployment_evidence_binding_image_set_hash_mismatch"),
        ("revision", "ops-43", "gitops_deployment_evidence_binding_ops_revision_mismatch"),
        ("sync", "OutOfSync", "gitops_deployment_evidence_binding_sync_status_mismatch"),
        ("health", "Degraded", "gitops_deployment_evidence_binding_health_status_mismatch"),
        ("binding_hash", "sha256:" + "b" * 64, "gitops_deployment_evidence_binding_hash_mismatch"),
        ("stale_ref_count", 1, "gitops_deployment_evidence_binding_stale_image_ref_count_mismatch"),
        ("desired_mutation", True, "gitops_deployment_evidence_binding_desired_state_mutation"),
        ("deployed_mutation", True, "gitops_deployment_evidence_binding_deployed_identity_mutation"),
    ],
)
def test_runtime_readiness_fails_closed_for_gitops_deployment_binding_mismatch(
    field, value, expected_gap
):
    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=_gitops_bound_live_evidence(),
    )
    if field == "desired_commit":
        packet["gitops_desired_state"]["source_commit"] = value
    elif field == "image_set":
        packet["deployed_identity"]["live_image_set_hash"] = value
    elif field == "revision":
        packet["argo_reconciliation"]["reconciled_ops_revision"] = value
    elif field == "sync":
        packet["argo_reconciliation"]["sync_status"] = value
    elif field == "health":
        packet["argo_reconciliation"]["health_status"] = value
    elif field == "binding_hash":
        packet["deployment_evidence_binding"]["canonical_tuple_hash"] = value
    elif field == "stale_ref_count":
        packet["deployed_identity"]["stale_image_ref_count"] = value
    elif field == "desired_mutation":
        packet["gitops_desired_state"]["production_mutation_performed"] = value
    else:
        packet["deployed_identity"]["production_mutation_performed"] = value

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert "ops.gitops_deployment_evidence_binding" in report["failed_claims"]
    assert expected_gap in report["gaps"]


def test_runtime_readiness_keeps_missing_gitops_deployment_binding_as_gap():
    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=_gitops_bound_live_evidence(),
    )
    packet.pop("deployment_evidence_binding")
    packet["evidence_provenance"]["mutation_scope"] = "none"

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "PASS_WITH_GAPS"
    assert "ops.gitops_deployment_evidence_binding" not in report["failed_claims"]
    assert "gitops_deployment_evidence_binding_unverified" in report["gaps"]


def test_runtime_readiness_rejects_mutable_ref_as_deployment_commit_anchor():
    evidence = _gitops_bound_live_evidence()
    evidence["expected_commit"] = "main"
    evidence["gitops_desired_state"]["source_commit"] = "main"
    evidence["deployed_identity"]["source_commit"] = "main"
    evidence["deployment_evidence_binding"] = build_deployment_evidence_binding(
        expected_commit="main",
        gitops_desired_state=evidence["gitops_desired_state"],
        argo_reconciliation=evidence["argo_reconciliation"],
        deployed_identity=evidence["deployed_identity"],
    )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit="main",
    )

    assert report["status"] == "FAIL"
    assert "ops.gitops_deployment_evidence_binding" in report["failed_claims"]
    assert "gitops_deployment_evidence_binding_packet_expected_commit_invalid" in report["gaps"]


@pytest.mark.parametrize(
    ("layer", "failed_claim"),
    [
        ("gitops_desired_state", "ops.gitops_desired_state.includes_expected_commit"),
        ("argo_reconciliation", "ops.argo_reconciliation.application_status"),
        ("deployed_identity", "live.deployed_identity.includes_expected_commit"),
        ("deployment_evidence_binding", "ops.gitops_deployment_evidence_binding"),
    ],
)
@pytest.mark.parametrize("bad_value", ["tampered", ["tampered"], None])
def test_runtime_readiness_fails_for_supplied_non_mapping_deployment_layer(
    layer, failed_claim, bad_value
):
    evidence = _gitops_bound_live_evidence()
    evidence.pop("deployment_evidence_binding")
    evidence[layer] = bad_value

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert failed_claim in report["failed_claims"]


@pytest.mark.parametrize(
    ("layer", "failed_claim"),
    [
        ("gitops_desired_state", "ops.gitops_desired_state.includes_expected_commit"),
        ("argo_reconciliation", "ops.argo_reconciliation.application_status"),
        ("deployed_identity", "live.deployed_identity.includes_expected_commit"),
        ("deployment_evidence_binding", "ops.gitops_deployment_evidence_binding"),
    ],
)
@pytest.mark.parametrize("bad_value", ["tampered", ["tampered"], None])
def test_runtime_readiness_normalizer_preserves_non_mapping_layer_failure(
    layer, failed_claim, bad_value
):
    capture = _gitops_bound_live_evidence()
    capture.pop("deployment_evidence_binding")
    capture[layer] = bad_value

    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=capture,
    )
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert failed_claim in report["failed_claims"]


@pytest.mark.parametrize(
    ("layer", "value", "failed_claim"),
    [
        (
            "gitops_desired_state",
            {
                "schema_version": "gitops_desired_state_identity.v1",
                "unknown_field": True,
            },
            "ops.gitops_desired_state.includes_expected_commit",
        ),
        (
            "deployed_identity",
            {
                "contains_expected_commit": True,
                "identity_source": "redacted_live_runtime_evidence",
                "manifest_path": "redacted",
            },
            "live.deployed_identity.includes_expected_commit",
        ),
    ],
)
def test_runtime_readiness_fails_for_unknown_field_without_strict_fields(
    layer, value, failed_claim
):
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        layer: value,
    }

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=evidence,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    assert report["status"] == "FAIL"
    assert failed_claim in report["failed_claims"]


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


def _temporal_recall_corrective_checkpoint(**overrides):
    expected_a = "sha256:" + "a" * 64
    expected_b = "sha256:" + "b" * 64
    identity_a = "sha256:" + "d" * 64
    identity_b = "sha256:" + "e" * 64
    checkpoint = {
        "schema_version": "temporal_recall_corrective_checkpoint.v1",
        "evidence_class": "runtime_semantic_acceptance",
        "temporal_query_hash": "sha256:" + "0" * 64,
        "selector_contract": {
            "as_of_supported": True,
            "date_range_supported": True,
            "invalid_range_rejected": True,
            "invalid_range_error_type": "McpToolError",
            "invalid_range_error_code": -32602,
        },
        "date_a": {
            "selector_hash": "sha256:" + "1" * 64,
            "expected_object_fingerprint": expected_a,
            "observed_object_fingerprint": expected_a,
            "expected_object_identity_fingerprint": identity_a,
            "observed_object_identity_fingerprint": identity_a,
            "work_unit_count": 1,
            "gap_count": 0,
            "confidence_score": 0.9,
        },
        "date_b": {
            "selector_hash": "sha256:" + "2" * 64,
            "expected_object_fingerprint": expected_b,
            "observed_object_fingerprint": expected_b,
            "expected_object_identity_fingerprint": identity_b,
            "observed_object_identity_fingerprint": identity_b,
            "work_unit_count": 1,
            "gap_count": 0,
            "confidence_score": 0.9,
        },
        "range_boundary": {
            "selector_hash": "sha256:" + "3" * 64,
            "expected_object_fingerprint": expected_a,
            "observed_object_fingerprint": expected_a,
            "expected_object_identity_fingerprint": identity_a,
            "observed_object_identity_fingerprint": identity_a,
            "work_unit_count": 1,
            "gap_count": 0,
            "confidence_score": 0.9,
        },
        "mismatch": {
            "selector_hash": "sha256:" + "4" * 64,
            "object_count": 0,
            "gap_count": 1,
            "confidence_score": 0.0,
        },
        "nonsense_query": {
            "query_hash": "sha256:" + "5" * 64,
            "result_count": 0,
            "current_count": 0,
            "accepted_count": 0,
            "semantic_ranker_bound": True,
            "semantic_ranker_used": True,
        },
        "semantic_query": {
            "query_hash": "sha256:" + "6" * 64,
            "expected_result_fingerprint": "sha256:" + "c" * 64,
            "observed_result_fingerprint": "sha256:" + "c" * 64,
            "result_count": 1,
            "why_retrieved_semantic_match": True,
            "score": 0.91,
            "minimum_score": 0.75,
            "semantic_ranker_bound": True,
            "semantic_ranker_used": True,
            "qdrant_semantic_result_lane_used": True,
        },
        "runtime_aggregate": {
            "schema_version": "temporal_correctness_runtime_aggregate.v1",
            "projection_currentness": {
                "source_hash_match": True,
                "source_state_digest": "sha256:" + "1" * 64,
                "graph_projection_state_digest": "sha256:" + "2" * 64,
                "session_memory_projection_state_digest": "sha256:" + "3" * 64,
                "source_projection_state_digest": "sha256:" + "4" * 64,
                "source_hash_mismatch_count": 0,
                "stale_projected_session_count": 0,
                "source_session_count": 126,
                "minimum_source_session_count": 126,
                "graph_projection_current_count": 126,
                "graph_projection_noncurrent_count": 0,
                "session_memory_projection_current_count": 126,
                "session_memory_projection_noncurrent_count": 0,
                "session_memory_source_hash_mismatch_count": 0,
                "session_memory_stale_projected_session_count": 0,
                "artifact_current": True,
                "artifact_missing_session_count": 0,
                "artifact_age_unknown_count": 0,
                "artifact_source_hash_mismatch_count": 0,
                "oldest_artifact_age_seconds": 60,
                "graph_run_scope_match": True,
                "graph_run_fresh": True,
                "graph_run_completed_age_seconds": 5,
                "graph_run_max_age_seconds": 900,
            },
            "entity_projection": {
                "valid_source_count": 126,
                "minimum_valid_source_count": 126,
                "baseline_coverage_count": 15,
                "coverage_count": 16,
                "baseline_backlog_count": 111,
                "backlog_count": 110,
                "error_count": 0,
            },
        },
        "runtime_aggregate_source": "live_mcp_runtime_packet",
        "runtime_postcheck_receipt_hash": "",
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    checkpoint.update(overrides)
    return checkpoint


def test_runtime_readiness_requires_temporal_corrective_checkpoint_before_semantic_validation():
    evidence = _sanitized_live_evidence()
    evidence.pop("temporal_recall_corrective_checkpoint")

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    checkpoint = claims["live.temporal_recall.corrective_checkpoint"]
    route_smokes = claims["live.brain_objects_query.route_smokes"]
    assert checkpoint["status"] == "not_validated"
    assert route_smokes["status"] == "not_validated"
    assert "temporal_work_recall" not in route_smokes["validated_routes"]
    assert "live_temporal_recall_corrective_checkpoint_unverified" in report["gaps"]


@pytest.mark.parametrize(
    "digest_field",
    (
        "source_state_digest",
        "graph_projection_state_digest",
        "session_memory_projection_state_digest",
        "source_projection_state_digest",
    ),
)
def test_temporal_checkpoint_readiness_requires_each_projection_state_digest(
    digest_field,
):
    checkpoint = _temporal_recall_corrective_checkpoint()
    checkpoint["runtime_aggregate"]["projection_currentness"][digest_field] = ""

    report = build_temporal_recall_corrective_checkpoint_readiness_report(
        checkpoint=checkpoint
    )

    assert report["status"] == "FAIL"
    assert report["failed_claims"] == [
        "live.temporal_recall.corrective_checkpoint"
    ]
    assert "temporal_corrective_projection_state_digest_invalid" in report["gaps"]


def test_runtime_readiness_rejects_positive_temporal_probe_with_gap() -> None:
    checkpoint = _temporal_recall_corrective_checkpoint()
    checkpoint["date_a"] = {**checkpoint["date_a"], "gap_count": 1}
    evidence = _sanitized_live_evidence(
        temporal_recall_corrective_checkpoint=checkpoint
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.temporal_recall.corrective_checkpoint"
    )
    assert claim["status"] == "failed"
    assert "temporal_corrective_date_a_not_fail_closed" in claim["gaps"]


def test_runtime_readiness_rejects_internal_error_as_invalid_range_proof() -> None:
    checkpoint = _temporal_recall_corrective_checkpoint()
    checkpoint["selector_contract"] = {
        **checkpoint["selector_contract"],
        "invalid_range_error_type": "McpError",
        "invalid_range_error_code": -32603,
    }
    evidence = _sanitized_live_evidence(
        temporal_recall_corrective_checkpoint=checkpoint
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.temporal_recall.corrective_checkpoint"
    )
    assert claim["status"] == "failed"
    assert "temporal_corrective_invalid_range_error_code_unexpected" in claim["gaps"]


def test_runtime_readiness_validates_complete_temporal_corrective_checkpoint():
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence()
    )

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    checkpoint = claims["live.temporal_recall.corrective_checkpoint"]
    route_smokes = claims["live.brain_objects_query.route_smokes"]
    assert checkpoint["status"] == "validated"
    assert checkpoint["date_ab_distinct"] is True
    assert checkpoint["hash_currentness_validated"] is True
    assert checkpoint["entity_aggregate_improved"] is True
    assert checkpoint["qdrant_semantic_result_lane_used"] is True
    assert route_smokes["status"] == "validated"
    assert "temporal_work_recall" in route_smokes["validated_routes"]


@pytest.mark.parametrize(
    ("mutation", "expected_gap"),
    [
        (
            lambda checkpoint: checkpoint["date_b"].update(
                expected_object_fingerprint=checkpoint["date_a"]["expected_object_fingerprint"],
                observed_object_fingerprint=checkpoint["date_a"]["expected_object_fingerprint"],
            ),
            "temporal_corrective_date_fingerprints_not_distinct",
        ),
        (
            lambda checkpoint: checkpoint["date_b"].update(
                expected_object_identity_fingerprint=checkpoint["date_a"][
                    "expected_object_identity_fingerprint"
                ],
                observed_object_identity_fingerprint=checkpoint["date_a"][
                    "observed_object_identity_fingerprint"
                ],
            ),
            "temporal_corrective_date_identities_not_distinct",
        ),
        (
            lambda checkpoint: checkpoint["date_b"].update(
                selector_hash=checkpoint["date_a"]["selector_hash"],
            ),
            "temporal_corrective_date_selectors_not_distinct",
        ),
        (
            lambda checkpoint: checkpoint["range_boundary"].update(
                expected_object_fingerprint="not-a-fingerprint",
                observed_object_fingerprint="not-a-fingerprint",
            ),
            "temporal_corrective_range_boundary_failed",
        ),
        (
            lambda checkpoint: checkpoint["mismatch"].update(object_count=1, gap_count=0),
            "temporal_corrective_mismatch_not_fail_closed",
        ),
        (
            lambda checkpoint: checkpoint["nonsense_query"].update(
                result_count=1,
                semantic_ranker_used=False,
            ),
            "temporal_corrective_nonsense_query_not_empty",
        ),
        (
            lambda checkpoint: checkpoint.pop("semantic_query"),
            "temporal_corrective_semantic_query_missing",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(result_count=2),
            "temporal_corrective_semantic_result_count_invalid",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(
                observed_result_fingerprint="sha256:" + "d" * 64,
            ),
            "temporal_corrective_semantic_result_fingerprint_mismatch",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(
                why_retrieved_semantic_match=False,
            ),
            "temporal_corrective_semantic_result_reason_invalid",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(score=0.74),
            "temporal_corrective_semantic_result_score_below_threshold",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(score=float("nan")),
            "temporal_corrective_semantic_result_score_below_threshold",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(
                qdrant_semantic_result_lane_used=False,
            ),
            "temporal_corrective_qdrant_semantic_result_lane_not_used",
        ),
        (
            lambda checkpoint: checkpoint["semantic_query"].update(
                semantic_ranker_used=False,
            ),
            "temporal_corrective_semantic_query_ranker_not_used",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["projection_currentness"].update(
                source_hash_match=False,
                stale_projected_session_count=1,
            ),
            "temporal_corrective_projection_hash_not_current",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["projection_currentness"].update(
                source_projection_state_digest="",
            ),
            "temporal_corrective_projection_state_digest_invalid",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["projection_currentness"].update(
                source_hash_match=False,
                session_memory_projection_current_count=125,
                session_memory_projection_noncurrent_count=1,
                session_memory_source_hash_mismatch_count=1,
                session_memory_stale_projected_session_count=1,
            ),
            "temporal_corrective_projection_hash_not_current",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["projection_currentness"].update(
                graph_projection_current_count=125,
                graph_projection_noncurrent_count=1,
            ),
            "temporal_corrective_projection_hash_not_current",
        ),
        (
            lambda checkpoint: (
                checkpoint["runtime_aggregate"]["projection_currentness"].update(
                    source_session_count=0,
                    minimum_source_session_count=0,
                    graph_projection_current_count=0,
                    graph_projection_noncurrent_count=0,
                    session_memory_projection_current_count=0,
                    session_memory_projection_noncurrent_count=0,
                ),
                checkpoint["runtime_aggregate"]["entity_projection"].update(
                    baseline_coverage_count=0,
                    coverage_count=0,
                    baseline_backlog_count=0,
                    backlog_count=0,
                    valid_source_count=0,
                    minimum_valid_source_count=0,
                ),
            ),
            "temporal_corrective_projection_hash_not_current",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["projection_currentness"].update(
                graph_run_scope_match=False,
            ),
            "temporal_corrective_projection_hash_not_current",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["projection_currentness"].update(
                graph_run_fresh=False,
                graph_run_completed_age_seconds=901,
            ),
            "temporal_corrective_projection_hash_not_current",
        ),
        (
            lambda checkpoint: checkpoint["runtime_aggregate"]["entity_projection"].update(
                coverage_count=15,
                backlog_count=111,
            ),
            "temporal_corrective_entity_aggregate_not_improved",
        ),
    ],
)
def test_runtime_readiness_fails_closed_for_incorrect_temporal_checkpoint(
    mutation,
    expected_gap,
):
    evidence = _sanitized_live_evidence()
    mutation(evidence["temporal_recall_corrective_checkpoint"])

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert report["status"] == "FAIL"
    assert claims["live.temporal_recall.corrective_checkpoint"]["status"] == "failed"
    assert expected_gap in report["gaps"]


def test_runtime_readiness_allows_live_corpus_growth_above_bounded_baseline() -> None:
    evidence = _sanitized_live_evidence()
    checkpoint = evidence["temporal_recall_corrective_checkpoint"]
    checkpoint["runtime_aggregate"]["projection_currentness"].update(
        source_session_count=127,
        graph_projection_current_count=127,
        session_memory_projection_current_count=127,
    )
    checkpoint["runtime_aggregate"]["entity_projection"].update(
        valid_source_count=127,
        coverage_count=17,
        backlog_count=110,
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.temporal_recall.corrective_checkpoint"
    )
    assert claim["status"] == "validated"
    assert claim["hash_currentness_validated"] is True
    assert claim["entity_aggregate_improved"] is True


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
            "authority_lanes": ["accepted_current"],
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


def _product_marker_evidence(*, build_association_hash):
    in_flight_statuses = (
        "clear",
        "atomic_commit_boundary",
        "atomic_commit_boundary",
        "clear",
        "atomic_commit_boundary",
    )
    markers = []
    for index, (plane, in_flight_status) in enumerate(
        zip(
            ("authority_ledger", "corpus", "queue", "index", "product_db"),
            in_flight_statuses,
            strict=True,
        ),
        start=1,
    ):
        markers.append(
            {
                "plane": plane,
                "generation_hash": "sha256:" + "a" * 64,
                "event_position_hash": "sha256:" + format(index, "x") * 64,
                "marker_hash": "sha256:" + format(index + 5, "x") * 64,
                "in_flight_count": 0,
                "in_flight_status": in_flight_status,
                "coverage_hash": "sha256:" + format(index + 10, "x") * 64,
                "coverage_status": "validated",
                "pre_post_status": "equal",
                "read_scope_status": "read_only",
            }
        )
    return {
        "schema_version": "product_mutation_marker_evidence.v1",
        "external_build_association_hash": build_association_hash,
        "marker_count": 5,
        "markers": markers,
        "reset_or_decrease_count": 0,
        "production_mutation_performed": False,
    }


def _single_bounded_denial_audit_v2(
    *,
    build_association_hash="sha256:" + "e" * 64,
    ops_revision="a" * 40,
    expected_commit=_BOUND_SOURCE_COMMIT,
    request_hash=None,
    **overrides,
):
    request_hash = request_hash or build_permission_audit_operation_hash(
        build_association_hash=build_association_hash,
        ops_revision=ops_revision,
        expected_commit=expected_commit,
    )
    evidence = {
        "schema_version": "permission_sensitive_runtime_audit_evidence.v2",
        "policy": "single_bounded_denial.v1",
        "build_association_hash": build_association_hash,
        "product_marker_evidence": _product_marker_evidence(
            build_association_hash=build_association_hash,
        ),
        "transport_call_count": 1,
        "permission_action_count": 1,
        "audit_events": [
            {
                "schema_version": "runtime_permission_audit_event.v2",
                "event_type": "permission_sensitive_runtime_action",
                "action": "single_bounded_denial.v1",
                "ledger_scope": "production",
                "permission": "denied",
                "authority_write_performed": False,
                "production_mutation_performed": False,
                "actor_ref_hash": "sha256:" + "c" * 64,
                "request_hash": request_hash,
                "protected_values_returned": False,
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            }
        ],
        "audit_store": {
            "status": "recorded",
            "append_count": 1,
            "stored_row_count": 1,
            "read_after_write_status": "validated",
            "request_hash": request_hash,
            "production_mutation_performed": False,
        },
        "postcheck": {
            "status": "validated",
            "product_mutation_markers_match": True,
            "unexpected_runtime_mutation_count": 0,
            "protected_values_returned": False,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
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
            "project": "workspace-index-advisor",
            "proposal_type": "propose_current",
            "proposal_write_performed": True,
            "proposal_write_target": "production_ledger",
            "authority_write_performed": False,
            "production_mutation_performed": True,
            "ledger_scope": "production",
            "target_object_id": target_object_id,
            "production_gate_ref_hash": approval_ref_hash,
        },
        "decision": {
            "project": "workspace-index-advisor",
            "decision_type": "reject_candidate",
            "new_authority_lane": "rejected",
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
    ]
    assert template["packet_field_templates"]["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert (
        template["packet_field_templates"]["temporal_recall_corrective_checkpoint"][
            "schema_version"
        ]
        == "temporal_recall_corrective_checkpoint.v1"
    )
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
    assert (
        "current_authority"
        in template["packet_field_templates"]["agent_context_startup_runtime"]["startup_context"][
            "required_sections"
        ]
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
    assert "product_marker_audit_unverified" in report["gaps"]
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
    assert "collect_argo_reconciliation" in plan["required_steps"]
    assert plan["gap_mapping"]["collect_argo_reconciliation"] == "argo_reconciliation_unverified"
    assert "gitops_desired_state" in template["required_packet_fields"]
    assert "argo_reconciliation" in template["required_packet_fields"]
    assert "deployment_evidence_binding" in template["required_packet_fields"]
    assert (
        template["packet_field_templates"]["gitops_desired_state"]["schema_version"]
        == "gitops_desired_state_identity.v1"
    )
    assert (
        template["packet_field_templates"]["argo_reconciliation"]["schema_version"]
        == "argo_reconciliation_identity.v1"
    )
    assert (
        template["packet_field_templates"]["deployment_evidence_binding"]["schema_version"]
        == "deployment_evidence_binding.v1"
    )
    assert "desired_image_set_hash" in template["packet_field_templates"]["gitops_desired_state"]
    assert "live_image_set_hash" in template["packet_field_templates"]["deployed_identity"]


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
    assert claims["ops.gitops_desired_state.includes_expected_commit"]["status"] == "not_validated"
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
    assert claims["ops.gitops_desired_state.includes_expected_commit"]["status"] == "failed"
    assert "ops.gitops_desired_state.includes_expected_commit" in report["failed_claims"]
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
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "not_validated"
    assert claims["live.production.permission_sensitive_audit"]["status"] == "not_validated"
    assert claims["live.evidence.provenance"]["status"] == "validated"
    assert "permission_sensitive_audit_unverified" in report["gaps"]
    assert "live_deployed_identity_unverified" not in report["gaps"]
    assert report["production_mutation_performed"] is False


def test_runtime_readiness_keeps_sanitized_live_evidence_at_p7_capability_gap():
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence(),
        expected_commit="7218cb2",
    )

    assert report["status"] == "PASS_WITH_GAPS"
    assert report["evidence_is_live"] is False
    assert report["production_ready"] is False
    assert report["production_readiness"] == "not_ready"
    assert "preference_artifact_collector_capability_missing" in report["gaps"]
    assert "gitops_deployment_evidence_binding_unverified" in report["gaps"]
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
    assert claims["live.preference_artifact.memory"]["status"] == "not_validated"
    assert claims["live.preference_artifact.memory"]["accepted_preference_count"] == 1
    assert claims["live.preference_artifact.memory"]["html_route_status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["status"] == "validated"
    assert claims["live.production.permission_sensitive_audit"]["event_count"] == 3
    assert claims["live.agent_context.startup_read_path"]["status"] == "validated"
    assert claims["live.agent_context.startup_read_path"]["startup_loaded"] is True
    assert "temporal_work_recall" in claims["live.brain_objects_query.route_smokes"]["required_routes"]
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "not_validated"
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
                "authority_lanes": [],
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
    assert "preference_artifact_agent_context_accepted_current_missing" in report["gaps"]
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


def test_runtime_readiness_validates_single_bounded_denial_v2_without_breaking_v1():
    v2_audit = _single_bounded_denial_audit_v2()
    v2_report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(
            permission_sensitive_audit=v2_audit
        ),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=v2_audit["build_association_hash"],
    )
    v1_report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence()
    )

    v2_claim = next(
        claim
        for claim in v2_report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )
    v1_claim = next(
        claim
        for claim in v1_report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert v2_claim["status"] == "validated"
    assert v2_claim["schema_version"] == "permission_sensitive_runtime_audit_evidence.v2"
    assert v2_claim["event_count"] == 1
    assert v2_claim["required_actions"] == ["single_bounded_denial.v1"]
    assert v1_claim["status"] == "validated"
    assert v1_claim["event_count"] == 3


@pytest.mark.parametrize(
    "layer",
    ["audit", "event", "store", "postcheck"],
)
def test_runtime_readiness_v2_rejects_unknown_fields(layer):
    audit = copy.deepcopy(_single_bounded_denial_audit_v2())
    targets = {
        "audit": audit,
        "event": audit["audit_events"][0],
        "store": audit["audit_store"],
        "postcheck": audit["postcheck"],
    }
    targets[layer]["unexpected_field"] = "must-fail"

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["build_association_hash"],
    )

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert "permission_sensitive_audit_v2_unexpected_field" in claim["gaps"]


def test_runtime_readiness_v2_rejects_filtered_malformed_event_entries():
    audit = copy.deepcopy(_single_bounded_denial_audit_v2())
    audit["audit_events"].append("malformed-event")

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["build_association_hash"],
    )

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert "permission_sensitive_audit_v2_event_shape_invalid" in claim["gaps"]


def test_runtime_readiness_v2_surfaces_product_marker_mismatch_as_mutation():
    audit = copy.deepcopy(_single_bounded_denial_audit_v2())
    audit["postcheck"].update(
        {
            "status": "failed",
            "product_mutation_markers_match": False,
            "unexpected_runtime_mutation_count": 1,
        }
    )
    audit["product_marker_evidence"]["markers"][0]["pre_post_status"] = "changed"

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["build_association_hash"],
    )

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert claim["production_mutation_performed"] is True
    assert "permission_sensitive_audit_v2_product_mutation_marker_mismatch" in claim["gaps"]
    assert (
        "permission_sensitive_audit_v2_product_marker_pre_post_mismatch:authority_ledger"
        in claim["gaps"]
    )
    assert "permission_sensitive_audit_v2_unexpected_runtime_mutation" in claim["gaps"]


def test_runtime_readiness_v2_duplicate_transport_result_fails_proof():
    audit = copy.deepcopy(_single_bounded_denial_audit_v2())
    audit["permission_action_count"] = 0
    audit["audit_events"] = []
    audit["audit_store"]["append_count"] = 0

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["build_association_hash"],
    )

    claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert claim["event_count"] == 0
    assert "permission_sensitive_audit_v2_permission_action_count_invalid" in claim["gaps"]
    assert "permission_sensitive_audit_v2_append_count_invalid" in claim["gaps"]


@pytest.mark.parametrize(
    ("mutation", "expected_gap"),
    [
        (
            "build_association",
            "permission_sensitive_audit_v2_external_build_association_mismatch",
        ),
        ("operation_hash", "permission_sensitive_audit_v2_operation_hash_mismatch"),
        ("transport_retry", "permission_sensitive_audit_v2_transport_call_count_invalid"),
        ("old_ops_revision", "permission_sensitive_audit_v2_operation_hash_mismatch"),
        ("other_expected_commit", "permission_sensitive_audit_v2_operation_hash_mismatch"),
    ],
)
def test_runtime_readiness_v2_binds_operation_to_current_packet(mutation, expected_gap):
    packet = _gitops_bound_live_evidence()
    audit = copy.deepcopy(_single_bounded_denial_audit_v2())
    external_expected_commit = _BOUND_SOURCE_COMMIT
    if mutation == "build_association":
        audit["build_association_hash"] = "sha256:" + "f" * 64
    elif mutation == "operation_hash":
        audit["audit_events"][0]["request_hash"] = "sha256:" + "e" * 64
        audit["audit_store"]["request_hash"] = "sha256:" + "e" * 64
    elif mutation == "transport_retry":
        audit["transport_call_count"] = 2
    elif mutation == "old_ops_revision":
        packet["gitops_desired_state"]["ops_revision"] = "b" * 40
        packet["argo_reconciliation"]["reconciled_ops_revision"] = "b" * 40
        packet["deployment_evidence_binding"] = build_deployment_evidence_binding(
            expected_commit=_BOUND_SOURCE_COMMIT,
            gitops_desired_state=packet["gitops_desired_state"],
            argo_reconciliation=packet["argo_reconciliation"],
            deployed_identity=packet["deployed_identity"],
        )
    else:
        external_expected_commit = "b" * 40
    packet["permission_sensitive_audit"] = audit

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet,
        expected_commit=external_expected_commit,
        expected_build_association_hash=audit["product_marker_evidence"][
            "external_build_association_hash"
        ],
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert expected_gap in claim["gaps"]


def test_runtime_readiness_v2_accepts_fresh_build_anchors_for_same_deployment_tuple():
    packet = _gitops_bound_live_evidence()
    binding_hash = packet["deployment_evidence_binding"]["canonical_tuple_hash"]
    operation_hashes = []

    for digit in ("d", "f"):
        build_association_hash = "sha256:" + digit * 64
        assert build_association_hash != binding_hash
        audit = _single_bounded_denial_audit_v2(
            build_association_hash=build_association_hash,
        )
        operation_hashes.append(audit["audit_events"][0]["request_hash"])
        report = build_source_to_candidate_runtime_readiness_report(
            live_evidence={**packet, "permission_sensitive_audit": audit},
            expected_commit=_BOUND_SOURCE_COMMIT,
            expected_build_association_hash=build_association_hash,
        )
        claim = next(
            item
            for item in report["claims"]
            if item["claim_id"] == "live.production.permission_sensitive_audit"
        )
        assert claim["status"] == "validated"

    assert operation_hashes[0] != operation_hashes[1]


def test_runtime_readiness_v2_requires_exact_build_association_schema_field():
    audit = _single_bounded_denial_audit_v2()
    audit.pop("build_association_hash")

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["product_marker_evidence"][
            "external_build_association_hash"
        ],
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert "permission_sensitive_audit_v2_evidence_shape_invalid" in claim["gaps"]


def test_runtime_readiness_v2_rejects_self_asserted_build_association_without_external_anchor():
    audit = _single_bounded_denial_audit_v2()

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert (
        "permission_sensitive_audit_v2_external_build_association_missing"
        in claim["gaps"]
    )


def test_runtime_readiness_v2_requires_exact_five_product_marker_evidence():
    audit = _single_bounded_denial_audit_v2()
    audit.pop("product_marker_evidence")

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["build_association_hash"],
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert "permission_sensitive_audit_v2_product_marker_evidence_missing" in claim[
        "gaps"
    ]


def test_runtime_readiness_v2_validates_exact_five_product_marker_evidence():
    audit = _single_bounded_denial_audit_v2()

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=audit["build_association_hash"],
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "validated"
    assert claim["gaps"] == []


@pytest.mark.parametrize(
    ("mutation", "expected_gap"),
    [
        (
            "duplicate_plane",
            "permission_sensitive_audit_v2_product_marker_plane_mismatch:corpus",
        ),
        (
            "in_flight",
            "permission_sensitive_audit_v2_product_marker_in_flight_invalid:index",
        ),
        (
            "coverage",
            "permission_sensitive_audit_v2_product_marker_coverage_invalid:queue",
        ),
        (
            "pre_post",
            "permission_sensitive_audit_v2_product_marker_pre_post_mismatch:product_db",
        ),
        (
            "read_scope",
            "permission_sensitive_audit_v2_product_marker_read_scope_invalid:corpus",
        ),
        (
            "reset",
            "permission_sensitive_audit_v2_product_marker_reset_or_decrease_invalid",
        ),
        (
            "association",
            "permission_sensitive_audit_v2_product_marker_association_mismatch",
        ),
    ],
)
def test_runtime_readiness_v2_fails_closed_for_exact_marker_mutation(
    mutation,
    expected_gap,
):
    audit = copy.deepcopy(_single_bounded_denial_audit_v2())
    external_association = audit["build_association_hash"]
    markers = audit["product_marker_evidence"]["markers"]
    if mutation == "duplicate_plane":
        markers[1]["plane"] = "authority_ledger"
    elif mutation == "in_flight":
        markers[3]["in_flight_count"] = 1
        markers[3]["in_flight_status"] = "unresolved"
    elif mutation == "coverage":
        markers[2]["coverage_status"] = "failed"
    elif mutation == "pre_post":
        markers[4]["pre_post_status"] = "changed"
    elif mutation == "read_scope":
        markers[1]["read_scope_status"] = "write_capable"
    elif mutation == "reset":
        audit["product_marker_evidence"]["reset_or_decrease_count"] = 1
    else:
        audit["product_marker_evidence"]["external_build_association_hash"] = (
            "sha256:" + "f" * 64
        )

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_gitops_bound_live_evidence(permission_sensitive_audit=audit),
        expected_commit=_BOUND_SOURCE_COMMIT,
        expected_build_association_hash=external_association,
    )

    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.production.permission_sensitive_audit"
    )
    assert claim["status"] == "failed"
    assert expected_gap in claim["gaps"]


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


def test_runtime_readiness_fails_when_agent_context_startup_omits_current_authority():
    evidence = _sanitized_live_evidence(
        agent_context_startup_runtime=_agent_context_startup_runtime_evidence(
            startup_context={
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
        )
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    startup = claims["live.agent_context.startup_read_path"]
    assert startup["status"] == "failed"
    assert "agent_context_startup_section_missing:current_authority" in startup["gaps"]
    assert "agent_context_startup_section_missing:current_authority" in report["gaps"]


def test_runtime_readiness_rejects_self_declared_live_startup_without_external_receipt():
    evidence = _sanitized_live_evidence(
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
        production_authority_execution={},
        production_authority_replacement_current={},
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    startup = claims["live.agent_context.startup_read_path"]
    assert startup["status"] == "failed"
    assert "agent_context_startup_external_consumer_receipt_missing" in startup["gaps"]
    assert "agent_context_startup_receipt_not_verified" in startup["gaps"]
    assert report["production_mutation_performed"] is False


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

    assert report["status"] == "FAIL"
    assert report["evidence_is_live"] is False
    assert report["production_ready"] is False
    assert report["production_readiness"] == "not_ready"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    provenance = claims["live.evidence.provenance"]
    assert provenance["status"] == "not_validated"
    assert provenance["is_live"] is False
    assert "live_evidence_provenance_network_not_used_for_live_mode" in report["gaps"]
    assert "agent_context_startup_external_consumer_receipt_missing" in report["gaps"]


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


def test_runtime_readiness_accepts_bounded_production_execution_for_artifact_preference():
    target_object_id = "ko:ArtifactPreference:p7-html-review-density"
    execution = _production_authority_execution_evidence()
    execution["proposal"]["target_object_id"] = target_object_id
    execution["decision"]["target_object_id"] = target_object_id
    execution["decision"]["decision_id"] = "decision:p7-artifact-preference-current"
    execution["decision"]["decision_type"] = "accept_current"
    execution["decision"]["new_authority_lane"] = "accepted_current"
    execution["read_after_write"].update(
        {
            "target_object_id": target_object_id,
            "authority_lane": "accepted_current",
            "decision_id": "decision:p7-artifact-preference-current",
        }
    )
    execution["postcheck"]["review_queue_status"] = "accepted"
    execution["scope"]["object_ids"] = [target_object_id]
    execution["scope"]["allowed_object_classes"] = ["ArtifactPreference"]
    evidence = _sanitized_live_evidence(production_authority_execution=execution)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    bounded_execution = claims["live.production.object_authority_bounded_execution"]
    assert bounded_execution["status"] == "validated"
    assert bounded_execution["target_object_id"] == target_object_id
    assert "bounded_production_authority_execution_unverified" not in report["gaps"]


def test_runtime_readiness_rejects_cross_project_bounded_execution_evidence():
    execution = _production_authority_execution_evidence()
    execution["decision"]["project"] = "other-project"
    evidence = _sanitized_live_evidence(production_authority_execution=execution)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    bounded_execution = claims["live.production.object_authority_bounded_execution"]
    assert bounded_execution["status"] == "failed"
    assert "bounded_execution_project_mismatch" in report["gaps"]


def test_runtime_readiness_rejects_non_current_artifact_preference_execution():
    target_object_id = "ko:ArtifactPreference:p7-html-review-density"
    execution = _production_authority_execution_evidence()
    execution["proposal"]["target_object_id"] = target_object_id
    execution["decision"]["target_object_id"] = target_object_id
    execution["read_after_write"]["target_object_id"] = target_object_id
    execution["scope"]["object_ids"] = [target_object_id]
    execution["scope"]["allowed_object_classes"] = ["ArtifactPreference"]
    evidence = _sanitized_live_evidence(production_authority_execution=execution)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    bounded_execution = claims["live.production.object_authority_bounded_execution"]
    assert bounded_execution["status"] == "failed"
    assert "bounded_execution_artifact_preference_not_accepted_current" in report["gaps"]


def test_runtime_readiness_rejects_artifact_preference_when_scope_omits_target_class():
    target_object_id = "ko:ArtifactPreference:p7-html-review-density"
    execution = _production_authority_execution_evidence()
    execution["proposal"]["target_object_id"] = target_object_id
    execution["decision"]["target_object_id"] = target_object_id
    execution["read_after_write"]["target_object_id"] = target_object_id
    execution["scope"]["object_ids"] = [target_object_id]
    execution["scope"]["allowed_object_classes"] = ["RepoDocument"]
    evidence = _sanitized_live_evidence(production_authority_execution=execution)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    bounded_execution = claims["live.production.object_authority_bounded_execution"]
    assert bounded_execution["status"] == "failed"
    assert "bounded_execution_allowed_object_class_missing" in report["gaps"]


def test_runtime_readiness_keeps_replacement_current_repo_document_only():
    replacement = _production_authority_replacement_current_evidence()
    prior_target = "ko:ArtifactPreference:p7-prior-current"
    successor_target = "ko:ArtifactPreference:p7-successor-current"
    replacement["prior_current"]["target_object_id"] = prior_target
    replacement["successor_current"]["target_object_id"] = successor_target
    replacement["scope"]["object_ids"] = [prior_target, successor_target]
    replacement["scope"]["allowed_object_classes"] = ["ArtifactPreference"]
    evidence = _sanitized_live_evidence(production_authority_replacement_current=replacement)

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    replacement_claim = claims["live.production.object_authority_replacement_current"]
    assert replacement_claim["status"] == "failed"
    assert "replacement_object_class_not_allowed" in report["gaps"]


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
        agent_context_startup_runtime={},
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
        agent_context_startup_runtime={},
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
                "style_preference": {"object_count": 1, "authority_lanes": ["accepted_current"]},
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


def test_runtime_readiness_requires_live_agent_context_style_preference_accepted_current():
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
                "style_preference": {"object_count": 1, "authority_lanes": ["reference_only"]},
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
    assert section_claim["style_preference_object_count"] == 1
    assert section_claim["style_preference_authority_lanes"] == ["reference_only"]
    assert "live_agent_context_style_preference_accepted_current_missing" in section_claim["gaps"]
    assert "live_agent_context_style_preference_accepted_current_missing" in report["gaps"]


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
    assert report["status"] == "PASS_WITH_GAPS"
    assert "preference_artifact_collector_capability_missing" in report["gaps"]
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


def test_preference_artifact_memory_runtime_evidence_does_not_promote_self_asserted_consumer_proof():
    target_object_id = "ko:ArtifactPreference:p7-live-current"
    memory_id = "mem_artifact_preference_p7_live_current"
    card_content_hash = "sha256:" + "c" * 64
    source_content_hash = "sha256:" + "a" * 64
    authority_proposal_id = "proposal:p7-live-current"
    authority_decision_id = "decision:p7-live-current"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "title": "Dense HTML review artifacts",
        "scope": {"project": "neurons"},
        "content_hash": source_content_hash,
        "payload": {
            "target_object_id": target_object_id,
            "memory_id": memory_id,
            "card_content_hash": card_content_hash,
            "authority_proposal_id": authority_proposal_id,
            "authority_decision_id": authority_decision_id,
            "project": "neurons",
            "source_content_hash": source_content_hash,
        },
    }
    preference_route = {
        "schema_version": "brain_objects_query.v1",
        "route": "code_style_preference",
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": "code_style_preference",
            "objects": [accepted],
            "lanes": {"accepted_current": [accepted], "proposal_only": []},
            "recommended_actions": [{"object_id": target_object_id, "action": "apply_preference"}],
            "gaps": [],
        },
    }
    html_route = {
        "schema_version": "brain_objects_query.v1",
        "route": "html_visualization_preference",
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": "html_visualization_preference",
            "objects": [accepted],
            "lanes": {"accepted_current": [accepted]},
            "recommended_actions": [{"object_id": target_object_id, "action": "apply_preference"}],
            "gaps": [],
        },
    }
    context_pack = {
        "schema_version": "llm_brain_context_resolve.v1",
        "authority": {
            "agent_context_product": {
                "schema_version": "agent_context_product_pack.v1",
                "sections": {
                    "style_preference": {
                        "object_count": 1,
                        "items": [accepted],
                        "authority_lanes": ["accepted_current"],
                    }
                },
                "surface_policy": {"mutation_allowed": False},
            }
        },
    }

    evidence = build_preference_artifact_memory_runtime_evidence(
        preference_route=preference_route,
        html_route=html_route,
        context_pack=context_pack,
        artifact_summary={
            "artifact_type": "html_review",
            "summary": "Public-safe review summary with findings and evidence references.",
            "artifact_fingerprint": "sha256:" + "f" * 64,
            "consumer_provenance": {
                "consumer": "html_artifact_review_product",
                "workflow": "review_rendered_artifact",
                "evidence_kind": "actual_consumer_output",
            },
            "finding_refs": ["finding:p7-density", "finding:p7-evidence"],
            "evidence_refs": ["evidence:p7-density", "evidence:p7-evidence"],
            "finding_count": 2,
            "evidence_ref_count": 2,
            "word_count": 240,
        },
    )

    assert evidence["schema_version"] == "preference_artifact_memory_runtime_evidence.v1"
    assert "evidence_class" not in evidence
    assert "evidence_source" not in evidence
    assert evidence["attestation_state"] == "unattested_runtime_read"
    assert evidence["read_surface_alignment"] == {
        "status": "validated",
        "target_object_id": target_object_id,
        "memory_id": memory_id,
        "card_content_hash": card_content_hash,
        "authority_proposal_id": authority_proposal_id,
        "project": "neurons",
        "source_content_hash": source_content_hash,
        "authority_decision_id": authority_decision_id,
        "code_style_preference_object_ids": [target_object_id],
        "html_visualization_preference_object_ids": [target_object_id],
        "style_preference_context_object_ids": [target_object_id],
    }
    assert evidence["artifact_review_check"]["status"] == "pass"
    assert evidence["artifact_review_check"]["ui_required"] is False
    assert evidence["artifact_review_check"]["raw_artifact_body_returned"] is False
    assert evidence["artifact_consumer_evidence"]["artifact_fingerprint"] == "sha256:" + "f" * 64
    assert evidence["artifact_consumer_evidence"]["consumer_provenance"]["evidence_kind"] == (
        "actual_consumer_output"
    )
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence(preference_artifact_memory=evidence)
    )
    claim = next(item for item in report["claims"] if item["claim_id"] == "live.preference_artifact.memory")
    assert claim["status"] == "failed"
    assert "preference_artifact_consumer_evidence_missing" in claim["gaps"]
    assert "preference_artifact_proposal_lane_missing" not in claim["gaps"]
    assert "preference_artifact_read_surface_alignment_failed" not in claim["gaps"]

    for field, value in (
        ("memory_id", "mem_artifact_preference_other"),
        ("card_content_hash", "sha256:" + "d" * 64),
        ("authority_proposal_id", "proposal:p7-other"),
    ):
        tampered = json.loads(json.dumps(evidence))
        tampered["html_visualization_route_smoke"]["object_pack"]["lanes"][
            "accepted_current"
        ][0][field] = value
        tampered_report = build_source_to_candidate_runtime_readiness_report(
            live_evidence=_sanitized_live_evidence(
                preference_artifact_memory=tampered
            )
        )
        tampered_claim = next(
            item
            for item in tampered_report["claims"]
            if item["claim_id"] == "live.preference_artifact.memory"
        )
        assert "preference_artifact_read_surface_alignment_failed" in tampered_claim[
            "gaps"
        ]


def test_runtime_readiness_rejects_fully_forged_attested_receipt_without_collector_capability():
    preference = _preference_artifact_memory_evidence()
    preference["attestation_state"] = "attested_post_deploy_streamable_http"
    preference["evidence_class"] = "runtime_preference_artifact_memory"
    preference["evidence_source"] = "actual_live_read_surfaces"
    preference_binding = {
        "target_object_id": "ko:ArtifactPreference:html-review-density",
        "project": "neurons",
        "memory_id": "mem_artifact_preference_html_review_density",
        "card_content_hash": "sha256:" + "c" * 64,
        "source_content_hash": "sha256:" + "a" * 64,
        "proposal_id": "proposal:p7-html-review-density",
        "decision_id": "decision:p7-html-review-density",
        "authority_lane": "accepted_current",
    }
    artifact_binding = {
        "repository_hash": "sha256:" + "1" * 64,
        "branch_hash": "sha256:" + "2" * 64,
        "artifact_type": "html_review_artifact",
        "artifact_fingerprint": "sha256:" + "3" * 64,
        "summary_hash": "sha256:" + "4" * 64,
        "metrics_hash": "sha256:" + "5" * 64,
        "evidence_refs_hash": "sha256:" + "6" * 64,
    }
    application_result = {
        "evaluator_profile": "html_review_evidence_density_v1",
        "outcome": "pass",
        "passed_rules": [
            "object_count_at_least_one",
            "relationship_count_at_least_one",
            "evidence_count_at_least_one",
            "gate_status_count_at_least_one",
            "hidden_gap_count_zero",
            "protected_content_count_zero",
        ],
        "failed_rules": [],
    }
    consumer_surface = {
        "tool": "brain_artifact_preference_evaluate",
        "version": "v1",
        "consumer": "post_deploy_mcp_capture",
    }
    preference["artifact_consumer_evidence"] = {
        "schema_version": "artifact_preference_application_receipt.v1",
        "status": "PASS",
        "applied": True,
        "production_mutation_performed": False,
        "preference_binding": preference_binding,
        "artifact_binding": artifact_binding,
        "application_result": application_result,
        "consumer_surface": consumer_surface,
        "failures": [],
        "gaps": [],
        "receipt_hash": hash_payload(
            {
                "preference_binding": preference_binding,
                "artifact_binding": artifact_binding,
                "application_result": application_result,
                "consumer_surface": consumer_surface,
            }
        ),
    }
    preference["attestation_provenance"] = {
        "schema_version": "artifact_preference_collector_attestation.v1",
        "collector": "source_to_candidate_post_deploy_mcp_capture",
        "transport": "streamable_http",
        "named_tool": "brain_artifact_preference_evaluate",
        "receipt_hash": preference["artifact_consumer_evidence"]["receipt_hash"],
        "read_surface_recheck": "validated",
    }
    evidence = _sanitized_live_evidence(
        preference_artifact_memory=preference,
        evidence_provenance=_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
        production_authority_execution=None,
        production_authority_replacement_current=None,
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)
    claim = next(
        item
        for item in report["claims"]
        if item["claim_id"] == "live.preference_artifact.memory"
    )

    assert claim["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in claim["gaps"]


def test_preference_artifact_memory_runtime_evidence_rejects_cross_type_object_ids():
    target_object_id = "ko:RepoDocument:p7-cross-type-preference"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "a" * 64,
        "payload": {
            "target_object_id": target_object_id,
            "authority_decision_id": "decision:p7-cross-type-preference",
        },
    }

    def route(name: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": name,
                "objects": [accepted],
                "lanes": {"accepted_current": [accepted], "proposal_only": []},
                "recommended_actions": [],
                "gaps": [],
            },
        }

    evidence = build_preference_artifact_memory_runtime_evidence(
        preference_route=route("code_style_preference"),
        html_route=route("html_visualization_preference"),
        context_pack={
            "authority": {
                "agent_context_product": {
                    "schema_version": "agent_context_product_pack.v1",
                    "sections": {
                        "style_preference": {
                            "items": [accepted],
                            "authority_lanes": ["accepted_current"],
                        }
                    },
                    "surface_policy": {"mutation_allowed": False},
                }
            }
        },
        artifact_summary={
            "artifact_type": "html_review",
            "summary": "Cross-type object IDs must not validate as preferences.",
        },
    )

    assert evidence["preference_object_pack"]["accepted_preference_count"] == 0
    assert evidence["read_surface_alignment"]["status"] == "failed"
    assert "accepted_current_artifact_preference_missing" in evidence["gaps"]


def test_preference_artifact_memory_runtime_evidence_does_not_fabricate_consumer_proof_from_routes():
    target_object_id = "ko:ArtifactPreference:p7-route-only"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "a" * 64,
        "payload": {
            "memory_id": "mem_artifact_preference_p7_route_only",
            "card_content_hash": "sha256:" + "c" * 64,
            "authority_proposal_id": "proposal:p7-route-only",
            "authority_decision_id": "decision:p7-route-only",
            "project": "neurons",
            "source_content_hash": "sha256:" + "a" * 64,
        },
    }

    def route(name: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": name,
                "objects": [accepted],
                "lanes": {"accepted_current": [accepted], "proposal_only": []},
                "recommended_actions": [],
                "gaps": [],
            },
        }

    evidence = build_preference_artifact_memory_runtime_evidence(
        preference_route=route("code_style_preference"),
        html_route=route("html_visualization_preference"),
        context_pack={
            "authority": {
                "agent_context_product": {
                    "schema_version": "agent_context_product_pack.v1",
                    "sections": {
                        "style_preference": {
                            "items": [accepted],
                            "authority_lanes": ["accepted_current"],
                        }
                    },
                    "surface_policy": {"mutation_allowed": False},
                }
            }
        },
        artifact_summary={
            "artifact_type": "html_review",
            "summary": "A collector-authored summary must not count as consumer proof.",
            "finding_count": 1,
            "evidence_ref_count": 3,
            "word_count": 9,
        },
    )

    assert evidence["read_surface_alignment"]["status"] == "validated"
    assert evidence["artifact_review_check"]["status"] == "failed"
    assert evidence["artifact_review_check"]["artifact_metrics"]["finding_count"] == 0
    assert "actual_artifact_consumer_provenance_missing" in evidence["artifact_review_check"]["failures"]
    assert "artifact_fingerprint_missing" in evidence["artifact_review_check"]["failures"]
    assert "artifact_consumer_evidence_missing" in evidence["gaps"]


@pytest.mark.parametrize(
    ("forbidden_key", "raw_value", "redacted_key"),
    [
        ("dataset_id", "raw-dataset-value-must-not-leak", "dataset_ref"),
        ("document_id", "raw-document-value-must-not-leak", "document_ref"),
    ],
)
def test_preference_artifact_runtime_collector_rejects_raw_external_ids_before_key_redaction(
    forbidden_key: str,
    raw_value: str,
    redacted_key: str,
):
    target_object_id = "ko:ArtifactPreference:p7-forbidden-runtime-input"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "a" * 64,
        "payload": {
            "authority_decision_id": "decision:p7-forbidden-runtime-input",
            "nested": {forbidden_key: raw_value},
        },
    }

    def route(name: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": name,
                "objects": [accepted],
                "lanes": {"accepted_current": [accepted], "proposal_only": []},
                "recommended_actions": [],
                "gaps": [],
            },
        }

    def preference_runner() -> dict:
        return build_preference_artifact_memory_runtime_evidence(
            preference_route=route("code_style_preference"),
            html_route=route("html_visualization_preference"),
            context_pack={
                "authority": {
                    "agent_context_product": {
                        "schema_version": "agent_context_product_pack.v1",
                        "sections": {
                            "style_preference": {
                                "items": [accepted],
                                "authority_lanes": ["accepted_current"],
                            }
                        },
                        "surface_policy": {"mutation_allowed": False},
                    }
                }
            },
            artifact_summary={
                "artifact_type": "html_review",
                "summary": "Public-safe consumer summary.",
                "artifact_fingerprint": "sha256:" + "f" * 64,
                "consumer_provenance": {
                    "consumer": "html_artifact_review_product",
                    "workflow": "review_rendered_artifact",
                    "evidence_kind": "actual_consumer_output",
                },
                "finding_refs": ["finding:p7-safe"],
                "evidence_refs": ["evidence:p7-safe"],
                "finding_count": 1,
                "evidence_ref_count": 1,
            },
        )

    packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        repository="pureliture/neurons",
        branch="main",
        route_runner=lambda name: route(name),
        preference_artifact_memory_runner=preference_runner,
        collection_mode="post_deploy_read_only_smoke",
        network_used=True,
    )

    serialized = json.dumps(packet, sort_keys=True)
    preference = packet["preference_artifact_memory"]
    assert preference["collector_error_type"] == "ValueError"
    assert preference["preference_object_pack"]["gaps"] == ["preference_artifact_collector_failed"]
    assert preference["postcheck"]["status"] == "failed"
    assert preference["postcheck"].get("raw_external_ids_returned") is not False
    assert raw_value not in serialized
    assert forbidden_key not in serialized
    assert redacted_key not in serialized


@pytest.mark.parametrize(
    ("ref_field", "raw_ref"),
    [
        ("finding_refs", "dataset:raw-external-id"),
        ("finding_refs", "finding:dataset-id=raw-external-id"),
        ("finding_refs", "finding:dataset%3Araw-external-id"),
        ("evidence_refs", "document:raw-external-id"),
        ("evidence_refs", "evidence:document-id=raw-external-id"),
        ("evidence_refs", "evidence:document%3Araw-external-id"),
    ],
)
def test_preference_artifact_runtime_evidence_rejects_raw_external_id_ref_values(
    ref_field: str,
    raw_ref: str,
):
    target_object_id = "ko:ArtifactPreference:p7-forbidden-runtime-ref"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "a" * 64,
        "payload": {"authority_decision_id": "decision:p7-forbidden-runtime-ref"},
    }

    def route(name: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": name,
                "objects": [accepted],
                "lanes": {"accepted_current": [accepted], "proposal_only": []},
                "recommended_actions": [],
                "gaps": [],
            },
        }

    artifact_summary = {
        "artifact_type": "html_review",
        "summary": "Public-safe consumer summary.",
        "artifact_fingerprint": "sha256:" + "f" * 64,
        "consumer_provenance": {
            "consumer": "html_artifact_review_product",
            "workflow": "review_rendered_artifact",
            "evidence_kind": "actual_consumer_output",
        },
        "finding_refs": ["finding:p7-safe"],
        "evidence_refs": ["evidence:p7-safe"],
        "finding_count": 1,
        "evidence_ref_count": 1,
    }
    artifact_summary[ref_field] = [raw_ref]

    with pytest.raises(ValueError, match="public-safe artifact refs"):
        build_preference_artifact_memory_runtime_evidence(
            preference_route=route("code_style_preference"),
            html_route=route("html_visualization_preference"),
            context_pack={
                "authority": {
                    "agent_context_product": {
                        "schema_version": "agent_context_product_pack.v1",
                        "sections": {
                            "style_preference": {
                                "items": [accepted],
                                "authority_lanes": ["accepted_current"],
                            }
                        },
                        "surface_policy": {"mutation_allowed": False},
                    }
                }
            },
            artifact_summary=artifact_summary,
        )


@pytest.mark.parametrize(
    ("ref_field", "raw_ref"),
    [
        ("finding_refs", "finding:dataset:raw-external-id"),
        ("finding_refs", "finding:dataset-id=raw-external-id"),
        ("finding_refs", "finding:dataset%3Araw-external-id"),
        ("evidence_refs", "evidence:document:raw-external-id"),
        ("evidence_refs", "evidence:document-id=raw-external-id"),
        ("evidence_refs", "evidence:document%3Araw-external-id"),
    ],
)
def test_runtime_readiness_rejects_raw_external_id_markers_in_direct_consumer_refs(
    ref_field: str,
    raw_ref: str,
):
    target_object_id = "ko:ArtifactPreference:p7-direct-runtime-ref"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "a" * 64,
        "payload": {"authority_decision_id": "decision:p7-direct-runtime-ref"},
    }

    def route(name: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": name,
                "objects": [accepted],
                "lanes": {"accepted_current": [accepted], "proposal_only": []},
                "recommended_actions": [],
                "gaps": [],
            },
        }

    preference = build_preference_artifact_memory_runtime_evidence(
        preference_route=route("code_style_preference"),
        html_route=route("html_visualization_preference"),
        context_pack={
            "authority": {
                "agent_context_product": {
                    "schema_version": "agent_context_product_pack.v1",
                    "sections": {
                        "style_preference": {
                            "items": [accepted],
                            "authority_lanes": ["accepted_current"],
                        }
                    },
                    "surface_policy": {"mutation_allowed": False},
                }
            }
        },
        artifact_summary={
            "artifact_type": "html_review",
            "summary": "Public-safe consumer summary.",
            "artifact_fingerprint": "sha256:" + "f" * 64,
            "consumer_provenance": {
                "consumer": "html_artifact_review_product",
                "workflow": "review_rendered_artifact",
                "evidence_kind": "actual_consumer_output",
            },
            "finding_refs": ["finding:p7-safe"],
            "evidence_refs": ["evidence:p7-safe"],
            "finding_count": 1,
            "evidence_ref_count": 1,
        },
    )
    preference["artifact_consumer_evidence"][ref_field] = [raw_ref]

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence(preference_artifact_memory=preference)
    )
    claim = next(item for item in report["claims"] if item["claim_id"] == "live.preference_artifact.memory")

    assert claim["status"] == "failed"
    assert "preference_artifact_consumer_evidence_missing" in claim["gaps"]


def test_preference_artifact_runtime_evidence_outputs_only_allowlisted_object_views():
    target_object_id = "ko:ArtifactPreference:p7-allowlist-view"
    hidden_value = "benign-but-not-required-runtime-metadata"
    accepted = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "title": "Dense HTML review artifacts",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "a" * 64,
        "payload": {
            "memory_id": "mem_artifact_preference_p7_allowlist_view",
            "card_content_hash": "sha256:" + "c" * 64,
            "authority_proposal_id": "proposal:p7-allowlist-view",
            "authority_decision_id": "decision:p7-allowlist-view",
            "project": "neurons",
            "source_content_hash": "sha256:" + "a" * 64,
            "reason": hidden_value,
        },
        "debug_metadata": {"note": hidden_value},
    }

    def route(name: str) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": name,
                "objects": [accepted],
                "lanes": {"accepted_current": [accepted], "proposal_only": []},
                "recommended_actions": [{"object_id": target_object_id, "action": "apply_preference"}],
                "gaps": [],
            },
        }

    evidence = build_preference_artifact_memory_runtime_evidence(
        preference_route=route("code_style_preference"),
        html_route=route("html_visualization_preference"),
        context_pack={
            "authority": {
                "agent_context_product": {
                    "schema_version": "agent_context_product_pack.v1",
                    "sections": {
                        "style_preference": {
                            "items": [accepted],
                            "authority_lanes": ["accepted_current"],
                        }
                    },
                    "surface_policy": {"mutation_allowed": False},
                }
            }
        },
        artifact_summary={
            "artifact_type": "html_review",
            "summary": "Public-safe consumer summary.",
            "artifact_fingerprint": "sha256:" + "f" * 64,
            "consumer_provenance": {
                "consumer": "html_artifact_review_product",
                "workflow": "review_rendered_artifact",
                "evidence_kind": "actual_consumer_output",
            },
            "finding_refs": ["finding:p7-safe"],
            "evidence_refs": ["evidence:p7-safe"],
            "finding_count": 1,
            "evidence_ref_count": 1,
        },
    )

    serialized = json.dumps(evidence, sort_keys=True)
    assert hidden_value not in serialized
    assert "debug_metadata" not in serialized
    output = evidence["preference_object_pack"]["lanes"]["accepted_current"][0]
    assert output == {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "title": "Dense HTML review artifacts",
        "memory_id": "mem_artifact_preference_p7_allowlist_view",
        "card_content_hash": "sha256:" + "c" * 64,
        "authority_proposal_id": "proposal:p7-allowlist-view",
        "project": "neurons",
        "content_hash": "sha256:" + "a" * 64,
        "source_content_hash": "sha256:" + "a" * 64,
        "authority_decision_id": "decision:p7-allowlist-view",
    }


@pytest.mark.parametrize(
    ("surface", "field", "value"),
    [
        ("html", "source_content_hash", "sha256:" + "b" * 64),
        ("context", "project", "other-project"),
        ("html", "memory_id", "mem_artifact_preference_other"),
        ("context", "card_content_hash", "sha256:" + "b" * 64),
        ("html", "authority_proposal_id", "proposal:p7-other"),
        ("html", "authority_decision_id", "decision:p7-other"),
    ],
)
def test_preference_artifact_memory_runtime_evidence_rejects_metadata_discontinuity(
    surface: str,
    field: str,
    value: str,
):
    target_object_id = "ko:ArtifactPreference:p7-continuity"
    base = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
        "scope": {"project": "neurons"},
        "content_hash": "sha256:" + "c" * 64,
        "payload": {
            "target_object_id": target_object_id,
            "memory_id": "mem_artifact_preference_p7_continuity",
            "card_content_hash": "sha256:" + "d" * 64,
            "authority_proposal_id": "proposal:p7-continuity",
            "authority_decision_id": "decision:p7-continuity",
            "project": "neurons",
            "source_content_hash": "sha256:" + "c" * 64,
        },
    }
    objects = {name: json.loads(json.dumps(base)) for name in ("code", "html", "context")}
    target = objects[surface]
    if field == "project":
        target["scope"]["project"] = value
    elif field in {
        "memory_id",
        "card_content_hash",
        "authority_proposal_id",
        "authority_decision_id",
        "source_content_hash",
    }:
        target["payload"][field] = value
    else:
        target[field] = value

    def route(route_name: str, obj: dict) -> dict:
        return {
            "schema_version": "brain_objects_query.v1",
            "route": route_name,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route_name,
                "objects": [obj],
                "lanes": {"accepted_current": [obj], "proposal_only": []},
                "recommended_actions": [],
                "gaps": [],
            },
        }

    evidence = build_preference_artifact_memory_runtime_evidence(
        preference_route=route("code_style_preference", objects["code"]),
        html_route=route("html_visualization_preference", objects["html"]),
        context_pack={
            "schema_version": "llm_brain_context_resolve.v1",
            "authority": {
                "agent_context_product": {
                    "schema_version": "agent_context_product_pack.v1",
                    "sections": {
                        "style_preference": {
                            "object_count": 1,
                            "items": [objects["context"]],
                            "authority_lanes": ["accepted_current"],
                        }
                    },
                    "surface_policy": {"mutation_allowed": False},
                }
            },
        },
        artifact_summary={
            "artifact_type": "html_review",
            "summary": "Public-safe continuity review summary.",
            "finding_count": 1,
            "evidence_ref_count": 1,
            "word_count": 5,
        },
    )

    assert evidence["read_surface_alignment"]["status"] == "failed"
    assert evidence["read_surface_alignment"]["target_object_id"] == ""
    assert "preference_read_surface_metadata_mismatch" in evidence["gaps"]


def test_preference_artifact_memory_runtime_evidence_keeps_missing_current_authority_as_failure():
    empty_pack = {
        "schema_version": "brain_objects_query.v1",
        "route": "code_style_preference",
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": "code_style_preference",
            "objects": [],
            "lanes": {"accepted_current": [], "proposal_only": []},
            "recommended_actions": [],
            "gaps": ["accepted_current preferences empty"],
        },
    }
    html = json.loads(json.dumps(empty_pack))
    html["route"] = "html_visualization_preference"
    html["object_pack"]["route"] = "html_visualization_preference"
    context = {
        "schema_version": "llm_brain_context_resolve.v1",
        "authority": {
            "agent_context_product": {
                "schema_version": "agent_context_product_pack.v1",
                "sections": {
                    "style_preference": {
                        "object_count": 0,
                        "items": [],
                        "authority_lanes": [],
                    }
                },
                "surface_policy": {"mutation_allowed": False},
            }
        },
    }

    evidence = build_preference_artifact_memory_runtime_evidence(
        preference_route=empty_pack,
        html_route=html,
        context_pack=context,
        artifact_summary={"artifact_type": "html_review", "summary": "Public-safe summary."},
    )

    assert evidence["attestation_state"] == "unattested_runtime_read"
    assert evidence["read_surface_alignment"]["status"] == "failed"
    assert "accepted_current_artifact_preference_missing" in evidence["gaps"]
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=_sanitized_live_evidence(preference_artifact_memory=evidence)
    )
    assert "live.preference_artifact.memory" in report["failed_claims"]


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
    assert preference["agent_context_preference_section"]["authority_lanes"] == [
        "accepted_current"
    ]
    assert preference["artifact_review_check"]["status"] == "pass"
    assert preference["artifact_review_check"]["ui_required"] is False
    assert preference["artifact_review_check"]["raw_artifact_body_returned"] is False
    assert preference["postcheck"]["status"] == "validated"
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=packet)
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.preference_artifact.memory"]["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in claims[
        "live.preference_artifact.memory"
    ]["gaps"]
    assert "live_preference_artifact_memory_unverified" not in report["gaps"]
    assert "accepted_preference_context_pack_live_unproven" not in report["gaps"]
    assert report["status"] == "PASS_WITH_GAPS"


def test_runtime_readiness_fails_when_preference_artifact_context_lacks_accepted_current_lane():
    evidence = _sanitized_live_evidence(
        preference_artifact_memory=_preference_artifact_memory_evidence(
            agent_context_preference_section={
                "schema_version": "agent_context_product_pack.v1",
                "section": "style_preference",
                "object_count": 1,
                "accepted_preference_count": 1,
                "authority_lanes": ["reference_only"],
                "surface_policy": {"mutation_allowed": False},
            },
        ),
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    preference = claims["live.preference_artifact.memory"]
    assert report["status"] == "FAIL"
    assert preference["status"] == "failed"
    assert preference["agent_context_authority_lanes"] == ["reference_only"]
    assert "preference_artifact_agent_context_accepted_current_missing" in preference["gaps"]
    assert "preference_artifact_agent_context_accepted_current_missing" in report["gaps"]


def test_runtime_readiness_fails_when_preference_artifact_count_lacks_accepted_current_lane():
    evidence = _sanitized_live_evidence(
        preference_artifact_memory=_preference_artifact_memory_evidence(
            preference_object_pack={
                "schema_version": "object_pack.v1",
                "route": "code_style_preference",
                "accepted_preference_count": 1,
                "proposal_preference_count": 1,
                "objects": [
                    {
                        "object_id": "ko:ArtifactPreference:html-review-density",
                        "object_type": "ArtifactPreference",
                        "authority_lane": "reference_only",
                    }
                ],
                "lanes": {
                    "accepted_current": [],
                    "proposal_only": [
                        {
                            "object_id": "ko:ArtifactPreference:visualization-proposal",
                            "object_type": "ArtifactPreference",
                            "authority_lane": "proposal_only",
                        }
                    ],
                },
                "recommended_actions": [],
                "gaps": [],
                "production_mutation_performed": False,
            },
        ),
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    preference = claims["live.preference_artifact.memory"]
    assert report["status"] == "FAIL"
    assert preference["status"] == "failed"
    assert preference["accepted_preference_count"] == 1
    assert preference["accepted_current_lane_count"] == 0
    assert "preference_artifact_accepted_current_lane_missing" in preference["gaps"]
    assert "preference_artifact_accepted_current_lane_missing" in report["gaps"]


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
    assert startup["startup_context"]["section_counts"]["current_authority"] >= 1
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
    assert "permission_sensitive_audit" not in packet
    assert packet["collector"]["permission_sensitive_audit_collected"] is False
    assert packet["collector"]["permission_sensitive_audit_collection_status"] == "not_collected"
    assert packet["collector"]["permission_sensitive_audit_schema"] == ""
    assert packet["agent_context_startup_runtime"]["schema_version"] == "agent_context_startup_runtime_evidence.v1"
    assert packet["agent_context_startup_runtime"]["startup_context"]["loaded_on_startup"] is True
    assert packet["agent_context_startup_runtime"]["startup_context"]["section_counts"]["current_authority"] >= 1
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
    assert claims["live.preference_artifact.memory"]["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in claims[
        "live.preference_artifact.memory"
    ]["gaps"]
    assert claims["live.production.permission_sensitive_audit"]["status"] == "not_validated"
    assert claims["live.production.permission_sensitive_audit"]["gaps"] == [
        "permission_sensitive_audit_unverified",
        "product_marker_audit_unverified",
    ]
    assert claims["live.agent_context.startup_read_path"]["status"] == "validated"


def test_neuron_knowledge_runtime_readiness_cli_collects_temporal_checkpoint_without_deployment_binding(
    tmp_path,
    capsys,
    monkeypatch,
):
    acceptance_file = tmp_path / "temporal-acceptance.json"
    acceptance_file.write_text("{}", encoding="utf-8")
    received: dict = {}

    async def _collect_temporal_checkpoint(**kwargs):
        received.update(kwargs)
        return {
            "schema_version": "temporal_recall_corrective_checkpoint_capture.v1",
            "collection": {
                "mode": "temporal_corrective_checkpoint_read_only",
                "network_used": True,
                "mutation_scope": "none",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
            "temporal_recall_corrective_checkpoint": (
                _temporal_recall_corrective_checkpoint()
            ),
            "production_mutation_performed": False,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_temporal_recall_corrective_checkpoint",
        _collect_temporal_checkpoint,
    )

    exit_code = main(
        [
            "source-to-candidate-runtime-readiness",
            "--collect-temporal-corrective-checkpoint",
            "--mcp-url",
            "https://mcp.example.test/mcp",
            "--temporal-acceptance-file",
            str(acceptance_file),
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--project",
            "neurons",
            "--consumer",
            "codex",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "temporal_recall_corrective_checkpoint_capture.v1"
    readiness = output["checkpoint_readiness"]
    assert readiness["schema_version"] == (
        "temporal_recall_corrective_checkpoint_readiness.v1"
    )
    assert readiness["status"] == "PASS"
    assert readiness["production_mutation_performed"] is False
    assert readiness["failed_claims"] == []
    assert readiness["gaps"] == []
    assert readiness["claim"]["status"] == "validated"
    assert received["temporal_acceptance"] == {}
    assert received["project"] == "neurons"
    assert "deployed_identity" not in received
    assert "gitops_desired_state" not in received
    assert "argo_reconciliation" not in received


def test_temporal_checkpoint_cli_returns_failure_exit_for_invalid_checkpoint(
    tmp_path,
    capsys,
    monkeypatch,
):
    acceptance_file = tmp_path / "temporal-acceptance.json"
    acceptance_file.write_text("{}", encoding="utf-8")
    failed_checkpoint = _temporal_recall_corrective_checkpoint()
    failed_checkpoint["runtime_aggregate"]["projection_currentness"][
        "source_projection_state_digest"
    ] = ""

    async def _collect_temporal_checkpoint(**_kwargs):
        return {
            "schema_version": "temporal_recall_corrective_checkpoint_capture.v1",
            "collection": {
                "mode": "temporal_corrective_checkpoint_read_only",
                "network_used": True,
                "mutation_scope": "none",
                "raw_private_evidence_returned": False,
                "secret_returned": False,
                "host_topology_returned": False,
                "raw_external_ids_returned": False,
            },
            "temporal_recall_corrective_checkpoint": failed_checkpoint,
            "production_mutation_performed": False,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_temporal_recall_corrective_checkpoint",
        _collect_temporal_checkpoint,
    )

    exit_code = main(
        [
            "source-to-candidate-runtime-readiness",
            "--collect-temporal-corrective-checkpoint",
            "--mcp-url",
            "https://mcp.example.test/mcp",
            "--temporal-acceptance-file",
            str(acceptance_file),
            "--project",
            "neurons",
        ]
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["checkpoint_readiness"]["status"] == "FAIL"
    assert output["checkpoint_readiness"]["failed_claims"] == [
        "live.temporal_recall.corrective_checkpoint"
    ]
    assert (
        "temporal_corrective_projection_state_digest_invalid"
        in output["checkpoint_readiness"]["gaps"]
    )


@pytest.mark.parametrize(
    "conflicting_args",
    [
        ["--live-evidence-file", "ignored.json"],
        ["--normalize-post-deploy-capture-file", "ignored.json"],
        ["--post-deploy-capture-file", "ignored.json"],
        ["--normalize-shadow-evidence-file", "ignored.json"],
        ["--shadow-evidence-file", "ignored.json"],
        ["--evidence-collection-plan"],
        ["--evidence-packet-template"],
        ["--collect-shadow-evidence"],
        ["--collect-post-deploy-mcp-capture"],
        ["--collect-agent-context-startup"],
        ["--gitops-desired-state-file", "ignored.json"],
        ["--argo-reconciliation-file", "ignored.json"],
        ["--deployed-identity-file", "ignored.json"],
        ["--artifact-descriptor-file", "ignored.json"],
    ],
)
def test_temporal_checkpoint_cli_rejects_other_modes_and_deployment_binding_inputs(
    tmp_path,
    conflicting_args,
):
    acceptance_file = tmp_path / "temporal-acceptance.json"
    acceptance_file.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "source-to-candidate-runtime-readiness",
            "--collect-temporal-corrective-checkpoint",
            "--mcp-url",
            "https://mcp.example.test/mcp",
            "--temporal-acceptance-file",
            str(acceptance_file),
            "--project",
            "neurons",
            *conflicting_args,
        ]
    )

    assert exit_code == 2


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
    preference_claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert preference_claim["status"] == "not_validated"
    assert "live_preference_artifact_memory_unverified" in preference_claim["gaps"]
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        assert f"shadow_route_smoke_not_implemented:{route}" in report["gaps"]

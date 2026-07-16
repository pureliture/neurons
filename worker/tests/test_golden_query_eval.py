import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_knowledge.llm_brain_core.golden_query_eval import (
    GOLDEN_QUERIES,
    REQUIRED_QUALITY_AXES,
    build_baseline_golden_query_report,
    build_product_activation_progress_report,
    build_phase_golden_query_coverage_report,
    build_source_to_authority_quality_gate_report,
    evaluate_object_pack_response,
    evaluate_product_evidence_summary,
)
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.object_packs import build_code_change_impact_pack
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
    _mint_collector_attested_evidence,
    build_deployment_evidence_binding,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
)
from agent_knowledge.llm_brain_core.objects.artifact_preference_evaluator import (
    ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
    artifact_descriptor_fingerprint,
    evaluate_artifact_preference,
)
from agent_knowledge.llm_brain_core.objects.post_deploy_mcp_capture import (
    collect_source_to_candidate_post_deploy_mcp_capture,
)
from agent_knowledge.llm_brain_core.objects.agent_context_consumer import (
    build_agent_context_consumer_challenge,
    build_agent_context_consumer_startup_receipt,
    build_agent_context_startup_runtime_evidence,
)
from agent_knowledge.llm_brain_core._util import hash_payload

_REQUIRED_ROUTE_NAMES = list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
_P9_STARTUP_NOW = datetime(2026, 7, 15, 3, 0, 0, tzinfo=timezone.utc)
_P9_STARTUP_PROOF_KEY = b"p9-golden-query-startup-proof-key"


def _valid_p3_projection_join_evidence():
    return {
        "phase": "P3",
        "schema_version": "source_to_candidate_projection_join_product_evidence.v1",
        "status": "PASS",
        "projection_join_claim_status": "validated",
        "projection_join_edge_count": 2,
        "evidence_is_live": True,
        "production_ready": False,
        "gaps": [],
        "production_mutation_performed": False,
    }


def _valid_p4_replacement_current_product_evidence():
    return {
        "phase": "P4",
        "schema_version": "object_authority_replacement_current_product_evidence.v1",
        "status": "PASS",
        "replacement_claim_status": "validated",
        "prior_authority_lane": "accepted_non_current",
        "successor_authority_lane": "accepted_current",
        "read_after_write_status": "validated",
        "postcheck_status": "validated",
        "object_count": 2,
        "live_evidence_provided": True,
        "evidence_is_live": True,
        "network_used": True,
        "evidence_collection_network_used": True,
        "production_mutation_performed": True,
        "production_ready": False,
        "runtime_readiness_failed": False,
        "evidence_provenance_status": "validated",
        "gaps": [],
    }


def _valid_p3_runtime_evidence(*, live: bool = True):
    return {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "projection_join": {
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
        },
        "evidence_provenance": {
            "schema_version": "source_to_candidate_runtime_evidence_provenance.v1",
            "collection_mode": "post_deploy_read_only_smoke" if live else "local_test_replay",
            "mutation_scope": "none",
            "network_used": live,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }


def _valid_p4_replacement_current_runtime_evidence(*, live: bool = True):
    approval_ref_hash = "sha256:" + "c" * 64
    return {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "production_authority_replacement_current": {
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
        },
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "redacted_operator_packet" if live else "local_test_replay",
            "mutation_scope": "bounded_production_authority_execution",
            "network_used": live,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": True,
    }


def _valid_p6_runtime_evidence(*, live: bool = True):
    return {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "session_project_rollup_runtime": {
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
        },
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke" if live else "local_test_replay",
            "mutation_scope": "none",
            "network_used": live,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }


def _valid_p7_runtime_evidence(*, live: bool = True):
    target_object_id = "ko:ArtifactPreference:html-review-density"
    memory_id = "mem_artifact_preference_html_review_density"
    card_content_hash = "sha256:" + "c" * 64
    source_content_hash = "sha256:" + "a" * 64
    authority_proposal_id = "proposal:p7-html-review-density"
    authority_decision_id = "decision:p7-html-review-density"
    accepted_object = {
        "object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
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
    proposal_object = {
        "object_id": "ko:ArtifactPreference:visualization-proposal",
        "object_type": "ArtifactPreference",
        "authority_lane": "proposal_only",
    }
    return {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "preference_artifact_memory": {
            "schema_version": "preference_artifact_memory_runtime_evidence.v1",
            "attestation_state": "unattested_runtime_read",
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
                "items": [accepted_object],
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
            "read_surface_alignment": {
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
            },
        },
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke" if live else "local_test_replay",
            "mutation_scope": "none",
            "network_used": live,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }


def _attested_p7_post_deploy_capture() -> dict:
    repository = "pureliture/neurons"
    branch = "main"
    project = "neurons"
    target_object_id = "ko:ArtifactPreference:html-review-density"
    source_content_hash = "sha256:" + "a" * 64
    proposal_id = "proposal:p7-html-review-density"
    decision_id = "decision:p7-html-review-density"
    card = {
        "memory_id": "mem_artifact_preference_html_review_density",
        "project": project,
        "card_type": "preference",
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "freshness": "current",
        "superseded_by": [],
        "typed_payload": {
            "target_object_id": target_object_id,
            "source_object_type": "ArtifactPreference",
            "source_content_hash": source_content_hash,
            "authority_proposal_id": proposal_id,
            "authority_decision_id": decision_id,
            "applies_to": "html_review_artifact",
            "evaluator_profile": "html_review_evidence_density_v1",
        },
    }
    card_content_hash = hash_payload(card)
    card["content_hash"] = card_content_hash
    card["card_hash"] = card_content_hash
    proposal = {
        "project": project,
        "proposal_id": proposal_id,
        "proposal_type": "propose_current",
        "target_object_id": target_object_id,
        "object_type": "ArtifactPreference",
        "status": "accepted",
        "decision_id": decision_id,
        "proposed_object": {
            "object_id": target_object_id,
            "object_type": "ArtifactPreference",
            "scope": {"project": project},
            "content_hash": source_content_hash,
            "payload": {"applies_to": "html_review_artifact"},
        },
    }
    decision = {
        "project": project,
        "proposal_id": proposal_id,
        "decision_id": decision_id,
        "target_object_id": target_object_id,
        "decision_type": "accept_current",
        "new_authority_lane": "accepted_current",
    }
    state = {
        "project": project,
        "target_object_id": target_object_id,
        "authority_lane": "accepted_current",
        "proposal_id": proposal_id,
        "decision_id": decision_id,
        "decision_type": "accept_current",
    }

    class _Ledger:
        def list_llm_brain_memory_cards(self, **_kwargs):
            return [card]

        def get_object_authority_state(self, _object_id):
            return state

        def get_object_review_proposal(self, _proposal_id):
            return proposal

        def list_object_authority_decisions(self, **_kwargs):
            return [decision]

    summary = "Rendered HTML review artifact exposes objects, relationships, evidence, and gate status."
    metrics = {
        "object_count": 2,
        "relationship_count": 1,
        "evidence_count": 2,
        "gate_status_count": 1,
        "hidden_gap_count": 0,
        "protected_content_count": 0,
    }
    evidence_refs = ["artifact:rendered-review", "evidence:review-findings"]
    descriptor = {
        "artifact_type": "html_review_artifact",
        "summary": summary,
        "artifact_fingerprint": artifact_descriptor_fingerprint(
            artifact_type="html_review_artifact",
            summary=summary,
            metrics=metrics,
            evidence_refs=evidence_refs,
        ),
        "metrics": metrics,
        "evidence_refs": evidence_refs,
    }
    receipt = evaluate_artifact_preference(
        ledger=_Ledger(),
        repository=repository,
        branch=branch,
        project=project,
        consumer="post_deploy_mcp_capture",
        **descriptor,
    )
    assert receipt["status"] == "PASS"

    runtime_packet = _valid_p7_runtime_evidence(live=True)
    preference = runtime_packet["preference_artifact_memory"]
    accepted = preference["preference_object_pack"]["lanes"]["accepted_current"][0]
    for field in (
        "memory_id",
        "card_content_hash",
        "source_content_hash",
        "proposal_id",
        "decision_id",
        "project",
    ):
        source_field = {
            "proposal_id": "authority_proposal_id",
            "decision_id": "authority_decision_id",
        }.get(field, field)
        accepted["payload"][source_field] = receipt["preference_binding"][field]
        preference["read_surface_alignment"][source_field] = receipt["preference_binding"][field]

    class _Session:
        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(
                tools=[SimpleNamespace(name=ARTIFACT_PREFERENCE_EVALUATOR_TOOL)]
            )

        async def call_tool(self, name, arguments):
            if name == RUNTIME_READINESS_AGENT_CONTEXT_TOOL:
                if arguments.get("evidence_collection_plan") is True:
                    return SimpleNamespace(
                        isError=False,
                        structuredContent={
                            "schema_version": "source_to_candidate_runtime_evidence_collection_plan.v1",
                            "collection_mode": "post_deploy_read_only_smoke",
                            "network_used": True,
                            "production_mutation_performed": False,
                        },
                    )
                return SimpleNamespace(
                    isError=False,
                    structuredContent=deepcopy(runtime_packet),
                )
            if name == ARTIFACT_PREFERENCE_EVALUATOR_TOOL:
                return SimpleNamespace(isError=False, structuredContent=deepcopy(receipt))
            if name == "brain_context_resolve":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "authority": {
                            "agent_context_product": {
                                "schema_version": "agent_context_product_pack.v1",
                                "sections": {},
                                "surface_policy": {"mutation_allowed": False},
                            }
                        }
                    },
                )
            route = str(arguments.get("route") or "")
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "schema_version": "brain_objects_query.v1",
                    "route": route,
                    "object_pack": {
                        "schema_version": "object_pack.v1",
                        "route": route,
                        "objects": [],
                        "lanes": {},
                        "recommended_actions": [],
                        "gaps": [],
                    },
                },
            )

    session = _Session()

    @asynccontextmanager
    async def _session_factory(_mcp_url):
        yield session

    # The collector owns attestation: the receipt comes from the named evaluator tool.
    return asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository=repository,
            branch=branch,
            project=project,
            expected_commit="bec7b38",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=descriptor,
            session_factory=_session_factory,
        )
    )


def _valid_p6_p7_runtime_evidence(*, live: bool = True):
    if not live:
        evidence = _valid_p6_runtime_evidence(live=False)
        evidence["preference_artifact_memory"] = _valid_p7_runtime_evidence(live=False)[
            "preference_artifact_memory"
        ]
        return evidence
    capture = _attested_p7_post_deploy_capture()
    evidence = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=capture,
    )
    preference = evidence["preference_artifact_memory"]
    evidence.clear()
    evidence.update(_valid_p6_runtime_evidence(live=True))
    evidence["preference_artifact_memory"] = preference
    return evidence


def _permission_sensitive_audit_runtime_evidence() -> dict:
    event_base = {
        "schema_version": "runtime_permission_audit_event.v1",
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
    tools = (
        "brain_approval_board_decide",
        "brain_object_proposal_create",
        "brain_object_decision_commit",
    )
    events = [
        {
            **event_base,
            "action": tool_name,
            "actor_ref_hash": "sha256:" + "a" * 64,
            "request_hash": "sha256:" + f"{index:x}" * 64,
        }
        for index, tool_name in enumerate(tools, start=1)
    ]
    return {
        "schema_version": "permission_sensitive_runtime_audit_evidence.v1",
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


def _valid_p8_runtime_evidence(*, live: bool = True):
    expected_commit = "bec7b38"
    desired_state = {
        "schema_version": "gitops_desired_state_identity.v1",
        "images_include_expected_commit": True,
        "desired_state_source": "sanitized_ops_manifest_summary",
        "target_revision": "main",
        "source_commit": expected_commit,
        "desired_image_set_hash": "sha256:" + "a" * 64,
        "ops_revision": "a" * 40,
        "expected_image_ref_count": 1,
        "production_mutation_performed": False,
    }
    deployed_identity = {
        "contains_expected_commit": True,
        "identity_source": "redacted_live_runtime_evidence",
        "source_commit": expected_commit,
        "live_image_set_hash": "sha256:" + "a" * 64,
        "stale_image_ref_count": 0,
        "production_mutation_performed": False,
    }
    return {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "expected_commit": expected_commit,
        "permission_sensitive_audit": _permission_sensitive_audit_runtime_evidence(),
        "deployed_identity": deployed_identity,
        "gitops_desired_state": desired_state,
        "argo_reconciliation": {
            "schema_version": "argo_reconciliation_identity.v1",
            "reconciliation_source": "sanitized_argo_application_summary",
            "reconciled_ops_revision": "a" * 40,
            "sync_status": "Synced",
            "health_status": "Healthy",
            "production_mutation_performed": False,
        },
        "deployment_evidence_binding": build_deployment_evidence_binding(
            expected_commit=expected_commit,
            gitops_desired_state=desired_state,
            argo_reconciliation={
                "schema_version": "argo_reconciliation_identity.v1",
                "reconciliation_source": "sanitized_argo_application_summary",
                "reconciled_ops_revision": "a" * 40,
                "sync_status": "Synced",
                "health_status": "Healthy",
                "production_mutation_performed": False,
            },
            deployed_identity=deployed_identity,
        ),
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke" if live else "local_test_replay",
            "mutation_scope": "none",
            "network_used": live,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }


def _valid_p6_p7_p8_runtime_evidence(*, live: bool = True):
    evidence = _valid_p6_p7_runtime_evidence(live=live)
    p8 = _valid_p8_runtime_evidence(live=live)
    evidence["expected_commit"] = p8["expected_commit"]
    evidence["permission_sensitive_audit"] = p8["permission_sensitive_audit"]
    evidence["gitops_desired_state"] = p8["gitops_desired_state"]
    evidence["argo_reconciliation"] = p8["argo_reconciliation"]
    evidence["deployed_identity"] = p8["deployed_identity"]
    evidence["deployment_evidence_binding"] = p8["deployment_evidence_binding"]
    evidence["evidence_provenance"] = p8["evidence_provenance"]
    return evidence


def _valid_p9_agent_context_product(
    *,
    style_count: int = 1,
    active_count: int = 1,
    current_authority_count: int = 1,
    current_authority_lanes: tuple[str, ...] = ("accepted_current",),
    style_authority_lanes: tuple[str, ...] = ("accepted_current",),
):
    def section_item(section: str, authority_lane: str = "reference_only") -> list[dict]:
        return [
            {
                "object_id": f"ko:P9:{section}",
                "object_type": "P9Fixture",
                "authority_lane": authority_lane,
            }
        ]

    return {
        "schema_version": "agent_context_product_pack.v1",
        "consumer": "codex",
        "sections": {
            "current_authority": {
                "object_count": current_authority_count,
                "authority_lanes": list(current_authority_lanes),
                "items": section_item(
                    "current_authority",
                    current_authority_lanes[0] if current_authority_lanes else "reference_only",
                )
                if current_authority_count
                else [],
            },
            "style_preference": {
                "object_count": style_count,
                "authority_lanes": list(style_authority_lanes),
                "items": section_item(
                    "style_preference",
                    style_authority_lanes[0] if style_authority_lanes else "reference_only",
                )
                if style_count
                else [],
            },
            "active_work": {
                "object_count": active_count,
                "authority_lanes": ["reference_only"],
                "items": section_item("active_work") if active_count else [],
            },
            "required_verification": {
                "object_count": 1,
                "authority_lanes": ["reference_only"],
                "items": section_item("required_verification"),
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
        "degraded_mode": {"active": False, "gaps": []},
        "missing_evidence_before_promotion": [],
        "action_hints": [
            {
                "action": "request_missing_evidence",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": [],
            },
            {
                "action": "promote_authority",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["approved_scope_required"],
            },
        ],
        "tool_hints": object_native_review_tool_hints([]),
    }


def _valid_p9_startup_runtime_evidence(
    *,
    product: dict | None = None,
):
    product = product or _valid_p9_agent_context_product()
    context_pack = {
        "schema_version": "llm_brain_context_resolve.v1",
        "authority": {"agent_context_product": product},
    }
    route_smokes = _valid_p9_route_smokes()
    challenge = build_agent_context_consumer_challenge(
        consumer="codex",
        project="neurons",
        repository="pureliture/neurons",
        branch="main",
        expected_commit="a" * 40,
        endpoint_origin="https://mcp.invalid",
        now=_P9_STARTUP_NOW,
        nonce="p9-golden-query-startup",
    )
    receipt = build_agent_context_consumer_startup_receipt(
        challenge=challenge,
        proof_key=_P9_STARTUP_PROOF_KEY,
        context_pack=context_pack,
        route_smokes=route_smokes,
        now=_P9_STARTUP_NOW,
        process_instance_seed="p9-golden-query-startup",
    )
    runtime = build_agent_context_startup_runtime_evidence(
        receipt=receipt,
        challenge=challenge,
        proof_key=_P9_STARTUP_PROOF_KEY,
        context_pack=context_pack,
        route_smokes=route_smokes,
        now=_P9_STARTUP_NOW,
    )
    runtime["collector_execution"] = {
        "runner_kind": "default_external_subprocess",
        "subprocess_attested": True,
    }
    return runtime


def _valid_p9_route_smokes():
    smokes = [
        {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "production_mutation_performed": False,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "recommended_actions": [],
                "lanes": {},
                "gaps": [],
            },
        }
        for route in _REQUIRED_ROUTE_NAMES
    ]
    for smoke in smokes:
        smoke["semantic_payload_hash"] = hash_payload(
            {"route": smoke["route"], "projection": "stable"}
        )
        smoke["source_payload_hash"] = hash_payload(
            {"route": smoke["route"], "observed_at": _P9_STARTUP_NOW.isoformat()}
        )
    return smokes


def _valid_p6_p7_p8_p9_runtime_evidence(*, live: bool = True):
    evidence = _valid_p6_p7_p8_runtime_evidence(live=live)
    source_product = _valid_p9_agent_context_product()
    product = deepcopy(source_product)
    product["source_payload_hash"] = hash_payload(source_product)
    route_smokes = _valid_p9_route_smokes()
    startup = _valid_p9_startup_runtime_evidence(product=source_product)
    startup["capture_bundle_binding"] = {
        "schema_version": "agent_context_capture_bundle_binding.v1",
        "agent_context_product_projection_hash": hash_payload(product),
        "source_product_hash": product["source_payload_hash"],
        "route_smoke_projection_hashes": {
            smoke["route"]: hash_payload(smoke) for smoke in route_smokes
        },
    }
    evidence["agent_context_product"] = product
    evidence["brain_objects_query_smokes"] = route_smokes
    evidence["agent_context_startup_runtime"] = startup
    if live:
        # The collector integration itself is covered in test_post_deploy_mcp_capture.
        evidence = _mint_collector_attested_evidence(
            evidence,
            attested_fields={
                "agent_context_startup_runtime",
                "preference_artifact_memory",
            },
        )
    return evidence


def test_golden_query_baseline_records_current_low_quality_failures():
    report = build_baseline_golden_query_report()

    assert report["schema_version"] == "knowledge_object_golden_query_eval.v1"
    assert len(report["queries"]) >= 10
    assert report["status"] == "baseline_red"
    assert all(item["passes"] is False for item in report["queries"])
    assert report["queries"][1]["query"] == "이 repo 문서 최신화하려면 뭘 봐야 해?"


def test_eval_requires_lane_evidence_gap_and_recommended_action():
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {
            "route": "documentation_cleanup",
            "lanes": {"accepted_current": []},
            "evidence": [],
            "gaps": [],
            "recommended_actions": [],
        },
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {
            "route": "documentation_cleanup",
            "lanes": {"accepted_current": [{"object_id": "ko:RepoDocument:readme"}]},
            "evidence": [{"evidence_id": "ev:source_hash:readme"}],
            "gaps": [],
            "recommended_actions": [{"object_id": "ko:RepoDocument:readme", "action": "keep"}],
        },
    )

    assert failing["passes"] is False
    assert "missing_evidence_or_gap" in failing["failures"]
    assert "missing_recommended_action" in failing["failures"]
    assert passing["passes"] is True


def test_eval_strict_axes_require_edge_freshness_and_gap_fields():
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {
            "route": "code_change_impact",
            "lanes": {"candidate": [{"object_id": "ko:Commit:change"}]},
            "evidence": [{"evidence_id": "ev:test"}],
            "recommended_actions": [{"object_id": "ko:Commit:change", "action": "run_tests"}],
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {
            "route": "code_change_impact",
            "lanes": {"candidate": [{"object_id": "ko:Commit:change"}]},
            "edges": [{"edge_id": "ke:validated_by:test", "edge_type": "validated_by"}],
            "evidence": [{"evidence_id": "ev:test", "verification_state": "test_verified"}],
            "verification": {
                "freshness_checked": [{"evidence_id": "ev:test"}],
                "runtime_verified": [],
                "runtime_unverified": [{"reason": "live_runtime_impact_unverified"}],
            },
            "gaps": [],
            "recommended_actions": [{"object_id": "ko:Commit:change", "action": "run_tests"}],
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "missing_edge" in failing["failures"]
    assert "missing_freshness" in failing["failures"]
    assert "missing_gap_field" in failing["failures"]
    assert passing["passes"] is True
    assert passing["checked_axes"] == REQUIRED_QUALITY_AXES


def test_eval_strict_axes_require_empty_authority_lane_disclosure():
    base_response = {
        "route": "documentation_cleanup",
        "lanes": {
            "accepted_current": [],
            "proposal_only": [{"object_id": "ko:RepoDocument:legacy"}],
        },
        "edges": [{"edge_id": "ke:requires_evidence:legacy", "edge_type": "requires_evidence"}],
        "evidence": [{"evidence_id": "ev:inventory:legacy", "verification_state": "source_hash_verified"}],
        "verification": {"freshness_checked": [{"evidence_id": "ev:inventory:legacy"}]},
        "recommended_actions": [{"object_id": "ko:RepoDocument:legacy", "action": "review_archive"}],
    }
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {**base_response, "gaps": []},
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {**base_response, "gaps": ["accepted_current documents empty"]},
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "empty_authority_lane_not_stated:accepted_current" in failing["failures"]
    assert passing["passes"] is True


def test_eval_strict_axes_require_freshness_specific_verification():
    response = {
        "route": "documentation_cleanup",
        "lanes": {"candidate": [{"object_id": "ko:RepoDocument:legacy"}]},
        "edges": [{"edge_id": "ke:requires_evidence:legacy", "edge_type": "requires_evidence"}],
        "evidence": [{"evidence_id": "ev:inventory:legacy", "verification_state": "source_hash_verified"}],
        "verification": {"unverified": [{"evidence_id": "ev:inventory:legacy"}]},
        "gaps": [],
        "recommended_actions": [{"object_id": "ko:RepoDocument:legacy", "action": "review_archive"}],
    }

    result = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        response,
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert result["passes"] is False
    assert "missing_freshness" in result["failures"]


def test_eval_strict_axes_require_runtime_evidence_for_runtime_claims():
    base_response = {
        "route": "deployment_runtime_truth",
        "lanes": {"candidate": [{"object_id": "ko:RuntimeTruth:deploy"}]},
        "edges": [{"edge_id": "ke:validated_by:deploy", "edge_type": "validated_by"}],
        "evidence": [{"evidence_id": "ev:pr-merge", "verification_state": "source_hash_verified"}],
        "gaps": [],
        "recommended_actions": [{"object_id": "ko:RuntimeTruth:deploy", "action": "verify_runtime"}],
    }
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[4],
        {**base_response, "verification": {"runtime_verified": [], "runtime_unverified": []}},
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[4],
        {
            **base_response,
            "verification": {
                "freshness_checked": [{"evidence_id": "ev:runtime-freshness"}],
                "runtime_verified": [],
                "runtime_unverified": [{"reason": "runtime_evidence_unverified"}],
            },
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "runtime_evidence_missing" in failing["failures"]
    assert passing["passes"] is True


def test_eval_strict_axes_detect_korean_runtime_claims():
    base_response = {
        "route": "code_change_impact",
        "lanes": {"candidate": [{"object_id": "ko:RuntimeSurface:lbrain"}]},
        "edges": [{"edge_id": "ke:requires_live_evidence:lbrain", "edge_type": "requires_live_evidence"}],
        "evidence": [{"evidence_id": "ev:test", "verification_state": "freshness_checked"}],
        "verification": {"freshness_checked": [{"evidence_id": "ev:test"}]},
        "gaps": [],
        "recommended_actions": [{"object_id": "ko:RuntimeSurface:lbrain", "action": "verify_runtime"}],
    }
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {**base_response, "verification": {"freshness_checked": [{"evidence_id": "ev:test"}]}},
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {
            **base_response,
            "verification": {
                "freshness_checked": [{"evidence_id": "ev:test"}],
                "runtime_verified": [],
                "runtime_unverified": [{"reason": "live_runtime_impact_unverified"}],
            },
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "runtime_evidence_missing" in failing["failures"]
    assert passing["passes"] is True


def test_code_change_impact_pack_passes_strict_axes_with_runtime_gap():
    pack = build_code_change_impact_pack(
        current_files=["worker/lib/agent_knowledge/llm_brain_core/objects/runtime_readiness.py"],
        consumer="codex",
    )

    result = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert result["passes"] is True


def test_phase_golden_query_coverage_reports_green_release_gate_with_gaps():
    report = build_phase_golden_query_coverage_report()

    assert report["schema_version"] == "knowledge_object_phase_golden_query_coverage.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["release_quality_gate"] == "green"
    assert "production_quality_not_green" not in report["gaps"]
    phases = {item["phase"]: item for item in report["phases"]}
    assert set(phases) >= {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"}
    assert phases["P1"]["result"] == "PASS_WITH_GAPS"
    assert phases["P5"]["result"] == "PASS"
    assert phases["P5"]["gaps"] == []
    assert phases["P4"]["golden_query_family"] == "review queue and authority promotion"
    assert phases["P4"]["result"] == "PASS_WITH_GAPS"
    assert phases["P4"]["required_axes"] == [
        "object",
        "edge",
        "evidence",
        "freshness",
        "gap",
        "recommended_action",
    ]
    assert "production_authority_pilot_not_executed" in phases["P4"]["gaps"]
    assert phases["P6"]["result"] == "PASS_WITH_GAPS"
    assert "handoff_pack_not_implemented" not in phases["P6"]["gaps"]
    assert "live_multi_device_rollup_unproven" in phases["P6"]["gaps"]
    assert phases["P7"]["result"] == "PASS_WITH_GAPS"
    assert phases["P7"]["golden_query_family"] == "code style drift"
    assert "accepted_preference_context_pack_live_unproven" in phases["P7"]["gaps"]
    assert phases["P8"]["result"] == "PASS_WITH_GAPS"
    assert phases["P8"]["golden_query_family"] == "pr merge and deploy truth"
    assert "live_runtime_rollout_identity_unproven" in phases["P8"]["gaps"]
    assert phases["P9"]["result"] == "PASS_WITH_GAPS"
    assert phases["P9"]["golden_query_family"] == "agent context productization"
    assert "production_consumer_context_pack_live_unproven" in phases["P9"]["gaps"]


def test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation():
    report = build_source_to_authority_quality_gate_report()

    assert report["schema_version"] == "source_to_authority_quality_gate_report.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["local_quality_gate"] == "green"
    assert report["release_quality_gate"] == "green"
    assert report["production_mutation_performed"] is False
    assert report["production_approval_gate"] == "preapproved"
    assert report["production_mutation_execution"] == "not_performed_by_local_gate"
    assert report["authority_write_scope"] == "local_test"
    checks = {item["id"]: item for item in report["path_checks"]}

    assert set(checks) >= {
        "source_to_candidate_graph",
        "candidate_review_edit",
        "approval_board_local_test",
        "authority_read_after_write",
        "production_decision_denial",
    }
    assert checks["source_to_candidate_graph"]["result"] == "PASS"
    assert checks["source_to_candidate_graph"]["quality_eval"]["passes"] is True
    assert checks["candidate_review_edit"]["result"] == "PASS"
    assert checks["candidate_review_edit"]["target_scope"] == "production"
    assert checks["candidate_review_edit"]["mutation_mode"] == "no_mutation"
    assert checks["candidate_review_edit"]["accepted_edit_actions"] == [
        "update_object",
        "add_evidence",
        "add_edge",
        "remove_edge",
        "remove_evidence",
    ]
    assert checks["candidate_review_edit"]["updated_edge_count"] == 1
    assert checks["candidate_review_edit"]["updated_evidence_count"] == 1
    assert checks["approval_board_local_test"]["result"] == "PASS"
    assert checks["authority_read_after_write"]["result"] == "PASS"
    assert checks["authority_read_after_write"]["quality_eval"]["passes"] is True
    assert checks["production_decision_denial"]["result"] == "PASS"
    assert checks["production_decision_denial"]["production_mutation_performed"] is False
    surface_checks = {item["id"]: item for item in report["product_surface_checks"]}
    assert set(surface_checks) >= {
        "mcp_brain_objects_query_tool",
        "mcp_source_to_candidate_graph_tool",
        "mcp_candidate_review_edit_tool",
        "mcp_approval_board_decide_tool",
        "mcp_source_to_candidate_runtime_readiness_tool",
    }
    assert surface_checks["mcp_brain_objects_query_tool"]["result"] == "PASS"
    assert surface_checks["mcp_brain_objects_query_tool"]["tool"] == "brain_objects_query"
    assert surface_checks["mcp_brain_objects_query_tool"]["production_mutation_performed"] is False
    assert surface_checks["mcp_source_to_candidate_graph_tool"]["result"] == "PASS"
    assert surface_checks["mcp_source_to_candidate_graph_tool"]["tool"] == "brain_source_to_candidate_graph"
    assert surface_checks["mcp_source_to_candidate_graph_tool"]["production_target_denied"] is True
    assert surface_checks["mcp_candidate_review_edit_tool"]["result"] == "PASS"
    assert surface_checks["mcp_candidate_review_edit_tool"]["authority_write_performed"] is False
    assert surface_checks["mcp_approval_board_decide_tool"]["result"] == "PASS"
    assert surface_checks["mcp_approval_board_decide_tool"]["production_target_denied"] is True
    assert surface_checks["mcp_source_to_candidate_runtime_readiness_tool"]["result"] == "PASS"
    assert surface_checks["mcp_source_to_candidate_runtime_readiness_tool"]["network_used"] is False
    assert surface_checks["mcp_source_to_candidate_runtime_readiness_tool"]["production_mutation_performed"] is False
    assert "production_authority_gate_preapproved_not_executed" in report["gaps"]
    assert "production_quality_not_green" not in report["gaps"]


def test_product_activation_progress_keeps_p2_to_p9_scope_visible():
    report = build_product_activation_progress_report()

    assert report["schema_version"] == "lbrain_product_activation_progress.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["goal_complete"] is False
    assert report["production_ready"] is False
    assert report["local_quality_gate"] == "green"
    assert report["release_quality_gate"] == "green"
    assert report["production_mutation_performed"] is False
    assert report["production_approval_gate"] == "preapproved"
    assert report["production_mutation_execution"] == "not_performed_by_local_gate"
    assert report["scope_phases"] == ["P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
    assert report["minimum_review_loop_checkpoint"]["phases"] == ["P2", "P3", "P4"]
    assert report["minimum_review_loop_checkpoint"]["status"] == "PASS_WITH_GAPS"
    assert report["next_phase"] == "P6"
    assert report["remaining_phases"] == ["P6", "P7", "P8", "P9"]
    assert report["hard_failures"] == []
    assert report["quality_gate_inputs"]["source_to_authority_local_quality_gate"] == "green"
    assert report["product_evidence_status"] == "PASS_WITH_GAPS"
    assert "production_quality_not_green" not in report["goal_completion_blockers"]
    assert "live_runtime_read_path_unverified" in report["goal_completion_blockers"]
    assert "future_phase_golden_query_slices_planned" not in report["goal_completion_blockers"]
    assert "future_phase_slices_planned" not in report["goal_completion_blockers"]
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    assert set(checks) == {"P2", "P3", "P4", "P6", "P7", "P8", "P9"}
    assert checks["P2"]["result"] == "PASS_WITH_GAPS"
    assert "p2_production_corpus_ingest_evidence_unverified" in checks["P2"]["gaps"]
    assert checks["P3"]["result"] == "PASS_WITH_GAPS"
    assert "p3_live_graph_qdrant_projection_join_unproven" in checks["P3"]["gaps"]
    assert checks["P4"]["result"] == "PASS_WITH_GAPS"
    assert "p4_replacement_current_execution_unverified" in checks["P4"]["gaps"]
    assert checks["P6"]["result"] == "PASS_WITH_GAPS"
    assert "p6_live_multi_device_rollup_unproven" in checks["P6"]["gaps"]
    assert checks["P7"]["result"] == "PASS_WITH_GAPS"
    assert "p7_accepted_preference_context_pack_live_unproven" in checks["P7"]["gaps"]
    assert "p7_html_artifact_review_live_unproven" in checks["P7"]["gaps"]
    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert "p8_runtime_evidence_unverified" in checks["P8"]["gaps"]
    assert "p8_runtime_evidence_collection_plan_not_live_evidence" in checks["P8"]["gaps"]
    assert "p8_runtime_evidence_packet_template_not_live_evidence" in checks["P8"]["gaps"]
    assert "p8_runtime_evidence_collector_not_live_evidence" in checks["P8"]["gaps"]
    assert "p8_shadow_route_smoke_collection_pending" in checks["P8"]["gaps"]
    assert "p8_shadow_route_smoke_collection_pending:deployment_runtime_truth" in checks["P8"]["gaps"]
    assert "p8_shadow_collection_run_pending" in checks["P8"]["gaps"]
    assert "p8_shadow_collection_run_pending:deployment_runtime_truth" in checks["P8"]["gaps"]
    assert checks["P9"]["result"] == "PASS_WITH_GAPS"
    assert "p9_runtime_evidence_unverified" in checks["P9"]["gaps"]
    assert "p9_production_consumer_context_pack_live_unproven" in checks["P9"]["gaps"]
    assert "p9_consumer_action_surface_runtime_policy_unproven" in checks["P9"]["gaps"]
    evidence = {item["phase"]: item for item in report["product_evidence_summary"]}
    assert set(evidence) == {"P2", "P3", "P4", "P6", "P7", "P8", "P9"}
    assert evidence["P2"]["schema_version"] == "reference_corpus_production_ingest_readiness.v1"
    assert evidence["P2"]["production_mutation_performed"] is False
    assert evidence["P3"]["schema_version"] == "source_to_candidate_projection_join_product_evidence.v1"
    assert evidence["P3"]["production_mutation_performed"] is False
    assert evidence["P3"]["projection_join_claim_status"] == "not_validated"
    assert "live_graph_qdrant_projection_join_unproven" in evidence["P3"]["gaps"]
    assert evidence["P6"]["schema_version"] == "object_extraction_session_project_rollup_preview.v1"
    assert evidence["P6"]["object_count"] >= 5
    assert evidence["P6"]["edge_count"] >= 6
    assert evidence["P6"]["evidence_count"] >= 1
    assert "live_multi_device_rollup_unproven" in evidence["P6"]["gaps"]
    assert evidence["P7"]["schema_version"] == "object_extraction_preference_style_preview.v1"
    assert evidence["P7"]["artifact_preference_pack_status"] == "pass"
    assert "accepted_preference_context_pack_live_unproven" in evidence["P7"]["gaps"]
    assert "html_artifact_review_live_unproven" in evidence["P7"]["gaps"]
    assert evidence["P8"]["schema_version"] == "object_extraction_runtime_truth_preview.v1"
    assert evidence["P8"]["runtime_unverified_count"] == 1
    assert evidence["P8"]["source_commit_matches_pr_head"] is True
    assert evidence["P8"]["permission"] == "allowed"
    assert evidence["P8"]["permission_reason"] == "approved_scope_present"
    assert evidence["P8"]["authority_write_performed"] is False
    assert (
        evidence["P8"]["runtime_evidence_collection_plan_schema"]
        == "source_to_candidate_runtime_evidence_collection_plan.v1"
    )
    assert evidence["P8"]["runtime_evidence_collection_plan_status"] == "ready"
    assert (
        evidence["P8"]["runtime_authority_bounded_execution_required_demote_step"]
        == "demote_prior_object_to_accepted_non_current_or_archive_only"
    )
    assert evidence["P8"]["runtime_authority_bounded_execution_demote_step_required"] is True
    assert evidence["P8"]["runtime_evidence_collection_plan_network_used"] is False
    assert evidence["P8"]["runtime_evidence_collection_plan_mutation_allowed"] is False
    assert evidence["P8"]["runtime_evidence_collection_plan_production_mutation_performed"] is False
    assert evidence["P8"]["runtime_evidence_collection_plan_readiness_claim"] == "plan_only_not_runtime_evidence"
    assert (
        evidence["P8"]["runtime_evidence_packet_template_schema"]
        == "source_to_candidate_runtime_evidence_packet_template.v1"
    )
    assert evidence["P8"]["runtime_evidence_packet_template_status"] == "template_ready"
    assert evidence["P8"]["runtime_evidence_packet_template_network_used"] is False
    assert evidence["P8"]["runtime_evidence_packet_template_mutation_allowed"] is False
    assert evidence["P8"]["runtime_evidence_packet_template_production_mutation_performed"] is False
    assert (
        evidence["P8"]["runtime_evidence_packet_template_readiness_claim"]
        == "template_only_not_runtime_evidence"
    )
    assert evidence["P8"]["runtime_evidence_packet_template_required_field_count"] >= 8
    assert evidence["P8"]["runtime_evidence_packet_template_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert (
        evidence["P8"]["shadow_route_smoke_request_schema"]
        == "source_to_candidate_runtime_shadow_collection_request.v1"
    )
    assert evidence["P8"]["shadow_route_smoke_request_status"] == "requested"
    assert evidence["P8"]["shadow_route_smoke_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert set(evidence["P8"]["shadow_route_smoke_pending_routes"]) == set(_REQUIRED_ROUTE_NAMES)
    assert evidence["P8"]["shadow_route_smoke_network_used"] is False
    assert evidence["P8"]["shadow_route_smoke_mutation_allowed"] is False
    assert evidence["P8"]["shadow_route_smoke_production_mutation_performed"] is False
    assert evidence["P8"]["shadow_route_smoke_readiness_claim"] == "request_only_not_live_evidence"
    assert (
        evidence["P8"]["shadow_collection_registration_schema"]
        == "source_to_candidate_runtime_shadow_collection_registration.v1"
    )
    assert evidence["P8"]["shadow_collection_registration_status"] == "registration_ready"
    assert evidence["P8"]["shadow_collection_registration_run_status"] == "not_run"
    assert evidence["P8"]["shadow_collection_registration_request_count"] == 1
    assert evidence["P8"]["shadow_collection_registration_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert set(evidence["P8"]["shadow_collection_registration_routes"]) == set(_REQUIRED_ROUTE_NAMES)
    assert evidence["P8"]["shadow_collection_registration_network_used"] is False
    assert evidence["P8"]["shadow_collection_registration_mutation_allowed"] is False
    assert evidence["P8"]["shadow_collection_registration_production_mutation_performed"] is False
    assert (
        evidence["P8"]["shadow_collection_registration_readiness_claim"]
        == "registration_only_not_runtime_evidence"
    )
    assert (
        evidence["P8"]["runtime_evidence_collector_packet_schema"]
        == "source_to_candidate_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert evidence["P8"]["runtime_evidence_collector_network_used"] is False
    assert evidence["P8"]["runtime_evidence_collector_production_mutation_performed"] is False
    assert (
        evidence["P8"]["runtime_evidence_collector_readiness_claim"]
        == "collector_packet_not_live_evidence"
    )
    assert (
        evidence["P8"]["runtime_evidence_post_deploy_capture_packet_schema"]
        == "source_to_candidate_runtime_evidence.v1"
    )
    assert (
        evidence["P8"]["runtime_evidence_post_deploy_capture_collection_mode"]
        == "post_deploy_read_only_smoke"
    )
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_network_used"] is True
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_production_mutation_performed"] is False
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_report_status"] == "PASS_WITH_GAPS"
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_production_ready"] is False
    assert (
        evidence["P8"]["runtime_evidence_collector_review_loop_schema"]
        == "source_to_candidate_review_loop_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_review_loop_candidate_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_review_loop_edited_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_review_loop_decision_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_review_loop_authority_scope"] == "local_test"
    assert (
        evidence["P8"]["runtime_evidence_collector_session_rollup_schema"]
        == "session_project_rollup_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_session_rollup_device_count"] >= 2
    assert evidence["P8"]["runtime_evidence_collector_session_rollup_visible_session_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_session_rollup_read_after_write_status"] == "validated"
    assert (
        evidence["P8"]["runtime_evidence_collector_preference_artifact_schema"]
        == "preference_artifact_memory_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_preference_accepted_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_preference_proposal_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_preference_html_route"] == "html_visualization_preference"
    assert evidence["P8"]["runtime_evidence_collector_preference_artifact_check_status"] == "pass"
    assert (
        evidence["P8"]["runtime_evidence_collector_permission_audit_schema"]
        == "permission_sensitive_runtime_audit_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_permission_audit_event_count"] == 3
    assert evidence["P8"]["runtime_evidence_collector_permission_audit_store_status"] == "recorded"
    assert (
        evidence["P8"]["runtime_evidence_collector_agent_context_startup_schema"]
        == "agent_context_startup_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_agent_context_startup_loaded"] is True
    assert evidence["P8"]["runtime_evidence_collector_agent_context_startup_read_path_tool"] == "brain_objects_query"
    assert evidence["P8"]["runtime_evidence_collector_agent_context_startup_route_count"] == len(
        _REQUIRED_ROUTE_NAMES
    )
    assert evidence["P9"]["schema_version"] == "agent_context_product_pack.v1"
    assert evidence["P9"]["section_counts"]["style_preference"] >= 1
    assert evidence["P9"]["section_counts"]["active_work"] >= 1
    assert evidence["P9"]["tool_hint_count"] >= 5
    assert evidence["P9"]["tool_hint_safe_target_count"] >= 5
    assert evidence["P9"]["unsafe_tool_hint_count"] == 0
    assert evidence["P9"]["mutation_allowed"] is False
    assert "production_consumer_context_pack_live_unproven" in evidence["P9"]["gaps"]
    assert "consumer_action_surface_runtime_policy_unproven" in evidence["P9"]["gaps"]
    assert all(item["production_mutation_performed"] is False for item in evidence.values())

    phase_progress = {item["phase"]: item for item in report["phase_progress"]}
    assert phase_progress["P4"]["quality_result"] == "PASS_WITH_GAPS"
    assert phase_progress["P5"]["state"] == "local_validated"
    assert phase_progress["P5"]["quality_result"] == "PASS"
    assert phase_progress["P5"]["gaps"] == []
    assert phase_progress["P6"]["state"] == "local_validated"
    assert phase_progress["P9"]["state"] == "local_validated"


def test_product_activation_progress_closes_p6_gap_with_live_session_project_rollup_evidence():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_runtime_evidence(live=True)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert checks["P6"]["result"] == "PASS"
    assert "p6_live_multi_device_rollup_unproven" not in checks["P6"]["gaps"]
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}
    assert phase_progress["P6"]["quality_result"] == "PASS"
    assert phase_progress["P6"]["gaps"] == []
    assert report["next_phase"] == "P7"
    assert report["remaining_phases"] == ["P7", "P8", "P9"]
    assert p6["rollup_claim_status"] == "validated"
    assert p6["live_evidence_provided"] is True
    assert p6["evidence_is_live"] is True
    assert p6["device_count"] == 2
    assert p6["visible_session_count"] == 2
    assert p6["all_device_session_count"] == 2
    assert p6["read_after_write_status"] == "validated"
    assert p6["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_closes_p7_gap_with_live_preference_artifact_evidence():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_p7_runtime_evidence(live=True)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p7 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P7")
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}

    assert checks["P6"]["result"] == "PASS"
    assert checks["P7"]["result"] == "PASS"
    assert "p7_accepted_preference_context_pack_live_unproven" not in checks["P7"]["gaps"]
    assert "p7_html_artifact_review_live_unproven" not in checks["P7"]["gaps"]
    assert phase_progress["P7"]["quality_result"] == "PASS"
    assert phase_progress["P7"]["gaps"] == []
    assert report["next_phase"] == "P8"
    assert report["remaining_phases"] == ["P8", "P9"]
    assert p7["preference_claim_status"] == "validated"
    assert p7["live_evidence_provided"] is True
    assert p7["evidence_is_live"] is True
    assert p7["accepted_preference_count"] == 1
    assert p7["proposal_preference_count"] == 1
    assert p7["html_route_status"] == "validated"
    assert p7["artifact_review_check_status"] == "pass"
    assert p7["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_accepts_live_current_preference_without_proposal():
    collected = _valid_p6_p7_runtime_evidence(live=True)
    evidence = dict(collected)
    preference = deepcopy(evidence["preference_artifact_memory"])
    pack = preference["preference_object_pack"]
    accepted = list(pack["lanes"]["accepted_current"])
    pack["proposal_preference_count"] = 0
    pack["objects"] = accepted
    pack["lanes"]["proposal_only"] = []
    pack["recommended_actions"] = [
        action
        for action in pack["recommended_actions"]
        if action["object_id"] == accepted[0]["object_id"]
    ]
    evidence["preference_artifact_memory"] = preference
    evidence = _mint_collector_attested_evidence(
        evidence,
        attested_fields={"preference_artifact_memory"},
    )

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p7 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P7")
    assert checks["P7"]["result"] == "PASS"
    assert "p7_preference_style_objects_missing" not in checks["P7"]["failures"]
    assert p7["preference_claim_status"] == "validated"
    assert p7["object_count"] == 1
    assert p7["accepted_preference_count"] == 1
    assert p7["proposal_preference_count"] == 0


def test_product_evaluator_keeps_two_object_expectation_for_local_p7_fixture():
    evidence = deepcopy(build_product_activation_progress_report()["product_evidence_summary"])
    p7 = next(item for item in evidence if item["phase"] == "P7")
    p7["object_count"] = 1

    result = evaluate_product_evidence_summary(evidence)

    check = next(item for item in result["checks"] if item["phase"] == "P7")
    assert check["result"] == "FAIL"
    assert "p7_preference_style_objects_missing" in check["failures"]


def test_product_evaluator_treats_none_p7_gaps_as_empty_list():
    evidence = deepcopy(build_product_activation_progress_report()["product_evidence_summary"])
    p7 = next(item for item in evidence if item["phase"] == "P7")
    p7["status"] = "PASS_WITH_GAPS"
    p7["preference_claim_status"] = "not_validated"
    p7["artifact_preference_pack_status"] = "pass_with_gaps"
    p7["gaps"] = None

    result = evaluate_product_evidence_summary(evidence)

    check = next(item for item in result["checks"] if item["phase"] == "P7")
    assert check["result"] == "FAIL"
    assert "p7_artifact_preference_pack_not_pass" in check["failures"]


def test_product_evaluator_keeps_replayed_one_object_p7_capture_as_gap():
    evidence = deepcopy(build_product_activation_progress_report()["product_evidence_summary"])
    p7 = next(item for item in evidence if item["phase"] == "P7")
    p7["status"] = "PASS_WITH_GAPS"
    p7["preference_claim_status"] = "not_validated"
    p7["artifact_preference_pack_status"] = "pass_with_gaps"
    p7["object_count"] = 1
    p7["accepted_preference_count"] = 1
    p7["proposal_preference_count"] = 0
    p7["source_evidence_ref_count"] = 1
    p7["gaps"] = ["preference_artifact_collector_capability_missing"]

    result = evaluate_product_evidence_summary(evidence)

    check = next(item for item in result["checks"] if item["phase"] == "P7")
    assert check["result"] == "PASS_WITH_GAPS"
    assert "p7_preference_style_objects_missing" not in check["failures"]
    assert "p7_artifact_preference_pack_not_pass" not in check["failures"]
    assert (
        "p7_preference_artifact_collector_capability_missing"
        in check["gaps"]
    )


def test_product_evaluator_fails_replayed_p7_capability_gap_without_current_object():
    evidence = deepcopy(build_product_activation_progress_report()["product_evidence_summary"])
    p7 = next(item for item in evidence if item["phase"] == "P7")
    p7["status"] = "PASS_WITH_GAPS"
    p7["preference_claim_status"] = "not_validated"
    p7["artifact_preference_pack_status"] = "pass_with_gaps"
    p7["object_count"] = 0
    p7["accepted_preference_count"] = 0
    p7["proposal_preference_count"] = 0
    p7["source_evidence_ref_count"] = 1
    p7["gaps"] = ["preference_artifact_collector_capability_missing"]

    result = evaluate_product_evidence_summary(evidence)

    check = next(item for item in result["checks"] if item["phase"] == "P7")
    assert check["result"] == "FAIL"
    assert "p7_preference_style_objects_missing" in check["failures"]


def test_product_activation_progress_fails_p7_when_preference_context_lacks_accepted_current_lane():
    evidence = _valid_p6_p7_runtime_evidence(live=True)
    evidence["preference_artifact_memory"]["agent_context_preference_section"] = {
        "schema_version": "agent_context_product_pack.v1",
        "section": "style_preference",
        "object_count": 1,
        "accepted_preference_count": 1,
        "authority_lanes": ["reference_only"],
        "surface_policy": {"mutation_allowed": False},
    }

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p7 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P7")
    assert checks["P7"]["result"] == "FAIL"
    assert (
        "p7_preference_artifact_agent_context_accepted_current_missing"
        in checks["P7"]["gaps"]
    )
    assert p7["preference_claim_status"] == "failed"
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p7_gap_without_runtime_preference_evidence_class():
    evidence = _valid_p6_p7_runtime_evidence(live=True)
    evidence["preference_artifact_memory"].pop("evidence_class")

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p7 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P7")

    assert checks["P6"]["result"] == "PASS"
    assert checks["P7"]["result"] == "PASS_WITH_GAPS"
    assert "p7_accepted_preference_context_pack_live_unproven" in checks["P7"]["gaps"]
    assert "p7_html_artifact_review_live_unproven" in checks["P7"]["gaps"]
    assert report["next_phase"] == "P7"
    assert report["remaining_phases"] == ["P7", "P8", "P9"]
    assert "preference_claim_status" not in p7
    assert p7["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is False


def test_product_activation_progress_fails_p7_when_artifact_review_returns_raw_body():
    evidence = _valid_p6_p7_runtime_evidence(live=True)
    preference = evidence["preference_artifact_memory"]
    preference["artifact_review_check"]["raw_artifact_body_returned"] = True

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p7 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P7")

    assert report["status"] == "FAIL"
    assert report["release_quality_gate"] == "blocked"
    assert checks["P7"]["result"] == "FAIL"
    assert "p7_preference_artifact_runtime_failed" in checks["P7"]["failures"]
    assert "p7_runtime_readiness_failed" in checks["P7"]["failures"]
    assert "p7_preference_artifact_raw_artifact_body_returned" in checks["P7"]["gaps"]
    assert p7["preference_claim_status"] == "failed"
    assert p7["artifact_review_check_status"] == "pass"
    assert p7["production_mutation_performed"] is False


def test_product_activation_progress_closes_p8_gap_with_live_runtime_authority_evidence():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_p7_p8_runtime_evidence(live=True),
        expected_commit="bec7b38",
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}

    assert checks["P6"]["result"] == "PASS"
    assert checks["P7"]["result"] == "PASS"
    assert checks["P8"]["result"] == "PASS"
    assert phase_progress["P8"]["quality_result"] == "PASS"
    assert phase_progress["P8"]["gaps"] == []
    assert report["next_phase"] == "P9"
    assert report["remaining_phases"] == ["P9"]
    assert p8["evidence_source"] == "live_runtime_authority_packet"
    assert p8["permission_audit_claim_status"] == "validated"
    assert p8["deployed_identity_claim_status"] == "validated"
    assert p8["evidence_provenance_status"] == "validated"
    assert p8["evidence_is_live"] is True
    assert p8["permission_audit_event_count"] == 3
    assert p8["permission_audit_store_status"] == "recorded"
    assert p8["source_commit_matches_pr_head"] is True
    assert p8["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_closes_p8_gitops_binding_gap_with_valid_binding():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=True)
    expected_commit = evidence["expected_commit"]
    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=evidence
    )

    report = build_product_activation_progress_report(
        live_evidence=packet,
        expected_commit=expected_commit,
    )
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "PASS"
    assert p8["gitops_deployment_binding_claim_status"] == "validated"


def test_product_activation_progress_fails_p8_for_tampered_gitops_binding():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=True)
    expected_commit = evidence["expected_commit"]
    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=evidence
    )
    packet["deployment_evidence_binding"]["canonical_tuple_hash"] = "sha256:" + "b" * 64

    report = build_product_activation_progress_report(
        live_evidence=packet,
        expected_commit=expected_commit,
    )
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}

    assert report["status"] == "FAIL"
    assert checks["P8"]["result"] == "FAIL"
    assert "p8_gitops_deployment_evidence_binding_hash_mismatch" in checks["P8"]["gaps"]


def test_product_activation_progress_detects_binding_only_packet_as_p8_failure():
    report = build_product_activation_progress_report(
        live_evidence={
            "schema_version": "source_to_candidate_runtime_evidence.v1",
            "deployment_evidence_binding": {
                "schema_version": "deployment_evidence_binding.v1",
                "canonical_tuple_hash": "sha256:" + "a" * 64,
            },
        },
        expected_commit="bec7b38",
    )
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}

    assert report["status"] == "FAIL"
    assert checks["P8"]["result"] == "FAIL"


def test_product_activation_progress_fails_p8_for_argo_nested_mutation():
    report = build_product_activation_progress_report(
        live_evidence={
            "schema_version": "source_to_candidate_runtime_evidence.v1",
            "argo_reconciliation": {
                "schema_version": "argo_reconciliation_identity.v1",
                "reconciliation_source": "sanitized_argo_application_summary",
                "reconciled_ops_revision": "a" * 40,
                "sync_status": "Synced",
                "health_status": "Healthy",
                "production_mutation_performed": True,
            },
        },
    )
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert report["status"] == "FAIL"
    assert checks["P8"]["result"] == "FAIL"
    assert p8["production_mutation_performed"] is True


def test_product_activation_progress_fails_p8_for_deployed_identity_nested_mutation():
    evidence = _valid_p6_p7_runtime_evidence(live=True)
    evidence["deployed_identity"] = {
        "contains_expected_commit": False,
        "identity_source": "redacted_live_runtime_evidence",
        "production_mutation_performed": True,
    }

    report = build_product_activation_progress_report(live_evidence=evidence)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert report["status"] == "FAIL"
    assert checks["P8"]["result"] == "FAIL"
    assert p8["deployed_identity_mutation_performed"] is True


def test_product_activation_progress_keeps_p8_permission_audit_gap_when_identity_only():
    evidence = _valid_p6_p7_runtime_evidence(live=True)
    p8 = _valid_p8_runtime_evidence(live=True)
    evidence["expected_commit"] = p8["expected_commit"]
    evidence["deployed_identity"] = p8["deployed_identity"]
    evidence["evidence_provenance"] = p8["evidence_provenance"]

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8_summary = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert "p8_permission_sensitive_audit_unverified" in checks["P8"]["gaps"]
    assert p8_summary["deployed_identity_claim_status"] == "validated"
    assert p8_summary["permission_audit_claim_status"] == "not_validated"
    assert p8_summary["source_commit_matches_pr_head"] is True
    assert report["next_phase"] == "P8"
    assert report["production_mutation_performed"] is False


def test_product_activation_progress_keeps_p8_live_gap_for_local_replay_runtime_authority_evidence():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_p7_p8_runtime_evidence(live=False)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert "p8_runtime_authority_evidence_not_live" in checks["P8"]["gaps"]
    assert p8["permission_audit_claim_status"] == "validated"
    assert p8["deployed_identity_claim_status"] == "validated"
    assert p8["evidence_is_live"] is False
    assert report["production_mutation_performed"] is False


def test_product_activation_progress_treats_unproven_p8_identity_as_gap_not_mismatch():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=False)
    evidence["deployed_identity"] = {
        "contains_expected_commit": False,
        "identity_source": "collector_not_deployed_identity_proof",
    }
    evidence.pop("deployment_evidence_binding")

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert "p8_source_commit_mismatch_with_pr_head" not in checks["P8"]["failures"]
    assert "p8_live_deployed_identity_expected_commit_unverified" in checks["P8"]["gaps"]
    assert p8["deployed_identity_claim_status"] == "not_validated"
    assert p8["source_commit_matches_pr_head"] is None
    assert p8["production_mutation_performed"] is False


def test_product_activation_progress_surfaces_gitops_desired_state_without_closing_p8_runtime():
    evidence = _valid_p6_p7_runtime_evidence(live=True)
    evidence["expected_commit"] = "e290495"
    evidence["gitops_desired_state"] = {
        "schema_version": "gitops_desired_state_identity.v1",
        "images_include_expected_commit": True,
        "desired_state_source": "sanitized_ops_manifest_summary",
        "target_revision": "main",
        "production_mutation_performed": False,
    }
    evidence["evidence_provenance"] = {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
        "collection_mode": "post_deploy_read_only_smoke",
        "mutation_scope": "none",
        "network_used": True,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert "p8_live_deployed_identity_unverified" in checks["P8"]["gaps"]
    assert "p8_permission_sensitive_audit_unverified" in checks["P8"]["gaps"]
    assert p8["gitops_desired_state_claim_status"] == "not_validated"
    assert p8["gitops_desired_state_matches_expected_commit"] is True
    assert p8["deployed_identity_claim_status"] == "not_validated"
    assert p8["production_mutation_performed"] is False
    assert report["next_phase"] == "P8"


def test_product_activation_progress_fails_when_gitops_desired_state_mismatches_expected_commit():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=True)
    evidence["gitops_desired_state"] = {
        "schema_version": "gitops_desired_state_identity.v1",
        "images_include_expected_commit": False,
        "desired_state_source": "sanitized_ops_manifest_summary",
        "target_revision": "main",
        "production_mutation_performed": False,
    }

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "FAIL"
    assert "p8_runtime_authority_live_failed" in checks["P8"]["failures"]
    assert "p8_gitops_desired_state_expected_commit_mismatch" in checks["P8"]["gaps"]
    assert p8["gitops_desired_state_claim_status"] == "failed"
    assert report["release_quality_gate"] == "blocked"
    assert report["production_mutation_performed"] is False


def test_product_activation_progress_propagates_gitops_desired_state_mutation_flag():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=True)
    evidence["gitops_desired_state"] = {
        "schema_version": "gitops_desired_state_identity.v1",
        "images_include_expected_commit": True,
        "desired_state_source": "sanitized_ops_manifest_summary",
        "target_revision": "main",
        "production_mutation_performed": True,
    }

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "FAIL"
    assert "p8_production_mutation_performed" in checks["P8"]["failures"]
    assert "p8_gitops_desired_state_mutation_invalid" in checks["P8"]["gaps"]
    assert p8["gitops_desired_state_claim_status"] == "failed"
    assert p8["gitops_desired_state_mutation_performed"] is True
    assert p8["production_mutation_performed"] is True
    assert report["production_mutation_performed"] is True


def test_product_activation_progress_scopes_p8_no_mutation_to_permission_audit_claim():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=True)
    replacement = _valid_p4_replacement_current_runtime_evidence(live=True)
    evidence["production_authority_replacement_current"] = replacement[
        "production_authority_replacement_current"
    ]

    report = build_product_activation_progress_report(
        live_evidence=evidence,
        expected_commit=evidence["expected_commit"],
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert checks["P8"]["result"] == "PASS"
    assert "p8_production_mutation_performed" not in checks["P8"]["failures"]
    assert p8["permission_audit_claim_status"] == "validated"
    assert p8["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is True


def test_product_activation_progress_fails_p8_when_permission_audit_returns_protected_values():
    evidence = _valid_p6_p7_p8_runtime_evidence(live=True)
    event = evidence["permission_sensitive_audit"]["audit_events"][0]
    event["protected_values_returned"] = True

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p8 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P8")

    assert report["status"] == "FAIL"
    assert report["release_quality_gate"] == "blocked"
    assert checks["P8"]["result"] == "FAIL"
    assert "p8_runtime_authority_live_failed" in checks["P8"]["failures"]
    assert "p8_permission_sensitive_audit_runtime_failed" in checks["P8"]["failures"]
    assert (
        "p8_permission_sensitive_audit_protected_values_returned:brain_approval_board_decide"
        in checks["P8"]["gaps"]
    )
    assert p8["permission_audit_claim_status"] == "failed"
    assert p8["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is False


def test_product_activation_progress_keeps_p9_bounded_codex_startup_gaps_visible():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_p7_p8_p9_runtime_evidence(live=True)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}

    assert checks["P9"]["result"] == "PASS_WITH_GAPS"
    assert phase_progress["P9"]["quality_result"] == "PASS_WITH_GAPS"
    assert "production_consumer_context_pack_live_unproven" in phase_progress["P9"]["gaps"]
    assert "consumer_action_surface_runtime_policy_unproven" in phase_progress["P9"]["gaps"]
    assert p9["evidence_source"] == "live_agent_context_packet"
    assert p9["product_sections_claim_status"] == "validated"
    assert p9["tool_hints_claim_status"] == "validated"
    assert p9["startup_read_path_claim_status"] == "not_validated"
    assert p9["evidence_provenance_status"] == "validated"
    assert p9["evidence_is_live"] is True
    assert p9["status"] == "PASS_WITH_GAPS"
    assert "p9_agent_context_consumer_startup_unvalidated:claude-code" in checks["P9"]["gaps"]
    assert "p9_agent_context_consumer_startup_unvalidated:gemini" in checks["P9"]["gaps"]
    assert "p9_agent_context_consumer_startup_unvalidated:hermes" in checks["P9"]["gaps"]
    assert (
        "p9_agent_context_action_surface_runtime_interception_unvalidated"
        in checks["P9"]["gaps"]
    )
    assert "p9_agent_context_codex_host_startup_hook_unvalidated" in checks["P9"]["gaps"]
    assert p9["section_counts"]["style_preference"] == 1
    assert p9["section_counts"]["active_work"] == 1
    assert p9["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p9_gap_for_empty_live_agent_context_sections():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    product = _valid_p9_agent_context_product(
        style_count=0,
        active_count=0,
    )
    evidence["agent_context_product"] = product
    evidence["agent_context_startup_runtime"] = _valid_p9_startup_runtime_evidence(product=product)

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "FAIL"
    assert "p9_agent_context_startup_read_path_failed" in checks["P9"]["failures"]
    assert "p9_live_agent_context_section_missing:style_preference" in checks["P9"]["gaps"]
    assert "p9_live_agent_context_section_missing:active_work" in checks["P9"]["gaps"]
    assert "p9_agent_context_startup_section_missing:style_preference" in checks["P9"]["gaps"]
    assert "p9_agent_context_startup_section_missing:active_work" in checks["P9"]["gaps"]
    assert p9["product_sections_claim_status"] == "not_validated"
    assert p9["startup_read_path_claim_status"] == "failed"
    assert p9["section_counts"]["style_preference"] == 0
    assert p9["section_counts"]["active_work"] == 0
    assert p9["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p9_gap_for_reference_only_current_authority():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    product = _valid_p9_agent_context_product(
        current_authority_lanes=("reference_only",),
    )
    evidence["agent_context_product"] = product
    evidence["agent_context_startup_runtime"] = _valid_p9_startup_runtime_evidence(product=product)

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "FAIL"
    assert "p9_agent_context_startup_read_path_failed" in checks["P9"]["failures"]
    assert (
        "p9_live_agent_context_current_authority_accepted_current_missing"
        in checks["P9"]["gaps"]
    )
    assert p9["product_sections_claim_status"] == "not_validated"
    assert p9["section_counts"]["current_authority"] == 1
    assert p9["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p9_gap_for_reference_only_style_preference():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    product = _valid_p9_agent_context_product(
        style_authority_lanes=("reference_only",),
    )
    evidence["agent_context_product"] = product
    evidence["agent_context_startup_runtime"] = _valid_p9_startup_runtime_evidence(product=product)

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "FAIL"
    assert "p9_agent_context_startup_read_path_failed" in checks["P9"]["failures"]
    assert (
        "p9_live_agent_context_style_preference_accepted_current_missing"
        in checks["P9"]["gaps"]
    )
    assert p9["product_sections_claim_status"] == "not_validated"
    assert p9["section_counts"]["style_preference"] == 1
    assert p9["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p9_gap_when_live_evidence_omits_startup_proof():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    evidence.pop("agent_context_startup_runtime")

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "PASS_WITH_GAPS"
    assert "p9_live_agent_context_startup_unverified" in checks["P9"]["gaps"]
    assert "p9_production_startup_read_path_unproven" in checks["P9"]["gaps"]
    assert "p9_consumer_action_surface_runtime_policy_unproven" in checks["P9"]["gaps"]
    assert p9["product_sections_claim_status"] == "validated"
    assert p9["startup_read_path_claim_status"] == "not_validated"
    assert p9["status"] == "PASS_WITH_GAPS"
    assert p9["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_fails_p9_for_self_declared_live_startup_receipt():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    evidence["agent_context_startup_runtime"]["evidence_origin"] = "server_runtime"

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "FAIL"
    assert "p9_agent_context_startup_read_path_failed" in checks["P9"]["failures"]
    assert "p9_agent_context_startup_external_consumer_receipt_missing" in checks["P9"]["gaps"]
    assert p9["startup_read_path_claim_status"] == "failed"
    assert p9["status"] == "FAIL"
    assert report["production_ready"] is False


def test_product_activation_progress_fails_p9_for_malformed_live_startup_receipt():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    evidence["agent_context_startup_runtime"]["startup_receipt"]["receipt_hash"] = (
        "sha256:" + "0" * 64
    )

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "FAIL"
    assert "p9_agent_context_startup_read_path_failed" in checks["P9"]["failures"]
    assert "p9_agent_context_startup_receipt_hash_mismatch" in checks["P9"]["gaps"]
    assert p9["startup_read_path_claim_status"] == "failed"
    assert p9["status"] == "FAIL"
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p9_live_gap_for_local_agent_context_replay():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_p7_p8_p9_runtime_evidence(live=False)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert checks["P9"]["result"] == "PASS_WITH_GAPS"
    assert "p9_agent_context_evidence_not_live" in checks["P9"]["gaps"]
    assert p9["product_sections_claim_status"] == "validated"
    assert p9["startup_read_path_claim_status"] == "validated"
    assert p9["evidence_is_live"] is False
    assert report["production_ready"] is False


def test_product_activation_progress_fails_p9_when_agent_context_tool_hint_is_unsafe():
    evidence = _valid_p6_p7_p8_p9_runtime_evidence(live=True)
    for hint in evidence["agent_context_product"]["tool_hints"]:
        if hint["tool"] == "brain_approval_board_decide":
            hint["execute_allowed"] = True
            hint["production_mutation_allowed"] = True
            hint["safe_targets"] = ["production"]

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p9 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P9")

    assert report["status"] == "FAIL"
    assert checks["P9"]["result"] == "FAIL"
    assert "p9_agent_context_tool_hints_failed" in checks["P9"]["failures"]
    assert "p9_brain_approval_board_decide_tool_hint_execute_allowed" in checks["P9"]["gaps"]
    assert "p9_brain_approval_board_decide_tool_hint_production_mutation_allowed" in checks["P9"]["gaps"]
    assert "p9_brain_approval_board_decide_tool_hint_safe_targets_not_allowed" in checks["P9"]["gaps"]
    assert p9["tool_hints_claim_status"] == "failed"
    assert report["production_ready"] is False


def test_product_activation_progress_closes_p3_gap_with_live_projection_join_evidence():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p3_runtime_evidence(live=True)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p3 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P3")
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}

    assert checks["P3"]["result"] == "PASS"
    assert "p3_live_graph_qdrant_projection_join_unproven" not in checks["P3"]["gaps"]
    assert p3["projection_join_claim_status"] == "validated"
    assert p3["projection_join_edge_count"] == 2
    assert p3["live_evidence_provided"] is True
    assert p3["evidence_is_live"] is True
    assert p3["network_used"] is True
    assert p3["evidence_collection_network_used"] is True
    assert p3["production_mutation_performed"] is False
    assert "live_graph_qdrant_projection_join_unproven" not in phase_progress["P3"]["gaps"]
    assert "live_graph_qdrant_projection_join_unproven" not in report["goal_completion_blockers"]
    assert report["production_mutation_performed"] is False


def test_product_activation_progress_closes_p4_gap_with_live_replacement_current_evidence():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p4_replacement_current_runtime_evidence(live=True)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p4 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P4")
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}

    assert checks["P4"]["result"] == "PASS"
    assert p4["replacement_claim_status"] == "validated"
    assert p4["prior_authority_lane"] == "accepted_non_current"
    assert p4["successor_authority_lane"] == "accepted_current"
    assert p4["evidence_is_live"] is True
    assert p4["production_mutation_performed"] is True
    assert phase_progress["P4"]["quality_result"] == "PASS"
    assert "production_authority_pilot_not_executed" not in phase_progress["P4"]["gaps"]
    assert "production_authority_write_evidence_missing" not in phase_progress["P4"]["gaps"]
    assert "production_authority_pilot_not_executed" not in report["goal_completion_blockers"]
    assert "production_authority_write_evidence_missing" not in report["goal_completion_blockers"]
    assert report["production_mutation_performed"] is True
    assert report["production_ready"] is False


def test_product_activation_progress_keeps_p6_gap_when_rollup_evidence_is_not_live():
    report = build_product_activation_progress_report(
        live_evidence=_valid_p6_runtime_evidence(live=False)
    )

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert p6["rollup_claim_status"] == "validated"
    assert p6["live_evidence_provided"] is True
    assert p6["evidence_is_live"] is False
    assert checks["P6"]["result"] == "PASS_WITH_GAPS"
    assert "p6_session_project_rollup_evidence_not_live" in checks["P6"]["gaps"]


def test_product_activation_progress_fails_p6_when_live_provenance_is_not_redacted():
    evidence = _valid_p6_runtime_evidence(live=True)
    evidence["evidence_provenance"] = {
        **evidence["evidence_provenance"],
        "raw_private_evidence_returned": True,
    }

    report = build_product_activation_progress_report(live_evidence=evidence)

    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert report["status"] == "FAIL"
    assert checks["P6"]["result"] == "FAIL"
    assert p6["rollup_claim_status"] == "validated"
    assert p6["evidence_is_live"] is True
    assert p6["evidence_provenance_status"] == "failed"
    assert "p6_runtime_readiness_failed" in checks["P6"]["failures"]
    assert "p6_evidence_provenance_failed" in checks["P6"]["failures"]


def test_product_evidence_summary_fails_when_p8_source_commit_mismatches_pr_head():
    progress = build_product_activation_progress_report()
    evidence = [
        {**item, "source_commit_matches_pr_head": False}
        if item.get("phase") == "P8"
        else item
        for item in progress["product_evidence_summary"]
    ]

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P8:product_evidence_failed" in result["hard_failures"]
    assert "p8_source_commit_mismatch_with_pr_head" in checks["P8"]["failures"]


def test_product_evidence_summary_marks_missing_p8_source_commit_identity_as_gap():
    progress = build_product_activation_progress_report()
    evidence = []
    for item in progress["product_evidence_summary"]:
        if item.get("phase") == "P8":
            item = dict(item)
            item.pop("source_commit_matches_pr_head")
        evidence.append(item)

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "PASS_WITH_GAPS"
    assert "P8:product_evidence_failed" not in result["hard_failures"]
    assert "p8_source_commit_matches_pr_head_unverified" in checks["P8"]["gaps"]


def test_product_evidence_summary_fails_closed_when_required_phase_evidence_is_missing():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 1,
                "edge_count": 0,
                "evidence_count": 0,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_unverified_count": 0,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": True,
                "production_mutation_performed": True,
            },
        ]
    )

    assert result["status"] == "FAIL"
    assert "P2:product_evidence_missing" in result["hard_failures"]
    assert "P3:product_evidence_missing" in result["hard_failures"]
    assert "P6:product_evidence_failed" in result["hard_failures"]
    assert "P7:product_evidence_missing" in result["hard_failures"]
    assert "P8:product_evidence_failed" in result["hard_failures"]
    checks = {item["phase"]: item for item in result["checks"]}
    assert "p6_session_rollup_incomplete" in checks["P6"]["failures"]
    assert "p8_production_mutation_performed" in checks["P8"]["failures"]


def test_product_evidence_summary_fails_when_p6_claims_pass_without_live_evidence():
    progress = build_product_activation_progress_report(
        live_evidence=_valid_p6_runtime_evidence(live=True)
    )
    evidence = [
        {
            **item,
            "evidence_is_live": False,
        }
        if item.get("phase") == "P6"
        else item
        for item in progress["product_evidence_summary"]
    ]

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P6:product_evidence_failed" in result["hard_failures"]
    assert "p6_live_evidence_missing_for_pass" in checks["P6"]["failures"]


def test_product_evidence_summary_fails_when_p2_claims_pass_without_live_evidence():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P2",
                "schema_version": "reference_corpus_production_ingest_readiness.v1",
                "status": "PASS",
                "live_evidence_provided": False,
                "production_mutation_performed": True,
                "network_used": False,
                "gaps": [],
            }
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P2:product_evidence_failed" in result["hard_failures"]
    assert "p2_live_evidence_missing_for_pass" in checks["P2"]["failures"]


def test_product_evidence_summary_tracks_p3_projection_join_as_first_class_gate():
    progress = build_product_activation_progress_report()
    evidence = [
        {
            **item,
            "status": "PASS",
            "projection_join_claim_status": "validated",
            "projection_join_edge_count": 2,
            "evidence_is_live": True,
            "production_ready": False,
            "gaps": [],
        }
        if item.get("phase") == "P3"
        else item
        for item in progress["product_evidence_summary"]
    ]

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "PASS_WITH_GAPS"
    assert "P3:product_evidence_failed" not in result["hard_failures"]
    assert checks["P3"]["result"] == "PASS"
    assert checks["P3"]["failures"] == []
    assert "p3_live_graph_qdrant_projection_join_unproven" not in checks["P3"]["gaps"]


def test_product_evidence_summary_fails_when_p3_projection_join_is_unsafe():
    progress = build_product_activation_progress_report()
    evidence = [
        {
            **item,
            "status": "FAIL",
            "projection_join_claim_status": "failed",
            "projection_join_edge_count": 0,
            "evidence_is_live": True,
            "production_mutation_performed": True,
            "raw_private_evidence_returned": True,
            "secret_returned": True,
            "host_topology_returned": True,
            "raw_external_ids_returned": True,
            "gaps": [
                "projection_join_edge_count_missing",
                "projection_join_production_mutation_performed",
                "projection_join_raw_private_evidence_returned",
                "projection_join_secret_returned",
                "projection_join_host_topology_returned",
                "projection_join_raw_external_ids_returned",
            ],
        }
        if item.get("phase") == "P3"
        else item
        for item in progress["product_evidence_summary"]
    ]

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P3:product_evidence_failed" in result["hard_failures"]
    assert "p3_production_mutation_performed" in checks["P3"]["failures"]
    assert "p3_projection_join_failed" in checks["P3"]["failures"]
    assert "p3_projection_join_edge_count_missing" in checks["P3"]["failures"]
    assert "p3_projection_join_raw_private_evidence_returned" in checks["P3"]["failures"]


def test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P2",
                "schema_version": "reference_corpus_production_ingest_readiness.v1",
                "status": "PASS_WITH_GAPS",
                "production_mutation_performed": False,
                "gaps": ["production_corpus_ingest_evidence_unverified"],
            },
            _valid_p3_projection_join_evidence(),
            _valid_p4_replacement_current_product_evidence(),
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 8,
                "edge_count": 8,
                "evidence_count": 1,
                "handoff_pack_schema": "session_project_handoff_pack.v1",
                "production_mutation_performed": False,
            },
            {
                "phase": "P7",
                "schema_version": "object_extraction_preference_style_preview.v1",
                "object_count": 2,
                "artifact_preference_pack_status": "pass",
                "accepted_preference_count": 1,
                "source_evidence_ref_count": 1,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_verified_count": 0,
                "runtime_unverified_count": 1,
                "source_commit_matches_pr_head": True,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": False,
                "runtime_authority_bounded_execution_required_demote_step": (
                    "demote_prior_object_to_accepted_non_current_or_archive_only"
                ),
                "runtime_authority_bounded_execution_demote_step_required": True,
                "runtime_evidence_collection_plan_schema": "source_to_candidate_runtime_evidence_collection_plan.v1",
                "runtime_evidence_collection_plan_status": "ready",
                "runtime_evidence_collection_plan_network_used": False,
                "runtime_evidence_collection_plan_mutation_allowed": False,
                "runtime_evidence_collection_plan_production_mutation_performed": False,
                "runtime_evidence_collection_plan_readiness_claim": "plan_only_not_runtime_evidence",
                "runtime_evidence_packet_template_schema": "source_to_candidate_runtime_evidence_packet_template.v1",
                "runtime_evidence_packet_template_status": "template_ready",
                "runtime_evidence_packet_template_network_used": False,
                "runtime_evidence_packet_template_mutation_allowed": False,
                "runtime_evidence_packet_template_production_mutation_performed": False,
                "runtime_evidence_packet_template_readiness_claim": "template_only_not_runtime_evidence",
                "runtime_evidence_packet_template_required_field_count": 9,
                "runtime_evidence_packet_template_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_request_schema": "source_to_candidate_runtime_shadow_collection_request.v1",
                "shadow_route_smoke_request_status": "requested",
                "shadow_route_smoke_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_pending_routes": list(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_network_used": False,
                "shadow_route_smoke_mutation_allowed": False,
                "shadow_route_smoke_production_mutation_performed": False,
                "shadow_route_smoke_readiness_claim": "request_only_not_live_evidence",
                "shadow_collection_registration_schema": "source_to_candidate_runtime_shadow_collection_registration.v1",
                "shadow_collection_registration_status": "registration_ready",
                "shadow_collection_registration_run_status": "not_run",
                "shadow_collection_registration_request_count": 1,
                "shadow_collection_registration_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_collection_registration_routes": list(_REQUIRED_ROUTE_NAMES),
                "shadow_collection_registration_network_used": False,
                "shadow_collection_registration_mutation_allowed": False,
                "shadow_collection_registration_production_mutation_performed": False,
                "shadow_collection_registration_readiness_claim": "registration_only_not_runtime_evidence",
                "runtime_evidence_collector_packet_schema": "source_to_candidate_runtime_evidence.v1",
                "runtime_evidence_collector_route_count": len(_REQUIRED_ROUTE_NAMES),
                "runtime_evidence_collector_network_used": False,
                "runtime_evidence_collector_production_mutation_performed": False,
                "runtime_evidence_collector_readiness_claim": "collector_packet_not_live_evidence",
                "runtime_evidence_post_deploy_capture_packet_schema": "source_to_candidate_runtime_evidence.v1",
                "runtime_evidence_post_deploy_capture_collection_mode": "post_deploy_read_only_smoke",
                "runtime_evidence_post_deploy_capture_network_used": True,
                "runtime_evidence_post_deploy_capture_production_mutation_performed": False,
                "runtime_evidence_post_deploy_capture_report_status": "PASS_WITH_GAPS",
                "runtime_evidence_post_deploy_capture_production_ready": False,
                "runtime_evidence_collector_review_loop_schema": "source_to_candidate_review_loop_evidence.v1",
                "runtime_evidence_collector_review_loop_candidate_count": 2,
                "runtime_evidence_collector_review_loop_edited_count": 1,
                "runtime_evidence_collector_review_loop_decision_count": 1,
                "runtime_evidence_collector_review_loop_authority_scope": "local_test",
                "runtime_evidence_collector_session_rollup_schema": "session_project_rollup_runtime_evidence.v1",
                "runtime_evidence_collector_session_rollup_device_count": 2,
                "runtime_evidence_collector_session_rollup_visible_session_count": 2,
                "runtime_evidence_collector_session_rollup_read_after_write_status": "validated",
                "runtime_evidence_collector_preference_artifact_schema": "preference_artifact_memory_runtime_evidence.v1",
                "runtime_evidence_collector_preference_accepted_count": 1,
                "runtime_evidence_collector_preference_proposal_count": 1,
                "runtime_evidence_collector_preference_html_route": "html_visualization_preference",
                "runtime_evidence_collector_preference_artifact_check_status": "pass",
                "runtime_evidence_collector_permission_audit_schema": "permission_sensitive_runtime_audit_evidence.v1",
                "runtime_evidence_collector_permission_audit_event_count": 3,
                "runtime_evidence_collector_permission_audit_store_status": "recorded",
                "runtime_evidence_collector_agent_context_startup_schema": "agent_context_startup_runtime_evidence.v1",
                "runtime_evidence_collector_agent_context_startup_loaded": True,
                "runtime_evidence_collector_agent_context_startup_read_path_tool": "brain_objects_query",
                "runtime_evidence_collector_agent_context_startup_route_count": len(_REQUIRED_ROUTE_NAMES),
                "production_mutation_performed": False,
            },
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 1},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 0,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "PASS_WITH_GAPS"
    assert result["hard_failures"] == []
    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert checks["P8"]["failures"] == []
    assert checks["P8"]["gaps"] == [
        "p8_runtime_evidence_unverified",
        "p8_runtime_verified_evidence_missing",
        "p8_runtime_evidence_collection_plan_not_live_evidence",
        "p8_runtime_evidence_packet_template_not_live_evidence",
        "p8_runtime_evidence_collector_not_live_evidence",
        "p8_shadow_route_smoke_collection_pending",
        *[
            f"p8_shadow_route_smoke_collection_pending:{route}"
            for route in _REQUIRED_ROUTE_NAMES
        ],
        "p8_shadow_collection_run_pending",
        *[
            f"p8_shadow_collection_run_pending:{route}"
            for route in _REQUIRED_ROUTE_NAMES
        ],
    ]


def test_product_evidence_summary_fails_when_p8_collection_plan_is_missing_or_mutating():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 8,
                "edge_count": 8,
                "evidence_count": 1,
                "handoff_pack_schema": "session_project_handoff_pack.v1",
                "production_mutation_performed": False,
            },
            {
                "phase": "P7",
                "schema_version": "object_extraction_preference_style_preview.v1",
                "object_count": 2,
                "artifact_preference_pack_status": "pass",
                "accepted_preference_count": 1,
                "source_evidence_ref_count": 1,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_verified_count": 0,
                "runtime_unverified_count": 1,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": False,
                "runtime_evidence_collection_plan_schema": "source_to_candidate_runtime_evidence_collection_plan.v1",
                "runtime_evidence_collection_plan_status": "ready",
                "runtime_evidence_collection_plan_network_used": True,
                "runtime_evidence_collection_plan_mutation_allowed": True,
                "runtime_evidence_collection_plan_production_mutation_performed": True,
                "runtime_evidence_collection_plan_readiness_claim": "runtime_verified",
                "runtime_evidence_packet_template_schema": "source_to_candidate_runtime_evidence_packet_template.v1",
                "runtime_evidence_packet_template_status": "completed",
                "runtime_evidence_packet_template_network_used": True,
                "runtime_evidence_packet_template_mutation_allowed": True,
                "runtime_evidence_packet_template_production_mutation_performed": True,
                "runtime_evidence_packet_template_readiness_claim": "runtime_verified",
                "runtime_evidence_packet_template_required_field_count": 0,
                "runtime_evidence_packet_template_route_count": 0,
                "shadow_route_smoke_request_schema": "source_to_candidate_runtime_shadow_collection_request.v1",
                "shadow_route_smoke_request_status": "requested",
                "shadow_route_smoke_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_pending_routes": list(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_network_used": True,
                "shadow_route_smoke_mutation_allowed": True,
                "shadow_route_smoke_production_mutation_performed": True,
                "shadow_route_smoke_readiness_claim": "runtime_verified",
                "shadow_collection_registration_schema": "source_to_candidate_runtime_shadow_collection_registration.v1",
                "shadow_collection_registration_status": "completed",
                "shadow_collection_registration_run_status": "completed",
                "shadow_collection_registration_request_count": 0,
                "shadow_collection_registration_route_count": 0,
                "shadow_collection_registration_routes": [],
                "shadow_collection_registration_network_used": True,
                "shadow_collection_registration_mutation_allowed": True,
                "shadow_collection_registration_production_mutation_performed": True,
                "shadow_collection_registration_readiness_claim": "runtime_verified",
                "runtime_evidence_post_deploy_capture_packet_schema": "not_runtime_evidence",
                "runtime_evidence_post_deploy_capture_collection_mode": "local_test_replay",
                "runtime_evidence_post_deploy_capture_network_used": False,
                "runtime_evidence_post_deploy_capture_production_mutation_performed": True,
                "runtime_evidence_post_deploy_capture_report_status": "PASS",
                "runtime_evidence_post_deploy_capture_production_ready": True,
                "production_mutation_performed": False,
            },
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 1},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 0,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P8:product_evidence_failed" in result["hard_failures"]
    assert "p8_runtime_evidence_collection_plan_used_network" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_collection_plan_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_collection_plan_mutated_production" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_collection_plan_claims_live_evidence" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_not_ready" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_used_network" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_mutated_production" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_packet_missing" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_collection_mode_missing" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_network_not_used" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_mutated_production" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_unexpected_report_status" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_claims_production_ready" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_claims_live_evidence" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_fields_missing" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_routes_missing" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_used_network" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_mutated_production" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_claims_live_evidence" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_not_ready" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_claims_run" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_requests_missing" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_routes_missing" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_used_network" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_mutated_production" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_claims_live_evidence" in checks["P8"]["failures"]


def test_product_evidence_summary_fails_when_p9_active_work_is_missing():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 8,
                "edge_count": 16,
                "evidence_count": 1,
                "handoff_pack_schema": "session_project_handoff_pack.v1",
                "production_mutation_performed": False,
            },
            {
                "phase": "P7",
                "schema_version": "object_extraction_preference_style_preview.v1",
                "object_count": 2,
                "artifact_preference_pack_status": "pass",
                "accepted_preference_count": 1,
                "source_evidence_ref_count": 1,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_unverified_count": 1,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": False,
                "production_mutation_performed": False,
            },
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 0},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 0,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P9:product_evidence_failed" in result["hard_failures"]
    assert "p9_active_work_section_missing" in checks["P9"]["failures"]


def test_product_evidence_summary_fails_when_p9_tool_hints_are_unsafe():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 1},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 1,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert checks["P9"]["result"] == "FAIL"
    assert "P9:product_evidence_failed" in result["hard_failures"]
    assert "p9_tool_hint_safety_violations" in checks["P9"]["failures"]

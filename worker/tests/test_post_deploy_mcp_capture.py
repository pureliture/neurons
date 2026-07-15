from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects.post_deploy_mcp_capture import (
    collect_source_to_candidate_post_deploy_mcp_capture,
    validate_post_deploy_mcp_url,
)
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_AGENT_CONTEXT_SECTIONS,
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
)
from agent_knowledge.public_safe_util import hash_payload


def _fake_agent_context_product(*, consumer: str = "codex") -> dict:
    missing_evidence = ["runtime_evidence_unverified"]
    return {
        "schema_version": "agent_context_product_pack.v1",
        "consumer": consumer,
        "sections": {
            "current_authority": {
                "object_count": 1,
                "items": [
                    {
                        "object_id": "fixture:current_authority",
                        "object_type": "RepoDocument",
                        "title": "current_authority",
                        "authority_lane": "accepted_current",
                        "recommended_action": "keep",
                    }
                ],
                "authority_lanes": ["accepted_current"],
                "gaps": [],
            },
            **{
                name: {
                    "object_count": 1,
                    "items": [
                        {
                            "object_id": f"fixture:{name}",
                            "object_type": "MemoryCard",
                            "title": name,
                            "authority_lane": (
                                "accepted_current" if name == "style_preference" else name
                            ),
                            "recommended_action": "read",
                        }
                    ],
                    "authority_lanes": [
                        "accepted_current" if name == "style_preference" else name
                    ],
                    "gaps": [],
                }
                for name in REQUIRED_AGENT_CONTEXT_SECTIONS
            },
        },
        "surface_policy": {
            "consumer": consumer,
            "read_only": True,
            "mutation_allowed": False,
            "allowed_actions": ["suggest_change", "run_verification", "request_missing_evidence"],
            "property_omissions": ["raw_body", "raw_source", "private_deploy_value", "secret"],
        },
        "degraded_mode": {"active": True, "gaps": missing_evidence},
        "freshness": {
            "stale_evidence_visible": False,
            "stale_memory_count": 0,
            "no_recent_source": False,
        },
        "missing_evidence_before_promotion": missing_evidence,
        "action_hints": [],
        "tool_hints": object_native_review_tool_hints(missing_evidence),
    }


def _runtime_projection_join_evidence(*, edge_count: int = 2) -> dict:
    return {
        "schema_version": "object_extraction_projection_join_preview.v1",
        "evidence_class": "runtime_projection_join",
        "status": "pass",
        "edge_count": edge_count,
        "production_mutation_performed": False,
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _session_project_rollup_runtime_evidence() -> dict:
    return {
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


def _preference_artifact_memory_evidence(
    *,
    actual_live_read_surfaces: bool = False,
    actual_consumer_proof: bool = True,
) -> dict:
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
    evidence = {
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
    }
    evidence["read_surface_alignment"] = {
        "status": "validated",
        "target_object_id": accepted_object["object_id"],
        "memory_id": memory_id,
        "card_content_hash": card_content_hash,
        "authority_proposal_id": authority_proposal_id,
        "project": "neurons",
        "source_content_hash": source_content_hash,
        "authority_decision_id": authority_decision_id,
        "code_style_preference_object_ids": [accepted_object["object_id"]],
        "html_visualization_preference_object_ids": [accepted_object["object_id"]],
        "style_preference_context_object_ids": [accepted_object["object_id"]],
    }
    if actual_consumer_proof:
        evidence["artifact_consumer_evidence"] = {
            "status": "validated",
            "consumer_provenance": {
                "consumer": "html_artifact_review_product",
                "workflow": "review_rendered_artifact",
                "evidence_kind": "actual_consumer_output",
            },
            "artifact_fingerprint": "sha256:" + "f" * 64,
            "finding_refs": ["finding:p7-density", "finding:p7-evidence"],
            "evidence_refs": ["evidence:p7-density", "evidence:p7-evidence"],
            "finding_count": 2,
            "evidence_ref_count": 2,
        }
    if actual_live_read_surfaces:
        evidence["evidence_class"] = "runtime_preference_artifact_memory"
        evidence["evidence_source"] = "actual_live_read_surfaces"
    return evidence


def _runtime_collected_packet(
    *,
    live: bool = False,
    session_project_rollup: bool = False,
    preference_artifact_memory: bool = False,
    preference_artifact_memory_actual: bool = False,
    preference_artifact_memory_consumer_proof: bool = True,
    production_mutation_performed: bool = False,
    provenance_overrides: dict | None = None,
) -> dict:
    packet = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "projection_join": _runtime_projection_join_evidence(),
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke" if live else "local_test_replay",
            "network_used": live,
            "mutation_scope": "none",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": production_mutation_performed,
    }
    packet["evidence_provenance"].update(provenance_overrides or {})
    if session_project_rollup:
        packet["session_project_rollup_runtime"] = _session_project_rollup_runtime_evidence()
    if preference_artifact_memory:
        packet["preference_artifact_memory"] = _preference_artifact_memory_evidence(
            actual_live_read_surfaces=preference_artifact_memory_actual,
            actual_consumer_proof=preference_artifact_memory_consumer_proof,
        )
    if not live:
        packet["collector"] = {
            "schema_version": "source_to_candidate_runtime_evidence_collector.v1",
            "readiness_claim": "collector_packet_not_live_evidence",
        }
    return packet


class _FakeMcpSession:
    def __init__(self) -> None:
        self.initialized = False
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=name)
                for name in ("brain_context_resolve", *REQUIRED_RUNTIME_TOOL_NAMES)
            ]
        )

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "brain_source_to_candidate_runtime_readiness":
            if arguments.get("collect_shadow_evidence") is True:
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=False,
                        session_project_rollup=True,
                        preference_artifact_memory=True,
                    ),
                )
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "schema_version": "source_to_candidate_runtime_evidence_collection_plan.v1",
                    "collection_mode": "post_deploy_read_only_smoke",
                    "network_used": False,
                    "production_mutation_performed": False,
                },
            )
        if name == "brain_context_resolve":
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "schema_version": "llm_brain_context_resolve.v1",
                    "authority": {
                        "agent_context_product": _fake_agent_context_product(
                            consumer=arguments.get("consumer", "codex")
                        )
                    },
                    "private_context_not_returned": True,
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
                    "edges": [],
                    "evidence": [],
                    "recommended_actions": [],
                    "lanes": {},
                    "gaps": [],
                },
            },
        )


def _actual_artifact_descriptor(**metric_overrides: int) -> dict:
    summary = (
        "Rendered HTML review artifact exposes objects, relationships, evidence, "
        "and explicit gate status."
    )
    metrics = {
        "object_count": 2,
        "relationship_count": 1,
        "evidence_count": 2,
        "gate_status_count": 1,
        "hidden_gap_count": 0,
        "protected_content_count": 0,
        **metric_overrides,
    }
    evidence_refs = ["artifact:rendered-review", "evidence:review-findings"]
    return {
        "artifact_type": "html_review_artifact",
        "summary": summary,
        "artifact_fingerprint": hash_payload(
            {
                "artifact_type": "html_review_artifact",
                "summary": summary,
                "metrics": metrics,
                "evidence_refs": evidence_refs,
            }
        ),
        "metrics": metrics,
        "evidence_refs": evidence_refs,
    }


def _artifact_preference_application_receipt(arguments: dict) -> dict:
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
        "repository_hash": hash_payload(arguments["repository"]),
        "branch_hash": hash_payload(arguments["branch"]),
        "artifact_type": arguments["artifact_type"],
        "artifact_fingerprint": arguments["artifact_fingerprint"],
        "summary_hash": hash_payload(arguments["summary"]),
        "metrics_hash": hash_payload(arguments["metrics"]),
        "evidence_refs_hash": hash_payload(arguments["evidence_refs"]),
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
    return {
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


def test_collect_post_deploy_mcp_capture_promotes_only_direct_named_consumer_receipt(
    monkeypatch,
    capsys,
):
    class _DirectConsumerSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_artifact_preference_application_receipt(arguments),
                )
            return await super().call_tool(name, arguments)

    session = _DirectConsumerSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    artifact_descriptor = _actual_artifact_descriptor()
    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=artifact_descriptor,
            session_factory=_fake_session_factory,
        )
    )

    direct_calls = [
        arguments
        for name, arguments in session.calls
        if name == "brain_artifact_preference_evaluate"
    ]
    assert len(direct_calls) == 1
    assert direct_calls[0]["project"] == "neurons"
    assert {
        key: direct_calls[0][key]
        for key in (
            "artifact_type",
            "summary",
            "artifact_fingerprint",
            "metrics",
            "evidence_refs",
        )
    } == artifact_descriptor
    assert all(
        arguments["project"] == "neurons"
        for name, arguments in session.calls
        if name
        in {
            "brain_source_to_candidate_runtime_readiness",
            "brain_context_resolve",
            "brain_objects_query",
            "brain_artifact_preference_evaluate",
        }
    )
    assert capture["artifact_preference_application_receipt"]["status"] == "PASS"
    assert capture["preference_artifact_memory"]["artifact_consumer_evidence"] == (
        capture["artifact_preference_application_receipt"]
    )
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promoted_to_live_evidence"
    ] is True
    assert capture["production_mutation_performed"] is False
    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.preference_artifact.memory"]["status"] == "validated"
    assert "live_preference_artifact_memory_unverified" not in report["gaps"]

    async def _return_capture(**_kwargs):
        return capture

    monkeypatch.setattr(
        object_cli,
        "collect_source_to_candidate_post_deploy_mcp_capture",
        _return_capture,
    )
    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--collect-post-deploy-mcp-capture",
                "--mcp-url",
                "https://mcp.example.test/mcp",
                "--expected-commit",
                "c2b8548",
            ]
        )
        == 0
    )
    cli_output = json.loads(capsys.readouterr().out)
    cli_claim = next(
        claim
        for claim in cli_output["runtime_readiness"]["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert cli_output["schema_version"] == "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
    assert cli_claim["status"] == "validated"


def test_collect_post_deploy_mcp_capture_rejects_authority_drift_after_evaluator():
    class _DriftingAuthoritySession(_FakeMcpSession):
        def __init__(self):
            super().__init__()
            self.runtime_read_count = 0

        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                self.runtime_read_count += 1
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                    preference_artifact_memory_actual=True,
                )
                if self.runtime_read_count > 1:
                    drifted = "decision:p7-html-review-density-drifted"
                    packet["preference_artifact_memory"]["read_surface_alignment"][
                        "authority_decision_id"
                    ] = drifted
                    for objects in (
                        packet["preference_artifact_memory"]["preference_object_pack"]["lanes"][
                            "accepted_current"
                        ],
                        packet["preference_artifact_memory"]["html_visualization_route_smoke"][
                            "object_pack"
                        ]["lanes"]["accepted_current"],
                        packet["preference_artifact_memory"]["agent_context_preference_section"][
                            "items"
                        ],
                    ):
                        objects[0]["authority_decision_id"] = drifted
                return SimpleNamespace(isError=False, structuredContent=packet)
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_artifact_preference_application_receipt(arguments),
                )
            return await super().call_tool(name, arguments)

    session = _DriftingAuthoritySession()

    @asynccontextmanager
    async def session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=_actual_artifact_descriptor(),
            session_factory=session_factory,
        )
    )

    assert session.runtime_read_count == 2
    assert "artifact_preference_application_receipt" not in capture
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promoted_to_live_evidence"
    ] is False


def test_collect_post_deploy_mcp_capture_requires_explicit_actual_descriptor_for_named_evaluator():
    class _NamedEvaluatorSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            return await super().call_tool(name, arguments)

    session = _NamedEvaluatorSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert not any(
        name == "brain_artifact_preference_evaluate" for name, _ in session.calls
    )
    assert "artifact_preference_application_receipt" not in capture
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promotion_blockers"
    ] == ["preference_artifact_consumer_evidence_missing"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda descriptor: descriptor.update({"dataset_id": "raw-external"}),
        lambda descriptor: descriptor["metrics"].update({"document_id": 1}),
    ],
)
def test_collect_post_deploy_mcp_capture_rejects_unknown_or_protected_descriptor_fields(
    mutation,
):
    descriptor = _actual_artifact_descriptor()
    mutation(descriptor)

    with pytest.raises(ValueError):
        asyncio.run(
            collect_source_to_candidate_post_deploy_mcp_capture(
                mcp_url="https://mcp.example.test/mcp",
                repository="pureliture/neurons",
                branch="main",
                project="neurons",
                artifact_descriptor=descriptor,
                session_factory=lambda _: None,
            )
        )


@pytest.mark.parametrize(
    "mode",
    [
        "tool_error",
        "stale_lineage",
        "repository_drift",
        "branch_drift",
        "project_drift",
        "memory_drift",
        "card_hash_drift",
        "proposal_drift",
        "descriptor_drift",
    ],
)
def test_collect_post_deploy_mcp_capture_keeps_direct_receipt_failures_as_gap(mode: str):
    class _RejectedConsumerSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                if mode == "tool_error":
                    return SimpleNamespace(isError=True, structuredContent={})
                receipt = _artifact_preference_application_receipt(arguments)
                if mode == "stale_lineage":
                    receipt["preference_binding"]["decision_id"] = "decision:stale-lineage"
                elif mode == "repository_drift":
                    receipt["artifact_binding"]["repository_hash"] = hash_payload(
                        "different/repository"
                    )
                elif mode == "branch_drift":
                    receipt["artifact_binding"]["branch_hash"] = hash_payload(
                        "different-branch"
                    )
                elif mode == "project_drift":
                    receipt["preference_binding"]["project"] = "different-project"
                elif mode == "memory_drift":
                    receipt["preference_binding"]["memory_id"] = "mem_artifact_preference_other"
                elif mode == "card_hash_drift":
                    receipt["preference_binding"]["card_content_hash"] = "sha256:" + "d" * 64
                elif mode == "proposal_drift":
                    receipt["preference_binding"]["proposal_id"] = "proposal:p7-other"
                elif mode == "descriptor_drift":
                    receipt["artifact_binding"]["artifact_fingerprint"] = (
                        "sha256:" + "e" * 64
                    )
                receipt["receipt_hash"] = hash_payload(
                    {
                        "preference_binding": receipt["preference_binding"],
                        "artifact_binding": receipt["artifact_binding"],
                        "application_result": receipt["application_result"],
                        "consumer_surface": receipt["consumer_surface"],
                    }
                )
                return SimpleNamespace(isError=False, structuredContent=receipt)
            return await super().call_tool(name, arguments)

    session = _RejectedConsumerSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=_actual_artifact_descriptor(),
            session_factory=_fake_session_factory,
        )
    )

    assert "artifact_preference_application_receipt" not in capture
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promoted_to_live_evidence"
    ] is False
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promotion_blockers"
    ] == ["preference_artifact_consumer_evidence_missing"]


@pytest.mark.parametrize("location", ["top_level", "nested"])
def test_collect_post_deploy_mcp_capture_never_emits_protected_extra_receipt_field(
    location: str,
):
    class _ProtectedReceiptSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                receipt = _artifact_preference_application_receipt(arguments)
                if location == "top_level":
                    receipt["document_id"] = "raw-external"
                else:
                    receipt["artifact_binding"]["dataset_id"] = "raw-external"
                receipt["receipt_hash"] = hash_payload(
                    {
                        "preference_binding": receipt["preference_binding"],
                        "artifact_binding": receipt["artifact_binding"],
                        "application_result": receipt["application_result"],
                        "consumer_surface": receipt["consumer_surface"],
                    }
                )
                return SimpleNamespace(isError=False, structuredContent=receipt)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _ProtectedReceiptSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=_actual_artifact_descriptor(),
            session_factory=_fake_session_factory,
        )
    )

    serialized = json.dumps(capture, sort_keys=True)
    assert "artifact_preference_application_receipt" not in capture
    assert "dataset_id" not in serialized
    assert "raw-external" not in serialized


@pytest.mark.parametrize(
    ("section", "field", "marker"),
    [
        ("preference_binding", "memory_id", "mem-safe document_id=raw-external"),
        ("preference_binding", "proposal_id", "proposal:safe dataset_id:raw-external"),
    ],
)
def test_collect_post_deploy_mcp_capture_rejects_raw_external_id_marker_in_allowed_receipt_value(
    section: str,
    field: str,
    marker: str,
):
    class _MarkedReceiptSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                receipt = _artifact_preference_application_receipt(arguments)
                receipt[section][field] = marker
                receipt["receipt_hash"] = hash_payload(
                    {
                        "preference_binding": receipt["preference_binding"],
                        "artifact_binding": receipt["artifact_binding"],
                        "application_result": receipt["application_result"],
                        "consumer_surface": receipt["consumer_surface"],
                    }
                )
                return SimpleNamespace(isError=False, structuredContent=receipt)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _MarkedReceiptSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=_actual_artifact_descriptor(),
            session_factory=_fake_session_factory,
        )
    )

    serialized = json.dumps(capture, sort_keys=True)
    assert "artifact_preference_application_receipt" not in capture
    assert "preference_artifact_memory" not in capture
    assert "document_id" not in serialized
    assert "dataset_id" not in serialized
    assert "raw-external" not in serialized


def test_collect_post_deploy_mcp_capture_rejects_descriptor_fingerprint_mismatch():
    descriptor = _actual_artifact_descriptor()
    descriptor["artifact_fingerprint"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="artifact_fingerprint"):
        asyncio.run(
            collect_source_to_candidate_post_deploy_mcp_capture(
                mcp_url="https://mcp.example.test/mcp",
                repository="pureliture/neurons",
                branch="main",
                project="neurons",
                artifact_descriptor=descriptor,
                session_factory=lambda _: None,
            )
        )


def test_collect_post_deploy_mcp_capture_does_not_evaluate_without_explicit_project():
    class _NamedEvaluatorSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

    session = _NamedEvaluatorSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            artifact_descriptor=_actual_artifact_descriptor(),
            session_factory=_fake_session_factory,
        )
    )

    assert not any(
        name == "brain_artifact_preference_evaluate" for name, _ in session.calls
    )
    assert "artifact_preference_application_receipt" not in capture
    assert capture["project_scope"]["source"] == "collector_default"


def test_collect_post_deploy_mcp_capture_keeps_missing_evaluator_as_gap():
    class _LiveWithoutEvaluatorSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            return await super().call_tool(name, arguments)

    session = _LiveWithoutEvaluatorSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=_actual_artifact_descriptor(),
            session_factory=_fake_session_factory,
        )
    )

    assert not any(
        name == "brain_artifact_preference_evaluate" for name, _ in session.calls
    )
    assert "artifact_preference_application_receipt" not in capture
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promotion_blockers"
    ] == ["preference_artifact_consumer_evidence_missing"]


def test_collect_post_deploy_mcp_capture_does_not_pass_html_only_preference_surface():
    class _HtmlOnlyConsumerSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                    preference_artifact_memory_actual=True,
                )
                preference = packet["preference_artifact_memory"]
                preference["preference_object_pack"]["lanes"]["accepted_current"] = []
                preference["preference_object_pack"]["objects"] = []
                preference["agent_context_preference_section"]["items"] = []
                preference["agent_context_preference_section"]["object_count"] = 0
                preference["agent_context_preference_section"]["accepted_preference_count"] = 0
                return SimpleNamespace(isError=False, structuredContent=packet)
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                receipt = _artifact_preference_application_receipt(arguments)
                receipt["status"] = "FAIL"
                receipt["application_result"]["outcome"] = "fail"
                receipt["application_result"]["passed_rules"].remove(
                    "relationship_count_at_least_one"
                )
                receipt["application_result"]["failed_rules"] = [
                    "relationship_count_at_least_one"
                ]
                receipt["failures"] = ["relationship_count_at_least_one"]
                receipt["receipt_hash"] = hash_payload(
                    {
                        "preference_binding": receipt["preference_binding"],
                        "artifact_binding": receipt["artifact_binding"],
                        "application_result": receipt["application_result"],
                        "consumer_surface": receipt["consumer_surface"],
                    }
                )
                return SimpleNamespace(isError=False, structuredContent=receipt)
            return await super().call_tool(name, arguments)

    session = _HtmlOnlyConsumerSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    artifact_descriptor = _actual_artifact_descriptor(relationship_count=0)
    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            artifact_descriptor=artifact_descriptor,
            session_factory=_fake_session_factory,
        )
    )

    arguments = next(
        arguments
        for name, arguments in session.calls
        if name == "brain_artifact_preference_evaluate"
    )
    assert arguments["metrics"]["object_count"] == 2
    assert arguments["metrics"]["relationship_count"] == 0
    assert "artifact_preference_application_receipt" not in capture
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promoted_to_live_evidence"
    ] is False


def test_collect_post_deploy_mcp_capture_uses_explicit_default_when_project_is_omitted():
    session = _FakeMcpSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="different-owner/different-repository",
            branch="main",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["project_scope"] == {
        "project": "neurons",
        "source": "collector_default",
        "repository_inference_used": False,
    }
    assert all(
        arguments["project"] == "neurons"
        for name, arguments in session.calls
        if name
        in {
            "brain_source_to_candidate_runtime_readiness",
            "brain_context_resolve",
            "brain_objects_query",
        }
    )


def test_post_deploy_mcp_url_rejects_values_that_can_leak_secrets_or_topology():
    assert validate_post_deploy_mcp_url("https://mcp.example.test/mcp") == "https://mcp.example.test/mcp"
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("file:///tmp/mcp")
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("https://user:secret@mcp.example.test/mcp")
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("https://mcp.example.test/mcp?token=secret")
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("https://mcp.example.test/mcp#fragment")


def test_collect_post_deploy_mcp_capture_uses_read_only_mcp_calls_and_sanitizes_output():
    session = _FakeMcpSession()
    seen_urls: list[str] = []

    @asynccontextmanager
    async def _fake_session_factory(mcp_url: str):
        seen_urls.append(mcp_url)
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            consumer="codex",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert seen_urls == ["https://mcp.example.test/mcp"]
    assert session.initialized is True
    assert capture["schema_version"] == "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
    assert set(REQUIRED_RUNTIME_TOOL_NAMES).issubset(set(capture["tool_names"]))
    assert capture["production_mutation_performed"] is False
    assert "projection_join" not in capture
    assert "session_project_rollup_runtime" not in capture
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"] == {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "collector_readiness_claim": "collector_packet_not_live_evidence",
        "projection_join_present": True,
        "projection_join_schema": "object_extraction_projection_join_preview.v1",
        "projection_join_edge_count": 2,
        "projection_join_promoted_to_live_evidence": False,
        "session_project_rollup_present": True,
        "session_project_rollup_schema": "session_project_rollup_runtime_evidence.v1",
        "session_project_rollup_preview_schema": "object_extraction_session_project_rollup_preview.v1",
        "session_project_rollup_device_count": 2,
        "session_project_rollup_work_unit_count": 1,
        "session_project_rollup_promoted_to_live_evidence": False,
        "preference_artifact_memory_present": True,
        "preference_artifact_memory_schema": "preference_artifact_memory_runtime_evidence.v1",
        "preference_artifact_accepted_preference_count": 1,
        "preference_artifact_proposal_preference_count": 1,
        "preference_artifact_review_check_status": "pass",
        "preference_artifact_memory_promoted_to_live_evidence": False,
        "preference_artifact_memory_promotion_blockers": ["collector_packet_not_live_evidence"],
        "evidence_collection_mode": "local_test_replay",
        "evidence_collection_network_used": False,
        "production_mutation_performed": False,
    }
    assert capture["agent_context_product"] == _fake_agent_context_product(consumer="codex")
    assert "private_context_not_returned" not in json.dumps(capture, sort_keys=True)
    assert capture["collection"] == {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
        "collector": "source_to_candidate_post_deploy_mcp_capture",
        "collection_mode": "post_deploy_read_only_smoke",
        "network_used": True,
        "mutation_scope": "none",
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    assert capture["evidence_provenance"] == capture["collection"]
    assert capture["deployed_identity"]["contains_expected_commit"] is True
    assert {item["route"] for item in capture["brain_objects_query_smokes"]} == set(
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )
    assert "mcp.example.test" not in json.dumps(capture, sort_keys=True)

    runtime_readiness_calls = [
        arguments
        for name, arguments in session.calls
        if name == "brain_source_to_candidate_runtime_readiness"
    ]
    assert runtime_readiness_calls == [
        {
            "evidence_collection_plan": True,
            "expected_commit": "c2b8548",
            "repository": "pureliture/neurons",
            "branch": "main",
            "project": "neurons",
            "consumer": "codex",
        },
        {
            "collect_shadow_evidence": True,
            "expected_commit": "c2b8548",
            "repository": "pureliture/neurons",
            "branch": "main",
            "project": "neurons",
            "consumer": "codex",
            "evidence_collection_mode": "post_deploy_read_only_smoke",
            "evidence_collection_network_used": True,
        }
    ]
    context_calls = [
        arguments
        for name, arguments in session.calls
        if name == "brain_context_resolve"
    ]
    assert context_calls == [
        {
            "repository": "pureliture/neurons",
            "branch": "main",
            "project": "neurons",
            "current_files": [],
            "current_request": (
                "source-to-candidate runtime readiness post-deploy "
                "agent context product capture"
            ),
            "limit": 8,
            "response_mode": "full",
            "consumer": "codex",
        }
    ]
    route_calls = [arguments for name, arguments in session.calls if name == "brain_objects_query"]
    assert [arguments["route"] for arguments in route_calls] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(arguments["response_mode"] == "full" for arguments in route_calls)
    assert all(arguments["consumer"] == "codex" for arguments in route_calls)
    assert all(arguments["project"] == "neurons" for arguments in route_calls)

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.agent_context.tool_hints"]["status"] == "validated"
    assert claims["live.agent_context.product_sections"]["status"] == "validated"
    assert claims["live.source_to_candidate.projection_join"]["status"] == "not_validated"
    assert "live_graph_qdrant_projection_join_unproven" in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_promotes_live_p6_rollup_from_read_only_runtime():
    class _LiveP6RollupSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        session_project_rollup=True,
                    ),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _LiveP6RollupSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["session_project_rollup_present"] is True
    assert (
        capture["runtime_collected_packet"]["session_project_rollup_schema"]
        == "session_project_rollup_runtime_evidence.v1"
    )
    assert (
        capture["runtime_collected_packet"]["session_project_rollup_promoted_to_live_evidence"]
        is True
    )
    assert capture["session_project_rollup_runtime"]["schema_version"] == (
        "session_project_rollup_runtime_evidence.v1"
    )
    assert capture["session_project_rollup_runtime"]["rollup_preview"]["scope"] == "all_devices"
    assert capture["session_project_rollup_runtime"]["read_after_write"]["route"] == (
        "temporal_work_recall"
    )
    assert capture["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.session_project.rollup"]["status"] == "validated"
    assert "live_session_project_rollup_unverified" not in report["gaps"]
    assert "live_multi_device_rollup_unproven" not in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_keeps_self_asserted_p7_consumer_packet_as_gap():
    class _LiveP7PreferenceSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                    ),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _LiveP7PreferenceSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["preference_artifact_memory_present"] is True
    assert capture["runtime_collected_packet"]["preference_artifact_memory_schema"] == (
        "preference_artifact_memory_runtime_evidence.v1"
    )
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_consumer_evidence_missing"
    ]
    assert "preference_artifact_memory" not in capture
    assert capture["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.preference_artifact.memory"]["status"] == "not_validated"
    assert "live_preference_artifact_memory_unverified" in report["gaps"]
    assert "accepted_preference_context_pack_live_unproven" in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_allowlists_runtime_builder_views_but_keeps_consumer_gap():
    evidence = json.loads(
        json.dumps(
            _preference_artifact_memory_evidence(
                actual_live_read_surfaces=True,
                actual_consumer_proof=True,
            )
        )
    )
    target_object_id = evidence["read_surface_alignment"]["target_object_id"]
    object_lists = [
        evidence["preference_object_pack"]["objects"],
        evidence["preference_object_pack"]["lanes"]["accepted_current"],
        evidence["html_visualization_route_smoke"]["object_pack"]["objects"],
        evidence["html_visualization_route_smoke"]["object_pack"]["lanes"]["accepted_current"],
        evidence["agent_context_preference_section"]["items"],
    ]
    for objects in object_lists:
        for obj in objects:
            if obj.get("object_id") != target_object_id:
                continue
            scope = obj.pop("scope", {})
            payload = obj.pop("payload", {})
            obj["project"] = scope["project"]
            obj["source_content_hash"] = obj["content_hash"]
            obj["authority_decision_id"] = payload["authority_decision_id"]

    class _AllowlistedP7PreferenceSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(live=True)
                packet["preference_artifact_memory"] = evidence
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _AllowlistedP7PreferenceSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promoted_to_live_evidence"
    ] is False
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_consumer_evidence_missing"
    ]
    assert "preference_artifact_memory" not in capture


def test_collect_post_deploy_mcp_capture_does_not_promote_self_asserted_consumer_evidence_or_metadata():
    evidence = _preference_artifact_memory_evidence(
        actual_live_read_surfaces=True,
        actual_consumer_proof=True,
    )
    evidence["untrusted_metadata"] = {"note": "remote self assertion"}

    class _SelfAssertedP7PreferenceSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(live=True)
                packet["preference_artifact_memory"] = evidence
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _SelfAssertedP7PreferenceSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"][
        "preference_artifact_memory_promoted_to_live_evidence"
    ] is False
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_consumer_evidence_missing"
    ]
    assert "untrusted_metadata" not in json.dumps(capture, sort_keys=True)


def test_collect_post_deploy_mcp_capture_rejects_cross_type_artifact_preference_ids():
    evidence = _preference_artifact_memory_evidence(
        actual_live_read_surfaces=True,
        actual_consumer_proof=True,
    )
    cross_type_id = "ko:RepoDocument:p7-cross-type-preference"
    original_id = evidence["read_surface_alignment"]["target_object_id"]
    for item in (
        evidence["preference_object_pack"]["objects"],
        evidence["preference_object_pack"]["lanes"]["accepted_current"],
        evidence["html_visualization_route_smoke"]["object_pack"]["objects"],
        evidence["html_visualization_route_smoke"]["object_pack"]["lanes"]["accepted_current"],
        evidence["agent_context_preference_section"]["items"],
    ):
        for obj in item:
            if obj.get("object_id") == original_id:
                obj["object_id"] = cross_type_id
                if isinstance(obj.get("payload"), dict):
                    obj["payload"]["target_object_id"] = cross_type_id
    alignment = evidence["read_surface_alignment"]
    alignment["target_object_id"] = cross_type_id
    for field in (
        "code_style_preference_object_ids",
        "html_visualization_preference_object_ids",
        "style_preference_context_object_ids",
    ):
        alignment[field] = [cross_type_id]

    class _CrossTypePreferenceSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get(
                "collect_shadow_evidence"
            ) is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(live=True)
                packet["preference_artifact_memory"] = evidence
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _CrossTypePreferenceSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["preference_artifact_accepted_preference_count"] == 0
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_accepted_current_lane_missing"
    ]


def test_collect_post_deploy_mcp_capture_does_not_promote_synthetic_p7_fixture_as_live_evidence():
    class _SyntheticP7Session(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=False,
                    ),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _SyntheticP7Session()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["preference_artifact_memory_present"] is True
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "deployed_identity_expected_commit_unverified"
    ]


def test_collect_post_deploy_mcp_capture_blocks_p7_promotion_without_actual_consumer_proof():
    class _MissingConsumerProofSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        preference_artifact_memory=True,
                        preference_artifact_memory_actual=True,
                        preference_artifact_memory_consumer_proof=False,
                    ),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _MissingConsumerProofSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_consumer_evidence_missing"
    ]


def test_collect_post_deploy_mcp_capture_rejects_raw_external_id_before_key_redaction():
    raw_value = "raw-dataset-value-must-not-leak"

    class _ForbiddenRuntimeInputSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                    preference_artifact_memory_actual=True,
                )
                packet["preference_artifact_memory"]["preference_object_pack"]["objects"][0][
                    "payload"
                ]["dataset_id"] = raw_value
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _ForbiddenRuntimeInputSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    serialized = json.dumps(capture, sort_keys=True)
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_runtime_input_forbidden"
    ]
    assert raw_value not in serialized
    assert "dataset_id" not in serialized
    assert "dataset_ref" not in serialized


def test_collect_post_deploy_mcp_capture_rejects_forbidden_fields_from_context_and_route_smokes():
    context_raw_value = "raw-context-dataset-value-must-not-leak"
    route_raw_value = "raw-route-document-value-must-not-leak"
    topology_raw_value = "raw-host-topology-value-must-not-leak"

    class _ForbiddenAuxiliaryResponseSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            result = await super().call_tool(name, arguments)
            structured = json.loads(json.dumps(result.structuredContent))
            if name == "brain_context_resolve":
                structured["authority"]["agent_context_product"]["datasetId"] = context_raw_value
            elif name == "brain_objects_query":
                structured["object_pack"]["objects"] = [{"documentId": route_raw_value}]
            return SimpleNamespace(isError=False, structuredContent=structured)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _ForbiddenAuxiliaryResponseSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
                "hostTopology": topology_raw_value,
            },
            session_factory=_fake_session_factory,
        )
    )

    serialized = json.dumps(capture, sort_keys=True)
    assert context_raw_value not in serialized
    assert route_raw_value not in serialized
    assert topology_raw_value not in serialized
    assert "datasetId" not in serialized
    assert "documentId" not in serialized
    assert "hostTopology" not in capture["deployed_identity"]
    assert capture["agent_context_product"]["missing_evidence_before_promotion"] == [
        "agent_context_product_capture_failed"
    ]
    assert all(
        smoke["object_pack"]["gaps"] == ["collector_route_smoke_forbidden"]
        for smoke in capture["brain_objects_query_smokes"]
    )


def test_collect_post_deploy_mcp_capture_blocks_promotions_when_runtime_reports_protected_output():
    class _ProtectedOutputRuntimePacketSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        session_project_rollup=True,
                        preference_artifact_memory=True,
                        provenance_overrides={"raw_private_evidence_returned": True},
                    ),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _ProtectedOutputRuntimePacketSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["production_mutation_performed"] is False
    assert capture["collection"]["raw_private_evidence_returned"] is True
    assert "projection_join" not in capture
    assert "session_project_rollup_runtime" not in capture
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["projection_join_promoted_to_live_evidence"] is False
    assert (
        capture["runtime_collected_packet"]["session_project_rollup_promoted_to_live_evidence"]
        is False
    )
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_memory_protected_output_reported"
    ]

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    assert report["status"] == "FAIL"
    assert "live.evidence.provenance" in report["failed_claims"]
    assert "live_evidence_provenance_raw_private_evidence_returned" in report["gaps"]


def test_collect_post_deploy_mcp_capture_blocks_p7_promotion_when_artifact_body_is_returned():
    class _RawArtifactBodyRuntimePacketSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                )
                packet["preference_artifact_memory"]["artifact_review_check"][
                    "raw_artifact_body_returned"
                ] = True
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _RawArtifactBodyRuntimePacketSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["preference_artifact_memory_present"] is True
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_raw_artifact_body_returned"
    ]
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["projection_join_promoted_to_live_evidence"] is True
    assert capture["projection_join"]["schema_version"] == "object_extraction_projection_join_preview.v1"
    assert capture["production_mutation_performed"] is False


def test_collect_post_deploy_mcp_capture_blocks_p7_promotion_without_unattested_runtime_marker():
    class _ShadowPreferenceRuntimePacketSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                )
                packet["preference_artifact_memory"].pop("attestation_state")
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _ShadowPreferenceRuntimePacketSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["preference_artifact_memory_present"] is True
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_memory_unattested_runtime_read_missing"
    ]
    assert "preference_artifact_memory" not in capture
    assert capture["runtime_collected_packet"]["projection_join_promoted_to_live_evidence"] is True
    assert capture["production_mutation_performed"] is False


def test_collect_post_deploy_mcp_capture_blocks_p7_promotion_without_accepted_current_context_lane():
    class _ReferenceOnlyPreferenceRuntimePacketSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                )
                packet["preference_artifact_memory"]["agent_context_preference_section"][
                    "authority_lanes"
                ] = ["reference_only"]
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _ReferenceOnlyPreferenceRuntimePacketSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["preference_artifact_memory_present"] is True
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_agent_context_accepted_current_missing"
    ]
    assert "preference_artifact_memory" not in capture
    assert capture["production_mutation_performed"] is False


def test_collect_post_deploy_mcp_capture_blocks_p7_promotion_when_count_lacks_accepted_current_lane():
    class _CountOnlyPreferenceRuntimePacketSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                packet = _runtime_collected_packet(
                    live=True,
                    preference_artifact_memory=True,
                )
                packet["preference_artifact_memory"]["preference_object_pack"]["lanes"][
                    "accepted_current"
                ] = []
                return SimpleNamespace(isError=False, structuredContent=packet)
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _CountOnlyPreferenceRuntimePacketSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["preference_artifact_accepted_preference_count"] == 0
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_accepted_current_lane_missing"
    ]
    assert "preference_artifact_memory" not in capture
    assert capture["production_mutation_performed"] is False


def test_collect_post_deploy_mcp_capture_preserves_runtime_mutation_and_protected_output_flags():
    class _UnsafeRuntimePacketSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        session_project_rollup=True,
                        preference_artifact_memory=True,
                        production_mutation_performed=True,
                        provenance_overrides={
                            "mutation_scope": "bounded_production_authority_execution",
                            "raw_private_evidence_returned": True,
                        },
                    ),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _UnsafeRuntimePacketSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            session_factory=_fake_session_factory,
        )
    )

    assert capture["production_mutation_performed"] is True
    assert capture["collection"]["mutation_scope"] == "bounded_production_authority_execution"
    assert capture["collection"]["raw_private_evidence_returned"] is True
    assert "projection_join" not in capture
    assert "session_project_rollup_runtime" not in capture
    assert "preference_artifact_memory" not in capture
    assert (
        capture["runtime_collected_packet"]["session_project_rollup_promoted_to_live_evidence"]
        is False
    )
    assert (
        capture["runtime_collected_packet"]["preference_artifact_memory_promoted_to_live_evidence"]
        is False
    )
    assert capture["runtime_collected_packet"]["preference_artifact_memory_promotion_blockers"] == [
        "preference_artifact_memory_mutation_reported"
    ]

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    assert report["status"] == "FAIL"
    assert report["production_mutation_performed"] is True
    assert "live.evidence.provenance" in report["failed_claims"]
    assert "live_evidence_provenance_read_only_mode_mutation_scope_mismatch" in report["gaps"]
    assert "live_evidence_provenance_raw_private_evidence_returned" in report["gaps"]


def test_collect_post_deploy_mcp_capture_promotes_live_projection_join_from_read_only_runtime():
    class _LiveProjectionJoinSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(live=True),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _LiveProjectionJoinSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["projection_join_promoted_to_live_evidence"] is True
    assert capture["projection_join"]["schema_version"] == "object_extraction_projection_join_preview.v1"
    assert capture["projection_join"]["evidence_class"] == "runtime_projection_join"
    assert capture["projection_join"]["edge_count"] == 2
    assert capture["projection_join"]["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.source_to_candidate.projection_join"]["status"] == "validated"
    assert "live_graph_qdrant_projection_join_unproven" not in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_keeps_tool_errors_as_public_safe_gaps():
    class _FailingRouteSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            listed.tools.append(SimpleNamespace(name="brain_artifact_preference_evaluate"))
            return listed

        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_objects_query" and arguments.get("route") == "temporal_work_recall":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(isError=True, structuredContent={})
            if name == "brain_artifact_preference_evaluate":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(isError=True, structuredContent={})
            return await super().call_tool(name, arguments)

    session = _FailingRouteSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    artifact_descriptor = _actual_artifact_descriptor()
    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            artifact_descriptor=artifact_descriptor,
            session_factory=_fake_session_factory,
        )
    )

    by_route = {item["route"]: item for item in capture["brain_objects_query_smokes"]}
    assert by_route["temporal_work_recall"]["collector_error_type"] == "McpToolError"
    assert by_route["temporal_work_recall"]["object_pack"]["gaps"] == ["collector_route_smoke_failed"]
    assert by_route["temporal_work_recall"]["production_mutation_performed"] is False
    evaluator_arguments = next(
        arguments
        for name, arguments in session.calls
        if name == "brain_artifact_preference_evaluate"
    )
    assert evaluator_arguments["metrics"] == artifact_descriptor["metrics"]


def test_collect_post_deploy_mcp_capture_keeps_missing_agent_context_as_public_safe_gap():
    class _MissingContextSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_context_resolve":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(isError=True, structuredContent={})
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _MissingContextSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            session_factory=_fake_session_factory,
        )
    )

    product = capture["agent_context_product"]
    assert product["schema_version"] == ""
    assert product["surface_policy"]["mutation_allowed"] is False
    assert product["missing_evidence_before_promotion"] == ["agent_context_product_capture_failed"]
    assert product["collector_error_type"] == "McpToolError"


def test_runtime_readiness_cli_collects_post_deploy_mcp_capture(monkeypatch, capsys, tmp_path):
    identity_file = tmp_path / "deployed-identity.json"
    identity_file.write_text(
        json.dumps(
            {
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            }
        ),
        encoding="utf-8",
    )
    artifact_descriptor_file = tmp_path / "artifact-descriptor.json"
    artifact_descriptor = _actual_artifact_descriptor()
    artifact_descriptor_file.write_text(
        json.dumps(artifact_descriptor),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    async def _fake_collect(**kwargs):
        seen.update(kwargs)
        return {
            "schema_version": "source_to_candidate_runtime_post_deploy_mcp_capture.v1",
            "collection": {
                "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
                "collection_mode": "post_deploy_read_only_smoke",
                "network_used": True,
                "mutation_scope": "none",
            },
            "production_mutation_performed": False,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_source_to_candidate_post_deploy_mcp_capture",
        _fake_collect,
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--collect-post-deploy-mcp-capture",
                "--mcp-url",
                "https://mcp.example.test/mcp",
                "--repository",
                "pureliture/neurons",
                "--branch",
                "main",
                "--project",
                "neurons",
                "--consumer",
                "codex",
                "--expected-commit",
                "c2b8548",
                "--deployed-identity-file",
                str(identity_file),
                "--artifact-descriptor-file",
                str(artifact_descriptor_file),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
    assert output["collection"]["network_used"] is True
    assert output["runtime_readiness"]["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert seen["mcp_url"] == "https://mcp.example.test/mcp"
    assert seen["repository"] == "pureliture/neurons"
    assert seen["branch"] == "main"
    assert seen["project"] == "neurons"
    assert seen["consumer"] == "codex"
    assert seen["expected_commit"] == "c2b8548"
    assert seen["deployed_identity"] == {
        "contains_expected_commit": True,
        "identity_source": "redacted_artifact_identity_summary",
    }
    assert seen["artifact_descriptor"] == artifact_descriptor
    assert str(artifact_descriptor_file) not in json.dumps(output, sort_keys=True)
    assert artifact_descriptor["summary"] not in json.dumps(output, sort_keys=True)

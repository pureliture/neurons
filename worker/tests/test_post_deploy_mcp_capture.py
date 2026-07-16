from __future__ import annotations

import asyncio
import io
import json
import os
from copy import deepcopy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.golden_query_eval import (
    build_product_activation_progress_report,
)
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects import post_deploy_mcp_capture
from agent_knowledge.llm_brain_core.objects.post_deploy_mcp_capture import (
    collect_agent_context_consumer_startup_receipt,
    collect_source_to_candidate_post_deploy_mcp_capture,
    validate_post_deploy_mcp_url,
)
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_AGENT_CONTEXT_SECTIONS,
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
)
from agent_knowledge.public_safe_util import hash_payload
from agent_knowledge.mcp_tools import BRAIN_QUERY_TOOL_NAME


_BOUND_SOURCE_COMMIT = "c" * 40


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
        "action_hints": [
            {
                "action": "request_missing_evidence",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": missing_evidence,
            },
            {
                "action": "promote_authority",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["approved_scope_required", *missing_evidence],
            },
        ],
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
    temporal_correctness_runtime: bool = False,
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
    if temporal_correctness_runtime:
        packet["temporal_correctness_runtime"] = _temporal_correctness_runtime_aggregate()
    if not live:
        packet["collector"] = {
            "schema_version": "source_to_candidate_runtime_evidence_collector.v1",
            "readiness_claim": "collector_packet_not_live_evidence",
        }
    return packet


def _temporal_correctness_runtime_aggregate() -> dict:
    return {
        "schema_version": "temporal_correctness_runtime_aggregate.v1",
        "projection_currentness": {
            "source_hash_match": True,
            "source_hash_mismatch_count": 0,
            "stale_projected_session_count": 0,
            "source_session_count": 126,
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
            "baseline_coverage_count": 15,
            "coverage_count": 16,
            "baseline_backlog_count": 111,
            "backlog_count": 110,
            "error_count": 0,
        },
        "production_mutation_performed": False,
    }


def _temporal_runtime_postcheck_receipt() -> dict:
    return {
        "schema_version": "temporal_correctness_runtime_postcheck_receipt.v1",
        "evidence_class": "bounded_live_runtime_postcheck",
        "exact_argv_sha256": "sha256:" + "8" * 64,
        "backup_receipt_sha256": "sha256:" + "9" * 64,
        "evidence_provenance": {
            "collection_mode": "bounded_live_runtime_postcheck",
            "network_used": True,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "aggregate": _temporal_correctness_runtime_aggregate(),
    }


def _minimal_temporal_acceptance_config() -> dict:
    return {
        "temporal_query": "temporal work recall",
        "date_a": {
            "as_of": "2026-07-09T12:00:00Z",
            "expected_object_fingerprint": "sha256:" + "a" * 64,
            "expected_object_identity_fingerprint": "sha256:" + "d" * 64,
        },
        "date_b": {
            "as_of": "2026-07-15T12:00:00Z",
            "expected_object_fingerprint": "sha256:" + "b" * 64,
            "expected_object_identity_fingerprint": "sha256:" + "e" * 64,
        },
        "range_boundary": {
            "date_from": "2026-07-09T12:00:00Z",
            "date_to": "2026-07-09T12:00:00Z",
            "expected_object_fingerprint": "sha256:" + "a" * 64,
            "expected_object_identity_fingerprint": "sha256:" + "d" * 64,
        },
        "mismatch": {"as_of": "2026-07-01T12:00:00Z"},
        "invalid_range": {
            "date_from": "2026-07-16T00:00:00Z",
            "date_to": "2026-07-15T00:00:00Z",
        },
        "nonsense_query": "quasar marmalade unrelated nonsense",
        "semantic_query": {
            "query": "temporal currentness verification",
            "expected_result_fingerprint": "sha256:" + "c" * 64,
        },
        "runtime_expectations": {
            "schema_version": "temporal_correctness_runtime_expectations.v1",
            "baseline_coverage_count": 15,
            "baseline_backlog_count": 111,
            "minimum_source_session_count": 126,
            "minimum_valid_source_count": 126,
            "max_artifact_age_seconds": 3600,
        },
    }


def test_temporal_acceptance_rejects_operator_supplied_runtime_aggregate() -> None:
    config = _minimal_temporal_acceptance_config()
    config["runtime_aggregate"] = _temporal_correctness_runtime_aggregate()

    with pytest.raises(ValueError, match="runtime_aggregate is untrusted"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)


def test_temporal_acceptance_rejects_operator_runtime_postcheck_receipt() -> None:
    config = _minimal_temporal_acceptance_config()
    config["runtime_postcheck_receipt"] = _temporal_runtime_postcheck_receipt()

    with pytest.raises(ValueError, match="runtime_postcheck_receipt is untrusted"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)


def test_temporal_acceptance_rejects_different_date_probe_query() -> None:
    config = _minimal_temporal_acceptance_config()
    config["date_b"]["query"] = "different temporal query"

    with pytest.raises(ValueError, match="date_b.query must equal temporal_query"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)


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


class _NestedObservedAtRouteMcpSession(_FakeMcpSession):
    """Return route payloads whose source observation can vary independently."""

    def __init__(
        self,
        *,
        observed_at: str,
        content_hash: str = "sha256:" + "c" * 64,
        request_id: str = "request:stable",
        valid_from: str = "2026-07-15T00:00:00+00:00",
    ) -> None:
        super().__init__()
        self.observed_at = observed_at
        self.content_hash = content_hash
        self.request_id = request_id
        self.valid_from = valid_from

    async def call_tool(self, name: str, arguments: dict):
        result = await super().call_tool(name, arguments)
        if name != "brain_objects_query":
            return result

        route = str(arguments["route"])
        result.structuredContent = deepcopy(result.structuredContent)
        result.structuredContent["object_pack"].update(
            {
                "objects": [
                    {
                        "schema_version": "knowledge_object_envelope.v1",
                        "object_id": f"ko:{route}",
                        "content_hash": self.content_hash,
                        "observed_at": self.observed_at,
                        "payload": {
                            "request_id": self.request_id,
                            "valid_from": self.valid_from,
                            "valid_to": "2026-07-16T00:00:00+00:00",
                        },
                    }
                ],
                "edges": [
                    {
                        "schema_version": "knowledge_edge.v1",
                        "edge_id": f"edge:{route}",
                        "content_hash": self.content_hash,
                        "observed_at": self.observed_at,
                    }
                ],
                "evidence": [
                    {
                        "schema_version": "evidence_ref.v1",
                        "evidence_id": f"evidence:{route}",
                        "content_hash": self.content_hash,
                        "observed_at": self.observed_at,
                    }
                ],
            }
        )
        return result


def test_route_semantic_hash_only_normalizes_root_object_pack_entity_paths():
    raw = {
        "schema_version": "brain_objects_query.v1",
        "route": "authority_archive_separation",
        "object_pack": {
            "schema_version": "object_pack.v1",
            "objects": [
                {
                    "schema_version": "knowledge_object_envelope.v1",
                    "object_id": "ko:outer",
                    "content_hash": "sha256:" + "a" * 64,
                    "observed_at": "2026-07-15T03:00:00+00:00",
                    "payload": {
                        "nested_pack": {
                            "schema_version": "object_pack.v1",
                            "objects": [
                                {
                                    "schema_version": "knowledge_object_envelope.v1",
                                    "object_id": "ko:nested",
                                    "content_hash": "sha256:" + "b" * 64,
                                    "observed_at": "nested-observation-a",
                                }
                            ],
                        }
                    },
                }
            ],
            "edges": [],
            "evidence": [],
            "lanes": {},
            "verification": {},
        },
    }
    outer_drift = deepcopy(raw)
    outer_drift["object_pack"]["objects"][0]["observed_at"] = (
        "2026-07-15T03:01:00+00:00"
    )
    nested_drift = deepcopy(raw)
    nested_drift["object_pack"]["objects"][0]["payload"]["nested_pack"][
        "objects"
    ][0]["observed_at"] = "nested-observation-b"

    semantic_hash = post_deploy_mcp_capture._route_semantic_payload_hash
    assert semantic_hash(raw) == semantic_hash(outer_drift)
    assert semantic_hash(raw) != semantic_hash(nested_drift)


def _collect_external_startup_capture(
    monkeypatch,
    *,
    collector_session: _FakeMcpSession,
    adapter_session: _FakeMcpSession,
) -> dict:
    subprocess_observed: dict = {}

    @asynccontextmanager
    async def _collector_session_factory(_mcp_url: str):
        yield collector_session

    @asynccontextmanager
    async def _adapter_session_factory(_mcp_url: str):
        yield adapter_session

    class _Process:
        returncode = 0

        async def communicate(self, challenge_payload):
            challenge = json.loads(challenge_payload.decode("utf-8"))
            proof_key = os.read(subprocess_observed["kwargs"]["pass_fds"][0], 32)
            receipt = await collect_agent_context_consumer_startup_receipt(
                mcp_url="https://mcp.example.test/mcp",
                repository="pureliture/neurons",
                branch="main",
                project="neurons",
                consumer="codex",
                expected_commit="a" * 40,
                challenge=challenge,
                proof_key=proof_key,
                session_factory=_adapter_session_factory,
            )
            return json.dumps(receipt).encode("utf-8"), b""

    async def _create_subprocess_exec(*_argv, **kwargs):
        subprocess_observed["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(
        post_deploy_mcp_capture.asyncio,
        "create_subprocess_exec",
        _create_subprocess_exec,
    )
    return asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
            expected_commit="a" * 40,
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            collect_agent_context_startup=True,
            session_factory=_collector_session_factory,
        )
    )


def _startup_claim(capture: dict) -> dict:
    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="a" * 40,
    )
    return {item["claim_id"]: item for item in report["claims"]}[
        "live.agent_context.startup_read_path"
    ]


def test_external_subprocess_receipt_v2_allows_nested_observed_at_drift(
    monkeypatch,
):
    capture = _collect_external_startup_capture(
        monkeypatch,
        collector_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:00:00+00:00"
        ),
        adapter_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:01:00+00:00"
        ),
    )

    startup = capture["agent_context_startup_runtime"]
    receipt = startup["startup_receipt"]
    route_manifest = receipt["context_binding"]["route_manifest"]
    captured_smokes = {
        smoke["route"]: smoke for smoke in capture["brain_objects_query_smokes"]
    }

    assert receipt["schema_version"] == "agent_context_consumer_startup_receipt.v2"
    assert startup["receipt_validation"] == {"status": "validated", "failures": []}
    assert startup["collector_execution"]["subprocess_attested"] is True
    for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES:
        binding = route_manifest[route]
        assert set(binding) == {
            "schema_version",
            "route",
            "route_request_hash",
            "semantic_projection_hash",
            "observed_source_payload_hash",
        }
        assert binding["schema_version"] == "agent_context_route_binding.v1"
        assert binding["route"] == route
        assert binding["route_request_hash"] == receipt["scope_binding"][
            "route_request_hashes"
        ][route]
        assert binding["semantic_projection_hash"] == captured_smokes[route][
            "semantic_payload_hash"
        ]
        assert binding["observed_source_payload_hash"] != captured_smokes[route][
            "source_payload_hash"
        ]

    assert _startup_claim(capture)["bounded_adapter_status"] == "validated"


@pytest.mark.parametrize(
    ("adapter_kwargs", "failure_route"),
    [
        ({"content_hash": "sha256:" + "d" * 64}, "authority_archive_separation"),
        ({"request_id": "request:changed"}, "authority_archive_separation"),
        ({"valid_from": "2026-07-15T00:01:00+00:00"}, "authority_archive_separation"),
    ],
)
def test_route_semantic_binding_rejects_content_or_nonvolatile_nested_changes(
    monkeypatch,
    adapter_kwargs,
    failure_route,
):
    capture = _collect_external_startup_capture(
        monkeypatch,
        collector_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:00:00+00:00"
        ),
        adapter_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:01:00+00:00",
            **adapter_kwargs,
        ),
    )

    claim = _startup_claim(capture)
    assert claim["status"] == "failed"
    assert (
        f"agent_context_startup_route_semantic_binding_mismatch:{failure_route}"
        in claim["gaps"]
    )


def test_capture_bundle_route_hash_rejects_parent_observed_source_hash_tampering(
    monkeypatch,
):
    capture = _collect_external_startup_capture(
        monkeypatch,
        collector_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:00:00+00:00"
        ),
        adapter_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:01:00+00:00"
        ),
    )

    bundle = capture["agent_context_startup_runtime"]["capture_bundle_binding"]
    route = "authority_archive_separation"
    assert bundle["route_smoke_projection_hashes"][route].startswith("sha256:")
    capture["brain_objects_query_smokes"][0]["source_payload_hash"] = "sha256:" + "0" * 64

    claim = _startup_claim(capture)
    assert claim["status"] == "failed"
    assert f"agent_context_startup_route_capture_binding_mismatch:{route}" in claim["gaps"]


def test_capture_bundle_rejects_duplicate_parent_route_smoke(monkeypatch):
    capture = _collect_external_startup_capture(
        monkeypatch,
        collector_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:00:00+00:00"
        ),
        adapter_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:01:00+00:00"
        ),
    )
    route = "authority_archive_separation"
    route_smoke = next(
        smoke
        for smoke in capture["brain_objects_query_smokes"]
        if smoke["route"] == route
    )
    capture["brain_objects_query_smokes"].append(deepcopy(route_smoke))

    claim = _startup_claim(capture)
    assert claim["status"] == "failed"
    assert f"agent_context_startup_route_capture_duplicate:{route}" in claim["gaps"]


def test_receipt_v2_rejects_cross_route_manifest_binding_swap(monkeypatch):
    capture = _collect_external_startup_capture(
        monkeypatch,
        collector_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:00:00+00:00"
        ),
        adapter_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:01:00+00:00"
        ),
    )

    route_manifest = capture["agent_context_startup_runtime"]["startup_receipt"][
        "context_binding"
    ]["route_manifest"]
    first, second = REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES[:2]
    route_manifest[first], route_manifest[second] = (
        route_manifest[second],
        route_manifest[first],
    )

    claim = _startup_claim(capture)
    assert claim["status"] == "failed"
    assert f"agent_context_startup_route_binding_route_mismatch:{first}" in claim["gaps"]
    assert f"agent_context_startup_route_request_binding_mismatch:{first}" in claim["gaps"]
    assert f"agent_context_startup_route_binding_route_mismatch:{second}" in claim["gaps"]
    assert f"agent_context_startup_route_request_binding_mismatch:{second}" in claim["gaps"]


def test_receipt_v2_missing_route_manifest_avoids_cascading_binding_noise(monkeypatch):
    capture = _collect_external_startup_capture(
        monkeypatch,
        collector_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:00:00+00:00"
        ),
        adapter_session=_NestedObservedAtRouteMcpSession(
            observed_at="2026-07-15T03:01:00+00:00"
        ),
    )
    route = "authority_archive_separation"
    route_manifest = capture["agent_context_startup_runtime"]["startup_receipt"][
        "context_binding"
    ]["route_manifest"]
    route_manifest.pop(route)

    gaps = _startup_claim(capture)["gaps"]
    assert f"agent_context_startup_route_missing:{route}" in gaps
    assert f"agent_context_startup_route_binding_shape_mismatch:{route}" not in gaps
    assert f"agent_context_startup_route_binding_schema_mismatch:{route}" not in gaps
    assert f"agent_context_startup_route_binding_route_mismatch:{route}" not in gaps
    assert f"agent_context_startup_route_request_binding_mismatch:{route}" not in gaps
    assert f"agent_context_startup_route_semantic_hash_invalid:{route}" not in gaps
    assert f"agent_context_startup_route_observed_hash_invalid:{route}" not in gaps


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


def test_collect_post_deploy_capture_promotes_verified_external_codex_startup_receipt(
    monkeypatch,
):
    collector_session = _NestedObservedAtRouteMcpSession(
        observed_at="2026-07-15T03:00:00+00:00"
    )
    adapter_session = _NestedObservedAtRouteMcpSession(
        observed_at="2026-07-15T03:01:00+00:00"
    )
    subprocess_observed: dict = {}

    @asynccontextmanager
    async def _collector_session_factory(_mcp_url: str):
        yield collector_session

    @asynccontextmanager
    async def _adapter_session_factory(_mcp_url: str):
        yield adapter_session

    class _Process:
        returncode = 0

        async def communicate(self, challenge_payload):
            challenge = json.loads(challenge_payload.decode("utf-8"))
            proof_fd = subprocess_observed["kwargs"]["pass_fds"][0]
            proof_key = os.read(proof_fd, 33)
            receipt = await collect_agent_context_consumer_startup_receipt(
                mcp_url="https://mcp.example.test/mcp",
                repository="pureliture/neurons",
                branch="main",
                project="neurons",
                consumer="codex",
                expected_commit="a" * 40,
                challenge=challenge,
                proof_key=proof_key,
                session_factory=_adapter_session_factory,
            )
            return json.dumps(receipt).encode("utf-8"), b""

    async def _create_subprocess_exec(*argv, **kwargs):
        subprocess_observed["argv"] = argv
        subprocess_observed["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(
        post_deploy_mcp_capture.asyncio,
        "create_subprocess_exec",
        _create_subprocess_exec,
    )

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
            expected_commit="a" * 40,
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            collect_agent_context_startup=True,
            session_factory=_collector_session_factory,
        )
    )

    startup = capture["agent_context_startup_runtime"]
    assert startup["evidence_origin"] == "external_consumer_process"
    assert startup["receipt_validation"] == {"status": "validated", "failures": []}
    assert startup["startup_context"]["loaded_on_startup"] is True
    assert startup["startup_context"]["section_counts"] == {
        "current_authority": 1,
        "style_preference": 1,
        "active_work": 1,
        "required_verification": 1,
    }
    assert startup["consumer_statuses"]["codex"]["status"] == "validated"
    assert startup["consumer_statuses"]["codex"]["host_startup_hook_status"] == (
        "not_validated"
    )
    assert startup["consumer_statuses"]["claude-code"]["status"] == "not_validated"
    assert startup["collector_execution"] == {
        "runner_kind": "default_external_subprocess",
        "subprocess_attested": True,
    }
    assert capture["production_mutation_performed"] is False

    adapter_context_calls = [
        arguments for name, arguments in adapter_session.calls if name == "brain_context_resolve"
    ]
    collector_context_calls = [
        arguments for name, arguments in collector_session.calls if name == "brain_context_resolve"
    ]
    adapter_route_calls = [
        arguments for name, arguments in adapter_session.calls if name == "brain_objects_query"
    ]
    collector_route_calls = [
        arguments for name, arguments in collector_session.calls if name == "brain_objects_query"
    ]
    assert len(adapter_context_calls) == 1
    assert adapter_context_calls == collector_context_calls
    assert adapter_context_calls[0]["consumer"] == "codex"
    assert adapter_context_calls[0]["project"] == "neurons"
    assert len(adapter_route_calls) == len(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert adapter_route_calls == collector_route_calls
    assert {call["route"] for call in adapter_route_calls} == set(
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )
    assert all(
        name in {"brain_context_resolve", "brain_objects_query"}
        for name, _ in adapter_session.calls
    )

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="a" * 40,
    )
    claim = {item["claim_id"]: item for item in report["claims"]}[
        "live.agent_context.startup_read_path"
    ]
    assert claim["status"] == "not_validated"
    assert claim["bounded_adapter_status"] == "validated"
    assert claim["host_startup_hook_status"] == "not_validated"
    assert claim["activation_scope"] == "codex_bounded_startup_read_only.v1"

    original_product_hash = capture["agent_context_product"]["source_payload_hash"]
    capture["agent_context_product"]["source_payload_hash"] = "sha256:" + "0" * 64
    substituted_product_report = (
        build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
            captured_evidence=capture,
            expected_commit="a" * 40,
        )
    )
    substituted_product_claim = {
        item["claim_id"]: item for item in substituted_product_report["claims"]
    }["live.agent_context.startup_read_path"]
    assert substituted_product_claim["status"] == "failed"
    assert "agent_context_startup_product_capture_binding_mismatch" in (
        substituted_product_claim["gaps"]
    )
    capture["agent_context_product"]["source_payload_hash"] = original_product_hash

    current_authority = capture["agent_context_product"]["sections"]["current_authority"]
    current_authority["object_count"] += 1
    substituted_projection_report = (
        build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
            captured_evidence=capture,
            expected_commit="a" * 40,
        )
    )
    substituted_projection_claim = {
        item["claim_id"]: item for item in substituted_projection_report["claims"]
    }["live.agent_context.startup_read_path"]
    assert substituted_projection_claim["status"] == "failed"
    assert "agent_context_startup_product_projection_binding_mismatch" in (
        substituted_projection_claim["gaps"]
    )
    current_authority["object_count"] -= 1

    capture["brain_objects_query_smokes"][0]["object_pack"]["object_count"] += 1
    substituted_route_report = (
        build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
            captured_evidence=capture,
            expected_commit="a" * 40,
        )
    )
    substituted_route_claim = {
        item["claim_id"]: item for item in substituted_route_report["claims"]
    }["live.agent_context.startup_read_path"]
    assert substituted_route_claim["status"] == "failed"
    assert (
        "agent_context_startup_route_capture_binding_mismatch:authority_archive_separation"
        in substituted_route_claim["gaps"]
    )
    capture["brain_objects_query_smokes"][0]["object_pack"]["object_count"] -= 1

    original_route_source_hash = capture["brain_objects_query_smokes"][0][
        "source_payload_hash"
    ]
    capture["brain_objects_query_smokes"][0]["source_payload_hash"] = (
        "sha256:" + "0" * 64
    )
    substituted_source_hash_report = (
        build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
            captured_evidence=capture,
            expected_commit="a" * 40,
        )
    )
    substituted_source_hash_claim = {
        item["claim_id"]: item for item in substituted_source_hash_report["claims"]
    }["live.agent_context.startup_read_path"]
    assert substituted_source_hash_claim["status"] == "failed"
    assert (
        "agent_context_startup_route_capture_binding_mismatch:authority_archive_separation"
        in substituted_source_hash_claim["gaps"]
    )
    capture["brain_objects_query_smokes"][0][
        "source_payload_hash"
    ] = original_route_source_hash

    serialized_capture = json.loads(json.dumps(capture))
    replay_report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=serialized_capture,
        expected_commit="a" * 40,
    )
    replay_claim = {item["claim_id"]: item for item in replay_report["claims"]}[
        "live.agent_context.startup_read_path"
    ]
    assert replay_claim["status"] == "not_validated"
    assert replay_claim["bounded_adapter_status"] == "not_validated"
    assert replay_claim["host_startup_hook_status"] == "not_validated"
    assert "agent_context_startup_collector_capability_missing" in replay_claim["gaps"]
    assert replay_report["status"] == "PASS_WITH_GAPS"
    assert replay_report["production_ready"] is False
    assert replay_report["production_mutation_performed"] is False

    activation_report = build_product_activation_progress_report(
        live_evidence=serialized_capture,
    )
    activation_checks = {
        item["phase"]: item for item in activation_report["product_evidence_checks"]
    }
    p9 = next(
        item
        for item in activation_report["product_evidence_summary"]
        if item["phase"] == "P9"
    )
    assert activation_report["status"] == "PASS_WITH_GAPS"
    assert activation_checks["P9"]["result"] == "PASS_WITH_GAPS"
    assert p9["startup_read_path_claim_status"] == "not_validated"
    assert p9["bounded_adapter_status"] == "not_validated"
    assert p9["host_startup_hook_status"] == "not_validated"
    assert activation_report["production_ready"] is False
    assert activation_report["production_mutation_performed"] is False

    capture["agent_context_startup_runtime"]["startup_receipt"]["receipt_hash"] = (
        "sha256:" + "0" * 64
    )
    mutated_report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="a" * 40,
    )
    mutated_claim = {item["claim_id"]: item for item in mutated_report["claims"]}[
        "live.agent_context.startup_read_path"
    ]
    assert mutated_claim["status"] == "failed"
    assert mutated_claim["bounded_adapter_status"] == "failed"
    assert mutated_claim["host_startup_hook_status"] == "failed"
    assert "agent_context_startup_collector_capability_missing" in mutated_claim["gaps"]

    capture["agent_context_startup_runtime"] = object()
    invalid_type_report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="a" * 40,
    )
    invalid_type_claim = {item["claim_id"]: item for item in invalid_type_report["claims"]}[
        "live.agent_context.startup_read_path"
    ]
    assert invalid_type_claim["status"] == "not_validated"


def test_collect_post_deploy_capture_does_not_attest_injected_startup_runner():
    collector_session = _FakeMcpSession()
    adapter_session = _FakeMcpSession()

    @asynccontextmanager
    async def _collector_session_factory(_mcp_url: str):
        yield collector_session

    @asynccontextmanager
    async def _adapter_session_factory(_mcp_url: str):
        yield adapter_session

    async def _injected_runner(**kwargs):
        return await collect_agent_context_consumer_startup_receipt(
            **kwargs,
            session_factory=_adapter_session_factory,
        )

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
            expected_commit="a" * 40,
            collect_agent_context_startup=True,
            agent_context_startup_runner=_injected_runner,
            session_factory=_collector_session_factory,
        )
    )

    startup = capture["agent_context_startup_runtime"]
    assert startup["receipt_validation"]["status"] == "validated"
    assert startup["collector_execution"] == {
        "runner_kind": "injected_runner_unattested",
        "subprocess_attested": False,
    }
    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="a" * 40,
    )
    claim = {item["claim_id"]: item for item in report["claims"]}[
        "live.agent_context.startup_read_path"
    ]
    assert claim["status"] == "failed"
    assert "agent_context_startup_collector_capability_missing" in claim["gaps"]
    assert "agent_context_startup_external_subprocess_unattested" in claim["gaps"]


def test_agent_context_startup_cli_reads_exact_one_time_key_from_inherited_fd(
    monkeypatch,
    capsys,
):
    proof_key = b"k" * 32
    read_fd, write_fd = os.pipe()
    os.write(write_fd, proof_key)
    os.close(write_fd)
    challenge = {"schema_version": "agent_context_consumer_challenge.v1"}
    observed: dict = {}

    async def _collect(**kwargs):
        observed.update(kwargs)
        return {
            "schema_version": "agent_context_consumer_startup_receipt.v2",
            "production_mutation_performed": False,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_agent_context_consumer_startup_receipt",
        _collect,
    )
    monkeypatch.setattr(object_cli.sys, "stdin", io.StringIO(json.dumps(challenge)))

    assert (
        object_cli.agent_context_startup_main(
            [
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
                "a" * 40,
                "--proof-fd",
                str(read_fd),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert observed["proof_key"] == proof_key
    assert observed["challenge"] == challenge
    assert proof_key.hex() not in output
    with pytest.raises(OSError):
        os.read(read_fd, 1)


def test_agent_context_startup_cli_rejects_proof_key_with_extra_byte(
    monkeypatch,
    capsys,
):
    proof_key = b"x" * 33
    read_fd, write_fd = os.pipe()
    os.write(write_fd, proof_key)
    os.close(write_fd)
    monkeypatch.setattr(
        object_cli.sys,
        "stdin",
        io.StringIO('{"schema_version":"agent_context_consumer_challenge.v1"}'),
    )

    with pytest.raises(SystemExit):
        object_cli.agent_context_startup_main(
            [
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
                "a" * 40,
                "--proof-fd",
                str(read_fd),
            ]
        )

    captured = capsys.readouterr()
    assert "agent context startup collection failed" in captured.err
    assert proof_key.hex() not in captured.err


def test_default_agent_context_startup_runner_keeps_proof_key_out_of_argv_and_stdin(
    monkeypatch,
):
    proof_key = b"p" * 32
    observed: dict = {}

    class _Process:
        returncode = 0

        async def communicate(self, stdin_payload):
            observed["stdin_payload"] = stdin_payload
            return (
                json.dumps(
                    {
                        "schema_version": "agent_context_consumer_startup_receipt.v2",
                        "production_mutation_performed": False,
                    }
                ).encode("utf-8"),
                b"",
            )

    async def _create_subprocess_exec(*argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(
        post_deploy_mcp_capture.asyncio,
        "create_subprocess_exec",
        _create_subprocess_exec,
    )
    challenge = {
        "schema_version": "agent_context_consumer_challenge.v1",
        "challenge_id": "challenge:fixture",
    }

    receipt = asyncio.run(
        post_deploy_mcp_capture._default_agent_context_startup_runner(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
            expected_commit="a" * 40,
            challenge=challenge,
            proof_key=proof_key,
        )
    )

    argv_text = " ".join(str(item) for item in observed["argv"])
    stdin_text = observed["stdin_payload"].decode("utf-8")
    assert receipt["schema_version"] == "agent_context_consumer_startup_receipt.v2"
    assert len(observed["kwargs"]["pass_fds"]) == 1
    assert "env" not in observed["kwargs"]
    assert proof_key.hex() not in argv_text
    assert proof_key.hex() not in stdin_text
    assert json.loads(stdin_text) == challenge


def test_collect_post_deploy_capture_fails_closed_for_tampered_startup_receipt():
    collector_session = _FakeMcpSession()
    adapter_session = _FakeMcpSession()

    @asynccontextmanager
    async def _collector_session_factory(_mcp_url: str):
        yield collector_session

    @asynccontextmanager
    async def _adapter_session_factory(_mcp_url: str):
        yield adapter_session

    async def _tampered_runner(**kwargs):
        receipt = await collect_agent_context_consumer_startup_receipt(
            **kwargs,
            session_factory=_adapter_session_factory,
        )
        receipt["issuer"]["kind"] = "server_runtime"
        return receipt

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
            expected_commit="a" * 40,
            collect_agent_context_startup=True,
            agent_context_startup_runner=_tampered_runner,
            session_factory=_collector_session_factory,
        )
    )

    startup = capture["agent_context_startup_runtime"]
    assert startup["startup_context"]["loaded_on_startup"] is False
    assert startup["receipt_validation"]["status"] == "failed"
    assert "agent_context_startup_issuer_not_external_consumer" in startup[
        "receipt_validation"
    ]["failures"]
    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="a" * 40,
    )
    claim = {item["claim_id"]: item for item in report["claims"]}[
        "live.agent_context.startup_read_path"
    ]
    assert claim["status"] == "failed"


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
    product = capture["agent_context_product"]
    assert product["schema_version"] == "agent_context_product_pack.v1"
    assert product["consumer"] == "codex"
    assert product["sections"]["current_authority"]["object_count"] == 1
    assert product["sections"]["current_authority"]["authority_lanes"] == [
        "accepted_current"
    ]
    assert product["sections"]["current_authority"]["item_hashes"]
    assert all("items" not in section for section in product["sections"].values())
    assert len(product["tool_hints"]) == len(REQUIRED_RUNTIME_TOOL_NAMES)
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
            "current_request": "agent context startup before task dispatch",
            "limit": 8,
            "response_mode": "full",
            "consumer": "codex",
        }
    ]
    route_calls = [arguments for name, arguments in session.calls if name == "brain_objects_query"]
    assert [arguments["route"] for arguments in route_calls] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(
        arguments["query"] == f"agent context startup route smoke: {arguments['route']}"
        for arguments in route_calls
    )
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
    assert claims["live.temporal_recall.corrective_checkpoint"]["status"] == "not_validated"
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "not_validated"
    assert "live_temporal_recall_corrective_checkpoint_unverified" in report["gaps"]
    assert claims["live.source_to_candidate.projection_join"]["status"] == "not_validated"
    assert "live_graph_qdrant_projection_join_unproven" in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_builds_temporal_semantic_acceptance_checkpoint():
    date_a_object = {
        "schema_version": "knowledge_object_envelope.v1",
        "object_id": "ko:WorkUnit:date-a",
        "object_type": "WorkUnit",
        "title": "Date A work",
        "content_hash": "sha256:" + "a" * 64,
        "observed_at": "2026-07-09T12:00:00Z",
    }
    date_b_object = {
        "schema_version": "knowledge_object_envelope.v1",
        "object_id": "ko:WorkUnit:date-b",
        "object_type": "WorkUnit",
        "title": "Date B work",
        "content_hash": "sha256:" + "b" * 64,
        "observed_at": "2026-07-15T12:00:00Z",
    }
    semantic_result = {
        "brain_id": "/project/neurons",
        "result_type": "session_memory",
        "retrieval_lane": "qdrant_semantic",
        "summary": "Temporal projection currentness semantic evidence",
        "why_retrieved": "semantic_match",
        "source_ref": "sanitized-semantic-ref",
        "privacy": "redacted",
        "score": 0.91,
    }
    semantic_query_text = "how is temporal projection currentness verified"

    class _TemporalAcceptanceSession(_FakeMcpSession):
        async def list_tools(self):
            listed = await super().list_tools()
            return SimpleNamespace(
                tools=[*listed.tools, SimpleNamespace(name=BRAIN_QUERY_TOOL_NAME)]
            )

        async def call_tool(self, name: str, arguments: dict):
            if (
                name == "brain_source_to_candidate_runtime_readiness"
                and arguments.get("collect_shadow_evidence") is True
            ):
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(
                        live=True,
                        temporal_correctness_runtime=True,
                    ),
                )
            if name == BRAIN_QUERY_TOOL_NAME:
                self.calls.append((name, dict(arguments)))
                if arguments.get("query") == semantic_query_text:
                    return SimpleNamespace(
                        isError=False,
                        structuredContent={
                            "schema_version": "brain_query.v2",
                            "results": [semantic_result],
                            "current": [],
                            "accepted": [],
                            "audit": {
                                "semantic_ranker_bound": True,
                                "semantic_ranker_used": True,
                            },
                        },
                    )
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "schema_version": "brain_query.v2",
                        "results": [],
                        "current": [],
                        "accepted": [],
                        "audit": {
                            "semantic_ranker_bound": True,
                            "semantic_ranker_used": True,
                        },
                    },
                )
            if name == "brain_objects_query" and any(
                field in arguments for field in ("as_of", "date_from", "date_to")
            ):
                self.calls.append((name, dict(arguments)))
                if (
                    arguments.get("date_from") == "2026-07-16T00:00:00Z"
                    and arguments.get("date_to") == "2026-07-15T00:00:00Z"
                ):
                    return SimpleNamespace(
                        isError=True,
                        structuredContent={"error_code": -32602},
                    )
                as_of = str(arguments.get("as_of") or "")
                if as_of.startswith("2026-07-09") or arguments.get("date_from"):
                    objects = [date_a_object]
                    gaps = []
                    confidence = {"score": 0.9}
                elif as_of.startswith("2026-07-15"):
                    objects = [date_b_object]
                    gaps = []
                    confidence = {"score": 0.9}
                else:
                    objects = []
                    gaps = ["temporal_evidence_mismatch"]
                    confidence = {"score": 0.0}
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "schema_version": "brain_objects_query.v1",
                        "route": "temporal_work_recall",
                        "object_pack": {
                            "schema_version": "object_pack.v1",
                            "route": "temporal_work_recall",
                            "objects": objects,
                            "edges": [],
                            "evidence": [],
                            "recommended_actions": [],
                            "lanes": {"accepted_current": objects},
                            "confidence": confidence,
                            "gaps": gaps,
                        },
                    },
                )
            return await super().call_tool(name, arguments)

    session = _TemporalAcceptanceSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    temporal_acceptance = {
        "temporal_query": "temporal work recall",
        "date_a": {
            "as_of": "2026-07-09T12:00:00Z",
            "expected_object_fingerprint": hash_payload(date_a_object),
            "expected_object_identity_fingerprint": (
                post_deploy_mcp_capture._temporal_work_unit_identity_fingerprint(
                    date_a_object
                )
            ),
        },
        "date_b": {
            "as_of": "2026-07-15T12:00:00Z",
            "expected_object_fingerprint": hash_payload(date_b_object),
            "expected_object_identity_fingerprint": (
                post_deploy_mcp_capture._temporal_work_unit_identity_fingerprint(
                    date_b_object
                )
            ),
        },
        "range_boundary": {
            "date_from": "2026-07-09T12:00:00Z",
            "date_to": "2026-07-09T12:00:00Z",
            "expected_object_fingerprint": hash_payload(date_a_object),
            "expected_object_identity_fingerprint": (
                post_deploy_mcp_capture._temporal_work_unit_identity_fingerprint(
                    date_a_object
                )
            ),
        },
        "mismatch": {"as_of": "2026-07-01T12:00:00Z"},
        "invalid_range": {
            "date_from": "2026-07-16T00:00:00Z",
            "date_to": "2026-07-15T00:00:00Z",
        },
        "nonsense_query": "quasar marmalade unrelated nonsense",
        "semantic_query": {
            "query": semantic_query_text,
            "expected_result_fingerprint": hash_payload(semantic_result),
        },
        "runtime_expectations": {
            "schema_version": "temporal_correctness_runtime_expectations.v1",
            "baseline_coverage_count": 15,
            "baseline_backlog_count": 111,
            "minimum_source_session_count": 126,
            "minimum_valid_source_count": 126,
            "max_artifact_age_seconds": 3600,
        },
    }
    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            temporal_acceptance=temporal_acceptance,
            session_factory=_fake_session_factory,
        )
    )

    checkpoint = capture["temporal_recall_corrective_checkpoint"]
    assert checkpoint["schema_version"] == "temporal_recall_corrective_checkpoint.v1"
    assert checkpoint["temporal_query_hash"] == hash_payload("temporal work recall")
    assert checkpoint["selector_contract"]["invalid_range_error_code"] == -32602
    assert checkpoint["date_a"]["observed_object_fingerprint"] == hash_payload(date_a_object)
    assert checkpoint["date_b"]["observed_object_fingerprint"] == hash_payload(date_b_object)
    assert checkpoint["date_a"]["observed_object_identity_fingerprint"] == (
        post_deploy_mcp_capture._temporal_work_unit_identity_fingerprint(date_a_object)
    )
    assert checkpoint["date_b"]["observed_object_identity_fingerprint"] == (
        post_deploy_mcp_capture._temporal_work_unit_identity_fingerprint(date_b_object)
    )
    assert checkpoint["mismatch"] == {
        "selector_hash": hash_payload({"as_of": "2026-07-01T12:00:00Z"}),
        "object_count": 0,
        "gap_count": 1,
        "confidence_score": 0.0,
    }
    assert checkpoint["nonsense_query"]["result_count"] == 0
    assert checkpoint["nonsense_query"]["semantic_ranker_bound"] is True
    assert checkpoint["nonsense_query"]["semantic_ranker_used"] is True
    assert checkpoint["semantic_query"] == {
        "query_hash": hash_payload(semantic_query_text),
        "expected_result_fingerprint": hash_payload(semantic_result),
        "observed_result_fingerprint": hash_payload(semantic_result),
        "result_count": 1,
        "why_retrieved_semantic_match": True,
        "score": 0.91,
        "minimum_score": 0.75,
        "semantic_ranker_bound": True,
        "semantic_ranker_used": True,
        "qdrant_semantic_result_lane_used": True,
    }
    assert checkpoint["runtime_aggregate_source"] == "live_mcp_runtime_packet"
    assert checkpoint["runtime_postcheck_receipt_hash"] == ""
    assert "ko:WorkUnit" not in json.dumps(checkpoint, sort_keys=True)
    assert semantic_query_text not in json.dumps(checkpoint, sort_keys=True)
    assert semantic_result["summary"] not in json.dumps(checkpoint, sort_keys=True)

    selector_calls = [
        arguments
        for name, arguments in session.calls
        if name == "brain_objects_query"
        and any(field in arguments for field in ("as_of", "date_from", "date_to"))
    ]
    assert len(selector_calls) == 5
    assert all(
        arguments["query"] == "temporal work recall" for arguments in selector_calls
    )
    assert any(arguments.get("as_of") == "2026-07-09T12:00:00Z" for arguments in selector_calls)
    assert any(arguments.get("as_of") == "2026-07-15T12:00:00Z" for arguments in selector_calls)
    assert any("date_from" in arguments and "date_to" in arguments for arguments in selector_calls)
    brain_query_calls = [
        arguments
        for name, arguments in session.calls
        if name == BRAIN_QUERY_TOOL_NAME
    ]
    assert len(brain_query_calls) == 2
    assert {arguments["query"] for arguments in brain_query_calls} == {
        temporal_acceptance["nonsense_query"],
        semantic_query_text,
    }

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.temporal_recall.corrective_checkpoint"]["status"] == "validated"
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "validated"


def test_temporal_acceptance_config_requires_positive_semantic_query():
    config = {
        "temporal_query": "temporal work recall",
        "date_a": {
            "as_of": "2026-07-09T12:00:00Z",
            "expected_object_fingerprint": "sha256:" + "a" * 64,
            "expected_object_identity_fingerprint": "sha256:" + "d" * 64,
        },
        "date_b": {
            "as_of": "2026-07-15T12:00:00Z",
            "expected_object_fingerprint": "sha256:" + "b" * 64,
            "expected_object_identity_fingerprint": "sha256:" + "e" * 64,
        },
        "range_boundary": {
            "date_from": "2026-07-09T12:00:00Z",
            "date_to": "2026-07-09T12:00:00Z",
            "expected_object_fingerprint": "sha256:" + "a" * 64,
            "expected_object_identity_fingerprint": "sha256:" + "d" * 64,
        },
        "mismatch": {"as_of": "2026-07-01T12:00:00Z"},
        "invalid_range": {
            "date_from": "2026-07-16T00:00:00Z",
            "date_to": "2026-07-15T00:00:00Z",
        },
        "nonsense_query": "quasar marmalade unrelated nonsense",
        "runtime_expectations": {
            "schema_version": "temporal_correctness_runtime_expectations.v1",
            "baseline_coverage_count": 15,
            "baseline_backlog_count": 111,
            "minimum_source_session_count": 126,
            "minimum_valid_source_count": 126,
            "max_artifact_age_seconds": 3600,
        },
    }

    with pytest.raises(ValueError, match="semantic_query"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)


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


def test_collect_post_deploy_mcp_capture_projects_untrusted_values_to_hashes():
    context_raw_value = "private transcript text must never leave the collector"
    route_raw_value = "opaque-raw-document-id-must-never-leak"
    secret_raw_value = "secret-value-under-an-innocent-key"
    tool_raw_value = "private-tool-name-must-never-leak"

    class _UntrustedValueSession(_FakeMcpSession):
        async def list_tools(self):
            result = await super().list_tools()
            result.tools.append(SimpleNamespace(name=tool_raw_value))
            return result

        async def call_tool(self, name: str, arguments: dict):
            if (
                name == "brain_source_to_candidate_runtime_readiness"
                and arguments.get("collect_shadow_evidence") is True
            ):
                self.calls.append((name, dict(arguments)))
                structured = _runtime_collected_packet(
                    live=True,
                    session_project_rollup=True,
                )
                structured["projection_join"]["summary"] = context_raw_value
                structured["session_project_rollup_runtime"]["rollup_preview"][
                    "metadata"
                ] = {"summary": secret_raw_value, "opaque_id": route_raw_value}
                return SimpleNamespace(isError=False, structuredContent=structured)
            result = await super().call_tool(name, arguments)
            structured = json.loads(json.dumps(result.structuredContent))
            if name == "brain_context_resolve":
                structured["authority"]["agent_context_product"]["sections"][
                    "current_authority"
                ]["items"][0]["title"] = context_raw_value
            elif name == "brain_objects_query":
                structured["object_pack"]["objects"] = [
                    {
                        "object_id": route_raw_value,
                        "object_type": "RepoDocument",
                        "title": context_raw_value,
                        "metadata": {"summary": secret_raw_value},
                    }
                ]
                structured["object_pack"]["recommended_actions"] = [
                    {"action": "request_evidence", "summary": secret_raw_value}
                ]
            return SimpleNamespace(isError=False, structuredContent=structured)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _UntrustedValueSession()

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
    assert context_raw_value not in serialized
    assert route_raw_value not in serialized
    assert secret_raw_value not in serialized
    assert tool_raw_value not in serialized
    assert capture["projection_join"]["source_payload_hash"].startswith("sha256:")
    assert capture["session_project_rollup_runtime"]["source_payload_hash"].startswith(
        "sha256:"
    )
    assert capture["agent_context_product"]["sections"]["current_authority"][
        "item_hashes"
    ]
    assert all(
        smoke["object_pack"]["object_count"] == 1
        and smoke["object_pack"]["source_payload_hash"].startswith("sha256:")
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
    temporal_acceptance_file = tmp_path / "temporal-acceptance.json"
    temporal_acceptance = {
        "date_a": {"as_of": "2026-07-09", "expected_object_fingerprint": "sha256:" + "a" * 64},
        "date_b": {"as_of": "2026-07-15", "expected_object_fingerprint": "sha256:" + "b" * 64},
    }
    temporal_acceptance_file.write_text(
        json.dumps(temporal_acceptance),
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
                "--temporal-acceptance-file",
                str(temporal_acceptance_file),
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
    assert seen["temporal_acceptance"] == temporal_acceptance
    assert str(artifact_descriptor_file) not in json.dumps(output, sort_keys=True)
    assert artifact_descriptor["summary"] not in json.dumps(output, sort_keys=True)


def test_runtime_readiness_cli_collect_post_deploy_capture_preserves_sanitized_gitops_state(
    monkeypatch, capsys, tmp_path
):
    gitops_file = tmp_path / "gitops-desired-state.json"
    gitops_state = {
        "schema_version": "gitops_desired_state_identity.v1",
        "desired_state_source": "sanitized_ops_manifest_summary",
        "source_commit": "c2b8548",
        "desired_image_set_hash": "sha256:" + "a" * 64,
        "ops_revision": "ops-42",
        "reconciled_ops_revision": "ops-42",
        "expected_image_ref_count": 1,
        "production_mutation_performed": False,
    }
    gitops_file.write_text(json.dumps(gitops_state), encoding="utf-8")
    seen: dict[str, object] = {}

    async def _fake_collect(**kwargs):
        seen.update(kwargs)
        return {
            "schema_version": "source_to_candidate_runtime_post_deploy_mcp_capture.v1",
            "expected_commit": kwargs["expected_commit"],
            "gitops_desired_state": kwargs["gitops_desired_state"],
            "production_mutation_performed": False,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_source_to_candidate_post_deploy_mcp_capture",
        _fake_collect,
    )
    monkeypatch.setattr(
        object_cli,
        "build_source_to_candidate_runtime_post_deploy_capture_readiness_report",
        lambda **_kwargs: {
            "schema_version": "source_to_candidate_runtime_readiness.v1",
            "status": "PASS_WITH_GAPS",
        },
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
                "--gitops-desired-state-file",
                str(gitops_file),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert seen["gitops_desired_state"] == gitops_state
    assert output["expected_commit"] == "c2b8548"
    assert output["gitops_desired_state"] == gitops_state


def test_runtime_readiness_cli_collect_post_deploy_capture_passes_argo_reconciliation(
    monkeypatch, capsys, tmp_path
):
    argo_file = tmp_path / "argo-reconciliation.json"
    argo_state = {
        "schema_version": "argo_reconciliation_identity.v1",
        "reconciliation_source": "sanitized_argo_application_summary",
        "reconciled_ops_revision": "a" * 40,
        "sync_status": "Synced",
        "health_status": "Healthy",
        "production_mutation_performed": False,
    }
    argo_file.write_text(json.dumps(argo_state), encoding="utf-8")
    seen: dict[str, object] = {}

    async def _fake_collect(**kwargs):
        seen.update(kwargs)
        return {"schema_version": "source_to_candidate_runtime_post_deploy_mcp_capture.v1"}

    monkeypatch.setattr(object_cli, "collect_source_to_candidate_post_deploy_mcp_capture", _fake_collect)
    monkeypatch.setattr(
        object_cli,
        "build_source_to_candidate_runtime_post_deploy_capture_readiness_report",
        lambda **_kwargs: {"schema_version": "source_to_candidate_runtime_readiness.v1", "status": "PASS_WITH_GAPS"},
    )

    assert main([
        "source-to-candidate-runtime-readiness",
        "--collect-post-deploy-mcp-capture",
        "--mcp-url", "https://mcp.example.test/mcp",
        "--argo-reconciliation-file", str(argo_file),
    ]) == 0

    capsys.readouterr()
    assert seen["argo_reconciliation"] == argo_state


def test_golden_query_eval_activation_progress_passes_external_expected_commit(
    monkeypatch, capsys
):
    seen: dict[str, object] = {}

    def _fake_progress(**kwargs):
        seen.update(kwargs)
        return {"schema_version": "knowledge_object_product_activation_progress.v1"}

    monkeypatch.setattr(object_cli, "build_product_activation_progress_report", _fake_progress)

    assert main([
        "golden-query-eval", "--activation-progress", "--expected-commit", "c2b8548",
    ]) == 0

    capsys.readouterr()
    assert seen["expected_commit"] == "c2b8548"


def test_collect_post_deploy_mcp_capture_calculates_gitops_deployment_binding():
    state = {
        "schema_version": "gitops_desired_state_identity.v1",
        "images_include_expected_commit": True,
        "desired_state_source": "sanitized_ops_manifest_summary",
        "target_revision": "main",
        "source_commit": _BOUND_SOURCE_COMMIT,
        "desired_image_set_hash": "sha256:" + "a" * 64,
        "ops_revision": "a" * 40,
        "expected_image_ref_count": 1,
        "production_mutation_performed": False,
    }
    session = _FakeMcpSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            expected_commit=_BOUND_SOURCE_COMMIT,
            gitops_desired_state=state,
            argo_reconciliation={
                "schema_version": "argo_reconciliation_identity.v1",
                "reconciliation_source": "sanitized_argo_application_summary",
                "reconciled_ops_revision": "a" * 40,
                "sync_status": "Synced",
                "health_status": "Healthy",
                "production_mutation_performed": False,
            },
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_live_runtime_evidence",
                "source_commit": _BOUND_SOURCE_COMMIT,
                "live_image_set_hash": "sha256:" + "a" * 64,
                "stale_image_ref_count": 0,
                "production_mutation_performed": False,
            },
            session_factory=_fake_session_factory,
        )
    )

    assert capture["deployment_evidence_binding"]["schema_version"] == "deployment_evidence_binding.v1"
    assert capture["deployment_evidence_binding"]["canonical_tuple_hash"].startswith("sha256:")
    assert capture["argo_reconciliation"]["reconciled_ops_revision"] == "a" * 40
    assert capture["deployed_identity"]["live_image_set_hash"] == "sha256:" + "a" * 64

    packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=capture,
    )
    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=packet,
        expected_commit=_BOUND_SOURCE_COMMIT,
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}

    assert claims["ops.gitops_deployment_evidence_binding"]["status"] == "validated"


def test_collect_post_deploy_mcp_capture_keeps_missing_live_identity_as_binding_gap():
    session = _FakeMcpSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            expected_commit=_BOUND_SOURCE_COMMIT,
            gitops_desired_state={
                "schema_version": "gitops_desired_state_identity.v1",
                "images_include_expected_commit": True,
                "desired_state_source": "sanitized_ops_manifest_summary",
                "target_revision": "main",
                "source_commit": _BOUND_SOURCE_COMMIT,
                "desired_image_set_hash": "sha256:" + "a" * 64,
                "ops_revision": "a" * 40,
                "expected_image_ref_count": 1,
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
            session_factory=_fake_session_factory,
        )
    )

    assert "deployment_evidence_binding" not in capture


@pytest.mark.parametrize("expected_commit", ["", "main"])
def test_collect_post_deploy_mcp_capture_does_not_mint_binding_without_immutable_commit(
    expected_commit,
):
    session = _FakeMcpSession()

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            expected_commit=expected_commit,
            gitops_desired_state={
                "schema_version": "gitops_desired_state_identity.v1",
                "images_include_expected_commit": True,
                "desired_state_source": "sanitized_ops_manifest_summary",
                "target_revision": "main",
                "source_commit": _BOUND_SOURCE_COMMIT,
                "desired_image_set_hash": "sha256:" + "a" * 64,
                "ops_revision": "a" * 40,
                "expected_image_ref_count": 1,
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
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_live_runtime_evidence",
                "source_commit": _BOUND_SOURCE_COMMIT,
                "live_image_set_hash": "sha256:" + "a" * 64,
                "stale_image_ref_count": 0,
                "production_mutation_performed": False,
            },
            session_factory=_fake_session_factory,
        )
    )

    assert "deployment_evidence_binding" not in capture


@pytest.mark.parametrize(
    ("readiness_status", "expected_exit_code"),
    [
        ("FAIL", 1),
        ("PASS", 0),
        ("PASS_WITH_GAPS", 0),
    ],
)
def test_runtime_readiness_cli_collect_post_deploy_mcp_capture_uses_readiness_exit_status(
    monkeypatch,
    capsys,
    readiness_status: str,
    expected_exit_code: int,
):
    capture = {
        "schema_version": "source_to_candidate_runtime_post_deploy_mcp_capture.v1",
        "production_mutation_performed": False,
    }

    async def _fake_collect(**_kwargs):
        return capture

    def _fake_readiness_report(**_kwargs):
        return {
            "schema_version": "source_to_candidate_runtime_readiness.v1",
            "status": readiness_status,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_source_to_candidate_post_deploy_mcp_capture",
        _fake_collect,
    )
    monkeypatch.setattr(
        object_cli,
        "build_source_to_candidate_runtime_post_deploy_capture_readiness_report",
        _fake_readiness_report,
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--collect-post-deploy-mcp-capture",
                "--mcp-url",
                "https://mcp.example.test/mcp",
            ]
        )
        == expected_exit_code
    )

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == capture["schema_version"]
    assert output["runtime_readiness"]["status"] == readiness_status

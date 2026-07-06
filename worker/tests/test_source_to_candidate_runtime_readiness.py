from __future__ import annotations

import json

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_source_to_candidate_runtime_readiness_report,
)


def _sanitized_live_evidence(**overrides):
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
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
    assert "live_mcp_review_tools_unverified" in report["gaps"]
    assert "live_brain_objects_query_route_smokes_unverified" in report["gaps"]
    assert "live_deployed_identity_unverified" in report["gaps"]
    assert "live_object_authority_gate_policy_unverified" in report["gaps"]


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
            _brain_objects_query_smoke("authority_archive_separation", gaps=["object_pack_route_not_implemented"]),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("temporal_work_recall"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
    )

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=evidence)

    assert report["status"] == "FAIL"
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "failed"
    assert "brain_objects_query_route_unimplemented:authority_archive_separation" in report["gaps"]


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


def test_runtime_readiness_requires_live_agent_context_product_sections():
    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "tool_hints": _safe_tool_hints(),
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


def test_runtime_readiness_fails_when_live_agent_context_allows_mutation():
    evidence = _sanitized_live_evidence(
        agent_context_product={
            "schema_version": "agent_context_product_pack.v1",
            "tool_hints": _safe_tool_hints(),
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
    assert report["production_mutation_performed"] is False

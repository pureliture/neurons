from __future__ import annotations

import json

from agent_knowledge.cli import main
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
            "tool_hints": [{"tool": name} for name in REQUIRED_RUNTIME_TOOL_NAMES],
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
        },
        "deployed_identity": {
            "contains_expected_commit": True,
            "identity_source": "redacted_live_runtime_evidence",
        },
    }
    evidence.update(overrides)
    return evidence


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
    assert "live_mcp_review_tools_unverified" in report["gaps"]
    assert "live_brain_objects_query_route_smokes_unverified" in report["gaps"]
    assert "live_deployed_identity_unverified" in report["gaps"]


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
    assert claims["live.brain_objects_query.route_smokes"]["status"] == "validated"
    assert "temporal_work_recall" in claims["live.brain_objects_query.route_smokes"]["required_routes"]
    assert claims["live.deployed_identity.includes_expected_commit"]["status"] == "validated"
    assert claims["live.production.source_to_candidate_denial"]["status"] == "denied_as_expected"
    assert claims["live.production.approval_board_denial"]["status"] == "denied_as_expected"


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

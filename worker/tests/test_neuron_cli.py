from __future__ import annotations

import json
import sqlite3

import pytest

from agent_knowledge.cli import BOUNDARY, COMMAND_HANDLERS, main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.knowledge_objects import EvidenceRef, KnowledgeEdge
from object_query_route_cases import REQUIRED_OBJECT_QUERY_ROUTE_CASES


def _reference_manifest() -> dict:
    return {
        "corpus_name": "palantir-ontology-mini",
        "sources": [
            {
                "source_id": "palantir-ontology-001",
                "title": "Ontology overview",
                "source_type": "WEB_PAGE",
                "source_url": "https://example.test/ontology",
                "normalized_path": "sources-normalized/palantir-ontology-001.md",
                "content_hash": "sha256:" + "1" * 64,
                "metadata_hash": "sha256:" + "2" * 64,
                "summary": "Objects, links, actions, functions.",
            },
            {
                "source_id": "palantir-ontology-002",
                "title": "Manual excerpt",
                "source_type": "TEXT",
                "normalized_path": "sources-normalized/palantir-ontology-002.md",
                "content_hash": "sha256:" + "3" * 64,
                "metadata_hash": "sha256:" + "4" * 64,
                "summary": "Manual source with missing URL.",
            },
        ],
    }


def _palantir_full_count_manifest() -> dict:
    sources = []
    for idx in range(1, 66):
        if idx <= 6:
            source_type = "PDF"
        elif idx <= 39:
            source_type = "WEB_PAGE"
        else:
            source_type = "TEXT"
        source = {
            "source_id": f"palantir-ontology-{idx:03d}",
            "title": f"Palantir ontology reference {idx:03d}",
            "source_type": source_type,
            "normalized_path": f"sources-normalized/palantir-ontology-{idx:03d}.md",
            "content_hash": "sha256:" + f"{idx:064x}"[-64:],
            "metadata_hash": "sha256:" + f"{idx + 100:064x}"[-64:],
            "summary": "Public-safe metadata-only reference fixture.",
        }
        if idx <= 39:
            source["source_url"] = f"https://example.test/palantir/ontology/{idx:03d}"
        sources.append(source)
    return {"corpus_name": "palantir-ontology", "sources": sources}


def _valid_p6_runtime_evidence(*, live: bool = True) -> dict:
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


def _preference_artifact_memory_runtime_evidence() -> dict:
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
    return {
        "schema_version": "preference_artifact_memory_runtime_evidence.v1",
        "evidence_class": "runtime_preference_artifact_memory",
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


def _valid_p6_p7_runtime_evidence(*, live: bool = True) -> dict:
    evidence = _valid_p6_runtime_evidence(live=live)
    evidence["preference_artifact_memory"] = _preference_artifact_memory_runtime_evidence()
    return evidence


def _valid_p3_post_deploy_capture(*, live: bool = True) -> dict:
    return {
        "schema_version": "source_to_candidate_runtime_post_deploy_mcp_capture.v1",
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


def test_neuron_knowledge_help_lists_server_owned_commands(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "usage: neuron-knowledge" in output
    for command in (
        "rag-ingress-state",
        "memory-regeneration",
        "session-memory-private-sync",
        "neuron-session-memory-build",
        "native-memory-sync",
        "session-memory-gc",
        "transcript-backfill",
        "session-entry-recall",
        "transcript-resources",
        "transcript-quality",
        "transcript-retrieval",
        "transcript-migration",
        "transcript-memory-gc",
        "transcript-session-gc",
        "transcript-volume-gc",
        "backfill",
        "memory",
        "context-for-prompt",
        "mcp-stdio",
        "eval",
        "derived-memory-resources",
        "session-memory-quarantine-terminal-skipped",
        "session-memory-repair-zombie-snapshots",
        "brain-context-resolve",
        "object-query",
        "object-explain",
        "corpus-status",
        "corpus-ingest-plan",
        "corpus-ingest",
        "object-authority-schema-ensure",
        "source-to-candidate-graph",
        "candidate-review-edit",
        "approval-board-decide",
        "golden-query-eval",
        "source-to-candidate-runtime-readiness",
        "okf-export",
        "brain-regression-gate",
        "couchdb-migration-flow",
        "couchdb-graph-trigger",
        "couchdb-graph-project",
        "couchdb-graph-status",
    ):
        assert command in COMMAND_HANDLERS
        assert command in output


def test_neuron_knowledge_boundary_is_server_owned(capsys):
    assert main(["--show-boundary"]) == 0
    assert capsys.readouterr().out.strip() == BOUNDARY


def test_neuron_knowledge_rejects_dendrite_command(capsys):
    assert main(["capture", "--help"]) == 2
    assert "unknown neurons command: capture" in capsys.readouterr().err


def test_neuron_knowledge_pending_server_command_fails_closed(capsys):
    assert main(["transcript-resources", "--help"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "neuron_knowledge_pending_command.v1"
    assert report["status"] == "blocked_pending_server_extraction"
    assert report["command"] == "transcript-resources"
    assert report["destination"] == "neurons"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_object_query_defaults_to_authority_archive_route(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "codex/knowledge-object-review-flow-roadmap",
            "--query",
            "LBrain source-to-candidate-graph product activation roadmap P5 P6 P7 P8 P9 current gaps",
            "--current-file",
            "docs/specs/roadmap.md",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "brain_objects_query.v1"
    assert report["route"] == "authority_archive_separation"
    assert report["object_pack"]["schema_version"] == "object_pack.v1"
    assert report["object_pack"]["route"] == "authority_archive_separation"
    assert "object_pack_route_not_implemented" not in report["object_pack"]["gaps"]
    assert report["object_pack"]["route_trace"]["route"] == "authority_archive_separation"
    assert report["object_pack"]["route_trace"]["route_source"] == "inferred"
    assert "reference_only" in report["object_pack"]["route_trace"]["selected_source_lanes"]
    assert report["object_pack"]["route_trace"]["stop_reason"] == "returned_object_pack"


def test_neuron_knowledge_object_query_accepts_explicit_route(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--route",
            "code_style_preference",
            "--query",
            "문서 정리 질문이어도 명시 route가 우선이어야 한다",
            "--consumer",
            "codex",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["route"] == "code_style_preference"
    assert report["response_mode"] == "compact"
    assert report["object_pack"]["route"] == "code_style_preference"
    assert report["object_pack"]["audit"]["consumer"] == "codex"
    assert "object_pack_route_not_implemented" not in report["object_pack"]["gaps"]


def test_neuron_knowledge_object_query_accepts_explicit_html_visualization_route(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--route",
            "html_visualization_preference",
            "--query",
            "문서 정리 질문이어도 명시 route가 우선이어야 한다",
            "--consumer",
            "codex",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    pack = report["object_pack"]
    assert rc == 0
    assert report["route"] == "html_visualization_preference"
    assert pack["route"] == "html_visualization_preference"
    assert pack["route_trace"]["route_source"] == "explicit"
    assert pack["route_trace"]["missing_evidence"] == [
        "accepted_html_preference_missing",
        "visualization_preference_missing",
    ]
    assert "object_pack_route_not_implemented" not in pack["gaps"]


def test_neuron_knowledge_object_query_infers_temporal_route(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--query",
            "어제 이 repo에서 뭐 했어? 작업 재개하려면 뭐 봐야 해?",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["route"] == "temporal_work_recall"
    assert report["object_pack"]["route"] == "temporal_work_recall"
    assert report["object_pack"]["audit"]["source_pack_names"] == ["current_work", "required_verification"]
    assert "object_pack_route_not_implemented" not in report["object_pack"]["gaps"]


def test_neuron_knowledge_object_query_infers_deployment_route_with_runtime_gap(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--query",
            "이 PR merge됐어? 배포도 됐어?",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["route"] == "deployment_runtime_truth"
    assert report["object_pack"]["route"] == "deployment_runtime_truth"
    assert "runtime_evidence_unverified" in report["object_pack"]["gaps"]
    assert "object_pack_route_not_implemented" not in report["object_pack"]["gaps"]
    assert report["object_pack"]["route_trace"]["missing_evidence"] == ["runtime_evidence_unverified"]
    assert report["object_pack"]["route_trace"]["stop_reason"] == "missing_evidence_gap_returned"


def test_neuron_knowledge_object_query_infers_code_change_impact_route(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--query",
            "이 파일 바꾸면 어떤 테스트/런타임 영향 있어?",
            "--current-file",
            "worker/lib/agent_knowledge/llm_brain_core/objects/runtime_readiness.py",
            "--consumer",
            "codex",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    pack = report["object_pack"]
    object_types = {obj["object_type"] for obj in pack["objects"]}
    assert rc == 0
    assert report["route"] == "code_change_impact"
    assert pack["route"] == "code_change_impact"
    assert {"RepoFile", "VerificationCommand", "RuntimeSurface"} <= object_types
    assert "live_runtime_impact_unverified" in pack["gaps"]
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert pack["route_trace"]["route"] == "code_change_impact"
    assert pack["route_trace"]["selected_source_lanes"] == ["candidate", "reference_only"]
    assert pack["route_trace"]["missing_evidence"] == [
        "live_runtime_impact_unverified",
        "source_freshness_unverified",
    ]


def test_neuron_knowledge_object_query_infers_html_visualization_preference_route(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--query",
            "내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.",
            "--consumer",
            "codex",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    pack = report["object_pack"]
    assert rc == 0
    assert report["route"] == "html_visualization_preference"
    assert pack["route"] == "html_visualization_preference"
    assert "accepted_html_preference_missing" in pack["gaps"]
    assert "visualization_preference_missing" in pack["gaps"]
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert pack["route_trace"]["route"] == "html_visualization_preference"
    assert pack["route_trace"]["stop_reason"] == "missing_evidence_gap_returned"
    assert pack["route_trace"]["missing_evidence"] == [
        "accepted_html_preference_missing",
        "visualization_preference_missing",
    ]


def test_neuron_knowledge_object_query_does_not_infer_html_route_for_generic_artifact_review(capsys):
    rc = main(
        [
            "object-query",
            "--repository",
            "pureliture/neurons",
            "--branch",
            "main",
            "--query",
            "이 산출물을 평가해줘.",
            "--consumer",
            "codex",
            "--response-mode",
            "compact",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["route"] == "authority_archive_separation"
    assert report["object_pack"]["route"] == "authority_archive_separation"
    assert report["object_pack"]["route_trace"]["route"] == "authority_archive_separation"
    assert "accepted_html_preference_missing" not in report["object_pack"]["gaps"]


@pytest.mark.parametrize(("route", "query", "current_files"), REQUIRED_OBJECT_QUERY_ROUTE_CASES)
def test_neuron_knowledge_object_query_required_routes_never_fallback(capsys, route, query, current_files):
    argv = [
        "object-query",
        "--repository",
        "pureliture/neurons",
        "--branch",
        "main",
        "--query",
        query,
        "--consumer",
        "codex",
        "--response-mode",
        "compact",
    ]
    for current_file in current_files:
        argv.extend(["--current-file", current_file])

    rc = main(argv)

    report = json.loads(capsys.readouterr().out)
    pack = report["object_pack"]
    assert rc == 0
    assert report["schema_version"] == "brain_objects_query.v1"
    assert report["route"] == route
    assert pack["schema_version"] == "object_pack.v1"
    assert pack["route"] == route
    assert pack["route_trace"]["schema_version"] == "object_query_route_trace.v1"
    assert pack["route_trace"]["route"] == route
    assert pack["route_trace"]["route_source"] == "inferred"
    assert "object_pack_route_not_implemented" not in pack["gaps"]


@pytest.mark.parametrize(("route", "query", "current_files"), REQUIRED_OBJECT_QUERY_ROUTE_CASES)
def test_neuron_knowledge_object_query_explicit_required_routes_never_fallback(
    capsys, route, query, current_files
):
    argv = [
        "object-query",
        "--repository",
        "pureliture/neurons",
        "--branch",
        "main",
        "--route",
        route,
        "--query",
        query,
        "--consumer",
        "codex",
        "--response-mode",
        "compact",
    ]
    for current_file in current_files:
        argv.extend(["--current-file", current_file])

    rc = main(argv)

    report = json.loads(capsys.readouterr().out)
    pack = report["object_pack"]
    assert rc == 0
    assert report["schema_version"] == "brain_objects_query.v1"
    assert report["route"] == route
    assert pack["schema_version"] == "object_pack.v1"
    assert pack["route"] == route
    assert pack["route_trace"]["schema_version"] == "object_query_route_trace.v1"
    assert pack["route_trace"]["route"] == route
    assert pack["route_trace"]["route_source"] == "explicit"
    assert "object_pack_route_not_implemented" not in pack["gaps"]


def test_neuron_knowledge_delegates_memory_regeneration_help(capsys):
    assert main(["memory-regeneration", "--help"]) == 0
    assert "usage: memory-regeneration" in capsys.readouterr().out


def test_neuron_knowledge_delegates_session_private_sync_help(capsys):
    assert main(["session-memory-private-sync", "--help"]) == 0
    assert "usage: session-memory-private-sync" in capsys.readouterr().out


def test_neuron_knowledge_corpus_ingest_production_target_requires_bounded_approval(capsys):
    rc = main(["corpus-ingest", "--project", "neurons", "--target", "production"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "reference_corpus_ingest.v1"
    assert report["status"] == "denied"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert report["authority_write_performed"] is False
    assert report["reason"] == "production_corpus_ingest_requires_bounded_approval"
    assert set(report["missing"]) == {"approved", "approval_ref_sha256", "manifest_file"}


def test_neuron_knowledge_corpus_ingest_production_denied_even_with_configured_ledger_env(
    tmp_path,
    capsys,
    monkeypatch,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")
    monkeypatch.setenv("NEURON_REFERENCE_CORPUS_LEDGER", str(ledger))

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "production",
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "denied"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_corpus_ingest_production_approved_writes_evidence_and_readiness(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")
    Ledger(ledger)

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "production",
            "--ledger",
            str(ledger),
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
            "--approved",
            "--approval-ref",
            "sha256:" + "e" * 64,
            "--expect-source-count",
            "2",
            "--expect-source-url-count",
            "1",
            "--expect-manual-text-without-url-count",
            "1",
            "--expect-source-type-count",
            "WEB_PAGE=1",
            "--expect-source-type-count",
            "TEXT=1",
        ]
    )

    evidence = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert evidence["schema_version"] == "reference_corpus_production_ingest_evidence.v1"
    assert evidence["production_mutation_performed"] is True
    assert evidence["authority_write_performed"] is False
    assert evidence["protected_values_returned"] is False
    assert evidence["approval"]["approval_ref_hash"] == "sha256:" + "e" * 64
    assert evidence["approval"]["scope"] == "single_project_single_corpus"
    assert evidence["corpus"]["source_count"] == 2
    assert evidence["corpus"]["storage_mode"] == "managed_snapshot"
    assert evidence["corpus"]["raw_body_policy"] == "no_raw_return_by_default"
    assert evidence["ingest"]["target"] == "production_corpus_store"
    assert evidence["ingest"]["corpus_write_performed"] is True
    assert evidence["ingest"]["authority_write_performed"] is False
    assert evidence["read_after_write"]["status"] == "validated"
    assert evidence["postcheck"]["raw_body_returned"] is False
    assert evidence["postcheck"]["raw_external_ids_returned"] is False
    assert evidence["evidence_provenance"]["network_used"] is False
    assert evidence["evidence_provenance"]["mutation_scope"] == "bounded_production_corpus_ingest"

    evidence_file = tmp_path / "production-corpus-ingest-evidence.json"
    evidence_file.write_text(json.dumps(evidence), encoding="utf-8")
    rc = main(
        [
            "corpus-ingest-readiness",
            "--evidence-file",
            str(evidence_file),
            "--expected-manifest-hash",
            evidence["corpus"]["manifest_hash"],
            "--expected-corpus-id",
            evidence["corpus"]["corpus_id"],
            "--expected-source-count",
            "2",
        ]
    )
    readiness = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert readiness["status"] == "PASS"
    assert readiness["production_mutation_performed"] is True

    assert main(["corpus-status", "--project", "neurons", "--ledger", str(ledger)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_count"] == 2
    assert status["corpus_count"] == 1
    assert status["storage_modes"] == {"managed_snapshot": 2}
    assert status["production_ingest_gate"] == "approved_bounded_cli_gate_required"


def test_neuron_knowledge_corpus_ingest_production_count_gate_fails_before_write(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")
    Ledger(ledger)

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "production",
            "--ledger",
            str(ledger),
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
            "--approved",
            "--approval-ref",
            "sha256:" + "f" * 64,
            "--expect-source-count",
            "3",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "FAIL"
    assert report["reason"] == "production_corpus_ingest_count_gate_failed"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert report["authority_write_performed"] is False

    assert main(["corpus-status", "--project", "neurons", "--ledger", str(ledger)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_count"] == 0
    assert status["gaps"] == ["reference_corpus_store_empty"]


def test_neuron_knowledge_corpus_ingest_production_approved_does_not_create_missing_file(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "missing.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "production",
            "--ledger",
            str(ledger),
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
            "--approved",
            "--approval-ref",
            "sha256:" + "a" * 64,
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "reference_corpus_ingest.v1"
    assert report["status"] == "FAIL"
    assert report["reason"] == "production_ledger_not_existing_or_server_backed"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_corpus_ingest_readiness_accepts_bounded_evidence_file(tmp_path, capsys):
    evidence_file = tmp_path / "production-corpus-ingest-evidence.json"
    evidence_file.write_text(
        json.dumps(
            {
                "schema_version": "reference_corpus_production_ingest_evidence.v1",
                "approval": {
                    "approved": True,
                    "approval_ref_hash": "sha256:" + "b" * 64,
                    "scope": "single_project_single_corpus",
                    "project": "neurons",
                    "max_corpora": 1,
                    "no_raw_body_returned": True,
                },
                "corpus": {
                    "corpus_id": "rc:palantir-ontology",
                    "manifest_hash": "sha256:" + "a" * 64,
                    "source_count": 65,
                    "storage_mode": "managed_snapshot",
                    "authority_lane": "reference_only",
                    "raw_body_policy": "no_raw_return_by_default",
                },
                "ingest": {
                    "target": "production_corpus_store",
                    "ledger_scope": "production",
                    "corpus_write_performed": True,
                    "production_mutation_performed": True,
                    "authority_write_performed": False,
                },
                "read_after_write": {
                    "status": "validated",
                    "corpus_id": "rc:palantir-ontology",
                    "manifest_hash": "sha256:" + "a" * 64,
                    "source_count": 65,
                },
                "rollback_or_deletion": {
                    "status": "planned",
                    "path": ["delete_snapshot_keep_metadata"],
                },
                "postcheck": {
                    "status": "validated",
                    "raw_body_returned": False,
                    "secret_returned": False,
                    "host_topology_returned": False,
                    "raw_external_ids_returned": False,
                },
                "evidence_provenance": {
                    "schema_version": "reference_corpus_production_ingest_evidence_provenance.v1",
                    "collection_mode": "post_deploy_bounded_production_ingest",
                    "network_used": True,
                    "mutation_scope": "bounded_production_corpus_ingest",
                    "raw_private_evidence_returned": False,
                    "secret_returned": False,
                    "host_topology_returned": False,
                    "raw_external_ids_returned": False,
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "corpus-ingest-readiness",
            "--evidence-file",
            str(evidence_file),
            "--expected-manifest-hash",
            "sha256:" + "a" * 64,
            "--expected-source-count",
            "65",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "reference_corpus_production_ingest_readiness.v1"
    assert report["status"] == "PASS"
    assert report["production_mutation_performed"] is True
    assert report["evidence_collection_network_used"] is True


def test_neuron_knowledge_object_authority_schema_ensure_requires_production_approval(tmp_path, capsys):
    ledger = tmp_path / "ledger.sqlite"

    rc = main(
        [
            "object-authority-schema-ensure",
            "--target",
            "production",
            "--ledger",
            str(ledger),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "object_authority_schema_ensure.v1"
    assert report["status"] == "denied"
    assert report["reason"] == "production_object_authority_schema_ensure_requires_approval"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_object_authority_schema_ensure_repairs_missing_overlay_tables(tmp_path, capsys):
    ledger = tmp_path / "ledger.sqlite"
    Ledger(ledger)
    with sqlite3.connect(ledger) as connection:
        connection.execute("DROP TABLE object_authority_states")
        connection.execute("DROP TABLE object_authority_decisions")
        connection.execute("DROP TABLE object_review_proposals")
        for version in (
            "agent_knowledge_object_review_proposals.v1",
            "agent_knowledge_object_authority_decisions.v1",
            "agent_knowledge_object_authority_states.v1",
        ):
            connection.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))

    rc = main(
        [
            "object-authority-schema-ensure",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "object_authority_schema_ensure.v1"
    assert report["status"] == "ensured"
    assert report["target"] == "local_test"
    assert report["mutation_performed"] is True
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    assert report["server_backed_ledger"] is False
    assert set(report["tables"]) == {
        "object_review_proposals",
        "object_authority_decisions",
        "object_authority_states",
    }
    with sqlite3.connect(ledger) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'object_%'"
            ).fetchall()
        }
        migration_versions = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT version FROM schema_migrations
                WHERE version IN (
                    'agent_knowledge_object_review_proposals.v1',
                    'agent_knowledge_object_authority_decisions.v1',
                    'agent_knowledge_object_authority_states.v1'
                )
                """
            ).fetchall()
        }
    assert {
        "object_review_proposals",
        "object_authority_decisions",
        "object_authority_states",
    }.issubset(table_names)
    assert {
        "agent_knowledge_object_review_proposals.v1",
        "agent_knowledge_object_authority_decisions.v1",
        "agent_knowledge_object_authority_states.v1",
    }.issubset(migration_versions)


def test_neuron_knowledge_object_authority_schema_ensure_accepts_bounded_production_approval(tmp_path, capsys):
    ledger = tmp_path / "ledger.sqlite"
    Ledger(ledger)
    with sqlite3.connect(ledger) as connection:
        connection.execute("DROP TABLE object_authority_states")

    rc = main(
        [
            "object-authority-schema-ensure",
            "--target",
            "production",
            "--ledger",
            str(ledger),
            "--approved",
            "--approval-ref",
            "sha256:" + "c" * 64,
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "object_authority_schema_ensure.v1"
    assert report["status"] == "ensured"
    assert report["target"] == "production"
    assert report["mutation_performed"] is True
    assert report["production_mutation_performed"] is True
    assert report["approval_ref_hash_present"] is True
    assert report["network_used"] is False
    assert report["server_backed_ledger"] is False
    assert report["protected_values_returned"] is False
    with sqlite3.connect(ledger) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='object_authority_states'"
        ).fetchone()


def test_neuron_knowledge_object_authority_schema_ensure_does_not_create_missing_production_file(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("NEURON_LEDGER_PG_DSN", raising=False)
    ledger = tmp_path / "missing.sqlite"

    rc = main(
        [
            "object-authority-schema-ensure",
            "--target",
            "production",
            "--ledger",
            str(ledger),
            "--approved",
            "--approval-ref",
            "sha256:" + "d" * 64,
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "object_authority_schema_ensure.v1"
    assert report["status"] == "FAIL"
    assert report["reason"] == "production_ledger_not_existing_or_server_backed"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_corpus_status_reports_storage_policy(capsys):
    rc = main(["corpus-status", "--project", "neurons"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "brain_corpus_status.v1"
    assert report["raw_body_policy"]["return_capability"] == "denied_without_explicit_approval"
    assert report["raw_body_policy"]["retention_class"] == "user_managed_reference"
    assert report["raw_body_policy"]["redaction_profile"] == "public_safe_summary"
    assert report["raw_body_policy"]["deletion_policy"] == "delete_snapshot_keep_metadata"
    assert report["raw_body_policy"]["license_source_rights"] == "operator_attested"
    assert report["source_rights_policy"] == "operator_attested_reference_use"
    assert "managed_snapshot" in report["supported_storage_modes"]
    assert "reference_corpus_store_empty" in report["gaps"]


def test_neuron_knowledge_corpus_ingest_plan_loads_full_count_manifest(tmp_path, capsys):
    manifest = tmp_path / "palantir-manifest.json"
    manifest.write_text(json.dumps(_palantir_full_count_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest-plan",
            "--project",
            "neurons",
            "--storage-mode",
            "external_object_store",
            "--manifest-file",
            str(manifest),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["corpus"]["name"] == "palantir-ontology"
    assert report["corpus"]["source_count"] == 65
    assert report["source_url_count"] == 39
    assert report["manual_text_without_url_count"] == 26
    assert report["source_type_counts"] == {"PDF": 6, "TEXT": 26, "WEB_PAGE": 33}
    assert report["manifest_hash"].startswith("sha256:")
    assert report["writes_planned"] is False


def test_neuron_knowledge_corpus_ingest_plan_expected_count_gate_passes(tmp_path, capsys):
    manifest = tmp_path / "palantir-manifest.json"
    manifest.write_text(json.dumps(_palantir_full_count_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest-plan",
            "--project",
            "neurons",
            "--storage-mode",
            "external_object_store",
            "--manifest-file",
            str(manifest),
            "--expect-source-count",
            "65",
            "--expect-source-url-count",
            "39",
            "--expect-manual-text-without-url-count",
            "26",
            "--expect-source-type-count",
            "PDF=6",
            "--expect-source-type-count",
            "WEB_PAGE=33",
            "--expect-source-type-count",
            "TEXT=26",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["count_gate_status"] == "pass"
    assert report["count_gate_gaps"] == []
    assert report["writes_planned"] is False


def test_neuron_knowledge_corpus_ingest_plan_expected_count_gate_fails_closed(tmp_path, capsys):
    manifest = tmp_path / "palantir-manifest.json"
    manifest.write_text(json.dumps(_palantir_full_count_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest-plan",
            "--project",
            "neurons",
            "--storage-mode",
            "external_object_store",
            "--manifest-file",
            str(manifest),
            "--expect-source-count",
            "66",
            "--expect-source-url-count",
            "39",
            "--expect-manual-text-without-url-count",
            "26",
            "--expect-source-type-count",
            "PDF=6",
            "--expect-source-type-count",
            "WEB_PAGE=33",
            "--expect-source-type-count",
            "TEXT=26",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["count_gate_status"] == "fail"
    assert report["count_gate_gaps"] == [{"field": "source_count", "expected": 66, "actual": 65}]
    assert report["writes_planned"] is False


def test_neuron_knowledge_corpus_ingest_local_test_is_preview_until_store_configured(capsys):
    rc = main(["corpus-ingest", "--project", "neurons", "--target", "local_test"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "reference_corpus_ingest.v1"
    assert report["status"] == "planned"
    assert report["mutation_performed"] is False
    assert report["writes_planned"] is True
    assert "reference_corpus_store_not_configured" in report["gaps"]


def test_neuron_knowledge_corpus_ingest_local_test_writes_configured_store(tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
        ]
    )
    ingest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert ingest["schema_version"] == "reference_corpus_store_write.v1"
    assert ingest["status"] == "stored"
    assert ingest["source_count"] == 2
    assert ingest["mutation_performed"] is True
    assert ingest["production_mutation_performed"] is False

    assert main(["corpus-status", "--project", "neurons", "--ledger", str(ledger), "--corpus-id", ingest["corpus_id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_count"] == 2
    assert status["storage_modes"] == {"managed_snapshot": 2}
    assert status["reference_object_count"] == 2
    assert status["document_source_count"] == 2
    assert status["version_count"] == 2
    assert status["snapshot_count"] == 2
    assert status["chunk_count"] == 2
    assert status["freshness_check_count"] == 2
    assert status["extraction_run_count"] == 1
    assert status["first_class_store_counts"]["document_sources"] == 2
    assert status["first_class_store_counts"]["document_snapshots"] == 2
    assert status["first_class_store_counts"]["document_chunks"] == 2
    assert status["first_class_store_counts"]["freshness_checks"] == 2
    assert status["first_class_store_counts"]["extraction_runs"] == 1
    assert status["document_sources"][0]["schema_version"] == "document_source.v1"
    assert status["document_versions"][0]["schema_version"] == "document_version.v1"
    assert status["document_snapshots"][0]["schema_version"] == "document_snapshot.v1"
    assert status["document_chunks"][0]["schema_version"] == "document_chunk.v1"
    assert status["freshness_checks"][0]["schema_version"] == "freshness_check.v1"
    assert status["extraction_runs"][0]["status"] == "completed"
    assert status["freshness_gaps"][0]["source_url_status"] == "missing_manual_text"
    assert status["gaps"] == []


def test_neuron_knowledge_corpus_ingest_manifest_corpus_name_override(tmp_path, capsys):
    manifest_payload = _reference_manifest()
    manifest_payload.pop("corpus_name")
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
            "--manifest-file",
            str(manifest),
            "--corpus-name",
            "palantir-ontology",
        ]
    )

    ingest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert ingest["schema_version"] == "reference_corpus_store_write.v1"
    assert ingest["corpus_id"] == "rc:93206b931d457c9a"


def test_neuron_knowledge_corpus_ingest_and_status_use_configured_local_test_ledger_env(
    tmp_path,
    capsys,
    monkeypatch,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")
    monkeypatch.setenv("NEURON_REFERENCE_CORPUS_LEDGER", str(ledger))

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
        ]
    )
    ingest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert ingest["schema_version"] == "reference_corpus_store_write.v1"
    assert ingest["status"] == "stored"
    assert ingest["production_mutation_performed"] is False

    assert main(["corpus-status", "--project", "neurons", "--corpus-id", ingest["corpus_id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_count"] == 2
    assert status["storage_modes"] == {"managed_snapshot": 2}
    assert status["gaps"] == []


def test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    assert (
        main(
            [
                "corpus-ingest",
                "--project",
                "neurons",
                "--target",
                "local_test",
                "--ledger",
                str(ledger),
                "--manifest-file",
                str(manifest),
                "--storage-mode",
                "managed_snapshot",
            ]
        )
        == 0
    )
    ingest = json.loads(capsys.readouterr().out)

    rc = main(
        [
            "source-to-candidate-graph",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
            "--corpus-id",
            ingest["corpus_id"],
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "source_to_candidate_graph_activation.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["production_mutation_performed"] is False
    assert report["ledger_mutation_performed"] is False
    assert report["input_store"]["corpus_id"] == ingest["corpus_id"]
    assert report["candidate_graph_review_pack"]["route"] == "candidate_graph_review"
    assert report["candidate_graph_review_pack"]["production_mutation_performed"] is False
    assert report["candidate_graph_review_pack"]["authority_write_performed"] is False
    assert report["candidate_graph_review_pack"]["minimal_edit_surface"]["supported"] is True
    assert report["candidate_graph_review_pack"]["lanes"]["candidate"]
    assert report["candidate_graph_review_pack"]["lanes"]["accepted_current"] == []
    assert report["candidate_graph_review_pack"]["raw_body_return_capability"] == "denied"
    assert report["quality_gate"]["source_to_candidate_graph"] == "PASS"
    assert "live_projection_join_unproven" in report["gaps"]
    assert all(
        item["raw_return_capability"] == "denied"
        for item in report["candidate_graph_review_pack"]["evidence"]
    )


def test_neuron_knowledge_source_to_candidate_graph_denies_production_without_mutation(
    tmp_path,
    capsys,
):
    ledger = tmp_path / "ledger.sqlite"

    rc = main(
        [
            "source-to-candidate-graph",
            "--project",
            "neurons",
            "--target",
            "production",
            "--ledger",
            str(ledger),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "object_substrate_cli_denied.v1"
    assert report["status"] == "denied"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_source_to_candidate_graph_does_not_create_missing_local_store(
    tmp_path,
    capsys,
):
    ledger = tmp_path / "missing-ledger.sqlite"

    rc = main(
        [
            "source-to-candidate-graph",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "source_to_candidate_graph_activation.v1"
    assert report["status"] == "FAIL"
    assert report["production_mutation_performed"] is False
    assert report["ledger_mutation_performed"] is False
    assert "reference_corpus_store_missing" in report["gaps"]
    assert ledger.exists() is False


def test_neuron_knowledge_candidate_review_and_approval_board_cli_chain_local_test(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    assert (
        main(
            [
                "corpus-ingest",
                "--project",
                "neurons",
                "--target",
                "local_test",
                "--ledger",
                str(ledger),
                "--manifest-file",
                str(manifest),
                "--storage-mode",
                "managed_snapshot",
            ]
        )
        == 0
    )
    ingest = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "source-to-candidate-graph",
                "--project",
                "neurons",
                "--target",
                "local_test",
                "--ledger",
                str(ledger),
                "--corpus-id",
                ingest["corpus_id"],
            ]
        )
        == 0
    )
    graph = json.loads(capsys.readouterr().out)
    pack_file = tmp_path / "candidate-pack.json"
    candidate_pack = graph["candidate_graph_review_pack"]
    pack_file.write_text(json.dumps(candidate_pack), encoding="utf-8")
    candidate_id = candidate_pack["lanes"]["candidate"][0]["object_id"]
    original_edge_id = candidate_pack["edges"][0]["edge_id"]
    original_evidence_id = candidate_pack["evidence"][0]["evidence_id"]
    added_evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/review-evidence.md"},
        content_hash="sha256:" + "8" * 64,
        summary="Reviewer attached CLI transport evidence.",
    )
    added_edge = KnowledgeEdge.from_parts(
        edge_type="review_supports",
        from_object_id=candidate_id,
        to_object_id=candidate_id,
        evidence_refs=[added_evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
    )
    edits_file = tmp_path / "candidate-edits.json"
    edits_file.write_text(
        json.dumps(
            [
                {
                    "action": "update_object",
                    "object_id": candidate_id,
                    "fields": {
                        "summary": "Reviewer clarified this candidate before local approval.",
                        "recommended_action": "promote",
                    },
                },
                {
                    "action": "add_evidence",
                    "attach_to_object_id": candidate_id,
                    "fields": {
                        "evidence_type": "source_hash",
                        "locator": {"kind": "relative_repo_path", "value": "docs/review-evidence.md"},
                        "content_hash": "sha256:" + "8" * 64,
                        "summary": "Reviewer attached CLI transport evidence.",
                    },
                },
                {
                    "action": "add_edge",
                    "fields": {
                        "edge_type": "review_supports",
                        "from_object_id": candidate_id,
                        "to_object_id": candidate_id,
                        "evidence_refs": [added_evidence.evidence_id],
                    },
                },
                {"action": "remove_edge", "edge_id": original_edge_id},
                {"action": "remove_evidence", "evidence_id": original_evidence_id},
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "candidate-review-edit",
                "--target",
                "production",
                "--mutation-mode",
                "no_mutation",
                "--pack-file",
                str(pack_file),
                "--edits-file",
                str(edits_file),
                "--reviewer-id",
                "reviewer-local",
            ]
        )
        == 0
    )
    edit_result = json.loads(capsys.readouterr().out)
    assert edit_result["schema_version"] == "candidate_review_edit_result.v1"
    assert edit_result["permission"] == "allowed"
    assert edit_result["target_scope"] == "production"
    assert edit_result["mutation_mode"] == "no_mutation"
    assert edit_result["candidate_state_changed"] is True
    assert edit_result["authority_write_performed"] is False
    assert edit_result["production_mutation_performed"] is False
    assert edit_result["updated_pack"]["lanes"]["accepted_current"] == []
    assert edit_result["rejected_edits"] == []
    assert [item["action"] for item in edit_result["accepted_edits"]] == [
        "update_object",
        "add_evidence",
        "add_edge",
        "remove_edge",
        "remove_evidence",
    ]
    assert added_evidence.evidence_id in {
        item["evidence_id"] for item in edit_result["updated_pack"]["evidence"]
    }
    assert original_evidence_id not in {
        item["evidence_id"] for item in edit_result["updated_pack"]["evidence"]
    }
    assert added_edge.edge_id in {item["edge_id"] for item in edit_result["updated_pack"]["edges"]}
    assert original_edge_id not in {item["edge_id"] for item in edit_result["updated_pack"]["edges"]}
    edited_pack_file = tmp_path / "edited-candidate-pack.json"
    edited_pack_file.write_text(json.dumps(edit_result["updated_pack"]), encoding="utf-8")
    decisions_file = tmp_path / "approval-decisions.json"
    decisions_file.write_text(
        json.dumps(
            [
                {
                    "action": "promote",
                    "object_id": candidate_id,
                    "reason": "Local test approval board preview.",
                    "approved_by": "reviewer-local",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "approval-board-decide",
                "--target",
                "local_test",
                "--pack-file",
                str(edited_pack_file),
                "--decisions-file",
                str(decisions_file),
                "--reviewer-id",
                "reviewer-local",
            ]
        )
        == 0
    )
    decision_result = json.loads(capsys.readouterr().out)
    assert decision_result["schema_version"] == "approval_board_decision_result.v1"
    assert decision_result["permission"] == "allowed"
    assert decision_result["authority_write_scope"] == "local_test"
    assert decision_result["production_mutation_performed"] is False
    assert decision_result["updated_pack"]["lanes"]["accepted_current"][0]["object_id"] == candidate_id


def test_neuron_knowledge_approval_board_cli_denies_production_without_mutation(
    tmp_path,
    capsys,
):
    pack_file = tmp_path / "candidate-pack.json"
    decisions_file = tmp_path / "approval-decisions.json"
    pack_file.write_text(
        json.dumps(
            {
                "schema_version": "object_pack.v1",
                "route": "candidate_graph_review",
                "candidate_graph_hash": "sha256:" + "5" * 64,
                "objects": [],
                "edges": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    decisions_file.write_text(
        json.dumps([{"action": "promote", "object_id": "ko:ReferenceDocument:test"}]),
        encoding="utf-8",
    )

    rc = main(
        [
            "approval-board-decide",
            "--target",
            "production",
            "--pack-file",
            str(pack_file),
            "--decisions-file",
            str(decisions_file),
            "--reviewer-id",
            "reviewer-local",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert result["schema_version"] == "approval_board_decision_result.v1"
    assert result["permission"] == "denied"
    assert result["production_mutation_performed"] is False
    assert result["authority_write_performed"] is False
    assert result["promotion_plan"]["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_baseline(capsys):
    assert main(["golden-query-eval", "--baseline"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "baseline_red"
    assert len(report["queries"]) >= 10


def test_neuron_knowledge_golden_query_eval_phase_coverage(capsys):
    assert main(["golden-query-eval", "--phase-coverage"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "knowledge_object_phase_golden_query_coverage.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["release_quality_gate"] == "green"


def test_neuron_knowledge_golden_query_eval_source_to_authority_gate(capsys):
    assert main(["golden-query-eval", "--source-to-authority-gate"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "source_to_authority_quality_gate_report.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["release_quality_gate"] == "green"
    assert report["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_activation_progress(capsys):
    assert main(["golden-query-eval", "--activation-progress"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "lbrain_product_activation_progress.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["scope_phases"] == ["P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
    assert report["next_phase"] == "P6"
    assert report["release_quality_gate"] == "green"
    assert report["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_activation_progress_accepts_live_evidence_file(
    tmp_path, capsys
):
    evidence_file = tmp_path / "p6-live-evidence.json"
    evidence_file.write_text(json.dumps(_valid_p6_runtime_evidence(live=True)), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--live-evidence-file",
                str(evidence_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert checks["P6"]["result"] == "PASS"
    assert "p6_live_multi_device_rollup_unproven" not in checks["P6"]["gaps"]
    assert p6["rollup_claim_status"] == "validated"
    assert p6["evidence_is_live"] is True
    assert p6["production_mutation_performed"] is False
    assert report["production_ready"] is False
    assert report["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_activation_progress_keeps_local_replay_gap(
    tmp_path, capsys
):
    evidence_file = tmp_path / "p6-local-replay-evidence.json"
    evidence_file.write_text(json.dumps(_valid_p6_runtime_evidence(live=False)), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--live-evidence-file",
                str(evidence_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert p6["rollup_claim_status"] == "validated"
    assert p6["evidence_is_live"] is False
    assert checks["P6"]["result"] == "PASS_WITH_GAPS"
    assert "p6_session_project_rollup_evidence_not_live" in checks["P6"]["gaps"]


def test_neuron_knowledge_golden_query_eval_activation_progress_accepts_post_deploy_capture_file(
    tmp_path, capsys
):
    capture_file = tmp_path / "p6-post-deploy-capture.json"
    capture_file.write_text(json.dumps(_valid_p6_runtime_evidence(live=True)), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert checks["P6"]["result"] == "PASS"
    assert p6["evidence_provenance_status"] == "validated"
    assert p6["evidence_is_live"] is True
    assert report["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_activation_progress_closes_p7_post_deploy_gap(
    tmp_path, capsys
):
    capture_file = tmp_path / "p6-p7-post-deploy-capture.json"
    capture_file.write_text(json.dumps(_valid_p6_p7_runtime_evidence(live=True)), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
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
    assert p7["evidence_provenance_status"] == "validated"
    assert p7["evidence_is_live"] is True
    assert p7["accepted_preference_count"] == 1
    assert p7["proposal_preference_count"] == 1
    assert p7["html_route_status"] == "validated"
    assert p7["artifact_review_check_status"] == "pass"
    assert p7["production_mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert report["production_ready"] is False


def test_neuron_knowledge_golden_query_eval_activation_progress_closes_p3_post_deploy_gap(
    tmp_path, capsys
):
    capture_file = tmp_path / "p3-post-deploy-capture.json"
    capture_file.write_text(
        json.dumps(_valid_p3_post_deploy_capture(live=True)),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p3 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P3")
    phase_progress = {item["phase"]: item for item in report["phase_progress"]}

    assert checks["P3"]["result"] == "PASS"
    assert p3["projection_join_claim_status"] == "validated"
    assert p3["projection_join_edge_count"] == 2
    assert p3["evidence_is_live"] is True
    assert p3["network_used"] is True
    assert p3["evidence_collection_network_used"] is True
    assert "p3_live_graph_qdrant_projection_join_unproven" not in checks["P3"]["gaps"]
    assert "live_graph_qdrant_projection_join_unproven" not in phase_progress["P3"]["gaps"]


def test_neuron_knowledge_golden_query_eval_activation_progress_treats_partial_post_deploy_capture_as_gap(
    tmp_path, capsys
):
    capture = _valid_p6_runtime_evidence(live=True)
    capture.pop("session_project_rollup_runtime")
    capture_file = tmp_path / "partial-post-deploy-capture.json"
    capture_file.write_text(json.dumps(capture), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    p6 = next(item for item in report["product_evidence_summary"] if item["phase"] == "P6")

    assert report["status"] == "PASS_WITH_GAPS"
    assert checks["P6"]["result"] == "PASS_WITH_GAPS"
    assert checks["P6"]["failures"] == []
    assert "p6_live_session_project_rollup_unverified" in checks["P6"]["gaps"]
    assert "p6_live_multi_device_rollup_unproven" in checks["P6"]["gaps"]
    assert "p6_session_rollup_incomplete" not in checks["P6"]["failures"]
    assert "p6_handoff_pack_missing" not in checks["P6"]["failures"]
    assert p6["rollup_claim_status"] == "not_validated"
    assert p6["evidence_is_live"] is True
    assert p6["evidence_count"] == 0
    assert report["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_activation_progress_fails_empty_p6_post_deploy_capture(
    tmp_path, capsys
):
    capture = _valid_p6_runtime_evidence(live=True)
    capture["session_project_rollup_runtime"] = {}
    capture_file = tmp_path / "empty-p6-post-deploy-capture.json"
    capture_file.write_text(json.dumps(capture), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}

    assert report["status"] == "FAIL"
    assert checks["P6"]["result"] == "FAIL"
    assert "p6_session_rollup_incomplete" in checks["P6"]["failures"]
    assert "p6_handoff_pack_missing" in checks["P6"]["failures"]


def test_neuron_knowledge_golden_query_eval_activation_progress_fails_malformed_p6_post_deploy_capture(
    tmp_path, capsys
):
    capture = _valid_p6_runtime_evidence(live=True)
    rollup = capture["session_project_rollup_runtime"]
    rollup["rollup_preview"]["scope"] = "same_device"
    rollup["handoff_pack"]["visible_session_count"] = 1
    capture_file = tmp_path / "malformed-p6-post-deploy-capture.json"
    capture_file.write_text(json.dumps(capture), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}

    assert report["status"] == "FAIL"
    assert checks["P6"]["result"] == "FAIL"
    assert "p6_session_rollup_runtime_failed" in checks["P6"]["failures"]
    assert "p6_runtime_readiness_failed" in checks["P6"]["failures"]
    assert "p6_session_project_rollup_scope_not_all_devices" in checks["P6"]["gaps"]
    assert (
        "p6_session_project_handoff_visible_session_count_mismatch"
        in checks["P6"]["gaps"]
    )
    assert "p6_handoff_pack_missing" not in checks["P6"]["failures"]


def test_neuron_knowledge_golden_query_eval_activation_progress_fails_mutating_post_deploy_capture(
    tmp_path, capsys
):
    capture = _valid_p6_runtime_evidence(live=True)
    capture["production_mutation_performed"] = True
    capture_file = tmp_path / "mutating-post-deploy-capture.json"
    capture_file.write_text(json.dumps(capture), encoding="utf-8")

    assert (
        main(
            [
                "golden-query-eval",
                "--activation-progress",
                "--post-deploy-capture-file",
                str(capture_file),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}

    assert report["status"] == "FAIL"
    assert report["release_quality_gate"] == "blocked"
    assert report["production_mutation_performed"] is True
    assert checks["P6"]["result"] == "FAIL"
    assert "p6_production_mutation_performed" in checks["P6"]["failures"]


def test_neuron_knowledge_memory_regeneration_live_args_fail_closed(tmp_path, capsys):
    rc = main(
        [
            "memory-regeneration",
            "run",
            "--output",
            "project-memory",
            "--ledger",
            str(tmp_path / "missing-ledger.sqlite3"),
            "--enqueue",
            "--ingress-url",
            "http://127.0.0.1:18080",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_native_memory_execute_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "native-memory-sync",
            "--ledger",
            str(tmp_path / "missing-ledger.sqlite3"),
            "--native-memory-id",
            "mem_test",
            "--execute",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_session_memory_gc_execute_requires_approval(tmp_path, capsys):
    # GC executor는 벤더링됐지만 live --execute는 approval 게이트 뒤다. 유효 approval
    # 없이 --execute하면 네트워크/뮤테이션 전에 fail closed(approval error, rc!=0).
    rc = main(
        [
            "session-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_session",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_transcript_memory_gc_execute_disable_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "transcript-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_transcript",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute-disable",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["hard_delete_performed"] is False


def test_neuron_knowledge_delegates_transcript_backfill_help(capsys):
    assert main(["transcript-backfill", "--help"]) == 0
    assert "usage: transcript-backfill" in capsys.readouterr().out


def test_neuron_knowledge_transcript_volume_gc_execute_requires_approval(tmp_path, capsys):
    rc = main(
        [
            "transcript-volume-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--transcript-dataset-id",
            "ds_transcript",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_transcript_session_gc_execute_requires_approval(tmp_path, capsys):
    rc = main(
        [
            "transcript-session-gc",
            "--transcript-dataset-id",
            "ds_transcript",
            "--session-memory-dataset-id",
            "ds_session_memory",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--backup-dir",
            str(tmp_path / "backup"),
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_couchdb_command_surface_metadata_importable():
    from agent_knowledge.cli import COMMAND_METADATA

    assert isinstance(COMMAND_METADATA, dict)
    assert COMMAND_METADATA


def test_neuron_knowledge_couchdb_command_surface_classification():
    from agent_knowledge.cli import COMMAND_METADATA

    couchdb_build_metadata = COMMAND_METADATA["couchdb-session-memory-build"]
    transcript_migration_metadata = COMMAND_METADATA["transcript-migration"]
    couchdb_migration_flow_metadata = COMMAND_METADATA["couchdb-migration-flow"]
    legacy_session_memory_metadata = COMMAND_METADATA["neuron-session-memory-build"]
    object_authority_schema_metadata = COMMAND_METADATA["object-authority-schema-ensure"]

    assert set(COMMAND_METADATA).issubset(COMMAND_HANDLERS)

    for command in (
        "couchdb-session-memory-build",
        "transcript-migration",
        "couchdb-migration-flow",
        "neuron-session-memory-build",
        "object-authority-schema-ensure",
    ):
        assert command in COMMAND_HANDLERS, f"command '{command}' should exist in COMMAND_HANDLERS"
        assert command in COMMAND_METADATA, f"command '{command}' should exist in COMMAND_METADATA"

    assert couchdb_build_metadata["runtime_category"] == "active_runtime"
    assert couchdb_build_metadata["deletion_candidate"] is False
    assert couchdb_build_metadata["live_mutation_requires_approval"] is True

    assert transcript_migration_metadata["runtime_category"] == "human_gated_migration"
    assert transcript_migration_metadata["deletion_candidate"] is False
    assert transcript_migration_metadata["live_mutation_requires_approval"] is True

    assert object_authority_schema_metadata["runtime_category"] == "human_gated_schema_repair"
    assert object_authority_schema_metadata["deletion_candidate"] is False
    assert object_authority_schema_metadata["live_mutation_requires_approval"] is True

    assert couchdb_migration_flow_metadata["runtime_category"] == "human_gated_migration"
    assert couchdb_migration_flow_metadata["deletion_candidate"] is False

    assert legacy_session_memory_metadata["runtime_category"] == "legacy_compatibility"
    assert legacy_session_memory_metadata["deletion_candidate"] is False
    assert legacy_session_memory_metadata["live_mutation_requires_approval"] is True

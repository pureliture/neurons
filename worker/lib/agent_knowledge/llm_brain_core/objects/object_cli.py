from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

from ...ledger import Ledger

from .._util import ensure_public_safe, require_sha256
from ..context import BrainReadService
from .golden_query_eval import (
    build_baseline_golden_query_report,
    build_product_activation_progress_report,
    build_phase_golden_query_coverage_report,
    build_source_to_authority_quality_gate_report,
)
from .extraction_pipeline import run_source_to_candidate_graph_activation_preview
from .okf_export import build_okf_bundle
from .object_packs import apply_approval_board_decisions, apply_candidate_review_edits, build_documentation_cleanup_pack
from .reference_corpus import (
    build_corpus_ingest_plan,
    build_reference_corpus_production_ingest_evidence,
    build_reference_corpus_production_ingest_readiness_report,
    default_corpus_policy_status,
    reference_corpus_objects_from_manifest,
)
from .runtime_readiness import (
    build_source_to_candidate_runtime_collected_shadow_evidence_packet,
    build_source_to_candidate_runtime_evidence_collection_plan,
    build_source_to_candidate_runtime_evidence_packet_template,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
    build_source_to_candidate_runtime_readiness_report,
    build_source_to_candidate_runtime_shadow_evidence_packet,
    build_source_to_candidate_runtime_shadow_readiness_report,
)

REFERENCE_CORPUS_LEDGER_ENV = "NEURON_REFERENCE_CORPUS_LEDGER"


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _load_manifest(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("manifest file must contain a mapping")
    return loaded


def _load_json_mapping(path: str, *, label: str) -> dict[str, Any]:
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} file must contain a JSON object")
    return loaded


def _load_json_list(path: str, *, label: str) -> list[dict[str, Any]]:
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise ValueError(f"{label} file must contain a JSON array")
    return [dict(item) for item in loaded if isinstance(item, dict)]


def _parse_expected_source_type_counts(values: list[str], parser: argparse.ArgumentParser) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            parser.error("--expect-source-type-count must use TYPE=N")
        source_type, count_text = value.split("=", 1)
        source_type = source_type.strip()
        if not source_type:
            parser.error("--expect-source-type-count source type must be non-empty")
        try:
            count = int(count_text)
        except ValueError:
            parser.error("--expect-source-type-count count must be an integer")
        if count < 0:
            parser.error("--expect-source-type-count count must be non-negative")
        counts[source_type] = count
    return counts


def _non_negative_int(value: str) -> int:
    count = int(value)
    if count < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return count


def _configured_reference_corpus_ledger(arg_value: str) -> str:
    return str(arg_value or os.environ.get(REFERENCE_CORPUS_LEDGER_ENV, "")).strip()


def _ledger_is_server_backed(ledger: Ledger) -> bool:
    return not bool(getattr(ledger._db_adapter, "is_file_backed", True))


def object_query_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge object-query")
    parser.add_argument("--query", required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--current-file", action="append", default=[])
    parser.add_argument("--object-type", action="append", default=[])
    parser.add_argument("--route", default="")
    parser.add_argument("--limit", type=_non_negative_int, default=20)
    parser.add_argument("--response-mode", choices=["full", "compact", "degraded"], default="full")
    parser.add_argument("--consumer", choices=["unspecified", "codex", "claude-code", "gemini", "hermes"], default="unspecified")
    args = parser.parse_args(argv)
    result = BrainReadService().brain_objects_query(
        repository=args.repository,
        branch=args.branch,
        query=args.query,
        current_files=[str(path) for path in args.current_file],
        project=args.project or None,
        object_types=[str(object_type) for object_type in args.object_type],
        route=args.route,
        limit=args.limit,
        response_mode=args.response_mode,
        consumer=args.consumer,
    )
    _print_json(result)
    return 0


def object_explain_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge object-explain")
    parser.add_argument("--object-id", required=True)
    args = parser.parse_args(argv)
    _print_json(
        {
            "schema_version": "brain_object_explain.v1",
            "object_id": args.object_id,
            "object": {},
            "edges": [],
            "evidence": [],
            "gaps": ["object_store_not_configured"],
        }
    )
    return 0


def corpus_status_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge corpus-status")
    parser.add_argument("--project", default="")
    parser.add_argument("--corpus-id", default="")
    parser.add_argument("--ledger", default="")
    args = parser.parse_args(argv)
    ledger_path = _configured_reference_corpus_ledger(args.ledger)
    if ledger_path:
        _print_json(Ledger(Path(ledger_path)).reference_corpus_status(project=args.project, corpus_id=args.corpus_id))
        return 0
    _print_json(
        {
            "schema_version": "brain_corpus_status.v1",
            "project": args.project,
            "corpus_id": args.corpus_id,
            "source_count": 0,
            "storage_modes": {},
            "reference_object_count": 0,
            "freshness_gaps": [],
            **default_corpus_policy_status(),
            "gaps": ["reference_corpus_store_empty"],
        }
    )
    return 0


def corpus_ingest_plan_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge corpus-ingest-plan")
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--storage-mode",
        choices=["external_object_store", "managed_snapshot", "metadata_only"],
        default="metadata_only",
    )
    parser.add_argument("--corpus-name", default="reference-corpus")
    parser.add_argument("--manifest-file", default="")
    parser.add_argument("--expect-source-count", type=_non_negative_int, default=None)
    parser.add_argument("--expect-source-url-count", type=_non_negative_int, default=None)
    parser.add_argument("--expect-manual-text-without-url-count", type=_non_negative_int, default=None)
    parser.add_argument("--expect-source-type-count", action="append", default=[])
    args = parser.parse_args(argv)
    manifest = (
        _load_manifest(args.manifest_file)
        if args.manifest_file
        else {"corpus_name": args.corpus_name, "sources": []}
    )
    report = build_corpus_ingest_plan(
        manifest,
        project=args.project,
        storage_mode=args.storage_mode,
        expected_source_count=args.expect_source_count,
        expected_source_url_count=args.expect_source_url_count,
        expected_manual_text_without_url_count=args.expect_manual_text_without_url_count,
        expected_source_type_counts=_parse_expected_source_type_counts(args.expect_source_type_count, parser),
    )
    _print_json(report)
    return 1 if report["count_gate_status"] == "fail" else 0


def corpus_ingest_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge corpus-ingest")
    parser.add_argument("--project", required=True)
    parser.add_argument("--target", choices=["local_test", "production"], default="local_test")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--manifest-file", default="")
    parser.add_argument("--approved", action="store_true")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument(
        "--storage-mode",
        choices=["external_object_store", "managed_snapshot", "metadata_only"],
        default="metadata_only",
    )
    parser.add_argument("--expect-source-count", type=_non_negative_int, default=None)
    parser.add_argument("--expect-source-url-count", type=_non_negative_int, default=None)
    parser.add_argument("--expect-manual-text-without-url-count", type=_non_negative_int, default=None)
    parser.add_argument("--expect-source-type-count", action="append", default=[])
    args = parser.parse_args(argv)
    production = args.target == "production"
    if production:
        missing = []
        if not args.approved:
            missing.append("approved")
        try:
            approval_ref = require_sha256(args.approval_ref, "approval_ref")
        except ValueError:
            approval_ref = ""
            missing.append("approval_ref_sha256")
        if not args.manifest_file:
            missing.append("manifest_file")
        if missing:
            _print_json(
                {
                    "schema_version": "reference_corpus_ingest.v1",
                    "status": "denied",
                    "reason": "production_corpus_ingest_requires_bounded_approval",
                    "target": "production",
                    "missing": missing,
                    "mutation_performed": False,
                    "production_mutation_performed": False,
                    "authority_write_performed": False,
                    "network_used": False,
                    "protected_values_returned": False,
                }
            )
            return 1
    else:
        approval_ref = ""
    ledger_path = _configured_reference_corpus_ledger(args.ledger)
    if production and not ledger_path:
        _print_json(
            {
                "schema_version": "reference_corpus_ingest.v1",
                "status": "FAIL",
                "reason": "ledger_not_configured",
                "target": "production",
                "mutation_performed": False,
                "production_mutation_performed": False,
                "authority_write_performed": False,
                "network_used": False,
                "protected_values_returned": False,
            }
        )
        return 1
    if production and not os.environ.get("NEURON_LEDGER_PG_DSN", "") and not Path(ledger_path).exists():
        _print_json(
            {
                "schema_version": "reference_corpus_ingest.v1",
                "status": "FAIL",
                "reason": "production_ledger_not_existing_or_server_backed",
                "target": "production",
                "mutation_performed": False,
                "production_mutation_performed": False,
                "authority_write_performed": False,
                "network_used": False,
                "protected_values_returned": False,
            }
        )
        return 1
    if ledger_path and args.manifest_file:
        manifest = _load_manifest(args.manifest_file)
        plan = build_corpus_ingest_plan(
            manifest,
            project=args.project,
            storage_mode=args.storage_mode,
            expected_source_count=args.expect_source_count,
            expected_source_url_count=args.expect_source_url_count,
            expected_manual_text_without_url_count=args.expect_manual_text_without_url_count,
            expected_source_type_counts=_parse_expected_source_type_counts(args.expect_source_type_count, parser),
        )
        if production and plan["count_gate_status"] == "fail":
            report = {
                "schema_version": "reference_corpus_ingest.v1",
                "status": "FAIL",
                "reason": "production_corpus_ingest_count_gate_failed",
                "target": "production",
                "count_gate_status": plan["count_gate_status"],
                "count_gate_gaps": plan["count_gate_gaps"],
                "mutation_performed": False,
                "production_mutation_performed": False,
                "authority_write_performed": False,
                "network_used": False,
                "protected_values_returned": False,
            }
            ensure_public_safe(report, "ReferenceCorpusProductionIngestDenied")
            _print_json(report)
            return 1
        bundle = reference_corpus_objects_from_manifest(
            manifest,
            project=args.project,
            storage_mode=args.storage_mode,
        )
        ledger = Ledger(Path(ledger_path))
        write_result = ledger.upsert_reference_corpus_bundle(bundle, project=args.project)
        if not production:
            _print_json(write_result)
            return 0
        read_after_write = ledger.reference_corpus_status(
            project=args.project,
            corpus_id=str(write_result.get("corpus_id") or ""),
        )
        evidence = build_reference_corpus_production_ingest_evidence(
            project=args.project,
            bundle=bundle,
            write_result=write_result,
            read_after_write_status=read_after_write,
            approval_ref_hash=approval_ref,
            evidence_collection_network_used=_ledger_is_server_backed(ledger),
        )
        _print_json(evidence)
        return 0 if evidence["read_after_write"]["status"] == "validated" else 1
    _print_json(
        {
            "schema_version": "reference_corpus_ingest.v1",
            "status": "planned",
            "target": "local_test",
            "project": args.project,
            "authority_lane": "reference_only",
            "mutation_performed": False,
            "production_mutation_performed": False,
            "writes_planned": True,
            "gaps": ["reference_corpus_store_not_configured"],
        }
    )
    return 0


def corpus_ingest_readiness_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge corpus-ingest-readiness")
    parser.add_argument("--evidence-file", default="")
    parser.add_argument("--expected-manifest-hash", default="")
    parser.add_argument("--expected-corpus-id", default="")
    parser.add_argument("--expected-source-count", type=_non_negative_int, default=None)
    args = parser.parse_args(argv)
    report = build_reference_corpus_production_ingest_readiness_report(
        live_evidence=_load_json_mapping(args.evidence_file, label="production corpus ingest evidence")
        if args.evidence_file
        else None,
        expected_manifest_hash=args.expected_manifest_hash,
        expected_source_count=args.expected_source_count,
        expected_corpus_id=args.expected_corpus_id,
    )
    _print_json(report)
    return 1 if report["status"] == "FAIL" else 0


def object_authority_schema_ensure_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge object-authority-schema-ensure")
    parser.add_argument("--target", choices=["local_test", "production"], default="local_test")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--approved", action="store_true")
    parser.add_argument("--approval-ref", default="")
    args = parser.parse_args(argv)
    production = args.target == "production"
    if production:
        missing = []
        if not args.approved:
            missing.append("approved")
        try:
            approval_ref = require_sha256(args.approval_ref, "approval_ref")
        except ValueError:
            approval_ref = ""
            missing.append("approval_ref_sha256")
        if missing:
            _print_json(
                {
                    "schema_version": "object_authority_schema_ensure.v1",
                    "status": "denied",
                    "reason": "production_object_authority_schema_ensure_requires_approval",
                    "target": "production",
                    "missing": missing,
                    "mutation_performed": False,
                    "production_mutation_performed": False,
                    "network_used": False,
                }
            )
            return 1
    else:
        approval_ref = ""
    ledger_path = _configured_reference_corpus_ledger(args.ledger)
    if not ledger_path:
        _print_json(
            {
                "schema_version": "object_authority_schema_ensure.v1",
                "status": "FAIL",
                "reason": "ledger_not_configured",
                "target": args.target,
                "mutation_performed": False,
                "production_mutation_performed": False,
                "network_used": False,
            }
        )
        return 1
    if production and not os.environ.get("NEURON_LEDGER_PG_DSN", "") and not Path(ledger_path).exists():
        _print_json(
            {
                "schema_version": "object_authority_schema_ensure.v1",
                "status": "FAIL",
                "reason": "production_ledger_not_existing_or_server_backed",
                "target": "production",
                "mutation_performed": False,
                "production_mutation_performed": False,
                "network_used": False,
            }
        )
        return 1
    try:
        result = Ledger(Path(ledger_path), initialize_schema=False).ensure_object_authority_schema()
    except Exception as exc:
        _print_json(
            {
                "schema_version": "object_authority_schema_ensure.v1",
                "status": "FAIL",
                "reason": f"ledger_schema_ensure_failed:{type(exc).__name__}",
                "target": args.target,
                "mutation_performed": False,
                "production_mutation_performed": False,
                "network_used": False,
            }
        )
        return 1
    result["target"] = args.target
    result["production_mutation_performed"] = production
    result["approval_ref_hash_present"] = bool(approval_ref)
    _print_json(result)
    return 0


def source_to_candidate_graph_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge source-to-candidate-graph")
    parser.add_argument("--project", required=True)
    parser.add_argument("--target", choices=["local_test", "production"], default="local_test")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--corpus-id", default="")
    parser.add_argument("--consumer", default="codex")
    args = parser.parse_args(argv)
    if args.target == "production":
        _print_json(
            {
                "schema_version": "object_substrate_cli_denied.v1",
                "status": "denied",
                "reason": "production_source_to_candidate_graph_requires_later_validation_goal",
                "mutation_performed": False,
                "production_mutation_performed": False,
                "network_used": False,
            }
        )
        return 1
    ledger_path = _configured_reference_corpus_ledger(args.ledger)
    if not ledger_path:
        _print_json(
            {
                "schema_version": "source_to_candidate_graph_activation.v1",
                "status": "FAIL",
                "project": args.project,
                "production_mutation_performed": False,
                "ledger_mutation_performed": False,
                "gaps": ["reference_corpus_store_not_configured"],
            }
        )
        return 1
    ledger_file = Path(ledger_path)
    if not ledger_file.exists():
        _print_json(
            {
                "schema_version": "source_to_candidate_graph_activation.v1",
                "status": "FAIL",
                "project": args.project,
                "production_mutation_performed": False,
                "ledger_mutation_performed": False,
                "gaps": ["reference_corpus_store_missing"],
            }
        )
        return 1
    status = Ledger(ledger_file).reference_corpus_status(project=args.project, corpus_id=args.corpus_id)
    report = run_source_to_candidate_graph_activation_preview(
        corpus_status=status,
        project=args.project,
        consumer=args.consumer,
    )
    _print_json(report)
    return 0 if report["quality_gate"]["source_to_candidate_graph"] == "PASS" else 1


def candidate_review_edit_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge candidate-review-edit")
    parser.add_argument("--target", choices=["local_test", "production"], default="local_test")
    parser.add_argument("--mutation-mode", choices=["no_mutation"], default="no_mutation")
    parser.add_argument("--pack-file", required=True)
    parser.add_argument("--edits-file", required=True)
    parser.add_argument("--reviewer-id", default="unspecified")
    args = parser.parse_args(argv)
    result = apply_candidate_review_edits(
        _load_json_mapping(args.pack_file, label="pack"),
        edits=_load_json_list(args.edits_file, label="edits"),
        reviewer={"id": args.reviewer_id},
        target_scope=args.target,
        mutation_mode=args.mutation_mode,
    )
    _print_json(result)
    return 0 if result["candidate_state_changed"] else 1


def approval_board_decide_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge approval-board-decide")
    parser.add_argument("--target", choices=["local_test", "production"], default="local_test")
    parser.add_argument("--pack-file", required=True)
    parser.add_argument("--decisions-file", required=True)
    parser.add_argument("--reviewer-id", default="unspecified")
    args = parser.parse_args(argv)
    result = apply_approval_board_decisions(
        _load_json_mapping(args.pack_file, label="pack"),
        decisions=_load_json_list(args.decisions_file, label="decisions"),
        reviewer={"id": args.reviewer_id},
        ledger_scope=args.target,
    )
    _print_json(result)
    return 0 if result["permission"] == "allowed" and result["decision_count"] else 1


def golden_query_eval_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge golden-query-eval")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--phase-coverage", action="store_true")
    parser.add_argument("--source-to-authority-gate", action="store_true")
    parser.add_argument("--activation-progress", action="store_true")
    args = parser.parse_args(argv)
    if args.activation_progress:
        _print_json(build_product_activation_progress_report())
        return 0
    if args.source_to_authority_gate:
        _print_json(build_source_to_authority_quality_gate_report())
        return 0
    if args.phase_coverage:
        _print_json(build_phase_golden_query_coverage_report())
        return 0
    _print_json(build_baseline_golden_query_report())
    return 0


def source_to_candidate_runtime_readiness_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge source-to-candidate-runtime-readiness")
    parser.add_argument("--live-evidence-file", default="")
    parser.add_argument("--normalize-post-deploy-capture-file", default="")
    parser.add_argument("--post-deploy-capture-file", default="")
    parser.add_argument("--normalize-shadow-evidence-file", default="")
    parser.add_argument("--shadow-evidence-file", default="")
    parser.add_argument("--expected-commit", default="")
    parser.add_argument("--evidence-collection-plan", action="store_true")
    parser.add_argument("--evidence-packet-template", action="store_true")
    parser.add_argument("--collect-shadow-evidence", action="store_true")
    parser.add_argument("--repository", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--consumer", default="codex")
    args = parser.parse_args(argv)
    if args.evidence_collection_plan:
        _print_json(
            build_source_to_candidate_runtime_evidence_collection_plan(
                expected_commit=args.expected_commit,
                repository=args.repository,
                branch=args.branch,
                consumer=args.consumer,
            )
        )
        return 0
    if args.evidence_packet_template:
        _print_json(
            build_source_to_candidate_runtime_evidence_packet_template(
                expected_commit=args.expected_commit,
                repository=args.repository,
                branch=args.branch,
                consumer=args.consumer,
            )
        )
        return 0
    if args.collect_shadow_evidence:
        read_service = BrainReadService()

        def route_runner(route: str) -> dict[str, Any]:
            return read_service.brain_objects_query(
                repository=args.repository,
                branch=args.branch,
                query=f"source-to-candidate runtime readiness route smoke: {route}",
                current_files=[],
                route=route,
                limit=5,
                response_mode="full",
                consumer=args.consumer,
            )

        _print_json(
            build_source_to_candidate_runtime_collected_shadow_evidence_packet(
                expected_commit=args.expected_commit,
                repository=args.repository,
                branch=args.branch,
                consumer=args.consumer,
                route_runner=route_runner,
            )
        )
        return 0
    if args.normalize_post_deploy_capture_file:
        _print_json(
            build_source_to_candidate_runtime_post_deploy_capture_packet(
                captured_evidence=_load_json_mapping(
                    args.normalize_post_deploy_capture_file,
                    label="post-deploy capture",
                ),
            )
        )
        return 0
    if args.post_deploy_capture_file:
        report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
            captured_evidence=_load_json_mapping(
                args.post_deploy_capture_file,
                label="post-deploy capture",
            ),
            expected_commit=args.expected_commit,
        )
        _print_json(report)
        return 1 if report["status"] == "FAIL" else 0
    if args.normalize_shadow_evidence_file:
        _print_json(
            build_source_to_candidate_runtime_shadow_evidence_packet(
                captured_evidence=_load_json_mapping(
                    args.normalize_shadow_evidence_file,
                    label="shadow evidence",
                ),
            )
        )
        return 0
    if args.shadow_evidence_file:
        report = build_source_to_candidate_runtime_shadow_readiness_report(
            captured_evidence=_load_json_mapping(
                args.shadow_evidence_file,
                label="shadow evidence",
            ),
            expected_commit=args.expected_commit,
        )
        _print_json(report)
        return 1 if report["status"] == "FAIL" else 0
    live_evidence = (
        _load_json_mapping(args.live_evidence_file, label="live evidence")
        if args.live_evidence_file
        else None
    )
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=live_evidence,
        expected_commit=args.expected_commit,
    )
    _print_json(report)
    return 1 if report["status"] == "FAIL" else 0


def okf_export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge okf-export")
    parser.add_argument("--root", default="okf")
    args = parser.parse_args(argv)
    bundle = build_okf_bundle({"documentation_cleanup": build_documentation_cleanup_pack(documents=[])}, root=args.root)
    _print_json({"schema_version": "okf_export_preview.v1", "files": sorted(bundle)})
    return 0

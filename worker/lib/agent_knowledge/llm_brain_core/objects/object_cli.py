from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

from ...ledger import Ledger

from .golden_query_eval import (
    build_baseline_golden_query_report,
    build_product_activation_progress_report,
    build_phase_golden_query_coverage_report,
    build_source_to_authority_quality_gate_report,
)
from .extraction_pipeline import run_source_to_candidate_graph_activation_preview
from .okf_export import build_okf_bundle
from .object_packs import apply_approval_board_decisions, apply_candidate_review_edits, build_documentation_cleanup_pack
from .reference_corpus import build_corpus_ingest_plan, default_corpus_policy_status, reference_corpus_objects_from_manifest
from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

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


def object_query_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge object-query")
    parser.add_argument("--query", required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--branch", default="")
    args = parser.parse_args(argv)
    _ = (args.repository, args.branch)
    pack = build_documentation_cleanup_pack(documents=[], route="documentation_cleanup")
    _print_json({"schema_version": "brain_objects_query.v1", "route": "documentation_cleanup", "object_pack": pack})
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
    parser.add_argument(
        "--storage-mode",
        choices=["external_object_store", "managed_snapshot", "metadata_only"],
        default="metadata_only",
    )
    args = parser.parse_args(argv)
    if args.target == "production":
        _print_json(
            {
                "schema_version": "object_substrate_cli_denied.v1",
                "status": "denied",
                "reason": "production_corpus_ingest_requires_later_validation_goal",
                "mutation_performed": False,
                "network_used": False,
            }
        )
        return 1
    ledger_path = _configured_reference_corpus_ledger(args.ledger)
    if ledger_path and args.manifest_file:
        manifest = _load_manifest(args.manifest_file)
        bundle = reference_corpus_objects_from_manifest(
            manifest,
            project=args.project,
            storage_mode=args.storage_mode,
        )
        _print_json(Ledger(Path(ledger_path)).upsert_reference_corpus_bundle(bundle, project=args.project))
        return 0
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
    parser.add_argument("--pack-file", required=True)
    parser.add_argument("--edits-file", required=True)
    parser.add_argument("--reviewer-id", default="unspecified")
    args = parser.parse_args(argv)
    result = apply_candidate_review_edits(
        _load_json_mapping(args.pack_file, label="pack"),
        edits=_load_json_list(args.edits_file, label="edits"),
        reviewer={"id": args.reviewer_id},
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
    parser.add_argument("--expected-commit", default="")
    args = parser.parse_args(argv)
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

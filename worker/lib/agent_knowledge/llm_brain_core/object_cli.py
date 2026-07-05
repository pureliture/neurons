from __future__ import annotations

import argparse
import json

from .golden_query_eval import build_baseline_golden_query_report
from .okf_export import build_okf_bundle
from .object_packs import build_documentation_cleanup_pack
from .reference_corpus import build_corpus_ingest_plan


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


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
    args = parser.parse_args(argv)
    _print_json(
        {
            "schema_version": "brain_corpus_status.v1",
            "project": args.project,
            "corpus_id": args.corpus_id,
            "source_count": 0,
            "storage_modes": {},
            "reference_object_count": 0,
            "freshness_gaps": [],
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
    args = parser.parse_args(argv)
    _print_json(
        build_corpus_ingest_plan(
            {"corpus_name": args.corpus_name, "sources": []},
            project=args.project,
            storage_mode=args.storage_mode,
        )
    )
    return 0


def corpus_ingest_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge corpus-ingest")
    parser.add_argument("--project", required=True)
    parser.add_argument("--target", choices=["local_test", "production"], default="local_test")
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


def golden_query_eval_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge golden-query-eval")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args(argv)
    _ = args
    _print_json(build_baseline_golden_query_report())
    return 0


def okf_export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge okf-export")
    parser.add_argument("--root", default="okf")
    args = parser.parse_args(argv)
    bundle = build_okf_bundle({"documentation_cleanup": build_documentation_cleanup_pack(documents=[])}, root=args.root)
    _print_json({"schema_version": "okf_export_preview.v1", "files": sorted(bundle)})
    return 0

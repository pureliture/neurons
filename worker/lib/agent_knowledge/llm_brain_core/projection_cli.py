from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel

from .ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .models import PROJECTION_SCHEMA_VERSION
from .projection import GraphProjectionWorker
from .runtime import source_ref_from_catalog_event
from .runtime_graph import build_graph_adapter_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge brain-project")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-ref-jsonl", action="append", default=[])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--enable-graph", action="store_true")
    parser.add_argument("--graph-required", action="store_true")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--skip-memory-cards", action="store_true")
    parser.add_argument("--skip-source-refs", action="store_true")
    parser.add_argument(
        "--resume-projected-ids",
        action="append",
        default=[],
        help=(
            "Path to a newline-delimited file of episode_ids already projected. "
            "Listed ids are skipped (no upsert round-trip) so a re-run resumes "
            "instead of re-upserting the whole window."
        ),
    )
    args = parser.parse_args(argv)

    try:
        report = run_projection(
            ledger_path=Path(args.ledger),
            project=str(args.project),
            source_ref_jsonl=[Path(item) for item in args.source_ref_jsonl or []],
            limit=int(args.limit),
            enable_graph=bool(args.enable_graph),
            graph_required=bool(args.graph_required),
            include_artifacts=not bool(args.skip_artifacts),
            include_memory_cards=not bool(args.skip_memory_cards),
            include_source_refs=not bool(args.skip_source_refs),
            resume_projected_ids=_load_resume_ids([Path(item) for item in args.resume_projected_ids or []]),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": PROJECTION_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": "projection failed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def run_projection(
    *,
    ledger_path: Path,
    project: str,
    source_ref_jsonl: list[Path],
    limit: int,
    enable_graph: bool,
    graph_required: bool,
    include_artifacts: bool,
    include_memory_cards: bool,
    include_source_refs: bool,
    resume_projected_ids: set[str] | None = None,
) -> dict[str, Any]:
    ledger = Ledger(ledger_path)
    artifact_store = LedgerSessionMemoryArtifactStore(ledger)
    source_catalog = LedgerSourceRefCatalog(ledger)
    imported, import_failures, imported_records = _import_source_refs(source_catalog, source_ref_jsonl)
    if import_failures:
        return {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "status": "failed",
            "project": project,
            "source_refs_imported": imported,
            "source_ref_import_failures": import_failures,
            "canonical_counts": {
                "artifacts": 0,
                "memory_cards": 0,
                "source_refs": 0,
            },
            "limit": int(limit),
            "truncated": {
                "any": False,
                "artifacts": False,
                "memory_cards": False,
            },
            "graph_enabled": enable_graph,
            "projection": {
                "status": "failed",
                "attempted": 0,
                "projected": 0,
                "duplicates": 0,
                "failed": 0,
                "episode_ids": [],
                "failures": [],
                "details": ["source_ref_import_failed"],
            },
            "raw_paths_printed": False,
        }
    # `list_recent` / `list_accepted_cards` return at most `limit` rows ordered
    # newest-first. When a source returns exactly its effective bound there may
    # be older rows beyond the window, so the re-projection covers only the most
    # recent `limit` items, not the full project history. Surface that as an
    # explicit `truncated` signal instead of letting the runbook imply full
    # coverage. The artifact store internally caps `limit` at 100, so the
    # effective bound is computed the same way for an honest comparison.
    artifact_bound = max(1, min(int(limit), 100))
    artifacts = artifact_store.list_recent(project=project, limit=limit) if include_artifacts else []
    cards = (
        LegacyLedgerBrainReadModel(ledger).list_accepted_cards(project=project, limit=limit)
        if include_memory_cards
        else []
    )
    source_refs = imported_records if include_source_refs else []
    artifacts_truncated = include_artifacts and len(artifacts) >= artifact_bound
    cards_truncated = include_memory_cards and len(cards) >= max(1, int(limit))
    graph_adapter = build_graph_adapter_from_env(
        enable_flag=True if enable_graph else None,
        required_flag=bool(graph_required),
    )
    projection = GraphProjectionWorker(graph_adapter).project_batch(
        artifacts=artifacts,
        memory_cards=cards,
        source_refs=source_refs,
        project=project,
        resume_projected_ids=resume_projected_ids,
    )
    projection_dict = projection.to_dict()
    status = "ok" if projection.status == "succeeded" and not import_failures else "failed"
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "status": status,
        "project": project,
        "source_refs_imported": imported,
        "source_ref_import_failures": import_failures,
        "canonical_counts": {
            "artifacts": len(artifacts),
            "memory_cards": len(cards),
            "source_refs": len(source_refs),
        },
        "limit": int(limit),
        "truncated": {
            "any": bool(artifacts_truncated or cards_truncated),
            "artifacts": bool(artifacts_truncated),
            "memory_cards": bool(cards_truncated),
        },
        "graph_enabled": enable_graph,
        "projection": projection_dict,
        "raw_paths_printed": False,
    }


def _load_resume_ids(paths: list[Path]) -> set[str]:
    """Read already-projected episode_ids from newline-delimited files.

    Missing/unreadable files contribute nothing (best-effort resume hint) rather
    than failing the run: a stale or absent resume file should degrade to a full
    re-projection, not block it.
    """

    ids: set[str] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                ids.add(stripped)
    return ids


def _import_source_refs(
    catalog: LedgerSourceRefCatalog,
    paths: list[Path],
) -> tuple[int, list[dict[str, Any]], list[Any]]:
    """Import SourceRef JSONL into the catalog as an all-or-nothing batch.

    Parse and validate every line across all supplied files first. Only when
    nothing fails do we register the records into the catalog. If any line is
    unreadable or malformed, no record is written, so a later bad line can never
    leave a partially-loaded catalog (which would survive across re-runs and
    silently widen recall scope). Registration is the last step and is treated
    as the commit point.
    """

    failures: list[dict[str, Any]] = []
    records: list[Any] = []
    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            failures.append({"source": path.name, "line": 0, "reason_code": type(exc).__name__})
            continue
        with handle:
            for line_no, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        raise ValueError("source ref line must decode to an object")
                    records.append(source_ref_from_catalog_event(parsed))
                except Exception as exc:
                    failures.append(
                        {
                            "source": path.name,
                            "line": line_no,
                            "reason_code": type(exc).__name__,
                        }
                    )

    # All-or-nothing: do not partially load the catalog when any line failed.
    if failures:
        return 0, failures, []

    # Commit the validated batch in a single transaction so a write error on any
    # record rolls back the whole import rather than leaving a partial catalog.
    catalog.register_all(records)
    return len(records), [], records

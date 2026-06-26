from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ._util import ensure_public_safe

BRAIN_REGRESSION_GATE_SCHEMA_VERSION = "llm_brain_regression_gate.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge brain-regression-gate")
    parser.add_argument("--before-json", required=True)
    parser.add_argument("--after-json", required=True)
    parser.add_argument("--graph-status-json", default="")
    parser.add_argument("--require-graph-available", action="store_true")
    parser.add_argument("--min-after-items", type=int, default=0)
    parser.add_argument("--min-entity-coverage", type=float, default=0.0)
    args = parser.parse_args(argv)

    try:
        report = evaluate_regression_gate(
            before=_read_json(Path(args.before_json)),
            after=_read_json(Path(args.after_json)),
            graph_status=_read_json(Path(args.graph_status_json)) if args.graph_status_json else None,
            require_graph_available=bool(args.require_graph_available),
            min_after_items=int(args.min_after_items),
            min_entity_coverage=float(args.min_entity_coverage),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": BRAIN_REGRESSION_GATE_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": "regression gate failed",
                    "raw_paths_printed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def evaluate_regression_gate(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    graph_status: dict[str, Any] | None = None,
    require_graph_available: bool = False,
    min_after_items: int = 0,
    min_entity_coverage: float = 0.0,
) -> dict[str, Any]:
    blockers: list[str] = []
    public_safe = _is_public_safe(before, "before", blockers)
    public_safe = _is_public_safe(after, "after", blockers) and public_safe
    if graph_status is not None:
        public_safe = _is_public_safe(graph_status, "graph_status", blockers) and public_safe

    before_graph = _graph_status(before)
    after_graph = _graph_status(after)
    if _graph_rank(after_graph) < _graph_rank(before_graph):
        blockers.append("graph_status_regressed")
    if require_graph_available and after_graph != "available":
        blockers.append("graph_not_available")
    before_memory = _memory_score(before)
    after_memory = _memory_score(after)
    if after_memory < before_memory:
        blockers.append("memory_count_regressed")
    after_items = _evidence_count(after)
    if after_items < max(0, int(min_after_items)):
        blockers.append("after_evidence_below_minimum")
    entity_coverage = _entity_coverage(graph_status or {})
    if float(min_entity_coverage) > 0 and entity_coverage < float(min_entity_coverage):
        blockers.append("entity_coverage_below_minimum")
    if (
        _raw_paths_printed(before)
        or _raw_paths_printed(after)
        or (graph_status is not None and _raw_paths_printed(graph_status))
    ):
        blockers.append("raw_paths_printed")

    return {
        "schema_version": BRAIN_REGRESSION_GATE_SCHEMA_VERSION,
        "status": "ok" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "graph": {
            "before": before_graph,
            "after": after_graph,
            "required_available": bool(require_graph_available),
        },
        "memory": {
            "before_score": before_memory,
            "after_score": after_memory,
        },
        "evidence": {
            "after_items": after_items,
            "min_after_items": max(0, int(min_after_items)),
        },
        "coverage": {
            "entity_coverage_ratio": entity_coverage,
            "min_entity_coverage": float(min_entity_coverage),
        },
        "public_safe": public_safe,
        "raw_paths_printed": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("gate input must be a JSON object")
    return payload


def _is_public_safe(value: dict[str, Any], label: str, blockers: list[str]) -> bool:
    try:
        ensure_public_safe(value, label)
        return True
    except ValueError:
        blockers.append(f"{label}_public_safety_violation")
        return False


def _body(report: dict[str, Any]) -> dict[str, Any]:
    nested = report.get("context_pack")
    return nested if isinstance(nested, dict) else report


def _graph_status(report: dict[str, Any]) -> str:
    body = _body(report)
    status = body.get("graph_status")
    if isinstance(status, dict):
        return str(status.get("status") or "unknown")
    return "unknown"


def _graph_rank(status: str) -> int:
    return {
        "available": 4,
        "degraded": 3,
        "unavailable": 2,
        "error": 1,
        "failed": 0,
        "unknown": 0,
    }.get(str(status or "unknown"), 0)


def _memory_score(report: dict[str, Any]) -> int:
    body = _body(report)
    status = body.get("memory_status")
    if not isinstance(status, dict):
        return 0
    total = 0
    for key in ("count", "artifact_count", "card_count"):
        try:
            total += int(status.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _evidence_count(report: dict[str, Any]) -> int:
    body = _body(report)
    total = 0
    for key in (
        "results",
        "graph_results",
        "unfinished_items",
        "relevant_decisions",
        "similar_incidents",
        "persona_constraints",
        "source_refs",
        "bridge_evidence",
    ):
        value = body.get(key)
        if isinstance(value, list):
            total += len(value)
    return total


def _entity_coverage(report: dict[str, Any]) -> float:
    projection_state = report.get("projection_state")
    if not isinstance(projection_state, dict):
        return 0.0
    try:
        return float(projection_state.get("entity_coverage_ratio") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _raw_paths_printed(report: dict[str, Any]) -> bool:
    return bool(report.get("raw_paths_printed") is True or _body(report).get("raw_paths_printed") is True)

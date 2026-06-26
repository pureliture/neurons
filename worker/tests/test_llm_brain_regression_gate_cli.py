from __future__ import annotations

import json

from agent_knowledge.llm_brain_core.regression_gate_cli import evaluate_regression_gate


def _context_pack(*, graph_status: str, memory_count: int = 2, evidence_count: int = 1) -> dict:
    return {
        "schema_version": "llm_brain_context_resolve.v1",
        "status": "ok",
        "context_pack": {
            "graph_status": {"status": graph_status, "authority": "derived_index"},
            "memory_status": {
                "status": "available",
                "authority": "canonical_artifact_and_card",
                "artifact_count": memory_count,
                "card_count": 0,
            },
            "relevant_decisions": [{"decision": f"decision-{i}"} for i in range(evidence_count)],
            "unfinished_items": [],
            "similar_incidents": [],
            "persona_constraints": [],
            "source_refs": [],
            "bridge_evidence": [],
        },
    }


def test_regression_gate_passes_when_graph_improves_and_coverage_meets_threshold():
    report = evaluate_regression_gate(
        before=_context_pack(graph_status="degraded", memory_count=2, evidence_count=1),
        after=_context_pack(graph_status="available", memory_count=2, evidence_count=3),
        graph_status={"projection_state": {"entity_coverage_ratio": 0.95}},
        require_graph_available=True,
        min_after_items=2,
        min_entity_coverage=0.9,
    )

    assert report["status"] == "ok"
    assert report["blockers"] == []
    assert report["graph"] == {
        "before": "degraded",
        "after": "available",
        "required_available": True,
    }
    assert report["public_safe"] is True
    assert report["raw_paths_printed"] is False


def test_regression_gate_blocks_graph_status_regression():
    report = evaluate_regression_gate(
        before=_context_pack(graph_status="available"),
        after=_context_pack(graph_status="unavailable"),
    )

    assert report["status"] == "blocked"
    assert "graph_status_regressed" in report["blockers"]


def test_regression_gate_blocks_public_safety_violation():
    after = _context_pack(graph_status="available")
    after["context_pack"]["relevant_decisions"] = [{"decision": "/Users/example/private.txt"}]

    report = evaluate_regression_gate(
        before=_context_pack(graph_status="available"),
        after=after,
    )

    assert report["status"] == "blocked"
    assert "after_public_safety_violation" in report["blockers"]
    assert report["public_safe"] is False


def test_regression_gate_blocks_memory_count_drop_and_low_coverage():
    report = evaluate_regression_gate(
        before=_context_pack(graph_status="available", memory_count=3),
        after=_context_pack(graph_status="available", memory_count=1),
        graph_status={"projection_state": {"entity_coverage_ratio": 0.5}},
        min_entity_coverage=0.75,
    )

    assert report["status"] == "blocked"
    assert "memory_count_regressed" in report["blockers"]
    assert "entity_coverage_below_minimum" in report["blockers"]


def test_regression_gate_blocks_raw_paths_printed_from_graph_status_input():
    report = evaluate_regression_gate(
        before=_context_pack(graph_status="available"),
        after=_context_pack(graph_status="available"),
        graph_status={
            "projection_state": {"entity_coverage_ratio": 0.95},
            "raw_paths_printed": True,
        },
    )

    assert report["status"] == "blocked"
    assert "raw_paths_printed" in report["blockers"]


def test_regression_gate_report_never_echoes_raw_path():
    after = _context_pack(graph_status="available")
    report = evaluate_regression_gate(
        before=_context_pack(graph_status="available"),
        after=after,
    )

    assert "/Users/" not in json.dumps(report, sort_keys=True)

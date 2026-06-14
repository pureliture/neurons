"""Held-out golden grader for the autopilot self-grading loop (EVAL LANE ONLY).

This module is the ONLY place that reads the golden answer-key. It lives outside the
``agent_knowledge`` product package on purpose: no product module (brain_query,
ragflow_projection, memory_evaluation, memory_miner, memory_promotion,
llm_brain_service, autopilot_loop, autopilot_cli) may import it. test_golden_grader
asserts that isolation. The golden grades the recall output of run_brain_query_v2; it
is never an input to the producing loop (steps mine/accept/supersede/recall run blind).

Headline metric: SILENT-LIE RATE / false_current_count — a golden superseded_conflict
subject that leaks into the current OR accepted lane. That is the single lie the
product exists to prevent, so it is graded as a negative oracle (must_not_appear_in).
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


_GRADED_LANES = ("current", "accepted", "archive", "conflicts")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def load_golden(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        golden = json.load(handle)
    if not isinstance(golden, list):
        raise ValueError("golden fixture must be a JSON array of entries")
    return golden


def _lanes_present_by_statement(recall: Mapping[str, Any]) -> dict[str, set[str]]:
    present: dict[str, set[str]] = {}
    for lane in _GRADED_LANES:
        for item in recall.get(lane) or []:
            if not isinstance(item, Mapping):
                continue
            key = _norm(item.get("summary") or item.get("render_text") or item.get("title"))
            if key:
                present.setdefault(key, set()).add(lane)
    return present


def grade_recall_against_golden(
    *,
    recall: Mapping[str, Any],
    golden: Sequence[Mapping[str, Any]],
) -> dict:
    present = _lanes_present_by_statement(recall)
    silent_lies: list[dict] = []
    current_expected = 0
    current_found = 0

    for entry in golden:
        appeared = present.get(_norm(entry.get("canonical_statement")), set())
        for forbidden_lane in entry.get("must_not_appear_in") or []:
            if forbidden_lane in appeared:
                silent_lies.append(
                    {"subject_key": entry.get("subject_key"), "lane": forbidden_lane}
                )
        if entry.get("expected_lane") == "current":
            current_expected += 1
            if "current" in appeared:
                current_found += 1

    false_current_count = sum(
        1 for lie in silent_lies if lie["lane"] in ("current", "accepted")
    )
    total = len(golden) or 1
    return {
        "schema_version": "llm_brain_golden_scorecard.v1",
        "false_current_count": false_current_count,
        "silent_lie_rate": len(silent_lies) / total,
        "current_lane_recall": (current_found / current_expected) if current_expected else 1.0,
        "silent_lies": silent_lies,
        "graded_entries": len(golden),
    }

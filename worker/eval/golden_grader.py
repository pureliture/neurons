"""Held-out golden grader for the autopilot self-grading loop (EVAL LANE ONLY).

This module is the ONLY place that reads the golden answer-key. It lives outside the
``agent_knowledge`` product package on purpose: no product module (brain_query,
index_projection, memory_evaluation, memory_miner, memory_promotion,
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
from typing import Any, Callable, Mapping, Sequence


_GRADED_LANES = ("current", "accepted", "archive", "conflicts")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def load_golden(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        golden = json.load(handle)
    if not isinstance(golden, list):
        raise ValueError("golden fixture must be a JSON array of entries")
    return golden


def _item_text(item: Mapping[str, Any]) -> str:
    return str(item.get("summary") or item.get("render_text") or item.get("title") or "")


def _default_match(statement: Any, item: Mapping[str, Any]) -> bool:
    return bool(_norm(statement)) and _norm(statement) == _norm(_item_text(item))


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def build_cosine_match_fn(
    embed_fn: Callable[[str], list],
    *,
    threshold: float = 0.8,
) -> Callable[[Any, Mapping[str, Any]], bool]:
    """Semantic match: cosine(embed(golden statement), embed(recall item text)) >= threshold.

    Credits LLM-paraphrased recall that exact normalized match would miss. Embeddings are
    cached per text. Used live with build_vertex_embedding_fn; unit-tested with a fake embed_fn.
    """
    cache: dict[str, list] = {}

    def _embed(text: str) -> list:
        key = _norm(text)
        if key not in cache:
            cache[key] = embed_fn(text)
        return cache[key]

    def match(statement: Any, item: Mapping[str, Any]) -> bool:
        text = _item_text(item)
        if not _norm(statement) or not _norm(text):
            return False
        return _cosine(_embed(str(statement)), _embed(text)) >= threshold

    return match


def _lane_items(recall: Mapping[str, Any]) -> dict[str, list]:
    return {
        lane: [it for it in (recall.get(lane) or []) if isinstance(it, Mapping)]
        for lane in _GRADED_LANES
    }


def grade_recall_against_golden(
    *,
    recall: Mapping[str, Any],
    golden: Sequence[Mapping[str, Any]],
    match_fn: Callable[[Any, Mapping[str, Any]], bool] | None = None,
) -> dict:
    match = match_fn or _default_match
    lane_items = _lane_items(recall)
    silent_lies: list[dict] = []
    current_expected = 0
    current_found = 0

    for entry in golden:
        statement = entry.get("canonical_statement")
        appeared = {
            lane for lane, items in lane_items.items() if any(match(statement, it) for it in items)
        }
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

from __future__ import annotations

from typing import Any, Mapping

from ._util import ensure_public_safe

GOLDEN_QUERIES = [
    "어제 이 repo에서 뭐 했어?",
    "이 repo 문서 최신화하려면 뭘 봐야 해?",
    "오래된 문서/개념 후보 알려줘.",
    "이 파일 바꾸면 어떤 테스트/런타임 영향 있어?",
    "이 PR merge됐어? 배포도 됐어?",
    "지금 current SoT와 stale archive를 분리해서 말해줘.",
    "이 Palantir reference 문서는 공식/current/source URL 확인 가능 자료야?",
    "이 corpus에서 LBrain object model 설계에 필요한 개념만 뽑아줘.",
    "내 Java code style과 다른 diff를 찾아줘.",
    "내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.",
]


def build_baseline_golden_query_report() -> dict[str, Any]:
    report = {
        "schema_version": "knowledge_object_golden_query_eval.v1",
        "status": "baseline_red",
        "queries": [
            {
                "index": index + 1,
                "query": query,
                "passes": False,
                "failures": [
                    "object_pack_missing",
                    "lane_separation_missing",
                    "evidence_or_gap_missing",
                    "recommended_action_missing",
                ],
            }
            for index, query in enumerate(GOLDEN_QUERIES)
        ],
    }
    ensure_public_safe(report, "GoldenQueryBaseline")
    return report


def evaluate_object_pack_response(query: str, response: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    lanes = response.get("lanes") if isinstance(response.get("lanes"), Mapping) else {}
    lane_items = []
    for value in lanes.values():
        if isinstance(value, list):
            lane_items.extend(value)
    evidence = response.get("evidence") if isinstance(response.get("evidence"), list) else []
    gaps = response.get("gaps") if isinstance(response.get("gaps"), list) else []
    actions = response.get("recommended_actions") if isinstance(response.get("recommended_actions"), list) else []
    if not response.get("route"):
        failures.append("missing_route")
    if not lane_items:
        failures.append("missing_object_lane")
    if not evidence and not gaps:
        failures.append("missing_evidence_or_gap")
    if not actions:
        failures.append("missing_recommended_action")
    result = {
        "query": query,
        "passes": not failures,
        "failures": failures,
    }
    ensure_public_safe(result, "GoldenQueryEvalResult")
    return result

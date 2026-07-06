from __future__ import annotations

from typing import Any, Mapping

from .._util import ensure_public_safe

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

REQUIRED_QUALITY_AXES = [
    "object",
    "edge",
    "evidence",
    "freshness",
    "gap",
    "recommended_action",
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


def build_phase_golden_query_coverage_report() -> dict[str, Any]:
    phases = [
        _phase_coverage(
            phase="P1",
            title="Production MCP Activation",
            golden_query_family="pr merge and deploy truth",
            query=GOLDEN_QUERIES[4],
            result="PASS_WITH_GAPS",
            evaluator="configured/live MCP smoke plus deployment identity check",
            gaps=[
                "current_session_mcp_namespace_stale",
                "current_main_image_identity_unproven",
            ],
        ),
        _phase_coverage(
            phase="P2",
            title="Living Reference Corpus Store",
            golden_query_family="reference corpus freshness/source authority",
            query=GOLDEN_QUERIES[6],
            result="PASS_WITH_GAPS",
            evaluator="reference corpus store local/test status and ingest policy checks",
            gaps=[
                "private_palantir_manifest_ingest_not_performed",
                "production_ingest_gate_denied",
            ],
        ),
        _phase_coverage(
            phase="P3",
            title="Processing And Object Extraction Pipeline",
            golden_query_family="corpus-to-design concept extraction",
            query=GOLDEN_QUERIES[7],
            result="PASS_WITH_GAPS",
            evaluator="extraction evaluator suite preview",
            gaps=[
                "live_graph_qdrant_projection_join_unproven",
            ],
        ),
        _phase_coverage(
            phase="P4",
            title="Review Queue And Authority Promotion",
            golden_query_family="review queue and authority promotion",
            query=GOLDEN_QUERIES[2],
            result="PASS_WITH_GAPS",
            evaluator="local/test review queue, authority decision, object query, and object explain checks",
            gaps=[
                "approved_production_pilot_missing",
                "production_authority_write_denied",
            ],
        ),
        _phase_coverage(
            phase="P5",
            title="Continuous Golden Query Quality Gates",
            golden_query_family="continuous phase coverage",
            query="P1-P10 phase coverage is explicit and gaps are visible.",
            result="in_progress",
            evaluator="phase golden query coverage report",
            gaps=[
                "release_quality_gate_not_green",
                "future_phase_slices_planned",
            ],
        ),
        _phase_coverage(
            phase="P6",
            title="Session, Device, Project, And Work-Unit 360",
            golden_query_family="temporal repo recall",
            query=GOLDEN_QUERIES[0],
            result="in_progress",
            evaluator="session project rollup local/test preview",
            gaps=[
                "handoff_pack_not_implemented",
                "live_multi_device_rollup_unproven",
            ],
        ),
        _phase_coverage(
            phase="P7",
            title="Preference, Style, And Artifact Memory",
            golden_query_family="code style drift",
            query=GOLDEN_QUERIES[8],
            result="in_progress",
            evaluator="artifact preference pack local/test preview",
            gaps=[
                "accepted_preference_context_pack_live_unproven",
                "html_artifact_review_live_unproven",
            ],
        ),
        _phase_coverage(
            phase="P8",
            title="Runtime Truth, Security, And Deployment Authority",
            golden_query_family="pr merge and deploy truth",
            query=GOLDEN_QUERIES[4],
            result="in_progress",
            evaluator="runtime authority policy local/test preview",
            gaps=[
                "live_runtime_rollout_identity_unproven",
                "production_permission_audit_live_unproven",
            ],
        ),
        _planned_phase("P9", "Agent-Facing Action Surface", "code change impact analysis", GOLDEN_QUERIES[3]),
        _planned_phase("P10", "Product Application Surface", "HTML/visualization review preference", GOLDEN_QUERIES[9]),
    ]
    report = {
        "schema_version": "knowledge_object_phase_golden_query_coverage.v1",
        "status": "PASS_WITH_GAPS",
        "release_quality_gate": "not_green",
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "phases": phases,
        "gaps": [
            "production_quality_not_green",
            "future_phase_golden_query_slices_planned",
        ],
    }
    ensure_public_safe(report, "PhaseGoldenQueryCoverage")
    return report


def evaluate_object_pack_response(
    query: str,
    response: Mapping[str, Any],
    *,
    required_axes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    checked_axes = [str(axis or "") for axis in required_axes or []]
    lanes = response.get("lanes") if isinstance(response.get("lanes"), Mapping) else {}
    lane_items = []
    for value in lanes.values():
        if isinstance(value, list):
            lane_items.extend(value)
    edges = response.get("edges") if isinstance(response.get("edges"), list) else []
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
    if "edge" in checked_axes and not edges and not _gap_declares_not_applicable(gaps, "edge"):
        failures.append("missing_edge")
    if "freshness" in checked_axes and not _has_freshness_signal(response, evidence):
        failures.append("missing_freshness")
    if "gap" in checked_axes and not isinstance(response.get("gaps"), list):
        failures.append("missing_gap_field")
    if checked_axes:
        for lane, value in lanes.items():
            safe_lane = str(lane or "")
            if isinstance(value, list) and not value and not _empty_lane_is_stated(safe_lane, gaps):
                failures.append(f"empty_authority_lane_not_stated:{safe_lane}")
    if checked_axes and _is_runtime_claim(query, response) and not _has_runtime_evidence_or_gap(response, evidence, gaps):
        failures.append("runtime_evidence_missing")
    result = {
        "query": query,
        "passes": not failures,
        "failures": failures,
        "checked_axes": checked_axes,
    }
    ensure_public_safe(result, "GoldenQueryEvalResult")
    return result


def _phase_coverage(
    *,
    phase: str,
    title: str,
    golden_query_family: str,
    query: str,
    result: str,
    evaluator: str,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "phase": phase,
        "title": title,
        "golden_query_family": golden_query_family,
        "query": query,
        "result": result,
        "evaluator": evaluator,
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "gaps": list(gaps),
    }


def _planned_phase(phase: str, title: str, golden_query_family: str, query: str) -> dict[str, Any]:
    return _phase_coverage(
        phase=phase,
        title=title,
        golden_query_family=golden_query_family,
        query=query,
        result="planned",
        evaluator="not_implemented",
        gaps=["phase_slice_not_implemented"],
    )


def _gap_declares_not_applicable(gaps: list[Any], axis: str) -> bool:
    wanted = {f"{axis}_not_applicable", f"{axis}s_not_applicable"}
    return any(str(item or "") in wanted for item in gaps)


def _has_freshness_signal(response: Mapping[str, Any], evidence: list[Any]) -> bool:
    if response.get("freshness") or response.get("freshness_gaps"):
        return True
    verification = response.get("verification")
    if isinstance(verification, Mapping):
        for key in ("freshness", "freshness_checked", "freshness_gaps", "freshness_verified"):
            value = verification.get(key)
            if isinstance(value, list) and value:
                return True
            if isinstance(value, Mapping) and value:
                return True
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, str) and value:
                return True
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        verification_state = str(item.get("verification_state") or "")
        evidence_type = str(item.get("evidence_type") or "").lower()
        if verification_state in {"freshness_checked", "freshness_verified"} or "freshness" in evidence_type:
            return True
    return False


def _empty_lane_is_stated(lane: str, gaps: list[Any]) -> bool:
    for item in gaps:
        text = str(item or "").lower()
        if lane.lower() in text and any(marker in text for marker in ("empty", "missing", "none")):
            return True
    return False


def _is_runtime_claim(query: str, response: Mapping[str, Any]) -> bool:
    route = str(response.get("route") or "").lower()
    text = f"{route} {query}".lower()
    return any(marker in text for marker in ("runtime", "deploy", "deployment", "배포", "live"))


def _has_runtime_evidence_or_gap(response: Mapping[str, Any], evidence: list[Any], gaps: list[Any]) -> bool:
    verification = response.get("verification")
    if isinstance(verification, Mapping):
        for key in ("runtime_verified", "runtime_unverified"):
            value = verification.get(key)
            if isinstance(value, list) and value:
                return True
    for item in evidence:
        if isinstance(item, Mapping) and str(item.get("verification_state") or "") in {
            "runtime_verified",
            "runtime_unverified",
        }:
            return True
    for item in gaps:
        if "runtime_evidence" in str(item or ""):
            return True
    return False

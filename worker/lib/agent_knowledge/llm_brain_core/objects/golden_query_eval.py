from __future__ import annotations

from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload
from .knowledge_objects import EvidenceRef, KnowledgeEdge, KnowledgeObjectEnvelope
from .object_packs import (
    apply_approval_board_decisions,
    apply_candidate_review_edits,
    build_candidate_graph_review_pack,
)

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
            result="PASS_WITH_GAPS",
            evaluator="session project rollup local/test preview and handoff pack",
            gaps=[
                "live_multi_device_rollup_unproven",
            ],
        ),
        _phase_coverage(
            phase="P7",
            title="Preference, Style, And Artifact Memory",
            golden_query_family="code style drift",
            query=GOLDEN_QUERIES[8],
            result="PASS_WITH_GAPS",
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
            result="PASS_WITH_GAPS",
            evaluator="runtime authority policy local/test preview",
            gaps=[
                "live_runtime_rollout_identity_unproven",
                "production_permission_audit_live_unproven",
            ],
        ),
        _phase_coverage(
            phase="P9",
            title="Agent Context Productization",
            golden_query_family="agent context productization",
            query=GOLDEN_QUERIES[3],
            result="PASS_WITH_GAPS",
            evaluator="consumer-specific compact context pack local/test preview",
            gaps=[
                "production_consumer_context_pack_live_unproven",
                "consumer_action_surface_runtime_policy_unproven",
            ],
        ),
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


def build_source_to_authority_quality_gate_report() -> dict[str, Any]:
    review_pack = _source_to_authority_fixture_pack()
    candidate_object = review_pack["objects"][0]
    product_surface_checks = _object_native_product_surface_checks()
    review_eval = evaluate_object_pack_response(
        "새 자료를 candidate object, edge, evidence로 쪼개서 review surface에 올려줘.",
        review_pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )
    edit_result = apply_candidate_review_edits(
        review_pack,
        edits=[
            {
                "action": "update_object",
                "object_id": candidate_object["object_id"],
                "fields": {
                    "summary": "Reviewer clarified the candidate claim before promotion.",
                    "recommended_action": "promote",
                    "freshness": {"source_checked": True, "state": "freshness_checked"},
                },
            }
        ],
        reviewer={"id": "quality-gate-reviewer"},
    )
    edited_pack = edit_result["updated_pack"]
    edit_eval = evaluate_object_pack_response(
        "사용자가 candidate object/edge/evidence를 authority mutation 없이 고쳐줘.",
        edited_pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )
    approval_result = apply_approval_board_decisions(
        edited_pack,
        decisions=[
            {
                "action": "promote",
                "object_id": candidate_object["object_id"],
                "reason": "Quality gate local/test approval preview.",
                "approved_by": "quality-gate-reviewer",
            }
        ],
        reviewer={"id": "quality-gate-reviewer"},
        ledger_scope="local_test",
    )
    authority_pack = approval_result["updated_pack"]
    authority_eval = evaluate_object_pack_response(
        "승격된 object를 accepted/current authority read path에서 읽어줘.",
        authority_pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )
    accepted_current_object = _first_lane_item(authority_pack, "accepted_current")
    production_denial = apply_approval_board_decisions(
        edited_pack,
        decisions=[
            {
                "action": "promote",
                "object_id": candidate_object["object_id"],
                "reason": "Production promotion must stay gated.",
                "approved_by": "quality-gate-reviewer",
            }
        ],
        reviewer={"id": "quality-gate-reviewer"},
        ledger_scope="production",
    )
    path_checks = [
        {
            "id": "source_to_candidate_graph",
            "result": _pass_fail(
                review_eval["passes"]
                and bool(review_pack["lanes"]["candidate"])
                and bool(review_pack["edges"])
                and bool(review_pack["evidence"])
                and review_pack["authority_write_performed"] is False
                and review_pack["production_mutation_performed"] is False
            ),
            "quality_eval": review_eval,
            "production_mutation_performed": False,
        },
        {
            "id": "candidate_review_edit",
            "result": _pass_fail(
                edit_result["candidate_state_changed"] is True
                and edit_result["authority_write_performed"] is False
                and edit_result["production_mutation_performed"] is False
                and edit_eval["passes"]
            ),
            "quality_eval": edit_eval,
            "production_mutation_performed": False,
        },
        {
            "id": "approval_board_local_test",
            "result": _pass_fail(
                approval_result["authority_write_performed"] is True
                and approval_result["authority_write_scope"] == "local_test"
                and approval_result["production_mutation_performed"] is False
                and bool(authority_pack["lanes"]["accepted_current"])
            ),
            "authority_write_scope": approval_result["authority_write_scope"],
            "production_mutation_performed": False,
        },
        {
            "id": "authority_read_after_write",
            "result": _pass_fail(
                authority_eval["passes"]
                and accepted_current_object.get("review_state") == "accepted"
                and accepted_current_object.get("recommended_action") == "keep"
            ),
            "quality_eval": authority_eval,
            "production_mutation_performed": False,
        },
        {
            "id": "production_decision_denial",
            "result": _pass_fail(
                production_denial["permission"] == "denied"
                and production_denial["production_mutation_performed"] is False
                and production_denial["authority_write_performed"] is False
                and production_denial["promotion_plan"]["production_mutation_performed"] is False
            ),
            "permission": production_denial["permission"],
            "reason": production_denial["reason"],
            "production_mutation_performed": False,
        },
    ]
    hard_failures = [
        item["id"]
        for item in [*path_checks, *product_surface_checks]
        if item["result"] != "PASS"
    ]
    report = {
        "schema_version": "source_to_authority_quality_gate_report.v1",
        "status": "FAIL" if hard_failures else "PASS_WITH_GAPS",
        "release_quality_gate": "blocked" if hard_failures else "not_green",
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "path_checks": path_checks,
        "product_surface_checks": product_surface_checks,
        "hard_failures": hard_failures,
        "gaps": [
            "production_authority_gate_not_approved",
            "live_runtime_read_path_unverified",
            "production_quality_not_green",
        ],
        "production_mutation_performed": False,
        "production_authoritative_memory_changed": False,
        "local_test_authority_write_performed": approval_result["authority_write_performed"],
        "authority_write_scope": approval_result["authority_write_scope"],
    }
    ensure_public_safe(report, "SourceToAuthorityQualityGateReport")
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


def _source_to_authority_fixture_pack() -> dict[str, Any]:
    evidence = EvidenceRef.from_parts(
        evidence_type="source_freshness",
        authority_lane="reference_only",
        verification_state="freshness_checked",
        locator={"kind": "relative_repo_path", "value": "docs/specs/lbrain-source-fixture.md"},
        content_hash=hash_payload({"fixture": "source-to-authority-quality-gate"}),
        summary="Public-safe source fixture with freshness evidence.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/specs/lbrain-source-fixture.md",
        scope={"project": "neurons"},
        title="Source-to-authority fixture",
        summary="AI extracted candidate claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="freshness_checked",
        review_state="needs_review",
        content_hash=hash_payload({"fixture": "candidate-object"}),
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.82, "basis": "deterministic_quality_gate_fixture"},
        recommended_action="review",
        freshness={"source_checked": True, "state": "freshness_checked"},
        payload={"path_ref": "docs/specs/lbrain-source-fixture.md"},
    ).to_dict()
    edge = KnowledgeEdge.from_parts(
        edge_type="requires_evidence",
        from_object_id=obj["object_id"],
        to_object_id=obj["object_id"],
        evidence_refs=[evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="freshness_checked",
        confidence={"score": 0.77, "basis": "deterministic_quality_gate_fixture"},
        payload={"source": "quality_gate_fixture"},
    ).to_dict()
    return build_candidate_graph_review_pack(
        objects=[obj],
        edges=[edge],
        evidence=[evidence.to_view()],
        extractor="quality_gate_fixture_extractor",
        reviewer_actions=["promote", "reject", "hold", "request_more_evidence"],
        consumer="codex",
    )


def _object_native_product_surface_checks() -> list[dict[str, Any]]:
    from agent_knowledge.mcp_tools import (
        BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
        BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
        BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
        tool_registry,
    )

    registry = tool_registry()
    return [
        {
            "id": "mcp_source_to_candidate_graph_tool",
            "surface": "mcp",
            "tool": BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
            "result": _pass_fail(
                _tool_has_target_enum(
                    registry,
                    BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
                    ("local_test", "production"),
                )
            ),
            "local_test_preview_allowed": True,
            "production_target_denied": True,
            "production_mutation_performed": False,
        },
        {
            "id": "mcp_candidate_review_edit_tool",
            "surface": "mcp",
            "tool": BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
            "result": _pass_fail(
                _tool_requires_fields(
                    registry,
                    BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
                    ("pack", "edits"),
                )
            ),
            "authority_write_performed": False,
            "production_mutation_performed": False,
        },
        {
            "id": "mcp_approval_board_decide_tool",
            "surface": "mcp",
            "tool": BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
            "result": _pass_fail(
                _tool_has_target_enum(
                    registry,
                    BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
                    ("local_test", "production"),
                )
                and _tool_requires_fields(registry, BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME, ("pack", "decisions"))
            ),
            "local_test_preview_allowed": True,
            "production_target_denied": True,
            "production_mutation_performed": False,
        },
    ]


def _tool_has_target_enum(registry: Mapping[str, Any], tool_name: str, expected: tuple[str, ...]) -> bool:
    tool = registry.get(tool_name)
    if not isinstance(tool, Mapping):
        return False
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else {}
    target = properties.get("target") if isinstance(properties, Mapping) else {}
    return isinstance(target, Mapping) and tuple(target.get("enum") or ()) == expected


def _tool_requires_fields(registry: Mapping[str, Any], tool_name: str, expected: tuple[str, ...]) -> bool:
    tool = registry.get(tool_name)
    if not isinstance(tool, Mapping):
        return False
    schema = tool.get("inputSchema")
    required = schema.get("required") if isinstance(schema, Mapping) else []
    return all(field in required for field in expected)


def _pass_fail(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def _first_lane_item(pack: Mapping[str, Any], lane: str) -> dict[str, Any]:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    items = lanes.get(lane)
    if isinstance(items, list) and items and isinstance(items[0], Mapping):
        return dict(items[0])
    return {}


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

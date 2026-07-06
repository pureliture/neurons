from __future__ import annotations

from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload, public_safe_text
from ..preference_authority import preference_rule_cards_from_memory_cards
from ..repo_style_profile import repo_style_profile_from_memory_cards
from .golden_query_eval import evaluate_object_pack_response
from .knowledge_objects import EvidenceRef, KnowledgeEdge, KnowledgeObjectEnvelope
from .object_packs import build_agent_context_object_packs, build_documentation_cleanup_pack, build_runtime_truth_pack
from .reference_corpus import reference_corpus_objects_from_manifest


def build_extractor_registry_report() -> dict[str, Any]:
    report = {
        "schema_version": "object_extractor_registry.v1",
        "production_mutation_performed": False,
        "extractors": [
            {
                "extractor": "reference_corpus_manifest",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["CorpusManifest"],
                "output_object_types": ["ReferenceCorpus", "ReferenceDocument"],
                "edge_types": ["member_of_corpus"],
                "strategy_scope": "reference_corpus_document_mapping",
                "gaps": [],
            },
            {
                "extractor": "repo_document_cleanup",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["RepoDocument"],
                "output_object_types": ["RepoDocument"],
                "edge_types": ["supersedes", "requires_evidence"],
                "strategy_scope": "repo_document_cleanup_mapping",
                "gaps": [],
            },
            {
                "extractor": "runtime_truth",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["RuntimeEvidence"],
                "output_object_types": ["PullRequest", "RuntimeTruth"],
                "edge_types": ["validated_by", "requires_live_evidence"],
                "strategy_scope": "runtime_evidence_mapping",
                "gaps": [],
            },
            {
                "extractor": "preference_style",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["MemoryCard"],
                "output_object_types": ["ArtifactPreference", "StyleRule"],
                "edge_types": ["supported_by_evidence"],
                "strategy_scope": "style_preference_mapping",
                "gaps": [],
            },
            {
                "extractor": "work_unit",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["Session", "PullRequest", "Commit", "Test"],
                "output_object_types": ["WorkUnit"],
                "edge_types": ["supported_by_evidence", "validated_by"],
                "strategy_scope": "temporal_work_recall_mapping",
                "gaps": [],
            },
            {
                "extractor": "session_detail",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["Session"],
                "output_object_types": ["Session"],
                "edge_types": ["part_of_work_unit", "supported_by_evidence"],
                "strategy_scope": "session_metadata_evidence_mapping",
                "gaps": [],
            },
            {
                "extractor": "session_project_rollup",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["Session"],
                "output_object_types": ["Device", "Session", "Repository", "Branch", "WorkUnit"],
                "edge_types": [
                    "repository_has_branch",
                    "session_on_device",
                    "session_in_repository",
                    "session_on_branch",
                    "part_of_work_unit",
                ],
                "strategy_scope": "session_device_project_rollup_mapping",
                "gaps": [],
            },
            {
                "extractor": "pr_commit_detail",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["PullRequest", "Commit", "Test"],
                "output_object_types": ["PullRequest", "Commit", "TestRun"],
                "edge_types": ["includes_commit", "validated_by"],
                "strategy_scope": "pr_commit_test_provenance_mapping",
                "gaps": [],
            },
            {
                "extractor": "graph_search_projection_join",
                "version": "0.1",
                "status": "implemented",
                "input_object_types": ["KnowledgeObjectEnvelope", "ProjectionHit"],
                "output_object_types": ["ProjectionHit"],
                "edge_types": ["projection_join"],
                "strategy_scope": "derived_projection_join_mapping",
                "gaps": [],
            },
        ],
    }
    ensure_public_safe(report, "ObjectExtractorRegistry")
    return report


def run_reference_corpus_extraction_preview(
    manifest: Mapping[str, Any],
    *,
    project: str,
    storage_mode: str,
) -> dict[str, Any]:
    bundle = reference_corpus_objects_from_manifest(
        manifest,
        project=project,
        storage_mode=storage_mode,
    )
    blocked = bundle["extraction_run"]["status"] != "completed"
    gaps = _extraction_gaps(bundle)
    if blocked:
        result = _blocked_preview(bundle=bundle, project=project, storage_mode=storage_mode, gaps=gaps)
        ensure_public_safe(result, "ObjectExtractionPreview")
        return result

    corpus_object = _corpus_object(bundle["corpus"], project=project)
    document_objects = [_stable_object(item) for item in bundle["objects"]]
    objects = [_stable_object(corpus_object.to_dict()), *document_objects]
    edges = [
        _stable_edge(
            KnowledgeEdge.from_parts(
                edge_type="member_of_corpus",
                from_object_id=obj["object_id"],
                to_object_id=objects[0]["object_id"],
                evidence_refs=obj.get("evidence_refs") or [],
                lifecycle_status="extracted",
                authority_lane="reference_only",
                verification_state="source_hash_verified",
                confidence={"score": 0.7, "basis": "deterministic_manifest_mapping"},
                payload={
                    "extractor": "reference_corpus_manifest",
                    "strategy": "manifest_summary_v1",
                },
            ).to_dict()
        )
        for obj in document_objects
    ]
    chunk_preview = [_chunk_preview(chunk) for chunk in bundle["chunks"]]
    result = {
        "schema_version": "object_extraction_preview.v1",
        "status": "completed",
        "project": public_safe_text(project, max_chars=120),
        "extractor": "reference_corpus_manifest",
        "extractor_version": "0.1",
        "storage_mode": public_safe_text(storage_mode, max_chars=80),
        "production_mutation_performed": False,
        "objects": objects,
        "edges": edges,
        "chunk_preview": chunk_preview,
        "object_count": len(objects),
        "edge_count": len(edges),
        "chunk_preview_count": len(chunk_preview),
        "strategy_comparison": _strategy_comparison(
            object_count=len(objects),
            edge_count=len(edges),
            chunk_preview_count=len(chunk_preview),
            storage_mode=storage_mode,
        ),
        "extraction_run": _preview_extraction_run(
            bundle["extraction_run"],
            object_count=len(objects),
            edge_count=len(edges),
            chunk_preview_count=len(chunk_preview),
            status="completed",
        ),
        "evaluator_report": _evaluator_report(status="completed", gaps=gaps),
        "gaps": gaps,
    }
    ensure_public_safe(result, "ObjectExtractionPreview")
    return result


def run_documentation_cleanup_strategy_comparison(
    *,
    documents: list[Mapping[str, Any]],
    consumer: str = "unspecified",
) -> dict[str, Any]:
    pack = build_documentation_cleanup_pack(
        documents=documents,
        route="documentation_cleanup",
        consumer=consumer,
    )
    eval_result = evaluate_object_pack_response(
        "이 repo 문서 최신화하려면 뭘 봐야 해?",
        pack,
    )
    failures = list(eval_result["failures"])
    lane_counts = _lane_counts(pack)
    if lane_counts.get("accepted_current", 0) == 0:
        failures.append("accepted_current_lane_empty")
    if lane_counts.get("proposal_only", 0) == 0:
        failures.append("archive_or_stale_candidate_lane_empty")
    status = "pass" if not failures else "fail"
    result = {
        "schema_version": "object_extraction_strategy_comparison.v1",
        "status": status,
        "consumer": public_safe_text(consumer, max_chars=80),
        "selected_strategy": "document_authority_pack_v1",
        "production_mutation_performed": False,
        "pack_preview": {
            "route": pack["route"],
            "object_count": len(pack["objects"]),
            "edge_count": len(pack["edges"]),
            "evidence_count": len(pack["evidence"]),
            "lane_counts": lane_counts,
            "recommended_action_count": len(pack["recommended_actions"]),
            "gaps": list(pack["gaps"]),
        },
        "strategy_comparison": [
            {
                "strategy": "document_authority_pack_v1",
                "scope": "repo_document_cleanup_mapping",
                "selected": True,
                "status": status,
                "object_count": len(pack["objects"]),
                "edge_count": len(pack["edges"]),
                "evidence_count": len(pack["evidence"]),
                "lane_counts": lane_counts,
                "gaps": failures,
            },
            {
                "strategy": "path_inventory_only_v1",
                "scope": "repo_document_cleanup_mapping",
                "selected": False,
                "status": "available_with_gap",
                "object_count": len(documents),
                "edge_count": 0,
                "evidence_count": len(documents),
                "lane_counts": {"reference_only": len(documents)},
                "gaps": ["authority_lane_inference_missing"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "documentation cleanup current-vs-archive",
            "passes": not failures,
            "failures": failures,
            "gaps": list(pack["gaps"]),
            "assertions": [
                "separates_current_from_archive",
                "includes_recommended_action",
                "includes_evidence_or_gap",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "DocumentationCleanupStrategyComparison")
    return result


def run_repo_document_extraction_preview(
    *,
    documents: list[Mapping[str, Any]],
    repository: str,
    consumer: str = "unspecified",
) -> dict[str, Any]:
    pack = build_documentation_cleanup_pack(
        documents=documents,
        route="documentation_cleanup",
        consumer=consumer,
    )
    objects = [_stable_object(obj) for obj in pack["objects"]]
    edges = _repo_document_edges(documents=documents, objects=objects)
    gaps = list(pack["gaps"])
    status = "pass" if objects and not gaps else "pass_with_gaps"
    lane_counts = _lane_counts(pack)
    result = {
        "schema_version": "object_extraction_repo_document_preview.v1",
        "status": status,
        "repository": public_safe_text(repository, max_chars=180),
        "consumer": public_safe_text(consumer, max_chars=80),
        "selected_strategy": "repo_document_pack_extraction_v1",
        "production_mutation_performed": False,
        "objects": objects,
        "edges": edges,
        "evidence": list(pack["evidence"]),
        "object_count": len(objects),
        "edge_count": len(edges),
        "evidence_count": len(pack["evidence"]),
        "lane_counts": lane_counts,
        "recommended_actions": list(pack["recommended_actions"]),
        "extraction_run": _repo_document_extraction_run(
            documents=documents,
            object_count=len(objects),
            edge_count=len(edges),
            evidence_count=len(pack["evidence"]),
            gaps=gaps,
        ),
        "strategy_comparison": [
            {
                "strategy": "repo_document_pack_extraction_v1",
                "scope": "repo_document_cleanup_mapping",
                "selected": True,
                "status": status,
                "object_count": len(objects),
                "edge_count": len(edges),
                "evidence_count": len(pack["evidence"]),
                "lane_counts": lane_counts,
                "gaps": gaps,
            },
            {
                "strategy": "path_inventory_only_v1",
                "scope": "repo_document_cleanup_mapping",
                "selected": False,
                "status": "available_with_gap",
                "object_count": len(documents),
                "edge_count": 0,
                "evidence_count": len(documents),
                "lane_counts": {"reference_only": len(documents)},
                "gaps": ["authority_lane_inference_missing"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "repo document cleanup extraction run",
            "passes": status == "pass",
            "failures": [] if status == "pass" else gaps or ["repo_document_extraction_empty"],
            "gaps": gaps,
            "assertions": [
                "repo_documents_have_authority_lanes",
                "cleanup_edges_are_public_safe",
                "recommended_actions_are_present",
                "production_mutation_performed_false",
            ],
        },
        "gaps": gaps,
    }
    ensure_public_safe(result, "RepoDocumentExtractionPreview")
    return result


def run_runtime_truth_extraction_preview(
    *,
    pull_request: Mapping[str, Any] | None,
    deployment: Mapping[str, Any] | None,
    live_evidence: Mapping[str, Any] | None,
    consumer: str = "unspecified",
) -> dict[str, Any]:
    pack = build_runtime_truth_pack(
        pull_request=pull_request,
        deployment=deployment,
        live_evidence=live_evidence,
    )
    runtime_verified_count = len(pack["verification"]["runtime_verified"])
    runtime_unverified_count = len(pack["verification"]["runtime_unverified"])
    runtime_truth_objects = (
        [_stable_object(_runtime_truth_object(deployment=deployment, live_evidence=live_evidence).to_dict())]
        if runtime_verified_count
        else []
    )
    pr_objects = [_stable_object(obj) for obj in pack["objects"]]
    edges = _runtime_truth_edges(pr_objects=pr_objects, runtime_truth_objects=runtime_truth_objects)
    status = "pass" if runtime_verified_count else "pass_with_gaps"
    result = {
        "schema_version": "object_extraction_runtime_truth_preview.v1",
        "status": status,
        "consumer": public_safe_text(consumer, max_chars=80),
        "selected_strategy": "merge_ci_deploy_live_separation_v1",
        "production_mutation_performed": False,
        "objects": pr_objects,
        "runtime_truth_objects": runtime_truth_objects,
        "edges": edges,
        "pack_preview": {
            "route": pack["route"],
            "object_count": len(pack["objects"]),
            "lane_counts": _lane_counts(pack),
            "runtime_verified_count": runtime_verified_count,
            "runtime_unverified_count": runtime_unverified_count,
            "gaps": list(pack["gaps"]),
            "recommended_action_count": len(pack["recommended_actions"]),
        },
        "strategy_comparison": [
            {
                "strategy": "merge_ci_deploy_live_separation_v1",
                "scope": "runtime_evidence_mapping",
                "selected": True,
                "status": status,
                "object_count": len(pr_objects) + len(runtime_truth_objects),
                "edge_count": len(edges),
                "runtime_verified_count": runtime_verified_count,
                "runtime_unverified_count": runtime_unverified_count,
                "gaps": list(pack["gaps"]),
            },
            {
                "strategy": "merge_only_v1",
                "scope": "runtime_evidence_mapping",
                "selected": False,
                "status": "rejected",
                "object_count": len(pr_objects),
                "edge_count": 0,
                "runtime_verified_count": 0,
                "runtime_unverified_count": 0,
                "gaps": ["deploy_inferred_from_merge_forbidden"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "pr merge and deploy truth",
            "passes": True,
            "failures": [],
            "gaps": list(pack["gaps"]),
            "assertions": [
                "merge_does_not_imply_deploy",
                "runtime_verified_requires_live_evidence",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "RuntimeTruthExtractionPreview")
    return result


def run_preference_style_extraction_preview(
    *,
    memory_cards: list[Mapping[str, Any]],
    repository: str,
    current_request: str = "",
    current_files: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    preference_rules = preference_rule_cards_from_memory_cards(
        [dict(card) for card in memory_cards],
        current_request=current_request,
        current_files=current_files,
    )
    style_profile = repo_style_profile_from_memory_cards(
        list(memory_cards),
        repository=repository,
    )
    packs = build_agent_context_object_packs(
        documents=[],
        preferences=preference_rules,
        style_profile=style_profile,
        current_work=[],
        required_verification=[],
        guardrails=[],
    )
    preference_objects = [_stable_object(obj) for obj in packs["preferences"]["objects"]]
    style_objects = [_stable_object(obj) for obj in packs["style"]["objects"]]
    objects = [*preference_objects, *style_objects]
    source_evidence_refs = _preference_style_evidence_refs(preference_rules, style_profile)
    consumed_memory_ids = {
        str(item.get("memory_id") or "")
        for item in [*preference_rules, *style_profile.get("claims", [])]
        if str(item.get("memory_id") or "")
    }
    ignored_input_count = sum(
        1
        for card in memory_cards
        if str(card.get("memory_id") or "") not in consumed_memory_ids
    )
    gaps = [] if objects else ["preference_style_objects_empty"]
    result = {
        "schema_version": "object_extraction_preference_style_preview.v1",
        "status": "pass" if objects else "pass_with_gaps",
        "repository": public_safe_text(repository, max_chars=180),
        "selected_strategy": "memory_card_preference_style_v1",
        "production_mutation_performed": False,
        "preference_count": len(preference_rules),
        "style_claim_count": len(style_profile.get("claims", [])),
        "ignored_input_count": ignored_input_count,
        "objects": objects,
        "source_evidence_refs": source_evidence_refs,
        "pack_preview": {
            "preferences": {
                "route": packs["preferences"]["route"],
                "object_count": len(preference_objects),
                "gaps": list(packs["preferences"]["gaps"]),
            },
            "style": {
                "route": packs["style"]["route"],
                "object_count": len(style_objects),
                "gaps": list(packs["style"]["gaps"]),
            },
        },
        "strategy_comparison": [
            {
                "strategy": "memory_card_preference_style_v1",
                "scope": "style_preference_mapping",
                "selected": True,
                "status": "pass" if objects else "pass_with_gaps",
                "object_count": len(objects),
                "source_evidence_ref_count": len(source_evidence_refs),
                "gaps": gaps,
            },
            {
                "strategy": "raw_session_body_inference_v1",
                "scope": "style_preference_mapping",
                "selected": False,
                "status": "rejected",
                "object_count": 0,
                "source_evidence_ref_count": 0,
                "gaps": ["raw_body_inference_forbidden"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "style and artifact preference memory",
            "passes": bool(objects),
            "failures": [] if objects else ["preference_style_objects_empty"],
            "gaps": gaps,
            "assertions": [
                "preference_and_style_are_distinct_objects",
                "source_evidence_refs_without_raw_body",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "PreferenceStyleExtractionPreview")
    return result


def run_work_unit_extraction_preview(
    *,
    work_item: Mapping[str, Any],
    evidence_items: list[Mapping[str, Any]],
    repository: str,
) -> dict[str, Any]:
    evidence_refs = [_work_unit_evidence_ref(item).to_view() for item in evidence_items]
    evidence_ids = [item["evidence_id"] for item in evidence_refs]
    work_id = public_safe_text(str(work_item.get("work_id") or work_item.get("id") or work_item.get("title") or ""), max_chars=160)
    work_object = KnowledgeObjectEnvelope.from_parts(
        object_type="WorkUnit",
        natural_key=work_id,
        scope={"repository": public_safe_text(repository, max_chars=180)},
        title=str(work_item.get("title") or work_id or "WorkUnit"),
        summary=str(work_item.get("summary") or ""),
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="source_hash_verified" if evidence_refs else "unverified",
        review_state="needs_review",
        content_hash=hash_payload({"work_item": dict(work_item), "evidence_ids": evidence_ids}),
        evidence_refs=evidence_ids,
        confidence={"score": 0.7 if evidence_refs else 0.2, "basis": "grouped_work_evidence"},
        recommended_action="review",
        payload={
            "work_id": work_id,
            "status": public_safe_text(str(work_item.get("status") or "unknown"), max_chars=80),
            "evidence_count": len(evidence_refs),
        },
    )
    object_view = _stable_object(work_object.to_dict())
    edges = [
        _stable_edge(
            KnowledgeEdge.from_parts(
                edge_type="validated_by" if evidence["evidence_type"] == "test" else "supported_by_evidence",
                from_object_id=object_view["object_id"],
                to_object_id=f"evidence:{evidence['evidence_id']}",
                evidence_refs=[evidence["evidence_id"]],
                lifecycle_status="observed",
                authority_lane="candidate",
                verification_state=evidence["verification_state"],
                confidence={"score": 0.65, "basis": "work_unit_evidence_ref"},
                payload={"evidence_type": evidence["evidence_type"]},
            ).to_dict()
        )
        for evidence in evidence_refs
    ]
    gaps = [] if evidence_refs else ["work_unit_evidence_missing"]
    result = {
        "schema_version": "object_extraction_work_unit_preview.v1",
        "status": "pass" if evidence_refs else "pass_with_gaps",
        "repository": public_safe_text(repository, max_chars=180),
        "selected_strategy": "evidence_ref_work_unit_v1",
        "production_mutation_performed": False,
        "object": object_view,
        "evidence": evidence_refs,
        "edges": edges,
        "evidence_count": len(evidence_refs),
        "edge_count": len(edges),
        "gaps": gaps,
        "strategy_comparison": [
            {
                "strategy": "evidence_ref_work_unit_v1",
                "scope": "temporal_work_recall_mapping",
                "selected": True,
                "status": "pass" if evidence_refs else "pass_with_gaps",
                "object_count": 1,
                "evidence_count": len(evidence_refs),
                "edge_count": len(edges),
                "gaps": gaps,
            },
            {
                "strategy": "raw_transcript_summary_v1",
                "scope": "temporal_work_recall_mapping",
                "selected": False,
                "status": "rejected",
                "object_count": 0,
                "evidence_count": 0,
                "edge_count": 0,
                "gaps": ["raw_transcript_body_forbidden"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "temporal work recall",
            "passes": bool(evidence_refs),
            "failures": [] if evidence_refs else ["work_unit_evidence_missing"],
            "gaps": gaps,
            "assertions": [
                "work_unit_groups_session_pr_commit_test_evidence",
                "raw_transcript_body_not_returned",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "WorkUnitExtractionPreview")
    return result


def run_session_detail_extraction_preview(
    *,
    sessions: list[Mapping[str, Any]],
    repository: str,
) -> dict[str, Any]:
    objects = [_stable_object(_session_object(session, repository=repository).to_dict()) for session in sessions]
    evidence = _session_evidence_views(sessions)
    edges = _session_edges(sessions=sessions, objects=objects)
    gaps = _session_detail_gaps(sessions=sessions, evidence=evidence)
    status = "pass" if objects and not gaps else "pass_with_gaps"
    result = {
        "schema_version": "object_extraction_session_detail_preview.v1",
        "status": status,
        "repository": public_safe_text(repository, max_chars=180),
        "selected_strategy": "session_metadata_evidence_v1",
        "production_mutation_performed": False,
        "objects": objects,
        "evidence": evidence,
        "edges": edges,
        "object_count": len(objects),
        "edge_count": len(edges),
        "evidence_count": len(evidence),
        "gaps": gaps,
        "extraction_run": _session_extraction_run(
            sessions=sessions,
            object_count=len(objects),
            edge_count=len(edges),
            evidence_count=len(evidence),
            gaps=gaps,
        ),
        "strategy_comparison": [
            {
                "strategy": "session_metadata_evidence_v1",
                "scope": "session_metadata_evidence_mapping",
                "selected": True,
                "status": status,
                "object_count": len(objects),
                "edge_count": len(edges),
                "evidence_count": len(evidence),
                "gaps": gaps,
            },
            {
                "strategy": "raw_session_body_inference_v1",
                "scope": "session_metadata_evidence_mapping",
                "selected": False,
                "status": "rejected",
                "object_count": 0,
                "edge_count": 0,
                "evidence_count": 0,
                "gaps": ["raw_session_body_forbidden"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "session detail extraction",
            "passes": status == "pass",
            "failures": [] if status == "pass" else gaps or ["session_detail_extraction_empty"],
            "gaps": gaps,
            "assertions": [
                "session_objects_use_metadata_only",
                "raw_session_body_not_returned",
                "session_edges_preserve_evidence_refs",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "SessionDetailExtractionPreview")
    return result


def run_session_project_rollup_preview(
    *,
    sessions: list[Mapping[str, Any]],
    repository: str,
    branch: str = "",
    project: str = "",
    requesting_device_id_hash: str = "",
    scope: str = "all_devices",
) -> dict[str, Any]:
    safe_repository = public_safe_text(repository, max_chars=180)
    safe_branch = public_safe_text(branch, max_chars=180)
    safe_project = public_safe_text(project or repository, max_chars=120)
    requested_device = public_safe_text(requesting_device_id_hash, max_chars=180)
    safe_scope = scope if scope in {"all_devices", "same_device"} else "all_devices"
    visible_sessions = [
        session
        for session in sessions
        if safe_scope == "all_devices"
        or not requested_device
        or public_safe_text(str(session.get("device_id_hash") or ""), max_chars=180) == requested_device
    ]
    repository_object = _rollup_object(
        object_type="Repository",
        natural_key=safe_repository,
        scope={"project": safe_project},
        title=safe_repository or "Repository",
        summary="Repository scope for session rollup.",
        payload={"repository": safe_repository, "project": safe_project},
    )
    branch_object = _rollup_object(
        object_type="Branch",
        natural_key=f"{safe_repository}:{safe_branch}",
        scope={"project": safe_project, "repository": safe_repository},
        title=safe_branch or "Branch",
        summary="Branch scope for session rollup.",
        payload={"repository": safe_repository, "branch": safe_branch},
    )
    session_objects = [_stable_object(_session_object(session, repository=safe_repository).to_dict()) for session in visible_sessions]
    device_objects = [
        _rollup_object(
            object_type="Device",
            natural_key=device_id,
            scope={"project": safe_project},
            title=device_id,
            summary="Hashed device identity for session rollup.",
            payload={"device_id_hash": device_id},
        )
        for device_id in sorted(_device_counts(sessions))
    ]
    work_unit_objects = [
        _rollup_object(
            object_type="WorkUnit",
            natural_key=work_unit_id,
            scope={"project": safe_project, "repository": safe_repository},
            title=work_unit_id,
            summary="Work unit referenced by session metadata.",
            payload={"work_unit_id": work_unit_id},
        )
        for work_unit_id in sorted(_work_unit_ids(visible_sessions))
    ]
    objects = [repository_object, branch_object, *device_objects, *work_unit_objects, *session_objects]
    evidence = _session_evidence_views(visible_sessions)
    edges = _session_project_rollup_edges(
        visible_sessions=visible_sessions,
        session_objects=session_objects,
        repository_object=repository_object,
        branch_object=branch_object,
        device_objects=device_objects,
        work_unit_objects=work_unit_objects,
    )
    gaps = _session_project_rollup_gaps(
        sessions=sessions,
        visible_sessions=visible_sessions,
        scope=safe_scope,
        requesting_device_id_hash=requested_device,
    )
    status = "pass" if visible_sessions and not gaps else "pass_with_gaps"
    result = {
        "schema_version": "object_extraction_session_project_rollup_preview.v1",
        "status": status,
        "repository": safe_repository,
        "branch": safe_branch,
        "project": safe_project,
        "scope": safe_scope,
        "requesting_device_id_hash": requested_device,
        "selected_strategy": "session_device_project_rollup_v1",
        "production_mutation_performed": False,
        "objects": objects,
        "edges": edges,
        "evidence": evidence,
        "object_count": len(objects),
        "edge_count": len(edges),
        "evidence_count": len(evidence),
        "visible_session_count": len(visible_sessions),
        "all_device_session_count": len(sessions),
        "device_count": len(_device_counts(sessions)),
        "per_device_counts": _device_counts(sessions),
        "gaps": gaps,
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "temporal repo recall",
            "passes": status == "pass",
            "failures": [] if status == "pass" else gaps or ["session_project_rollup_empty"],
            "gaps": gaps,
            "assertions": [
                "same_device_and_all_device_views_are_distinct",
                "device_session_project_branch_work_unit_edges_present",
                "raw_host_path_and_transcript_not_returned",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "SessionProjectRollupPreview")
    return result


def run_pr_commit_extraction_preview(
    *,
    pull_request: Mapping[str, Any],
    commits: list[Mapping[str, Any]],
    test_runs: list[Mapping[str, Any]],
    repository: str,
) -> dict[str, Any]:
    pr_object = _stable_object(_pull_request_object(pull_request, repository=repository).to_dict())
    commit_objects = [_stable_object(_commit_object(item, repository=repository).to_dict()) for item in commits]
    test_objects = [_stable_object(_test_run_object(item, repository=repository).to_dict()) for item in test_runs]
    test_by_id = {
        str(item.get("test_id") or item.get("id") or item.get("ref") or ""): obj
        for item, obj in zip(test_runs, test_objects, strict=False)
    }
    edges = [
        _stable_edge(
            KnowledgeEdge.from_parts(
                edge_type="includes_commit",
                from_object_id=pr_object["object_id"],
                to_object_id=obj["object_id"],
                evidence_refs=obj.get("evidence_refs") or [],
                lifecycle_status="observed",
                authority_lane="candidate",
                verification_state=obj["verification_state"],
                confidence={"score": 0.68, "basis": "pr_commit_ref"},
                payload={"extractor": "pr_commit_detail"},
            ).to_dict()
        )
        for obj in commit_objects
    ]
    edges.extend(_commit_test_edges(commits=commits, commit_objects=commit_objects, test_by_id=test_by_id))
    missing_test_refs = _missing_commit_test_refs(commits=commits, test_by_id=test_by_id)
    gaps = ["commit_test_ref_missing"] if missing_test_refs else []
    status = "pass" if not gaps and pr_object and commit_objects else "pass_with_gaps"
    result = {
        "schema_version": "object_extraction_pr_commit_preview.v1",
        "status": status,
        "repository": public_safe_text(repository, max_chars=180),
        "selected_strategy": "pr_commit_ci_evidence_v1",
        "production_mutation_performed": False,
        "objects": [pr_object, *commit_objects, *test_objects],
        "edges": edges,
        "runtime_truth_objects": [],
        "object_counts": {
            "PullRequest": 1 if pr_object else 0,
            "Commit": len(commit_objects),
            "TestRun": len(test_objects),
        },
        "edge_count": len(edges),
        "gaps": gaps,
        "pack_preview": {
            "runtime_verified_count": 0,
            "runtime_unverified_count": 1 if _is_merged_pr(pull_request) else 0,
            "missing_test_ref_count": len(missing_test_refs),
            "production_mutation_performed": False,
        },
        "strategy_comparison": [
            {
                "strategy": "pr_commit_ci_evidence_v1",
                "scope": "pr_commit_test_provenance_mapping",
                "selected": True,
                "status": status,
                "object_count": 1 + len(commit_objects) + len(test_objects),
                "edge_count": len(edges),
                "gaps": gaps,
            },
            {
                "strategy": "merge_only_runtime_truth_v1",
                "scope": "runtime_evidence_mapping",
                "selected": False,
                "status": "rejected",
                "object_count": 0,
                "edge_count": 0,
                "gaps": ["runtime_truth_requires_live_evidence"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "pr commit and test provenance",
            "passes": status == "pass",
            "failures": [] if status == "pass" else gaps or ["pr_commit_evidence_missing"],
            "gaps": gaps,
            "assertions": [
                "pr_commit_test_objects_are_separate",
                "commit_test_edges_preserve_evidence",
                "merge_does_not_imply_runtime",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "PrCommitExtractionPreview")
    return result


def run_graph_search_projection_join_preview(
    *,
    objects: list[Mapping[str, Any]],
    projection_hits: list[Mapping[str, Any]],
    repository: str,
) -> dict[str, Any]:
    object_by_id = {
        str(obj.get("object_id") or ""): obj
        for obj in objects
        if str(obj.get("object_id") or "")
    }
    missing_target_refs = _missing_projection_target_refs(
        projection_hits=projection_hits,
        object_by_id=object_by_id,
    )
    matched_hits = [
        hit
        for hit in projection_hits
        if str(hit.get("object_ref") or hit.get("target_object_id") or "") in object_by_id
    ]
    projection_objects = [
        _stable_object(_projection_hit_object(hit, repository=repository).to_dict())
        for hit in matched_hits
    ]
    edges = _projection_join_edges(
        projection_hits=matched_hits,
        projection_objects=projection_objects,
    )
    gaps = _projection_join_gaps(
        projection_objects=projection_objects,
        missing_target_refs=missing_target_refs,
        projection_hits=projection_hits,
    )
    status = "pass" if projection_objects and not gaps else "pass_with_gaps"
    result = {
        "schema_version": "object_extraction_projection_join_preview.v1",
        "status": status,
        "repository": public_safe_text(repository, max_chars=180),
        "selected_strategy": "projection_join_read_only_v1",
        "production_mutation_performed": False,
        "canonical_authority_unchanged": True,
        "authority_promotion_performed": False,
        "objects": [_stable_object(obj) for obj in objects],
        "projection_objects": projection_objects,
        "edges": edges,
        "object_count": len(objects),
        "projection_object_count": len(projection_objects),
        "edge_count": len(edges),
        "gaps": gaps,
        "pack_preview": {
            "joined_object_count": len({edge["from_object_id"] for edge in edges}),
            "missing_target_count": len(missing_target_refs),
            "graph_hit_count": _projection_source_count(matched_hits, "graph"),
            "search_hit_count": _projection_source_count(matched_hits, "search"),
            "production_mutation_performed": False,
        },
        "strategy_comparison": [
            {
                "strategy": "projection_join_read_only_v1",
                "scope": "derived_projection_join_mapping",
                "selected": True,
                "status": status,
                "object_count": len(objects) + len(projection_objects),
                "edge_count": len(edges),
                "gaps": gaps,
            },
            {
                "strategy": "projection_as_authority_v1",
                "scope": "authority_mapping",
                "selected": False,
                "status": "rejected",
                "object_count": 0,
                "edge_count": 0,
                "gaps": ["derived_projection_cannot_become_canonical_authority"],
            },
        ],
        "evaluator_report": {
            "schema_version": "object_extraction_evaluator_report.v1",
            "golden_query_slice": "graph/search projection object join",
            "passes": status == "pass",
            "failures": [] if status == "pass" else gaps or ["projection_join_missing"],
            "gaps": gaps,
            "assertions": [
                "projection_hits_stay_derived_projection",
                "projection_edges_do_not_promote_authority",
                "missing_targets_report_gaps",
                "production_mutation_performed_false",
            ],
        },
    }
    ensure_public_safe(result, "GraphSearchProjectionJoinPreview")
    return result


def run_extraction_evaluator_suite_preview(
    *,
    evaluation_reports: list[Mapping[str, Any]],
    variance_samples: list[Mapping[str, Any]],
    suite_name: str,
) -> dict[str, Any]:
    deterministic = _deterministic_fixture_check(evaluation_reports)
    golden = _golden_query_check(evaluation_reports)
    strategy = _strategy_comparison_check(evaluation_reports)
    variance = _variance_check(variance_samples)
    model_prompt = _model_prompt_comparison_check(evaluation_reports)
    failures = [
        *deterministic["failures"],
        *golden["failures"],
        *strategy["failures"],
        *variance["failures"],
        *model_prompt["failures"],
    ]
    gaps = list(dict.fromkeys(failures))
    result = {
        "schema_version": "object_extraction_evaluator_suite.v1",
        "status": "pass" if not failures else "pass_with_gaps",
        "suite_name": public_safe_text(suite_name, max_chars=120),
        "production_mutation_performed": False,
        "coverage": {
            "deterministic_fixture_checks": "pass" if deterministic["passes"] else "fail",
            "golden_query_checks": "pass" if golden["passes"] else "fail",
            "strategy_comparison_checks": "pass" if strategy["passes"] else "fail",
            "variance_checks": "pass" if variance["passes"] else "fail",
            "model_prompt_comparison": model_prompt["status"],
        },
        "deterministic_fixture": deterministic,
        "golden_query": golden,
        "strategy_comparison": strategy,
        "variance": variance,
        "model_prompt_comparison": model_prompt,
        "failures": failures,
        "gaps": gaps,
    }
    ensure_public_safe(result, "ExtractionEvaluatorSuitePreview")
    return result


def _blocked_preview(
    *,
    bundle: Mapping[str, Any],
    project: str,
    storage_mode: str,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "object_extraction_preview.v1",
        "status": "blocked",
        "project": public_safe_text(project, max_chars=120),
        "extractor": "reference_corpus_manifest",
        "extractor_version": "0.1",
        "storage_mode": public_safe_text(storage_mode, max_chars=80),
        "production_mutation_performed": False,
        "objects": [],
        "edges": [],
        "chunk_preview": [],
        "object_count": 0,
        "edge_count": 0,
        "chunk_preview_count": 0,
        "strategy_comparison": _strategy_comparison(
            object_count=0,
            edge_count=0,
            chunk_preview_count=0,
            storage_mode=storage_mode,
            blocked=True,
        ),
        "extraction_run": _preview_extraction_run(
            bundle["extraction_run"],
            object_count=0,
            edge_count=0,
            chunk_preview_count=0,
            status="blocked",
        ),
        "evaluator_report": _evaluator_report(status="blocked", gaps=gaps),
        "gaps": gaps,
    }


def _corpus_object(corpus: Mapping[str, Any], *, project: str) -> KnowledgeObjectEnvelope:
    corpus_id = public_safe_text(str(corpus.get("corpus_id") or ""), max_chars=160)
    content_hash = str(corpus.get("manifest_ref") or hash_payload(corpus))
    return KnowledgeObjectEnvelope.from_parts(
        object_type="ReferenceCorpus",
        natural_key=corpus_id,
        scope={"project": project},
        title=str(corpus.get("name") or corpus_id),
        summary=f"Reference corpus with {corpus.get('source_count', 0)} sources.",
        lifecycle_status="extracted",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        review_state="not_required",
        content_hash=content_hash,
        confidence={"score": 0.65, "basis": "manifest_hash"},
        recommended_action="use_as_reference",
        freshness={"policy": corpus.get("freshness_policy", "")},
        payload={
            "corpus_id": corpus_id,
            "storage_mode": public_safe_text(str(corpus.get("storage_mode") or ""), max_chars=80),
            "source_count": int(corpus.get("source_count") or 0),
        },
    )


def _stable_object(obj: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "object_id",
        "object_type",
        "scope",
        "title",
        "summary",
        "lifecycle_status",
        "authority_lane",
        "verification_state",
        "review_state",
        "content_hash",
        "source_refs",
        "evidence_refs",
        "edge_refs",
        "confidence",
        "recommended_action",
        "freshness",
        "privacy_class",
        "payload",
    }
    return {key: obj[key] for key in obj if key in allowed}


def _stable_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "edge_id",
        "edge_type",
        "from_object_id",
        "to_object_id",
        "evidence_refs",
        "lifecycle_status",
        "authority_lane",
        "verification_state",
        "direction",
        "confidence",
        "freshness",
        "payload",
    }
    return {key: edge[key] for key in edge if key in allowed}


def _deterministic_fixture_check(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    if not reports:
        failures.append("evaluation_reports_empty")
    if any(bool(report.get("production_mutation_performed")) for report in reports):
        failures.append("production_mutation_claimed")
    return {
        "passes": not failures,
        "checked_count": len(reports),
        "failures": failures,
        "assertions": [
            "reports_exist",
            "production_mutation_performed_false",
            "public_safe_report_shape",
        ],
    }


def _golden_query_check(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    checked_count = 0
    slices: list[str] = []
    for report in reports:
        evaluator = report.get("evaluator_report") if isinstance(report.get("evaluator_report"), Mapping) else {}
        if not evaluator:
            failures.append("evaluator_report_missing")
            continue
        checked_count += 1
        golden_slice = public_safe_text(str(evaluator.get("golden_query_slice") or "unknown"), max_chars=160)
        slices.append(golden_slice)
        if not bool(evaluator.get("passes")):
            failures.append(f"golden_query_failed:{golden_slice}")
    if not reports:
        failures.append("evaluation_reports_empty")
    return {
        "passes": not failures,
        "checked_count": checked_count,
        "golden_query_slices": slices,
        "failures": failures,
    }


def _strategy_comparison_check(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    checked_count = 0
    selected_count = 0
    for report in reports:
        comparisons = report.get("strategy_comparison") if isinstance(report.get("strategy_comparison"), list) else []
        if not comparisons:
            failures.append("strategy_comparison_missing")
            continue
        checked_count += 1
        if any(bool(item.get("selected")) for item in comparisons if isinstance(item, Mapping)):
            selected_count += 1
        else:
            failures.append("selected_strategy_missing")
    if not reports:
        failures.append("evaluation_reports_empty")
    return {
        "passes": not failures,
        "checked_count": checked_count,
        "selected_strategy_count": selected_count,
        "failures": failures,
    }


def _variance_check(samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    output_hashes = [_report_output_hash(sample) for sample in samples]
    unique_output_hash_count = len(set(output_hashes))
    failures: list[str] = []
    if not samples:
        failures.append("variance_samples_empty")
    elif unique_output_hash_count > 1:
        failures.append("variance_detected")
    return {
        "passes": not failures,
        "sample_count": len(samples),
        "unique_output_hash_count": unique_output_hash_count,
        "failures": failures,
    }


def _model_prompt_comparison_check(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    model_call_count = sum(_report_model_call_count(report) for report in reports)
    if model_call_count == 0:
        return {
            "passes": True,
            "status": "not_applicable_no_llm",
            "model_call_count": 0,
            "failures": [],
            "gaps": [],
        }
    return {
        "passes": False,
        "status": "missing_model_prompt_comparison",
        "model_call_count": model_call_count,
        "failures": ["model_prompt_comparison_missing"],
        "gaps": ["model_prompt_comparison_missing"],
    }


def _report_output_hash(report: Mapping[str, Any]) -> str:
    return hash_payload(
        {
            "schema_version": report.get("schema_version"),
            "status": report.get("status"),
            "selected_strategy": report.get("selected_strategy"),
            "object_count": report.get("object_count"),
            "edge_count": report.get("edge_count"),
            "chunk_preview_count": report.get("chunk_preview_count"),
            "evidence_count": report.get("evidence_count"),
            "projection_object_count": report.get("projection_object_count"),
            "objects": report.get("objects"),
            "edges": report.get("edges"),
            "chunk_preview": report.get("chunk_preview"),
            "gaps": report.get("gaps"),
            "evaluator_report": report.get("evaluator_report"),
        }
    )


def _report_model_call_count(report: Mapping[str, Any]) -> int:
    extraction_run = report.get("extraction_run") if isinstance(report.get("extraction_run"), Mapping) else {}
    cost = extraction_run.get("cost_estimate") if isinstance(extraction_run.get("cost_estimate"), Mapping) else {}
    try:
        return int(cost.get("model_calls") or 0)
    except (TypeError, ValueError):
        return 0


def _lane_counts(pack: Mapping[str, Any]) -> dict[str, int]:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    return {
        str(lane): len(items) if isinstance(items, list) else 0
        for lane, items in sorted(lanes.items())
    }


def _runtime_truth_object(
    *,
    deployment: Mapping[str, Any] | None,
    live_evidence: Mapping[str, Any] | None,
) -> KnowledgeObjectEnvelope:
    evidence_id = public_safe_text(str((live_evidence or {}).get("evidence_id") or ""), max_chars=160)
    target_ref = public_safe_text(str((deployment or {}).get("target") or "deployment"), max_chars=120)
    content_hash = hash_payload({"target_ref": target_ref, "evidence_id": evidence_id})
    return KnowledgeObjectEnvelope.from_parts(
        object_type="RuntimeTruth",
        natural_key=f"{target_ref}:{evidence_id}",
        scope={"project": "neurons", "target_ref": target_ref},
        title=f"Runtime truth for {target_ref}",
        summary=str((live_evidence or {}).get("summary") or "Runtime evidence verified."),
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="runtime_verified",
        review_state="needs_review",
        content_hash=content_hash,
        evidence_refs=[evidence_id],
        confidence={"score": 0.75, "basis": "runtime_verified_live_evidence"},
        recommended_action="review",
        payload={
            "target_ref": target_ref,
            "claim": "runtime_verified",
        },
    )


def _repo_document_edges(
    *,
    documents: list[Mapping[str, Any]],
    objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    object_by_path = {
        str(obj.get("payload", {}).get("path_ref") or ""): obj
        for obj in objects
        if str(obj.get("payload", {}).get("path_ref") or "")
    }
    edges: list[dict[str, Any]] = []
    for doc in documents:
        path = public_safe_text(str(doc.get("path") or doc.get("document_path") or ""), max_chars=240)
        obj = object_by_path.get(path)
        if not obj:
            continue
        superseded_by = public_safe_text(str(doc.get("superseded_by") or ""), max_chars=240)
        superseding_obj = object_by_path.get(superseded_by)
        if superseding_obj:
            edge = KnowledgeEdge.from_parts(
                edge_type="supersedes",
                from_object_id=superseding_obj["object_id"],
                to_object_id=obj["object_id"],
                evidence_refs=obj.get("evidence_refs") or [],
                lifecycle_status="proposed",
                authority_lane="proposal_only",
                verification_state="unverified",
                confidence={"score": 0.65, "basis": "repo_document_superseded_by"},
                payload={"relationship": "supersedes", "path_ref": path},
            )
            edges.append(_stable_edge(edge.to_dict()))
        if bool(doc.get("requires_evidence")):
            gap_ref = f"evidence_gap:{hash_payload({'path': path})[7:19]}"
            edge = KnowledgeEdge.from_parts(
                edge_type="requires_evidence",
                from_object_id=obj["object_id"],
                to_object_id=gap_ref,
                evidence_refs=obj.get("evidence_refs") or [],
                lifecycle_status="proposed",
                authority_lane="proposal_only",
                verification_state="unverified",
                confidence={"score": 0.6, "basis": "repo_document_requires_evidence"},
                payload={"relationship": "requires_evidence", "path_ref": path},
            )
            edges.append(_stable_edge(edge.to_dict()))
    return edges


def _repo_document_extraction_run(
    *,
    documents: list[Mapping[str, Any]],
    object_count: int,
    edge_count: int,
    evidence_count: int,
    gaps: list[str],
) -> dict[str, Any]:
    input_hash = hash_payload(documents)
    return {
        "schema_version": "object_extraction_run_preview.v1",
        "run_id": f"run:repo-document:{input_hash[7:19]}",
        "status": "completed" if object_count else "blocked",
        "input_hash": input_hash,
        "extractor": "repo_document_cleanup",
        "extractor_version": "0.1",
        "output_object_count": object_count,
        "output_edge_count": edge_count,
        "output_evidence_count": evidence_count,
        "quality_metrics": {
            "public_safe_scan": "pass",
            "authority_lane_separation": "pass",
            "missing_evidence_gap_count": sum(1 for gap in gaps if "evidence" in gap),
        },
        "cost_estimate": {
            "model_calls": 0,
            "estimated_usd": 0.0,
        },
        "speed": {
            "runtime_class": "local_deterministic_fixture",
            "external_network_calls": 0,
        },
        "token_budget": {
            "llm_tokens": 0,
            "budget_required": False,
        },
        "debug_trace_available": True,
        "debug_trace": [
            "load_document_inventory",
            "map_repo_documents",
            "build_cleanup_edges",
            "public_safe_preview",
        ],
    }


def _session_object(
    session: Mapping[str, Any],
    *,
    repository: str,
) -> KnowledgeObjectEnvelope:
    session_ref = public_safe_text(
        str(session.get("session_id_hash") or session.get("session_ref") or session.get("id") or ""),
        max_chars=180,
    )
    device_ref = public_safe_text(str(session.get("device_id_hash") or ""), max_chars=180)
    provider = public_safe_text(str(session.get("provider") or "unknown"), max_chars=80)
    summary = public_safe_text(str(session.get("summary") or "Session metadata."), max_chars=360)
    work_unit_id = public_safe_text(str(session.get("work_unit_id") or ""), max_chars=180)
    evidence_refs = _session_evidence_ids(session)
    return KnowledgeObjectEnvelope.from_parts(
        object_type="Session",
        natural_key=session_ref,
        scope={
            "repository": public_safe_text(repository, max_chars=180),
            "device_id_hash": device_ref,
        },
        title=session_ref or "Session",
        summary=summary,
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="source_hash_verified" if evidence_refs else "unverified",
        review_state="needs_review",
        content_hash=hash_payload(
            {
                "session_ref": session_ref,
                "device_ref": device_ref,
                "provider": provider,
                "summary": summary,
                "work_unit_id": work_unit_id,
                "evidence_refs": evidence_refs,
            }
        ),
        evidence_refs=evidence_refs,
        confidence={"score": 0.68 if evidence_refs else 0.25, "basis": "session_metadata_evidence"},
        recommended_action="review",
        payload={
            "session_ref": session_ref,
            "provider": provider,
            "work_unit_id": work_unit_id,
            "evidence_count": len(evidence_refs),
            "raw_body_returnable": False,
        },
    )


def _session_evidence_views(sessions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    evidence: list[dict[str, Any]] = []
    for session in sessions:
        for ref in _session_evidence_ids(session):
            if ref in seen:
                continue
            seen.add(ref)
            ev = EvidenceRef.from_parts(
                evidence_type="session_evidence",
                authority_lane="candidate",
                verification_state="source_hash_verified",
                locator={"kind": "evidence_ref", "value": ref},
                content_hash=hash_payload({"session_evidence_ref": ref}),
                summary=f"Session evidence reference {ref}.",
                producer={"extractor": "session_detail"},
            )
            evidence.append(ev.to_view())
    return evidence


def _session_edges(
    *,
    sessions: list[Mapping[str, Any]],
    objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for session, obj in zip(sessions, objects, strict=False):
        work_unit_id = public_safe_text(str(session.get("work_unit_id") or ""), max_chars=180)
        if work_unit_id:
            edge = KnowledgeEdge.from_parts(
                edge_type="part_of_work_unit",
                from_object_id=obj["object_id"],
                to_object_id=f"work_unit:{hash_payload({'work_unit_id': work_unit_id})[7:19]}",
                evidence_refs=obj.get("evidence_refs") or [],
                lifecycle_status="observed",
                authority_lane="candidate",
                verification_state=obj["verification_state"],
                confidence={"score": 0.66, "basis": "session_work_unit_ref"},
                payload={"work_unit_id": work_unit_id},
            )
            edges.append(_stable_edge(edge.to_dict()))
        for ref in _session_evidence_ids(session):
            edge = KnowledgeEdge.from_parts(
                edge_type="supported_by_evidence",
                from_object_id=obj["object_id"],
                to_object_id=f"evidence:{ref}",
                evidence_refs=[ref],
                lifecycle_status="observed",
                authority_lane="candidate",
                verification_state="source_hash_verified",
                confidence={"score": 0.65, "basis": "session_evidence_ref"},
                payload={"evidence_ref": ref},
            )
            edges.append(_stable_edge(edge.to_dict()))
    return edges


def _rollup_object(
    *,
    object_type: str,
    natural_key: str,
    scope: Mapping[str, Any],
    title: str,
    summary: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _stable_object(
        KnowledgeObjectEnvelope.from_parts(
            object_type=object_type,
            natural_key=natural_key or object_type,
            scope=scope,
            title=title or object_type,
            summary=summary,
            lifecycle_status="observed",
            authority_lane="candidate",
            verification_state="source_hash_verified",
            review_state="needs_review",
            content_hash=hash_payload({"object_type": object_type, "natural_key": natural_key, "scope": dict(scope)}),
            evidence_refs=[],
            confidence={"score": 0.6, "basis": "session_rollup_metadata"},
            recommended_action="review",
            payload=dict(payload),
        ).to_dict()
    )


def _device_counts(sessions: list[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session in sessions:
        device_id = public_safe_text(str(session.get("device_id_hash") or "device:unknown"), max_chars=180)
        counts[device_id] = counts.get(device_id, 0) + 1
    return dict(sorted(counts.items()))


def _work_unit_ids(sessions: list[Mapping[str, Any]]) -> set[str]:
    return {
        public_safe_text(str(session.get("work_unit_id") or ""), max_chars=180)
        for session in sessions
        if str(session.get("work_unit_id") or "")
    }


def _session_project_rollup_edges(
    *,
    visible_sessions: list[Mapping[str, Any]],
    session_objects: list[dict[str, Any]],
    repository_object: dict[str, Any],
    branch_object: dict[str, Any],
    device_objects: list[dict[str, Any]],
    work_unit_objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = [
        _rollup_edge(
            "repository_has_branch",
            repository_object["object_id"],
            branch_object["object_id"],
            [],
            {"repository": repository_object["title"], "branch": branch_object["title"]},
        )
    ]
    devices_by_id = {str(obj.get("payload", {}).get("device_id_hash") or ""): obj for obj in device_objects}
    work_units_by_id = {str(obj.get("payload", {}).get("work_unit_id") or ""): obj for obj in work_unit_objects}
    for session, session_obj in zip(visible_sessions, session_objects, strict=False):
        evidence_refs = [str(item) for item in session_obj.get("evidence_refs") or []]
        device_id = public_safe_text(str(session.get("device_id_hash") or "device:unknown"), max_chars=180)
        device_obj = devices_by_id.get(device_id)
        if device_obj is not None:
            edges.append(_rollup_edge("session_on_device", session_obj["object_id"], device_obj["object_id"], evidence_refs, {"device_id_hash": device_id}))
        edges.append(_rollup_edge("session_in_repository", session_obj["object_id"], repository_object["object_id"], evidence_refs, {"repository": repository_object["title"]}))
        edges.append(_rollup_edge("session_on_branch", session_obj["object_id"], branch_object["object_id"], evidence_refs, {"branch": branch_object["title"]}))
        work_unit_id = public_safe_text(str(session.get("work_unit_id") or ""), max_chars=180)
        work_unit_obj = work_units_by_id.get(work_unit_id)
        if work_unit_obj is not None:
            edges.append(_rollup_edge("part_of_work_unit", session_obj["object_id"], work_unit_obj["object_id"], evidence_refs, {"work_unit_id": work_unit_id}))
    return edges


def _rollup_edge(
    edge_type: str,
    from_object_id: str,
    to_object_id: str,
    evidence_refs: list[str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    verification_state = "source_hash_verified" if evidence_refs else "unverified"
    return _stable_edge(
        KnowledgeEdge.from_parts(
            edge_type=edge_type,
            from_object_id=from_object_id,
            to_object_id=to_object_id,
            evidence_refs=evidence_refs,
            lifecycle_status="observed",
            authority_lane="candidate",
            verification_state=verification_state,
            confidence={"score": 0.62, "basis": "session_rollup_metadata"},
            payload=payload,
        ).to_dict()
    )


def _session_project_rollup_gaps(
    *,
    sessions: list[Mapping[str, Any]],
    visible_sessions: list[Mapping[str, Any]],
    scope: str,
    requesting_device_id_hash: str,
) -> list[str]:
    gaps: list[str] = []
    if not sessions:
        gaps.append("session_objects_empty")
    if scope == "same_device" and not requesting_device_id_hash:
        gaps.append("requesting_device_required")
    if sessions and not visible_sessions:
        gaps.append("visible_sessions_empty")
    if any(_session_has_raw_body(session) for session in sessions):
        gaps.append("raw_session_body_ignored")
    return gaps


def _session_detail_gaps(
    *,
    sessions: list[Mapping[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[str]:
    gaps: list[str] = []
    if any(_session_has_raw_body(session) for session in sessions):
        gaps.append("raw_session_body_ignored")
    if sessions and not evidence:
        gaps.append("session_evidence_missing")
    if not sessions:
        gaps.append("session_objects_empty")
    return gaps


def _session_extraction_run(
    *,
    sessions: list[Mapping[str, Any]],
    object_count: int,
    edge_count: int,
    evidence_count: int,
    gaps: list[str],
) -> dict[str, Any]:
    safe_inputs = [
        {
            "session_id_hash": public_safe_text(
                str(session.get("session_id_hash") or session.get("session_ref") or session.get("id") or ""),
                max_chars=180,
            ),
            "device_id_hash": public_safe_text(str(session.get("device_id_hash") or ""), max_chars=180),
            "provider": public_safe_text(str(session.get("provider") or ""), max_chars=80),
            "summary": public_safe_text(str(session.get("summary") or ""), max_chars=360),
            "work_unit_id": public_safe_text(str(session.get("work_unit_id") or ""), max_chars=180),
            "evidence_refs": _session_evidence_ids(session),
        }
        for session in sessions
    ]
    input_hash = hash_payload(safe_inputs)
    return {
        "schema_version": "object_extraction_run_preview.v1",
        "run_id": f"run:session-detail:{input_hash[7:19]}",
        "status": "completed" if object_count else "blocked",
        "input_hash": input_hash,
        "extractor": "session_detail",
        "extractor_version": "0.1",
        "output_object_count": object_count,
        "output_edge_count": edge_count,
        "output_evidence_count": evidence_count,
        "quality_metrics": {
            "public_safe_scan": "pass",
            "raw_body_return": "denied",
            "gap_count": len(gaps),
        },
        "cost_estimate": {
            "model_calls": 0,
            "estimated_usd": 0.0,
        },
        "speed": {
            "runtime_class": "local_deterministic_fixture",
            "external_network_calls": 0,
        },
        "token_budget": {
            "llm_tokens": 0,
            "budget_required": False,
        },
        "debug_trace_available": True,
        "debug_trace": [
            "load_session_metadata",
            "drop_raw_body_fields",
            "map_session_objects",
            "build_session_edges",
            "public_safe_preview",
        ],
    }


def _session_evidence_ids(session: Mapping[str, Any]) -> list[str]:
    return [
        public_safe_text(str(ref or ""), max_chars=180)
        for ref in session.get("evidence_refs") or []
        if public_safe_text(str(ref or ""), max_chars=180)
    ]


def _session_has_raw_body(session: Mapping[str, Any]) -> bool:
    raw_keys = {"raw_transcript", "transcript", "raw_body", "body"}
    return any(key in session and bool(session.get(key)) for key in raw_keys)


def _runtime_truth_edges(
    *,
    pr_objects: list[dict[str, Any]],
    runtime_truth_objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not pr_objects or not runtime_truth_objects:
        return []
    pr = pr_objects[0]
    runtime_truth = runtime_truth_objects[0]
    evidence_refs = runtime_truth.get("evidence_refs") or []
    edge = KnowledgeEdge.from_parts(
        edge_type="validated_by",
        from_object_id=pr["object_id"],
        to_object_id=runtime_truth["object_id"],
        evidence_refs=evidence_refs,
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="runtime_verified",
        confidence={"score": 0.75, "basis": "live_runtime_evidence"},
        payload={"claim": "deploy_requires_live_evidence"},
    )
    return [_stable_edge(edge.to_dict())]


def _pull_request_object(
    pull_request: Mapping[str, Any],
    *,
    repository: str,
) -> KnowledgeObjectEnvelope:
    pr_id = public_safe_text(
        str(pull_request.get("pr_id") or pull_request.get("id") or pull_request.get("number") or ""),
        max_chars=160,
    )
    title = public_safe_text(
        str(pull_request.get("title") or pr_id or "PullRequest"),
        max_chars=240,
    )
    state = public_safe_text(str(pull_request.get("state") or "unknown"), max_chars=80)
    merge_commit = public_safe_text(str(pull_request.get("merge_commit") or ""), max_chars=80)
    evidence_refs = [ref for ref in [pr_id, merge_commit] if ref]
    return KnowledgeObjectEnvelope.from_parts(
        object_type="PullRequest",
        natural_key=pr_id,
        scope={"repository": public_safe_text(repository, max_chars=180)},
        title=title,
        summary=f"Pull request {pr_id} is {state}.",
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="source_hash_verified",
        review_state="needs_review",
        content_hash=hash_payload(
            {
                "pr_id": pr_id,
                "state": state,
                "merge_commit": merge_commit,
                "head_ref": pull_request.get("head_ref") or "",
            }
        ),
        evidence_refs=evidence_refs,
        confidence={"score": 0.7, "basis": "pr_metadata"},
        recommended_action="review",
        payload={
            "pr_id": pr_id,
            "state": state,
            "merged": _is_merged_pr(pull_request),
        },
    )


def _commit_object(
    commit: Mapping[str, Any],
    *,
    repository: str,
) -> KnowledgeObjectEnvelope:
    sha = public_safe_text(
        str(commit.get("sha") or commit.get("commit") or commit.get("id") or ""),
        max_chars=80,
    )
    title = public_safe_text(str(commit.get("title") or sha or "Commit"), max_chars=240)
    test_refs = [public_safe_text(str(ref or ""), max_chars=160) for ref in commit.get("test_refs") or []]
    evidence_refs = [sha, *[ref for ref in test_refs if ref]]
    return KnowledgeObjectEnvelope.from_parts(
        object_type="Commit",
        natural_key=sha,
        scope={"repository": public_safe_text(repository, max_chars=180)},
        title=title,
        summary=title,
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="source_hash_verified",
        review_state="needs_review",
        content_hash=hash_payload({"sha": sha, "title": title, "test_refs": test_refs}),
        evidence_refs=evidence_refs,
        confidence={"score": 0.68, "basis": "commit_metadata"},
        recommended_action="review",
        payload={
            "sha": sha,
            "test_ref_count": len(test_refs),
        },
    )


def _test_run_object(
    test_run: Mapping[str, Any],
    *,
    repository: str,
) -> KnowledgeObjectEnvelope:
    test_id = public_safe_text(
        str(test_run.get("test_id") or test_run.get("id") or test_run.get("ref") or ""),
        max_chars=160,
    )
    status = public_safe_text(str(test_run.get("status") or "unknown"), max_chars=80)
    summary = public_safe_text(str(test_run.get("summary") or test_id or "Test run"), max_chars=360)
    verification_state = "test_verified" if status == "pass" else "unverified"
    return KnowledgeObjectEnvelope.from_parts(
        object_type="TestRun",
        natural_key=test_id,
        scope={"repository": public_safe_text(repository, max_chars=180)},
        title=test_id,
        summary=summary,
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state=verification_state,
        review_state="needs_review",
        content_hash=hash_payload({"test_id": test_id, "status": status, "summary": summary}),
        evidence_refs=[test_id],
        confidence={"score": 0.74 if status == "pass" else 0.35, "basis": "test_run_metadata"},
        recommended_action="review",
        payload={
            "test_id": test_id,
            "status": status,
        },
    )


def _commit_test_edges(
    *,
    commits: list[Mapping[str, Any]],
    commit_objects: list[dict[str, Any]],
    test_by_id: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for commit, commit_object in zip(commits, commit_objects, strict=False):
        for test_ref in commit.get("test_refs") or []:
            safe_test_ref = public_safe_text(str(test_ref or ""), max_chars=160)
            test_object = test_by_id.get(safe_test_ref)
            if not test_object:
                continue
            edge = KnowledgeEdge.from_parts(
                edge_type="validated_by",
                from_object_id=commit_object["object_id"],
                to_object_id=test_object["object_id"],
                evidence_refs=[safe_test_ref],
                lifecycle_status="observed",
                authority_lane="candidate",
                verification_state=test_object["verification_state"],
                confidence={"score": 0.74, "basis": "commit_test_ref"},
                payload={"extractor": "pr_commit_detail"},
            )
            edges.append(_stable_edge(edge.to_dict()))
    return edges


def _missing_commit_test_refs(
    *,
    commits: list[Mapping[str, Any]],
    test_by_id: Mapping[str, dict[str, Any]],
) -> list[str]:
    missing: list[str] = []
    for commit in commits:
        for test_ref in commit.get("test_refs") or []:
            safe_test_ref = public_safe_text(str(test_ref or ""), max_chars=160)
            if safe_test_ref and safe_test_ref not in test_by_id:
                missing.append(safe_test_ref)
    return missing


def _is_merged_pr(pull_request: Mapping[str, Any]) -> bool:
    state = str(pull_request.get("state") or "").lower()
    return bool(pull_request.get("merged")) or state == "merged" or bool(pull_request.get("merge_commit"))


def _projection_hit_object(
    hit: Mapping[str, Any],
    *,
    repository: str,
) -> KnowledgeObjectEnvelope:
    hit_id = public_safe_text(str(hit.get("hit_id") or hit.get("id") or ""), max_chars=180)
    source = public_safe_text(str(hit.get("source") or "projection"), max_chars=80)
    object_ref = public_safe_text(str(hit.get("object_ref") or hit.get("target_object_id") or ""), max_chars=180)
    summary = public_safe_text(str(hit.get("summary") or "Derived projection hit."), max_chars=360)
    score = _safe_score(hit.get("score"))
    return KnowledgeObjectEnvelope.from_parts(
        object_type="ProjectionHit",
        natural_key=hit_id,
        scope={
            "repository": public_safe_text(repository, max_chars=180),
            "projection_source": source,
        },
        title=hit_id or f"{source} projection hit",
        summary=summary,
        lifecycle_status="observed",
        authority_lane="derived_projection",
        verification_state="unverified",
        review_state="not_required",
        content_hash=hash_payload(
            {
                "hit_id": hit_id,
                "source": source,
                "object_ref": object_ref,
                "summary": summary,
                "score": score,
            }
        ),
        evidence_refs=[hit_id] if hit_id else [],
        confidence={"score": score, "basis": "derived_projection_score"},
        recommended_action="join_as_context_only",
        payload={
            "hit_id": hit_id,
            "source": source,
            "object_ref": object_ref,
        },
    )


def _projection_join_edges(
    *,
    projection_hits: list[Mapping[str, Any]],
    projection_objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for hit, projection_object in zip(projection_hits, projection_objects, strict=False):
        object_ref = public_safe_text(str(hit.get("object_ref") or hit.get("target_object_id") or ""), max_chars=180)
        hit_id = public_safe_text(str(hit.get("hit_id") or hit.get("id") or ""), max_chars=180)
        edge = KnowledgeEdge.from_parts(
            edge_type="projection_join",
            from_object_id=object_ref,
            to_object_id=projection_object["object_id"],
            evidence_refs=[hit_id] if hit_id else [],
            lifecycle_status="observed",
            authority_lane="derived_projection",
            verification_state="unverified",
            confidence={"score": _safe_score(hit.get("score")), "basis": "projection_hit_similarity"},
            payload={
                "projection_source": public_safe_text(str(hit.get("source") or "projection"), max_chars=80),
                "authority_effect": "none",
            },
        )
        edges.append(_stable_edge(edge.to_dict()))
    return edges


def _missing_projection_target_refs(
    *,
    projection_hits: list[Mapping[str, Any]],
    object_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    missing: list[str] = []
    for hit in projection_hits:
        object_ref = public_safe_text(str(hit.get("object_ref") or hit.get("target_object_id") or ""), max_chars=180)
        if object_ref and object_ref not in object_by_id:
            missing.append(object_ref)
    return missing


def _projection_join_gaps(
    *,
    projection_objects: list[dict[str, Any]],
    missing_target_refs: list[str],
    projection_hits: list[Mapping[str, Any]],
) -> list[str]:
    gaps: list[str] = []
    if missing_target_refs:
        gaps.append("projection_join_target_missing")
    if projection_hits and not projection_objects and not missing_target_refs:
        gaps.append("projection_join_empty")
    if not projection_hits:
        gaps.append("projection_hits_empty")
    return gaps


def _projection_source_count(projection_hits: list[Mapping[str, Any]], source: str) -> int:
    return sum(1 for hit in projection_hits if str(hit.get("source") or "").lower() == source)


def _safe_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return score


def _preference_style_evidence_refs(
    preference_rules: list[Mapping[str, Any]],
    style_profile: Mapping[str, Any],
) -> list[str]:
    refs: list[str] = []
    for item in [*preference_rules, *style_profile.get("claims", [])]:
        for ref in item.get("evidence_refs") or []:
            safe_ref = public_safe_text(str(ref or ""), max_chars=180)
            if safe_ref and safe_ref not in refs:
                refs.append(safe_ref)
    return refs


def _work_unit_evidence_ref(item: Mapping[str, Any]) -> EvidenceRef:
    evidence_type = public_safe_text(str(item.get("kind") or "evidence"), max_chars=80)
    ref = public_safe_text(str(item.get("ref") or item.get("id") or ""), max_chars=180)
    summary = public_safe_text(str(item.get("summary") or evidence_type), max_chars=360)
    verification_state = "test_verified" if evidence_type == "test" else "source_hash_verified"
    return EvidenceRef.from_parts(
        evidence_type=evidence_type,
        authority_lane="candidate",
        verification_state=verification_state,
        locator={"kind": "evidence_ref", "value": ref},
        content_hash=hash_payload({"kind": evidence_type, "ref": ref, "summary": summary}),
        summary=summary,
        producer={"extractor": "work_unit"},
    )


def _chunk_preview(chunk: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "document_chunk_preview.v1",
        "chunk_id": public_safe_text(str(chunk.get("chunk_id") or ""), max_chars=160),
        "snapshot_id": public_safe_text(str(chunk.get("snapshot_id") or ""), max_chars=160),
        "ordinal": int(chunk.get("ordinal") or 0),
        "content_hash": str(chunk.get("content_hash") or ""),
        "summary": public_safe_text(str(chunk.get("summary") or ""), max_chars=320),
        "raw_body_returnable": False,
        "return_capability": "denied_without_explicit_approval",
    }


def _strategy_comparison(
    *,
    object_count: int,
    edge_count: int,
    chunk_preview_count: int,
    storage_mode: str,
    blocked: bool = False,
) -> list[dict[str, Any]]:
    status = "blocked" if blocked else "pass"
    return [
        {
            "strategy": "manifest_summary_v1",
            "scope": "reference_corpus_document_mapping",
            "selected": True,
            "status": status,
            "object_count": object_count,
            "edge_count": edge_count,
            "chunk_preview_count": chunk_preview_count,
            "cost_class": "local_deterministic",
            "gaps": ["extraction_blocked"] if blocked else [],
        },
        {
            "strategy": "metadata_only_locator_v1",
            "scope": "reference_corpus_document_mapping",
            "selected": False,
            "status": "available_with_gap" if storage_mode == "metadata_only" else "not_selected",
            "object_count": 0,
            "edge_count": 0,
            "chunk_preview_count": 0,
            "cost_class": "local_deterministic",
            "gaps": ["requires_source_body_or_summary"] if storage_mode == "metadata_only" else [],
        },
    ]


def _preview_extraction_run(
    run: Mapping[str, Any],
    *,
    object_count: int,
    edge_count: int,
    chunk_preview_count: int,
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": "object_extraction_run_preview.v1",
        "run_id": public_safe_text(str(run.get("run_id") or ""), max_chars=160),
        "status": status,
        "input_hash": str(run.get("input_hash") or ""),
        "extractor": "reference_corpus_manifest",
        "extractor_version": "0.1",
        "output_object_count": object_count,
        "output_edge_count": edge_count,
        "quality_metrics": {
            "public_safe_scan": "pass",
            "chunk_preview_count": chunk_preview_count,
            "no_raw_output_scan": "pass",
        },
        "cost_estimate": {
            "model_calls": 0,
            "estimated_usd": 0.0,
        },
        "speed": {
            "runtime_class": "local_deterministic_fixture",
            "external_network_calls": 0,
        },
        "token_budget": {
            "llm_tokens": 0,
            "budget_required": False,
        },
        "debug_trace_available": True,
        "debug_trace": [
            "load_manifest_metadata",
            "map_reference_documents",
            "build_member_edges",
            "public_safe_preview",
        ],
    }


def _evaluator_report(*, status: str, gaps: list[str]) -> dict[str, Any]:
    if status == "completed":
        failures: list[str] = []
    else:
        failures = ["extraction_blocked"]
    return {
        "schema_version": "object_extraction_evaluator_report.v1",
        "golden_query_slice": "reference corpus freshness/source authority",
        "passes": not failures,
        "failures": failures,
        "gaps": gaps,
        "assertions": [
            "objects_have_authority_lane",
            "edges_have_evidence_refs",
            "chunk_preview_has_no_raw_body",
            "production_mutation_performed_false",
        ],
    }


def _extraction_gaps(bundle: Mapping[str, Any]) -> list[str]:
    gaps: list[str] = []
    rejected = bundle.get("rejected_inputs") if isinstance(bundle.get("rejected_inputs"), list) else []
    if rejected:
        gaps.append("content_hash_mismatch")
    freshness = bundle.get("freshness_gaps") if isinstance(bundle.get("freshness_gaps"), list) else []
    if freshness:
        gaps.append("freshness_gap")
    return gaps

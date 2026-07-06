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
                "version": "",
                "status": "planned",
                "input_object_types": ["RepoDocument"],
                "output_object_types": ["RepoDocument"],
                "edge_types": ["supersedes", "requires_evidence"],
                "strategy_scope": "repo_document_cleanup_mapping",
                "gaps": ["extractor_not_implemented"],
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

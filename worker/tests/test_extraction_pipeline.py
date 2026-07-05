from agent_knowledge.llm_brain_core.objects.extraction_pipeline import (
    build_extractor_registry_report,
    run_documentation_cleanup_strategy_comparison,
    run_reference_corpus_extraction_preview,
    run_runtime_truth_extraction_preview,
)


def _manifest():
    return {
        "corpus_name": "palantir-ontology-mini",
        "sources": [
            {
                "source_id": "palantir-ontology-001",
                "title": "Ontology overview",
                "source_type": "WEB_PAGE",
                "source_url": "https://example.test/ontology",
                "normalized_path": "sources-normalized/palantir-ontology-001.md",
                "content_hash": "sha256:" + "1" * 64,
                "metadata_hash": "sha256:" + "2" * 64,
                "summary": "Objects, links, actions, functions.",
            },
            {
                "source_id": "palantir-ontology-002",
                "title": "Manual excerpt",
                "source_type": "TEXT",
                "normalized_path": "sources-normalized/palantir-ontology-002.md",
                "content_hash": "sha256:" + "3" * 64,
                "metadata_hash": "sha256:" + "4" * 64,
                "summary": "Manual source with missing URL.",
            },
        ],
    }


def test_extractor_registry_reports_implemented_and_gap_extractors():
    report = build_extractor_registry_report()

    assert report["schema_version"] == "object_extractor_registry.v1"
    assert report["production_mutation_performed"] is False
    by_name = {item["extractor"]: item for item in report["extractors"]}
    assert by_name["reference_corpus_manifest"]["status"] == "implemented"
    assert by_name["reference_corpus_manifest"]["output_object_types"] == [
        "ReferenceCorpus",
        "ReferenceDocument",
    ]
    assert by_name["repo_document_cleanup"]["status"] == "planned"
    assert by_name["repo_document_cleanup"]["gaps"] == ["extractor_not_implemented"]
    assert by_name["runtime_truth"]["status"] == "implemented"
    assert by_name["runtime_truth"]["output_object_types"] == [
        "PullRequest",
        "RuntimeTruth",
    ]


def test_reference_corpus_extraction_preview_creates_deterministic_objects_edges_and_chunk_preview():
    first = run_reference_corpus_extraction_preview(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )
    second = run_reference_corpus_extraction_preview(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )

    assert first["schema_version"] == "object_extraction_preview.v1"
    assert first["status"] == "completed"
    assert first["production_mutation_performed"] is False
    assert first["extraction_run"]["debug_trace_available"] is True
    assert first["extraction_run"]["quality_metrics"]["public_safe_scan"] == "pass"
    assert first["extraction_run"]["cost_estimate"]["model_calls"] == 0
    assert first["extraction_run"]["token_budget"]["llm_tokens"] == 0
    assert first["object_count"] == 3
    assert first["edge_count"] == 2
    assert first["chunk_preview_count"] == 2
    assert first["objects"] == second["objects"]
    assert first["edges"] == second["edges"]
    assert first["chunk_preview"] == second["chunk_preview"]
    assert all(edge["edge_type"] == "member_of_corpus" for edge in first["edges"])
    assert all(chunk["raw_body_returnable"] is False for chunk in first["chunk_preview"])
    assert all("body_storage_ref" not in chunk for chunk in first["chunk_preview"])
    assert first["strategy_comparison"][0]["selected"] is True
    assert first["strategy_comparison"][0]["strategy"] == "manifest_summary_v1"
    assert first["evaluator_report"]["golden_query_slice"] == "reference corpus freshness/source authority"
    assert first["evaluator_report"]["passes"] is True


def test_reference_corpus_extraction_preview_blocks_hash_mismatch_without_objects_or_edges():
    manifest = _manifest()
    manifest["sources"][0]["computed_content_hash"] = "sha256:" + "9" * 64

    result = run_reference_corpus_extraction_preview(
        manifest,
        project="neurons",
        storage_mode="managed_snapshot",
    )

    assert result["status"] == "blocked"
    assert result["object_count"] == 0
    assert result["edge_count"] == 0
    assert result["chunk_preview_count"] == 0
    assert result["objects"] == []
    assert result["edges"] == []
    assert result["chunk_preview"] == []
    assert result["gaps"] == ["content_hash_mismatch", "freshness_gap"]
    assert result["evaluator_report"]["passes"] is False
    assert result["evaluator_report"]["failures"] == ["extraction_blocked"]


def test_documentation_cleanup_strategy_comparison_reports_lane_evaluator_evidence():
    result = run_documentation_cleanup_strategy_comparison(
        documents=[
            {
                "path": "README.md",
                "status": "source_of_truth",
                "reason": "approved_markdown_source",
                "confidence": 0.9,
                "evidence_refs": ["file_inventory:README.md"],
            },
            {
                "path": "docs/old.md",
                "status": "archive_candidate",
                "reason": "stale_or_superseded_memory_card",
                "confidence": 0.7,
                "evidence_refs": ["file_inventory:docs/old.md"],
            },
        ],
        consumer="codex",
    )

    assert result["schema_version"] == "object_extraction_strategy_comparison.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "document_authority_pack_v1"
    assert result["pack_preview"]["route"] == "documentation_cleanup"
    assert result["pack_preview"]["object_count"] == 2
    assert result["pack_preview"]["lane_counts"]["accepted_current"] == 1
    assert result["pack_preview"]["lane_counts"]["proposal_only"] == 1
    assert result["pack_preview"]["recommended_action_count"] == 2
    assert result["pack_preview"]["evidence_count"] == 2
    assert result["strategy_comparison"][0]["selected"] is True
    assert result["strategy_comparison"][0]["gaps"] == []
    assert result["strategy_comparison"][1]["strategy"] == "path_inventory_only_v1"
    assert result["strategy_comparison"][1]["gaps"] == ["authority_lane_inference_missing"]
    assert result["evaluator_report"]["golden_query_slice"] == "documentation cleanup current-vs-archive"
    assert result["evaluator_report"]["passes"] is True


def test_runtime_truth_extraction_preview_keeps_merge_and_deploy_separate_without_live_evidence():
    result = run_runtime_truth_extraction_preview(
        pull_request={"id": "pr:73", "merged": True, "head_sha": "abc123"},
        deployment={"target": "production"},
        live_evidence=None,
        consumer="codex",
    )

    assert result["schema_version"] == "object_extraction_runtime_truth_preview.v1"
    assert result["status"] == "pass_with_gaps"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "merge_ci_deploy_live_separation_v1"
    assert result["pack_preview"]["route"] == "deployment_runtime_truth"
    assert result["pack_preview"]["lane_counts"]["candidate"] == 1
    assert result["pack_preview"]["runtime_verified_count"] == 0
    assert result["pack_preview"]["runtime_unverified_count"] == 1
    assert result["pack_preview"]["gaps"] == ["runtime_evidence_unverified"]
    assert result["objects"][0]["object_type"] == "PullRequest"
    assert result["objects"][0]["payload"] == {"merged": True}
    assert result["runtime_truth_objects"] == []
    assert result["edges"] == []
    assert result["evaluator_report"]["golden_query_slice"] == "pr merge and deploy truth"
    assert result["evaluator_report"]["passes"] is True
    assert result["evaluator_report"]["assertions"] == [
        "merge_does_not_imply_deploy",
        "runtime_verified_requires_live_evidence",
        "production_mutation_performed_false",
    ]


def test_runtime_truth_extraction_preview_creates_runtime_verified_object_only_with_live_evidence():
    result = run_runtime_truth_extraction_preview(
        pull_request={"id": "pr:73", "merged": True, "head_sha": "abc123"},
        deployment={"target": "production"},
        live_evidence={
            "evidence_id": "ev:runtime:production",
            "verification_state": "runtime_verified",
            "summary": "Sanitized live runtime smoke passed.",
        },
        consumer="codex",
    )

    assert result["status"] == "pass"
    assert result["pack_preview"]["runtime_verified_count"] == 1
    assert result["pack_preview"]["runtime_unverified_count"] == 0
    assert result["pack_preview"]["gaps"] == []
    assert result["runtime_truth_objects"][0]["object_type"] == "RuntimeTruth"
    assert result["runtime_truth_objects"][0]["verification_state"] == "runtime_verified"
    assert result["runtime_truth_objects"][0]["authority_lane"] == "candidate"
    assert result["runtime_truth_objects"][0]["evidence_refs"] == ["ev:runtime:production"]
    assert result["runtime_truth_objects"][0]["payload"] == {
        "target_ref": "production",
        "claim": "runtime_verified",
    }
    assert result["edges"][0]["edge_type"] == "validated_by"
    assert result["evaluator_report"]["passes"] is True

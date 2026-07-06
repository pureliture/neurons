from agent_knowledge.llm_brain_core.objects.extraction_pipeline import (
    build_extractor_registry_report,
    run_documentation_cleanup_strategy_comparison,
    run_extraction_evaluator_suite_preview,
    run_graph_search_projection_join_preview,
    run_preference_style_extraction_preview,
    run_pr_commit_extraction_preview,
    run_reference_corpus_extraction_preview,
    run_repo_document_extraction_preview,
    run_runtime_truth_extraction_preview,
    run_session_detail_extraction_preview,
    run_session_project_rollup_preview,
    run_work_unit_extraction_preview,
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
    assert by_name["repo_document_cleanup"]["status"] == "implemented"
    assert by_name["repo_document_cleanup"]["gaps"] == []
    assert by_name["runtime_truth"]["status"] == "implemented"
    assert by_name["runtime_truth"]["output_object_types"] == [
        "PullRequest",
        "RuntimeTruth",
    ]
    assert by_name["preference_style"]["status"] == "implemented"
    assert by_name["preference_style"]["output_object_types"] == [
        "ArtifactPreference",
        "StyleRule",
    ]
    assert by_name["work_unit"]["status"] == "implemented"
    assert by_name["work_unit"]["output_object_types"] == ["WorkUnit"]
    assert by_name["pr_commit_detail"]["status"] == "implemented"
    assert by_name["pr_commit_detail"]["output_object_types"] == [
        "PullRequest",
        "Commit",
        "TestRun",
    ]
    assert by_name["graph_search_projection_join"]["status"] == "implemented"
    assert by_name["graph_search_projection_join"]["output_object_types"] == [
        "ProjectionHit",
    ]
    assert by_name["session_detail"]["status"] == "implemented"
    assert by_name["session_detail"]["output_object_types"] == ["Session"]


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


def test_repo_document_extraction_preview_reports_full_run_edges_and_metrics():
    result = run_repo_document_extraction_preview(
        documents=[
            {
                "path": "README.md",
                "status": "source_of_truth",
                "reason": "approved_repo_entrypoint",
                "confidence": 0.9,
                "evidence_refs": ["inventory:README.md"],
            },
            {
                "path": "docs/legacy.md",
                "status": "superseded",
                "reason": "new roadmap supersedes legacy note",
                "confidence": 0.7,
                "evidence_refs": ["inventory:docs/legacy.md"],
                "superseded_by": "README.md",
            },
            {
                "path": "docs/generated-view.md",
                "status": "generated_companion",
                "reason": "generated companion view",
                "confidence": 0.6,
                "requires_evidence": True,
            },
        ],
        repository="neurons",
        consumer="codex",
    )

    assert result["schema_version"] == "object_extraction_repo_document_preview.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "repo_document_pack_extraction_v1"
    assert result["object_count"] == 3
    assert result["edge_count"] == 2
    assert result["evidence_count"] == 3
    assert result["lane_counts"] == {
        "accepted_current": 1,
        "accepted_non_current": 0,
        "archive_only": 0,
        "candidate": 0,
        "derived_projection": 1,
        "proposal_only": 1,
        "reference_only": 0,
        "rejected": 0,
    }
    assert all(item["object_type"] == "RepoDocument" for item in result["objects"])
    assert [edge["edge_type"] for edge in result["edges"]] == [
        "supersedes",
        "requires_evidence",
    ]
    assert result["extraction_run"]["output_object_count"] == 3
    assert result["extraction_run"]["output_edge_count"] == 2
    assert result["extraction_run"]["quality_metrics"] == {
        "public_safe_scan": "pass",
        "authority_lane_separation": "pass",
        "missing_evidence_gap_count": 0,
    }
    assert result["extraction_run"]["cost_estimate"]["model_calls"] == 0
    assert result["extraction_run"]["token_budget"]["llm_tokens"] == 0
    assert result["evaluator_report"]["golden_query_slice"] == "repo document cleanup extraction run"
    assert result["evaluator_report"]["passes"] is True


def test_repo_document_extraction_preview_reports_gap_without_current_document():
    result = run_repo_document_extraction_preview(
        documents=[
            {
                "path": "docs/archive.md",
                "status": "archive_candidate",
                "reason": "candidate only",
                "confidence": 0.5,
            },
        ],
        repository="neurons",
        consumer="codex",
    )

    assert result["status"] == "pass_with_gaps"
    assert "accepted_current documents empty" in result["gaps"]
    assert result["evaluator_report"]["passes"] is False
    assert result["evaluator_report"]["failures"] == [
        "accepted_current documents empty",
        "review_proposals_needed",
    ]


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


def test_preference_style_extraction_preview_maps_memory_cards_without_raw_body():
    result = run_preference_style_extraction_preview(
        memory_cards=[
            {
                "memory_id": "mem_pref_html",
                "card_type": "preference",
                "summary": "HTML review artifact preference",
                "confidence": 0.9,
                "currentness": "current",
                "typed_payload": {
                    "preference": "HTML review artifacts should be information dense.",
                    "applies_to": "html review",
                    "reason": "User repeatedly prefers dense review artifacts.",
                    "exceptions": ["short status reports can stay concise"],
                },
                "source_refs": [{"source_ref_id": "session:preference-evidence"}],
            },
            {
                "memory_id": "mem_style_tests",
                "card_type": "repo_style",
                "summary": "Use uv for worker tests",
                "confidence": 0.8,
                "typed_payload": {
                    "claim": "Python worker tests use uv run pytest.",
                    "repo_scope": "neurons/worker",
                    "reason": "Repo AGENTS and repeated test evidence.",
                    "files": ["worker/tests/test_extraction_pipeline.py"],
                    "commits": ["commit:abc123"],
                },
            },
            {
                "memory_id": "mem_workflow",
                "card_type": "workflow_contract",
                "summary": "Use dedicated worktrees.",
                "typed_payload": {"rule": "Use dedicated worktrees."},
            },
        ],
        repository="neurons",
        current_request="review HTML artifact for worker tests",
        current_files=["worker/tests/test_extraction_pipeline.py"],
    )

    assert result["schema_version"] == "object_extraction_preference_style_preview.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "memory_card_preference_style_v1"
    assert result["preference_count"] == 1
    assert result["style_claim_count"] == 1
    assert result["ignored_input_count"] == 1
    assert result["pack_preview"]["preferences"]["object_count"] == 1
    assert result["pack_preview"]["style"]["object_count"] == 1
    assert result["objects"][0]["object_type"] == "ArtifactPreference"
    assert result["objects"][0]["title"] == "HTML review artifacts should be information dense."
    assert result["objects"][1]["object_type"] == "StyleRule"
    assert result["objects"][1]["title"] == "Python worker tests use uv run pytest."
    assert result["source_evidence_refs"] == [
        "mem_pref_html",
        "session:preference-evidence",
        "mem_style_tests",
        "worker/tests/test_extraction_pipeline.py",
        "commit:abc123",
    ]
    assert result["evaluator_report"]["golden_query_slice"] == "style and artifact preference memory"
    assert result["evaluator_report"]["passes"] is True


def test_work_unit_extraction_preview_groups_session_pr_commit_and_tests_without_raw_transcript():
    result = run_work_unit_extraction_preview(
        work_item={
            "work_id": "work:p3-runtime",
            "title": "P3 runtime truth extraction preview",
            "summary": "Added runtime truth extraction preview.",
            "status": "in_progress",
        },
        evidence_items=[
            {"kind": "session", "ref": "session:abc", "summary": "Implemented runtime preview."},
            {"kind": "pull_request", "ref": "pr:73", "summary": "Merge evidence exists."},
            {"kind": "commit", "ref": "commit:e0958fd", "summary": "Preference style preview commit."},
            {"kind": "test", "ref": "pytest:worker", "summary": "1516 passed."},
        ],
        repository="neurons",
    )

    assert result["schema_version"] == "object_extraction_work_unit_preview.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["object"]["object_type"] == "WorkUnit"
    assert result["object"]["title"] == "P3 runtime truth extraction preview"
    assert result["object"]["payload"] == {
        "work_id": "work:p3-runtime",
        "status": "in_progress",
        "evidence_count": 4,
    }
    assert result["evidence_count"] == 4
    assert [item["evidence_type"] for item in result["evidence"]] == [
        "session",
        "pull_request",
        "commit",
        "test",
    ]
    assert all(item["raw_return_capability"] == "denied" for item in result["evidence"])
    assert all("raw_transcript" not in str(item) for item in result["evidence"])
    assert [edge["edge_type"] for edge in result["edges"]] == [
        "supported_by_evidence",
        "supported_by_evidence",
        "supported_by_evidence",
        "validated_by",
    ]
    assert result["evaluator_report"]["golden_query_slice"] == "temporal work recall"
    assert result["evaluator_report"]["passes"] is True


def test_work_unit_extraction_preview_reports_gap_without_evidence():
    result = run_work_unit_extraction_preview(
        work_item={
            "work_id": "work:empty",
            "title": "Empty work item",
            "summary": "No evidence yet.",
            "status": "planned",
        },
        evidence_items=[],
        repository="neurons",
    )

    assert result["status"] == "pass_with_gaps"
    assert result["evidence"] == []
    assert result["edges"] == []
    assert result["gaps"] == ["work_unit_evidence_missing"]
    assert result["evaluator_report"]["passes"] is False
    assert result["evaluator_report"]["failures"] == ["work_unit_evidence_missing"]


def test_session_detail_extraction_preview_maps_session_metadata_without_raw_body():
    result = run_session_detail_extraction_preview(
        sessions=[
            {
                "session_id_hash": "session:alpha",
                "device_id_hash": "device:one",
                "provider": "codex",
                "summary": "Implemented P3 extraction preview.",
                "work_unit_id": "work:p3",
                "evidence_refs": ["commit:edabc0a", "pytest:worker"],
            },
            {
                "session_id_hash": "session:beta",
                "device_id_hash": "device:one",
                "provider": "codex",
                "summary": "Verified projection join preview.",
                "work_unit_id": "work:p3",
                "evidence_refs": ["commit:52b613e"],
            },
        ],
        repository="neurons",
    )

    assert result["schema_version"] == "object_extraction_session_detail_preview.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "session_metadata_evidence_v1"
    assert result["object_count"] == 2
    assert result["edge_count"] == 5
    assert result["evidence_count"] == 3
    assert all(item["object_type"] == "Session" for item in result["objects"])
    assert all(item["payload"]["raw_body_returnable"] is False for item in result["objects"])
    assert [edge["edge_type"] for edge in result["edges"]] == [
        "part_of_work_unit",
        "supported_by_evidence",
        "supported_by_evidence",
        "part_of_work_unit",
        "supported_by_evidence",
    ]
    assert result["strategy_comparison"][1]["strategy"] == "raw_session_body_inference_v1"
    assert result["strategy_comparison"][1]["status"] == "rejected"
    assert result["strategy_comparison"][1]["gaps"] == ["raw_session_body_forbidden"]
    assert result["evaluator_report"]["golden_query_slice"] == "session detail extraction"
    assert result["evaluator_report"]["passes"] is True


def test_session_detail_extraction_preview_reports_gap_when_raw_body_is_ignored():
    result = run_session_detail_extraction_preview(
        sessions=[
            {
                "session_id_hash": "session:raw",
                "summary": "Synthetic summary only.",
                "raw_transcript": "synthetic body that must not be returned",
            },
        ],
        repository="neurons",
    )

    assert result["status"] == "pass_with_gaps"
    assert result["object_count"] == 1
    assert result["edge_count"] == 0
    assert result["evidence_count"] == 0
    assert result["gaps"] == ["raw_session_body_ignored", "session_evidence_missing"]
    assert "synthetic body" not in str(result)
    assert result["evaluator_report"]["passes"] is False
    assert result["evaluator_report"]["failures"] == [
        "raw_session_body_ignored",
        "session_evidence_missing",
    ]


def test_session_project_rollup_preview_separates_same_device_and_all_devices():
    sessions = [
        {
            "session_id_hash": "sha256:session-alpha",
            "device_id_hash": "sha256:device-one",
            "provider": "codex",
            "summary": "Implemented project rollup preview.",
            "work_unit_id": "work:p6",
            "evidence_refs": ["commit:p6a"],
            "host_path": "HOST_PATH_SENTINEL",
        },
        {
            "session_id_hash": "sha256:session-beta",
            "device_id_hash": "sha256:device-one",
            "provider": "codex",
            "summary": "Verified same-device recall.",
            "work_unit_id": "work:p6",
            "evidence_refs": ["pytest:p6"],
        },
        {
            "session_id_hash": "sha256:session-gamma",
            "device_id_hash": "sha256:device-two",
            "provider": "codex",
            "summary": "Captured all-device handoff context.",
            "work_unit_id": "work:p6",
            "evidence_refs": ["commit:p6b"],
        },
    ]

    all_devices = run_session_project_rollup_preview(
        sessions=sessions,
        repository="neurons",
        branch="codex/p6",
        project="neurons",
        scope="all_devices",
    )
    same_device = run_session_project_rollup_preview(
        sessions=sessions,
        repository="neurons",
        branch="codex/p6",
        project="neurons",
        requesting_device_id_hash="sha256:device-one",
        scope="same_device",
    )

    assert all_devices["schema_version"] == "object_extraction_session_project_rollup_preview.v1"
    assert all_devices["status"] == "pass"
    assert all_devices["visible_session_count"] == 3
    assert same_device["visible_session_count"] == 2
    assert same_device["all_device_session_count"] == 3
    assert same_device["per_device_counts"] == {
        "sha256:device-one": 2,
        "sha256:device-two": 1,
    }
    object_types = {obj["object_type"] for obj in all_devices["objects"]}
    assert {"Device", "Session", "Repository", "Branch", "WorkUnit"}.issubset(object_types)
    edge_types = {edge["edge_type"] for edge in all_devices["edges"]}
    assert {
        "repository_has_branch",
        "session_on_device",
        "session_in_repository",
        "session_on_branch",
        "part_of_work_unit",
    }.issubset(edge_types)
    assert "HOST_PATH_SENTINEL" not in str(all_devices)
    assert "raw_transcript" not in str(all_devices)
    assert all_devices["evaluator_report"]["golden_query_slice"] == "temporal repo recall"
    assert all_devices["evaluator_report"]["passes"] is True


def test_session_project_rollup_preview_links_specs_prs_and_commits_bidirectionally():
    result = run_session_project_rollup_preview(
        sessions=[
            {
                "session_id_hash": "sha256:session-alpha",
                "device_id_hash": "sha256:device-one",
                "provider": "codex",
                "summary": "Implemented bidirectional rollup.",
                "work_unit_id": "work:p6",
                "evidence_refs": ["commit:p6c"],
            },
        ],
        specs=[{"spec_ref": "docs/specs/p6/design.md", "work_unit_id": "work:p6"}],
        pull_requests=[{"pr_id": "pr:73", "number": 73, "work_unit_id": "work:p6"}],
        commits=[{"commit_id": "commit:abc123", "pull_request_id": "pr:73", "work_unit_id": "work:p6"}],
        repository="neurons",
        branch="codex/p6",
        project="neurons",
    )

    object_types = {obj["object_type"] for obj in result["objects"]}
    assert {"Spec", "PullRequest", "Commit"}.issubset(object_types)
    edge_types = {edge["edge_type"] for edge in result["edges"]}
    assert {
        "work_unit_has_session",
        "device_has_session",
        "work_unit_has_spec",
        "spec_part_of_work_unit",
        "work_unit_has_pull_request",
        "pull_request_part_of_work_unit",
        "pull_request_includes_commit",
        "commit_part_of_work_unit",
    }.issubset(edge_types)
    assert result["linked_spec_count"] == 1
    assert result["linked_pull_request_count"] == 1
    assert result["linked_commit_count"] == 1
    assert result["evaluator_report"]["passes"] is True


def test_session_project_rollup_preview_builds_safe_handoff_pack():
    result = run_session_project_rollup_preview(
        sessions=[
            {
                "session_id_hash": "sha256:session-alpha",
                "device_id_hash": "sha256:device-one",
                "provider": "codex",
                "summary": "Prepared handoff pack.",
                "work_unit_id": "work:p6",
                "evidence_refs": ["commit:p6d"],
                "raw_transcript": "SOURCE_BODY_SENTINEL",
            },
        ],
        specs=[{"spec_ref": "docs/specs/p6/design.md", "work_unit_id": "work:p6"}],
        pull_requests=[{"pr_id": "pr:73", "number": 73, "work_unit_id": "work:p6"}],
        commits=[{"commit_id": "commit:def456", "pull_request_id": "pr:73", "work_unit_id": "work:p6"}],
        repository="neurons",
        branch="codex/p6",
        project="neurons",
    )

    handoff = result["handoff_pack"]
    assert handoff["schema_version"] == "session_project_handoff_pack.v1"
    assert handoff["raw_return_capability"] == "denied"
    assert handoff["visible_session_count"] == 1
    assert handoff["object_refs"]["Session"]
    assert handoff["object_refs"]["WorkUnit"]
    assert handoff["object_refs"]["PullRequest"]
    assert "raw_session_body_ignored" in handoff["gaps"]
    assert "verify_live_multi_device_rollup" in handoff["recommended_next_actions"]
    assert "SOURCE_BODY_SENTINEL" not in str(handoff)


def test_pr_commit_extraction_preview_maps_pr_commits_and_tests_without_runtime_inference():
    result = run_pr_commit_extraction_preview(
        pull_request={
            "pr_id": "pr:73",
            "number": 73,
            "title": "test(deploy): #40 k3s public contract static guard 추가",
            "state": "merged",
            "merge_commit": "c3f3e34",
            "head_ref": "codex/40-pr7-k3s-public-contract",
        },
        commits=[
            {
                "sha": "3ff8835",
                "title": "#40 PR7 리뷰 피드백 반영",
                "test_refs": ["test:static-guard"],
            },
            {
                "sha": "13c75ed",
                "title": "#40 k3s public contract 리뷰 대응",
                "test_refs": ["test:gradle"],
            },
        ],
        test_runs=[
            {
                "test_id": "test:static-guard",
                "summary": "Static contract guard passed.",
                "status": "pass",
            },
            {
                "test_id": "test:gradle",
                "summary": "Gradle test passed.",
                "status": "pass",
            },
        ],
        repository="neurons",
    )

    assert result["schema_version"] == "object_extraction_pr_commit_preview.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "pr_commit_ci_evidence_v1"
    assert result["object_counts"] == {
        "PullRequest": 1,
        "Commit": 2,
        "TestRun": 2,
    }
    assert [item["object_type"] for item in result["objects"]] == [
        "PullRequest",
        "Commit",
        "Commit",
        "TestRun",
        "TestRun",
    ]
    assert [edge["edge_type"] for edge in result["edges"]] == [
        "includes_commit",
        "includes_commit",
        "validated_by",
        "validated_by",
    ]
    assert result["runtime_truth_objects"] == []
    assert result["pack_preview"]["runtime_verified_count"] == 0
    assert result["pack_preview"]["runtime_unverified_count"] == 1
    assert result["strategy_comparison"][1]["strategy"] == "merge_only_runtime_truth_v1"
    assert result["strategy_comparison"][1]["status"] == "rejected"
    assert result["strategy_comparison"][1]["gaps"] == [
        "runtime_truth_requires_live_evidence",
    ]
    assert result["evaluator_report"]["golden_query_slice"] == "pr commit and test provenance"
    assert result["evaluator_report"]["passes"] is True
    assert result["evaluator_report"]["assertions"] == [
        "pr_commit_test_objects_are_separate",
        "commit_test_edges_preserve_evidence",
        "merge_does_not_imply_runtime",
        "production_mutation_performed_false",
    ]


def test_pr_commit_extraction_preview_reports_gap_for_missing_test_refs():
    result = run_pr_commit_extraction_preview(
        pull_request={
            "pr_id": "pr:gap",
            "title": "Missing test evidence PR",
            "state": "open",
        },
        commits=[
            {
                "sha": "abc123",
                "title": "Add unverified change",
                "test_refs": ["test:missing"],
            },
        ],
        test_runs=[],
        repository="neurons",
    )

    assert result["status"] == "pass_with_gaps"
    assert result["object_counts"] == {
        "PullRequest": 1,
        "Commit": 1,
        "TestRun": 0,
    }
    assert result["edge_count"] == 1
    assert result["gaps"] == ["commit_test_ref_missing"]
    assert result["pack_preview"]["missing_test_ref_count"] == 1
    assert result["pack_preview"]["runtime_unverified_count"] == 0
    assert result["evaluator_report"]["passes"] is False
    assert result["evaluator_report"]["failures"] == ["commit_test_ref_missing"]


def test_graph_search_projection_join_preview_joins_without_promoting_authority():
    base = run_pr_commit_extraction_preview(
        pull_request={
            "pr_id": "pr:73",
            "title": "Merged deployment contract PR",
            "state": "merged",
            "merge_commit": "c3f3e34",
        },
        commits=[
            {
                "sha": "3ff8835",
                "title": "#40 PR7 리뷰 피드백 반영",
                "test_refs": ["test:static-guard"],
            }
        ],
        test_runs=[
            {
                "test_id": "test:static-guard",
                "summary": "Static contract guard passed.",
                "status": "pass",
            }
        ],
        repository="neurons",
    )
    pr_object = base["objects"][0]
    commit_object = base["objects"][1]

    result = run_graph_search_projection_join_preview(
        objects=base["objects"],
        projection_hits=[
            {
                "hit_id": "graph:pr73:summary",
                "source": "graph",
                "object_ref": pr_object["object_id"],
                "summary": "Derived graph hit for the merged PR.",
                "score": 0.78,
            },
            {
                "hit_id": "search:commit:3ff8835",
                "source": "search",
                "object_ref": commit_object["object_id"],
                "summary": "Search mirror hit for the commit evidence.",
                "score": 0.72,
            },
        ],
        repository="neurons",
    )

    assert result["schema_version"] == "object_extraction_projection_join_preview.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["selected_strategy"] == "projection_join_read_only_v1"
    assert result["canonical_authority_unchanged"] is True
    assert result["authority_promotion_performed"] is False
    assert result["object_count"] == len(base["objects"])
    assert result["projection_object_count"] == 2
    assert result["edge_count"] == 2
    assert all(item["object_type"] == "ProjectionHit" for item in result["projection_objects"])
    assert all(item["authority_lane"] == "derived_projection" for item in result["projection_objects"])
    assert all(item["verification_state"] == "unverified" for item in result["projection_objects"])
    assert [edge["edge_type"] for edge in result["edges"]] == [
        "projection_join",
        "projection_join",
    ]
    assert all(edge["authority_lane"] == "derived_projection" for edge in result["edges"])
    assert result["strategy_comparison"][1]["strategy"] == "projection_as_authority_v1"
    assert result["strategy_comparison"][1]["status"] == "rejected"
    assert result["strategy_comparison"][1]["gaps"] == [
        "derived_projection_cannot_become_canonical_authority",
    ]
    assert result["evaluator_report"]["golden_query_slice"] == "graph/search projection object join"
    assert result["evaluator_report"]["passes"] is True


def test_graph_search_projection_join_preview_reports_gap_for_unknown_object_target():
    result = run_graph_search_projection_join_preview(
        objects=[],
        projection_hits=[
            {
                "hit_id": "graph:missing",
                "source": "graph",
                "object_ref": "ko:missing",
                "summary": "Projection target missing.",
                "score": 0.5,
            }
        ],
        repository="neurons",
    )

    assert result["status"] == "pass_with_gaps"
    assert result["projection_object_count"] == 0
    assert result["edge_count"] == 0
    assert result["gaps"] == ["projection_join_target_missing"]
    assert result["pack_preview"]["missing_target_count"] == 1
    assert result["evaluator_report"]["passes"] is False
    assert result["evaluator_report"]["failures"] == ["projection_join_target_missing"]


def test_extraction_evaluator_suite_preview_covers_golden_variance_strategy_and_model_gates():
    reference_first = run_reference_corpus_extraction_preview(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )
    reference_second = run_reference_corpus_extraction_preview(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )
    repo_docs = run_repo_document_extraction_preview(
        documents=[
            {
                "path": "README.md",
                "status": "source_of_truth",
                "reason": "approved_repo_entrypoint",
                "confidence": 0.9,
                "evidence_refs": ["inventory:README.md"],
            },
            {
                "path": "docs/legacy.md",
                "status": "superseded",
                "reason": "new roadmap supersedes legacy note",
                "confidence": 0.7,
                "evidence_refs": ["inventory:docs/legacy.md"],
                "superseded_by": "README.md",
            },
        ],
        repository="neurons",
        consumer="codex",
    )

    result = run_extraction_evaluator_suite_preview(
        evaluation_reports=[reference_first, repo_docs],
        variance_samples=[reference_first, reference_second],
        suite_name="p3-local-test",
    )

    assert result["schema_version"] == "object_extraction_evaluator_suite.v1"
    assert result["status"] == "pass"
    assert result["production_mutation_performed"] is False
    assert result["suite_name"] == "p3-local-test"
    assert result["coverage"] == {
        "deterministic_fixture_checks": "pass",
        "golden_query_checks": "pass",
        "strategy_comparison_checks": "pass",
        "variance_checks": "pass",
        "model_prompt_comparison": "not_applicable_no_llm",
    }
    assert result["golden_query"]["checked_count"] == 2
    assert result["golden_query"]["passes"] is True
    assert result["variance"]["sample_count"] == 2
    assert result["variance"]["unique_output_hash_count"] == 1
    assert result["variance"]["passes"] is True
    assert result["model_prompt_comparison"]["status"] == "not_applicable_no_llm"
    assert result["model_prompt_comparison"]["model_call_count"] == 0
    assert result["strategy_comparison"]["checked_count"] == 2
    assert result["failures"] == []
    assert result["gaps"] == []


def test_extraction_evaluator_suite_preview_reports_variance_gap():
    reference = run_reference_corpus_extraction_preview(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )
    variant = dict(reference)
    variant["object_count"] = reference["object_count"] + 1

    result = run_extraction_evaluator_suite_preview(
        evaluation_reports=[reference],
        variance_samples=[reference, variant],
        suite_name="p3-variance-gap",
    )

    assert result["status"] == "pass_with_gaps"
    assert result["coverage"]["variance_checks"] == "fail"
    assert result["variance"]["unique_output_hash_count"] == 2
    assert result["variance"]["passes"] is False
    assert result["failures"] == ["variance_detected"]
    assert result["gaps"] == ["variance_detected"]

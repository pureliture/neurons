from __future__ import annotations

import json

from agent_knowledge.cli import BOUNDARY, COMMAND_HANDLERS, main


def _reference_manifest() -> dict:
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


def _palantir_full_count_manifest() -> dict:
    sources = []
    for idx in range(1, 66):
        if idx <= 6:
            source_type = "PDF"
        elif idx <= 39:
            source_type = "WEB_PAGE"
        else:
            source_type = "TEXT"
        source = {
            "source_id": f"palantir-ontology-{idx:03d}",
            "title": f"Palantir ontology reference {idx:03d}",
            "source_type": source_type,
            "normalized_path": f"sources-normalized/palantir-ontology-{idx:03d}.md",
            "content_hash": "sha256:" + f"{idx:064x}"[-64:],
            "metadata_hash": "sha256:" + f"{idx + 100:064x}"[-64:],
            "summary": "Public-safe metadata-only reference fixture.",
        }
        if idx <= 39:
            source["source_url"] = f"https://example.test/palantir/ontology/{idx:03d}"
        sources.append(source)
    return {"corpus_name": "palantir-ontology", "sources": sources}


def test_neuron_knowledge_help_lists_server_owned_commands(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "usage: neuron-knowledge" in output
    for command in (
        "rag-ingress-state",
        "memory-regeneration",
        "session-memory-private-sync",
        "neuron-session-memory-build",
        "native-memory-sync",
        "session-memory-gc",
        "transcript-backfill",
        "session-entry-recall",
        "transcript-resources",
        "transcript-quality",
        "transcript-retrieval",
        "transcript-migration",
        "transcript-memory-gc",
        "transcript-session-gc",
        "transcript-volume-gc",
        "backfill",
        "memory",
        "context-for-prompt",
        "mcp-stdio",
        "eval",
        "derived-memory-resources",
        "session-memory-quarantine-terminal-skipped",
        "session-memory-repair-zombie-snapshots",
        "brain-context-resolve",
        "object-query",
        "object-explain",
        "corpus-status",
        "corpus-ingest-plan",
        "corpus-ingest",
        "source-to-candidate-graph",
        "candidate-review-edit",
        "approval-board-decide",
        "golden-query-eval",
        "source-to-candidate-runtime-readiness",
        "okf-export",
        "brain-regression-gate",
        "couchdb-migration-flow",
        "couchdb-graph-trigger",
        "couchdb-graph-project",
        "couchdb-graph-status",
    ):
        assert command in COMMAND_HANDLERS
        assert command in output


def test_neuron_knowledge_boundary_is_server_owned(capsys):
    assert main(["--show-boundary"]) == 0
    assert capsys.readouterr().out.strip() == BOUNDARY


def test_neuron_knowledge_rejects_dendrite_command(capsys):
    assert main(["capture", "--help"]) == 2
    assert "unknown neurons command: capture" in capsys.readouterr().err


def test_neuron_knowledge_pending_server_command_fails_closed(capsys):
    assert main(["transcript-resources", "--help"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "neuron_knowledge_pending_command.v1"
    assert report["status"] == "blocked_pending_server_extraction"
    assert report["command"] == "transcript-resources"
    assert report["destination"] == "neurons"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_delegates_memory_regeneration_help(capsys):
    assert main(["memory-regeneration", "--help"]) == 0
    assert "usage: memory-regeneration" in capsys.readouterr().out


def test_neuron_knowledge_delegates_session_private_sync_help(capsys):
    assert main(["session-memory-private-sync", "--help"]) == 0
    assert "usage: session-memory-private-sync" in capsys.readouterr().out


def test_neuron_knowledge_corpus_ingest_production_target_denied(capsys):
    rc = main(["corpus-ingest", "--project", "neurons", "--target", "production"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "object_substrate_cli_denied.v1"
    assert report["status"] == "denied"
    assert report["mutation_performed"] is False
    assert report["reason"] == "production_corpus_ingest_requires_later_validation_goal"


def test_neuron_knowledge_corpus_ingest_production_denied_even_with_configured_ledger_env(
    tmp_path,
    capsys,
    monkeypatch,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")
    monkeypatch.setenv("NEURON_REFERENCE_CORPUS_LEDGER", str(ledger))

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "production",
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "denied"
    assert report["mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_corpus_status_reports_storage_policy(capsys):
    rc = main(["corpus-status", "--project", "neurons"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "brain_corpus_status.v1"
    assert report["raw_body_policy"]["return_capability"] == "denied_without_explicit_approval"
    assert report["raw_body_policy"]["retention_class"] == "user_managed_reference"
    assert report["raw_body_policy"]["redaction_profile"] == "public_safe_summary"
    assert report["raw_body_policy"]["deletion_policy"] == "delete_snapshot_keep_metadata"
    assert report["raw_body_policy"]["license_source_rights"] == "operator_attested"
    assert report["source_rights_policy"] == "operator_attested_reference_use"
    assert "managed_snapshot" in report["supported_storage_modes"]
    assert "reference_corpus_store_empty" in report["gaps"]


def test_neuron_knowledge_corpus_ingest_plan_loads_full_count_manifest(tmp_path, capsys):
    manifest = tmp_path / "palantir-manifest.json"
    manifest.write_text(json.dumps(_palantir_full_count_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest-plan",
            "--project",
            "neurons",
            "--storage-mode",
            "external_object_store",
            "--manifest-file",
            str(manifest),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["corpus"]["name"] == "palantir-ontology"
    assert report["corpus"]["source_count"] == 65
    assert report["source_url_count"] == 39
    assert report["manual_text_without_url_count"] == 26
    assert report["source_type_counts"] == {"PDF": 6, "TEXT": 26, "WEB_PAGE": 33}
    assert report["manifest_hash"].startswith("sha256:")
    assert report["writes_planned"] is False


def test_neuron_knowledge_corpus_ingest_plan_expected_count_gate_passes(tmp_path, capsys):
    manifest = tmp_path / "palantir-manifest.json"
    manifest.write_text(json.dumps(_palantir_full_count_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest-plan",
            "--project",
            "neurons",
            "--storage-mode",
            "external_object_store",
            "--manifest-file",
            str(manifest),
            "--expect-source-count",
            "65",
            "--expect-source-url-count",
            "39",
            "--expect-manual-text-without-url-count",
            "26",
            "--expect-source-type-count",
            "PDF=6",
            "--expect-source-type-count",
            "WEB_PAGE=33",
            "--expect-source-type-count",
            "TEXT=26",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["count_gate_status"] == "pass"
    assert report["count_gate_gaps"] == []
    assert report["writes_planned"] is False


def test_neuron_knowledge_corpus_ingest_plan_expected_count_gate_fails_closed(tmp_path, capsys):
    manifest = tmp_path / "palantir-manifest.json"
    manifest.write_text(json.dumps(_palantir_full_count_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest-plan",
            "--project",
            "neurons",
            "--storage-mode",
            "external_object_store",
            "--manifest-file",
            str(manifest),
            "--expect-source-count",
            "66",
            "--expect-source-url-count",
            "39",
            "--expect-manual-text-without-url-count",
            "26",
            "--expect-source-type-count",
            "PDF=6",
            "--expect-source-type-count",
            "WEB_PAGE=33",
            "--expect-source-type-count",
            "TEXT=26",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["count_gate_status"] == "fail"
    assert report["count_gate_gaps"] == [{"field": "source_count", "expected": 66, "actual": 65}]
    assert report["writes_planned"] is False


def test_neuron_knowledge_corpus_ingest_local_test_is_preview_until_store_configured(capsys):
    rc = main(["corpus-ingest", "--project", "neurons", "--target", "local_test"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "reference_corpus_ingest.v1"
    assert report["status"] == "planned"
    assert report["mutation_performed"] is False
    assert report["writes_planned"] is True
    assert "reference_corpus_store_not_configured" in report["gaps"]


def test_neuron_knowledge_corpus_ingest_local_test_writes_configured_store(tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
        ]
    )
    ingest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert ingest["schema_version"] == "reference_corpus_store_write.v1"
    assert ingest["status"] == "stored"
    assert ingest["source_count"] == 2
    assert ingest["mutation_performed"] is True
    assert ingest["production_mutation_performed"] is False

    assert main(["corpus-status", "--project", "neurons", "--ledger", str(ledger), "--corpus-id", ingest["corpus_id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_count"] == 2
    assert status["storage_modes"] == {"managed_snapshot": 2}
    assert status["reference_object_count"] == 2
    assert status["document_source_count"] == 2
    assert status["version_count"] == 2
    assert status["snapshot_count"] == 2
    assert status["chunk_count"] == 2
    assert status["freshness_check_count"] == 2
    assert status["extraction_run_count"] == 1
    assert status["first_class_store_counts"]["document_sources"] == 2
    assert status["first_class_store_counts"]["document_snapshots"] == 2
    assert status["first_class_store_counts"]["document_chunks"] == 2
    assert status["first_class_store_counts"]["freshness_checks"] == 2
    assert status["first_class_store_counts"]["extraction_runs"] == 1
    assert status["document_sources"][0]["schema_version"] == "document_source.v1"
    assert status["document_versions"][0]["schema_version"] == "document_version.v1"
    assert status["document_snapshots"][0]["schema_version"] == "document_snapshot.v1"
    assert status["document_chunks"][0]["schema_version"] == "document_chunk.v1"
    assert status["freshness_checks"][0]["schema_version"] == "freshness_check.v1"
    assert status["extraction_runs"][0]["status"] == "completed"
    assert status["freshness_gaps"][0]["source_url_status"] == "missing_manual_text"
    assert status["gaps"] == []


def test_neuron_knowledge_corpus_ingest_and_status_use_configured_local_test_ledger_env(
    tmp_path,
    capsys,
    monkeypatch,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")
    monkeypatch.setenv("NEURON_REFERENCE_CORPUS_LEDGER", str(ledger))

    rc = main(
        [
            "corpus-ingest",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--manifest-file",
            str(manifest),
            "--storage-mode",
            "managed_snapshot",
        ]
    )
    ingest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert ingest["schema_version"] == "reference_corpus_store_write.v1"
    assert ingest["status"] == "stored"
    assert ingest["production_mutation_performed"] is False

    assert main(["corpus-status", "--project", "neurons", "--corpus-id", ingest["corpus_id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_count"] == 2
    assert status["storage_modes"] == {"managed_snapshot": 2}
    assert status["gaps"] == []


def test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    assert (
        main(
            [
                "corpus-ingest",
                "--project",
                "neurons",
                "--target",
                "local_test",
                "--ledger",
                str(ledger),
                "--manifest-file",
                str(manifest),
                "--storage-mode",
                "managed_snapshot",
            ]
        )
        == 0
    )
    ingest = json.loads(capsys.readouterr().out)

    rc = main(
        [
            "source-to-candidate-graph",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
            "--corpus-id",
            ingest["corpus_id"],
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["schema_version"] == "source_to_candidate_graph_activation.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["production_mutation_performed"] is False
    assert report["ledger_mutation_performed"] is False
    assert report["input_store"]["corpus_id"] == ingest["corpus_id"]
    assert report["candidate_graph_review_pack"]["route"] == "candidate_graph_review"
    assert report["candidate_graph_review_pack"]["production_mutation_performed"] is False
    assert report["candidate_graph_review_pack"]["authority_write_performed"] is False
    assert report["candidate_graph_review_pack"]["minimal_edit_surface"]["supported"] is True
    assert report["candidate_graph_review_pack"]["lanes"]["candidate"]
    assert report["candidate_graph_review_pack"]["lanes"]["accepted_current"] == []
    assert report["candidate_graph_review_pack"]["raw_body_return_capability"] == "denied"
    assert report["quality_gate"]["source_to_candidate_graph"] == "PASS"
    assert "live_projection_join_unproven" in report["gaps"]
    assert all(
        item["raw_return_capability"] == "denied"
        for item in report["candidate_graph_review_pack"]["evidence"]
    )


def test_neuron_knowledge_source_to_candidate_graph_denies_production_without_mutation(
    tmp_path,
    capsys,
):
    ledger = tmp_path / "ledger.sqlite"

    rc = main(
        [
            "source-to-candidate-graph",
            "--project",
            "neurons",
            "--target",
            "production",
            "--ledger",
            str(ledger),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "object_substrate_cli_denied.v1"
    assert report["status"] == "denied"
    assert report["mutation_performed"] is False
    assert report["production_mutation_performed"] is False
    assert ledger.exists() is False


def test_neuron_knowledge_source_to_candidate_graph_does_not_create_missing_local_store(
    tmp_path,
    capsys,
):
    ledger = tmp_path / "missing-ledger.sqlite"

    rc = main(
        [
            "source-to-candidate-graph",
            "--project",
            "neurons",
            "--target",
            "local_test",
            "--ledger",
            str(ledger),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "source_to_candidate_graph_activation.v1"
    assert report["status"] == "FAIL"
    assert report["production_mutation_performed"] is False
    assert report["ledger_mutation_performed"] is False
    assert "reference_corpus_store_missing" in report["gaps"]
    assert ledger.exists() is False


def test_neuron_knowledge_candidate_review_and_approval_board_cli_chain_local_test(
    tmp_path,
    capsys,
):
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.sqlite"
    manifest.write_text(json.dumps(_reference_manifest()), encoding="utf-8")

    assert (
        main(
            [
                "corpus-ingest",
                "--project",
                "neurons",
                "--target",
                "local_test",
                "--ledger",
                str(ledger),
                "--manifest-file",
                str(manifest),
                "--storage-mode",
                "managed_snapshot",
            ]
        )
        == 0
    )
    ingest = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "source-to-candidate-graph",
                "--project",
                "neurons",
                "--target",
                "local_test",
                "--ledger",
                str(ledger),
                "--corpus-id",
                ingest["corpus_id"],
            ]
        )
        == 0
    )
    graph = json.loads(capsys.readouterr().out)
    pack_file = tmp_path / "candidate-pack.json"
    pack_file.write_text(json.dumps(graph["candidate_graph_review_pack"]), encoding="utf-8")
    candidate_id = graph["candidate_graph_review_pack"]["lanes"]["candidate"][0]["object_id"]
    edits_file = tmp_path / "candidate-edits.json"
    edits_file.write_text(
        json.dumps(
            [
                {
                    "action": "update_object",
                    "object_id": candidate_id,
                    "fields": {
                        "summary": "Reviewer clarified this candidate before local approval.",
                        "recommended_action": "promote",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "candidate-review-edit",
                "--pack-file",
                str(pack_file),
                "--edits-file",
                str(edits_file),
                "--reviewer-id",
                "reviewer-local",
            ]
        )
        == 0
    )
    edit_result = json.loads(capsys.readouterr().out)
    assert edit_result["schema_version"] == "candidate_review_edit_result.v1"
    assert edit_result["candidate_state_changed"] is True
    assert edit_result["authority_write_performed"] is False
    assert edit_result["production_mutation_performed"] is False
    assert edit_result["updated_pack"]["lanes"]["accepted_current"] == []
    edited_pack_file = tmp_path / "edited-candidate-pack.json"
    edited_pack_file.write_text(json.dumps(edit_result["updated_pack"]), encoding="utf-8")
    decisions_file = tmp_path / "approval-decisions.json"
    decisions_file.write_text(
        json.dumps(
            [
                {
                    "action": "promote",
                    "object_id": candidate_id,
                    "reason": "Local test approval board preview.",
                    "approved_by": "reviewer-local",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "approval-board-decide",
                "--target",
                "local_test",
                "--pack-file",
                str(edited_pack_file),
                "--decisions-file",
                str(decisions_file),
                "--reviewer-id",
                "reviewer-local",
            ]
        )
        == 0
    )
    decision_result = json.loads(capsys.readouterr().out)
    assert decision_result["schema_version"] == "approval_board_decision_result.v1"
    assert decision_result["permission"] == "allowed"
    assert decision_result["authority_write_scope"] == "local_test"
    assert decision_result["production_mutation_performed"] is False
    assert decision_result["updated_pack"]["lanes"]["accepted_current"][0]["object_id"] == candidate_id


def test_neuron_knowledge_approval_board_cli_denies_production_without_mutation(
    tmp_path,
    capsys,
):
    pack_file = tmp_path / "candidate-pack.json"
    decisions_file = tmp_path / "approval-decisions.json"
    pack_file.write_text(
        json.dumps(
            {
                "schema_version": "object_pack.v1",
                "route": "candidate_graph_review",
                "candidate_graph_hash": "sha256:" + "5" * 64,
                "objects": [],
                "edges": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    decisions_file.write_text(
        json.dumps([{"action": "promote", "object_id": "ko:ReferenceDocument:test"}]),
        encoding="utf-8",
    )

    rc = main(
        [
            "approval-board-decide",
            "--target",
            "production",
            "--pack-file",
            str(pack_file),
            "--decisions-file",
            str(decisions_file),
            "--reviewer-id",
            "reviewer-local",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert result["schema_version"] == "approval_board_decision_result.v1"
    assert result["permission"] == "denied"
    assert result["production_mutation_performed"] is False
    assert result["authority_write_performed"] is False
    assert result["promotion_plan"]["production_mutation_performed"] is False


def test_neuron_knowledge_golden_query_eval_baseline(capsys):
    assert main(["golden-query-eval", "--baseline"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "baseline_red"
    assert len(report["queries"]) >= 10


def test_neuron_knowledge_golden_query_eval_phase_coverage(capsys):
    assert main(["golden-query-eval", "--phase-coverage"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "knowledge_object_phase_golden_query_coverage.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["release_quality_gate"] == "not_green"


def test_neuron_knowledge_golden_query_eval_source_to_authority_gate(capsys):
    assert main(["golden-query-eval", "--source-to-authority-gate"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "source_to_authority_quality_gate_report.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["release_quality_gate"] == "not_green"
    assert report["production_mutation_performed"] is False


def test_neuron_knowledge_memory_regeneration_live_args_fail_closed(tmp_path, capsys):
    rc = main(
        [
            "memory-regeneration",
            "run",
            "--output",
            "project-memory",
            "--ledger",
            str(tmp_path / "missing-ledger.sqlite3"),
            "--enqueue",
            "--ingress-url",
            "http://127.0.0.1:18080",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_native_memory_execute_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "native-memory-sync",
            "--ledger",
            str(tmp_path / "missing-ledger.sqlite3"),
            "--native-memory-id",
            "mem_test",
            "--execute",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_session_memory_gc_execute_requires_approval(tmp_path, capsys):
    # GC executor는 벤더링됐지만 live --execute는 approval 게이트 뒤다. 유효 approval
    # 없이 --execute하면 네트워크/뮤테이션 전에 fail closed(approval error, rc!=0).
    rc = main(
        [
            "session-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_session",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_transcript_memory_gc_execute_disable_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "transcript-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_transcript",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute-disable",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["hard_delete_performed"] is False


def test_neuron_knowledge_delegates_transcript_backfill_help(capsys):
    assert main(["transcript-backfill", "--help"]) == 0
    assert "usage: transcript-backfill" in capsys.readouterr().out


def test_neuron_knowledge_transcript_volume_gc_execute_requires_approval(tmp_path, capsys):
    rc = main(
        [
            "transcript-volume-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--transcript-dataset-id",
            "ds_transcript",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_transcript_session_gc_execute_requires_approval(tmp_path, capsys):
    rc = main(
        [
            "transcript-session-gc",
            "--transcript-dataset-id",
            "ds_transcript",
            "--session-memory-dataset-id",
            "ds_session_memory",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--backup-dir",
            str(tmp_path / "backup"),
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_couchdb_command_surface_metadata_importable():
    from agent_knowledge.cli import COMMAND_METADATA

    assert isinstance(COMMAND_METADATA, dict)
    assert COMMAND_METADATA


def test_neuron_knowledge_couchdb_command_surface_classification():
    from agent_knowledge.cli import COMMAND_METADATA

    couchdb_build_metadata = COMMAND_METADATA["couchdb-session-memory-build"]
    transcript_migration_metadata = COMMAND_METADATA["transcript-migration"]
    couchdb_migration_flow_metadata = COMMAND_METADATA["couchdb-migration-flow"]
    legacy_session_memory_metadata = COMMAND_METADATA["neuron-session-memory-build"]

    assert set(COMMAND_METADATA).issubset(COMMAND_HANDLERS)

    for command in (
        "couchdb-session-memory-build",
        "transcript-migration",
        "couchdb-migration-flow",
        "neuron-session-memory-build",
    ):
        assert command in COMMAND_HANDLERS, f"command '{command}' should exist in COMMAND_HANDLERS"
        assert command in COMMAND_METADATA, f"command '{command}' should exist in COMMAND_METADATA"

    assert couchdb_build_metadata["runtime_category"] == "active_runtime"
    assert couchdb_build_metadata["deletion_candidate"] is False
    assert couchdb_build_metadata["live_mutation_requires_approval"] is True

    assert transcript_migration_metadata["runtime_category"] == "human_gated_migration"
    assert transcript_migration_metadata["deletion_candidate"] is False
    assert transcript_migration_metadata["live_mutation_requires_approval"] is True

    assert couchdb_migration_flow_metadata["runtime_category"] == "human_gated_migration"
    assert couchdb_migration_flow_metadata["deletion_candidate"] is False

    assert legacy_session_memory_metadata["runtime_category"] == "legacy_compatibility"
    assert legacy_session_memory_metadata["deletion_candidate"] is False
    assert legacy_session_memory_metadata["live_mutation_requires_approval"] is True

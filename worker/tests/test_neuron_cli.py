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
        "golden-query-eval",
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
    assert status["snapshot_count"] == 2
    assert status["chunk_count"] == 2
    assert status["extraction_runs"][0]["status"] == "completed"
    assert status["freshness_gaps"][0]["source_url_status"] == "missing_manual_text"
    assert status["gaps"] == []


def test_neuron_knowledge_golden_query_eval_baseline(capsys):
    assert main(["golden-query-eval", "--baseline"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "baseline_red"
    assert len(report["queries"]) >= 10


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

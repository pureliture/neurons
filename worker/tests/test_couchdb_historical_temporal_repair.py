from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_knowledge.couchdb_source.historical_temporal_repair as temporal_repair
from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.couchdb_source.document_model import (
    ProjectionStatus,
    SourceRedactionLeak,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_projection_state_document,
    build_source_locator_hash,
    build_source_revision_token,
    build_transcript_session_document,
    coverage_manifest_doc_id,
    projection_state_doc_id,
)
from agent_knowledge.couchdb_source.historical_temporal_repair import (
    REPAIR_SCHEMA_VERSION,
    collect_historical_candidates,
    main,
    repair_historical_temporal_gaps,
)
from agent_knowledge.couchdb_source.migration_cli import enumerate_provider_files
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore, SourceStoreConflict
from agent_knowledge.session_memory.transcript_chunking import build_transcript_chunks
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
    TranscriptTurn,
)
from agent_knowledge.session_memory.transcript_parsers import ParsedTranscript


PROJECT = "neurons"
PROVIDER = "codex"
SESSION_HASH = "sha256:" + "b" * 64
SOURCE_LOCATOR_HASH = "sha256:" + "d" * 64


def _limits() -> dict[str, int]:
    return {
        "source_file_limit": 10,
        "source_entry_limit": 100,
        "target_document_limit": 10,
        "patch_limit": 10,
    }


def _cli_args(source_root: Path, **overrides: object) -> list[str]:
    options: dict[str, object] = {
        "provider": PROVIDER,
        "project": PROJECT,
        "source_root": source_root,
        "source_file_limit": 1,
        "source_entry_limit": 100,
        "target_document_limit": 1,
        "patch_limit": 1,
        "batch_limit": None,
        "max_runtime_seconds": 30,
    }
    options.update(overrides)
    args: list[str] = []
    for name, value in options.items():
        if isinstance(value, bool):
            if value:
                args.append(f"--{name.replace('_', '-')}")
        elif value is not None:
            args.extend((f"--{name.replace('_', '-')}", str(value)))
    return args


def _seed_store(
    *,
    source_locator_hash: str = SOURCE_LOCATOR_HASH,
) -> tuple[InMemoryCouchDBSourceStore, dict]:
    store = InMemoryCouchDBSourceStore()
    session = TranscriptSession(
        session_id_hash=SESSION_HASH,
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-07-01T10:00:00Z",
        ended_at="2026-07-01T10:01:00Z",
    )
    missing = TranscriptChunk.from_text(
        chunk_id="historical-chunk",
        session_id_hash=SESSION_HASH,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=2,
        text="public historical repair body",
    )
    chunk = build_conversation_chunk_document(
        chunk=missing,
        source_locator_hash=source_locator_hash,
    )
    store.put(build_transcript_session_document(session=session))
    store.put(chunk)
    coverage = build_coverage_manifest_document(
        session_id_hash=SESSION_HASH,
        provider=PROVIDER,
        project=PROJECT,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk["content_hash"]],
        tool_evidence_coverage_hashes=[],
        conversation_revision_tokens=[
            build_source_revision_token(chunk, material_hash_field="content_hash")
        ],
    )
    store.put(coverage)
    store.put(
        build_projection_state_document(
            session_id_hash=SESSION_HASH,
            provider=PROVIDER,
            project=PROJECT,
            source_hash=coverage["source_hash"],
            projection_status=ProjectionStatus.PENDING,
        )
    )
    repaired = dict(chunk)
    repaired.update(
        {
            "observed_at_start": "2026-07-01T10:00:01Z",
            "observed_at_end": "2026-07-01T10:00:05Z",
        }
    )
    return store, repaired


def _add_gap_chunk(
    store: InMemoryCouchDBSourceStore,
    *,
    chunk_id: str,
    text: str,
    source_locator_hash: str = SOURCE_LOCATOR_HASH,
) -> dict:
    chunk = build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id=chunk_id,
            session_id_hash=SESSION_HASH,
            provider=PROVIDER,
            project=PROJECT,
            turn_start_index=3,
            turn_end_index=3,
            text=text,
        ),
        source_locator_hash=source_locator_hash,
    )
    store.put(chunk)
    repaired = dict(chunk)
    repaired.update(
        {
            "observed_at_start": "2026-07-01T10:00:06Z",
            "observed_at_end": "2026-07-01T10:00:09Z",
        }
    )
    return repaired


def _mark_projection_projected(store: InMemoryCouchDBSourceStore) -> None:
    coverage = store.get(coverage_manifest_doc_id(SESSION_HASH))
    assert coverage is not None
    source_hash = str(coverage["source_hash"])
    store.put(
        build_projection_state_document(
            session_id_hash=SESSION_HASH,
            provider=PROVIDER,
            project=PROJECT,
            projection_status=ProjectionStatus.PROJECTED,
            active_content_hash="sha256:" + "c" * 64,
            source_hash=source_hash,
            projected_source_hash=source_hash,
        )
    )


def _temporal_chunk(*, session_id_hash: str, chunk_id: str) -> TranscriptChunk:
    return TranscriptChunk.from_text(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=1,
        text=f"public temporal source {chunk_id}",
        observed_at_start="2026-07-01T10:00:01Z",
        observed_at_end="2026-07-01T10:00:02Z",
    )


def _parsed_temporal_source(session_id_hash: str) -> ParsedTranscript:
    return ParsedTranscript(
        session=TranscriptSession(
            session_id_hash=session_id_hash,
            provider=PROVIDER,
            project=PROJECT,
            started_at="2026-07-01T10:00:00Z",
        ),
        turns=[],
        tool_events=[],
        parser_warnings=[],
        source_status="source_locator_private_spool_only",
    )


def test_chunking_passes_valid_native_turn_bounds_and_drops_untrusted_bounds():
    session = TranscriptSession(
        session_id_hash=SESSION_HASH,
        provider=PROVIDER,
        project=PROJECT,
        started_at="",
    )
    valid = ParsedTranscript(
        session=session,
        turns=[
            TranscriptTurn("turn-a", SESSION_HASH, 1, "user", "2026-07-01T10:00:01Z", "hello"),
            TranscriptTurn("turn-b", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:05Z", "world"),
        ],
        tool_events=[],
        parser_warnings=[],
        source_status="source_locator_private_spool_only",
    )
    reversed_turns = ParsedTranscript(
        session=session,
        turns=[
            TranscriptTurn("turn-c", SESSION_HASH, 1, "user", "2026-07-01T10:00:05Z", "hello"),
            TranscriptTurn("turn-d", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:01Z", "world"),
        ],
        tool_events=[],
        parser_warnings=[],
        source_status="source_locator_private_spool_only",
    )

    assert [(chunk.observed_at_start, chunk.observed_at_end) for chunk in build_transcript_chunks(valid)] == [
        ("2026-07-01T10:00:01Z", "2026-07-01T10:00:05Z")
    ]
    missing = ParsedTranscript(
        session=session,
        turns=[
            TranscriptTurn("turn-e", SESSION_HASH, 1, "user", "", "hello"),
            TranscriptTurn("turn-f", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:05Z", "world"),
        ],
        tool_events=[],
        parser_warnings=[],
        source_status="source_locator_private_spool_only",
    )
    invalid = ParsedTranscript(
        session=session,
        turns=[
            TranscriptTurn("turn-g", SESSION_HASH, 1, "user", "not-a-time", "hello"),
            TranscriptTurn("turn-h", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:05Z", "world"),
        ],
        tool_events=[],
        parser_warnings=[],
        source_status="source_locator_private_spool_only",
    )
    for parsed in (reversed_turns, missing, invalid):
        assert [(chunk.observed_at_start, chunk.observed_at_end) for chunk in build_transcript_chunks(parsed)] == [
        ("", "")
        ]


def test_dry_run_uses_live_gap_snapshot_and_never_mutates_source():
    store, candidate = _seed_store()
    before = store.get(candidate["_id"])

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["schema_version"] == REPAIR_SCHEMA_VERSION
    assert report["status"] == "dry_run"
    assert report["snapshot_gap_count"] == 1
    assert report["planned_update_count"] == 1
    assert report["mutation_performed"] is False
    assert report["plan_digest"].startswith("sha256:")
    assert store.get(candidate["_id"]) == before


def test_execute_patches_only_exact_snapshot_then_recomputes_successful_session():
    store, candidate = _seed_store()
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
    )

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
    )

    current = store.get(candidate["_id"])
    assert report["updated_count"] == 1
    assert report["coverage_recomputed_session_count"] == 1
    assert report["projection_pending_session_count"] == 1
    assert current["content_hash"] == candidate["content_hash"]
    assert current["observed_at_start"] == candidate["observed_at_start"]
    assert store.get(projection_state_doc_id(SESSION_HASH))["projection_status"] == ProjectionStatus.PENDING
    assert store.get(coverage_manifest_doc_id(SESSION_HASH))["source_hash"]


def test_content_mismatch_invalid_or_reversed_historical_bounds_never_become_candidates():
    store, candidate = _seed_store()
    conflicting = dict(candidate)
    conflicting["content_hash"] = "sha256:" + "f" * 64
    invalid = dict(candidate)
    invalid["observed_at_start"] = "2026-07-01T10:00:06Z"
    invalid["observed_at_end"] = "2026-07-01T10:00:01Z"

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[conflicting, invalid],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["content_conflict_count"] == 1
    assert report["mutation_performed"] is False
    assert store.get(candidate["_id"])["observed_at_start"] == ""


def test_candidate_duplicates_are_idempotent_but_archive_conflicts_are_excluded():
    store, candidate = _seed_store()

    duplicate_report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, dict(candidate)],
        max_runtime_seconds=30,
        **_limits(),
    )
    conflicting = dict(candidate)
    conflicting["observed_at_end"] = "2026-07-01T10:00:06Z"
    conflict_report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, conflicting],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert duplicate_report["candidate_duplicate_count"] == 1
    assert duplicate_report["planned_update_count"] == 1
    assert conflict_report["archive_conflict_count"] == 1
    assert conflict_report["target_archive_conflict_count"] == 1
    assert conflict_report["planned_update_count"] == 0
    assert conflict_report["selected_batch_count"] == 0
    assert conflict_report["gap_count"] == 1


def test_unmatched_archive_conflict_is_informational_not_a_live_gap_error():
    store, candidate = _seed_store()
    unrelated_a = dict(candidate)
    unrelated_b = dict(candidate)
    unrelated_a["_id"] = "conversation_chunk:unmatched-archive-candidate"
    unrelated_b["_id"] = unrelated_a["_id"]
    unrelated_b["observed_at_end"] = "2026-07-01T10:00:06Z"

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, unrelated_a, unrelated_b],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["archive_conflict_count"] == 1
    assert report["target_archive_conflict_count"] == 0
    assert report["error_count"] == 0


def test_full_target_snapshot_changes_plan_digest_and_blocks_old_execute_plan():
    store, candidate = _seed_store()
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
        **_limits(),
    )
    changed_bound = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=11,
    )
    changed_entry_bound = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
        source_file_limit=10,
        source_entry_limit=11,
        target_document_limit=10,
        patch_limit=10,
    )
    changed_redaction_skip_count = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
        non_target_source_redaction_skip_count=1,
        **_limits(),
    )
    changed_redaction_skip_execute = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
        non_target_source_redaction_skip_count=1,
        **_limits(),
    )
    _add_gap_chunk(store, chunk_id="unmatched-new-gap", text="unmatched new temporal gap")

    execute = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        target_fingerprints={"couchdb_source": "sha256:" + "a" * 64},
        **_limits(),
    )

    assert changed_bound["plan_digest"] != plan["plan_digest"]
    assert changed_entry_bound["source_entry_limit"] == 11
    assert changed_entry_bound["plan_digest"] != plan["plan_digest"]
    assert changed_redaction_skip_count["plan_digest"] != plan["plan_digest"]
    assert changed_redaction_skip_execute["status"] == "blocked_plan_drift"
    assert execute["status"] == "blocked_plan_drift"
    assert store.get(candidate["_id"])["observed_at_start"] == ""


def test_default_source_entry_limit_is_recorded_and_bound_into_plan_digest():
    store, candidate = _seed_store()
    common = {
        "source_store": store,
        "provider": PROVIDER,
        "project": PROJECT,
        "historical_documents": [candidate],
        "source_file_limit": 10,
        "target_document_limit": 10,
        "patch_limit": 10,
        "max_runtime_seconds": 30,
    }

    default_plan = repair_historical_temporal_gaps(**common)
    explicit_plan = repair_historical_temporal_gaps(
        **common,
        source_entry_limit=temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT,
    )
    changed_plan = repair_historical_temporal_gaps(
        **common,
        source_entry_limit=temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT + 1,
    )

    assert default_plan["source_entry_limit"] == temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT
    assert default_plan["plan_digest"] == explicit_plan["plan_digest"]
    assert default_plan["plan_digest"] != changed_plan["plan_digest"]


def test_repair_selects_a_deterministic_bounded_batch_and_reports_remaining_work():
    store, candidate = _seed_store()
    second = _add_gap_chunk(store, chunk_id="patch-limit-second", text="second temporal gap")
    third = _add_gap_chunk(store, chunk_id="patch-limit-third", text="third temporal gap")
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[third, candidate, second],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=3,
        batch_limit=2,
        max_runtime_seconds=30,
    )
    reordered_plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[second, third, candidate],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=3,
        batch_limit=2,
        max_runtime_seconds=30,
    )
    bounded = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second, third],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=3,
        batch_limit=2,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
    )

    assert plan["eligible_update_count"] == 3
    assert plan["total_eligible_update_count"] == plan["eligible_update_count"]
    assert plan["selected_batch_count"] == 2
    assert plan["remaining_eligible_update_count"] == 1
    assert plan["plan_digest"] == reordered_plan["plan_digest"]
    assert plan["snapshot_fingerprint"].startswith("sha256:")
    assert plan["selected_batch_fingerprint"] == reordered_plan["selected_batch_fingerprint"]
    assert plan["status"] == "dry_run_batch_ready"
    assert bounded["status"] == "completed_batch_pending"
    assert bounded["batch_execution_succeeded"] is True
    assert bounded["error_count"] == 0
    assert bounded["updated_count"] == 2
    assert bounded["remaining_temporal_gap_count"] == 1


def test_batch_stays_ready_while_an_unrepairable_target_gap_remains():
    store, candidate = _seed_store()
    unrepairable = _add_gap_chunk(
        store,
        chunk_id="unrepairable-target",
        text="unrepairable target gap",
    )
    mismatched_candidate = dict(unrepairable)
    mismatched_candidate["content_hash"] = "sha256:" + "e" * 64

    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, mismatched_candidate],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=2,
        batch_limit=1,
        max_runtime_seconds=30,
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, mismatched_candidate],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=2,
        batch_limit=1,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
    )

    assert plan["status"] == "dry_run_batch_ready_with_gaps"
    assert plan["batch_ready"] is True
    assert plan["unrepairable_gap_count"] == 1
    assert plan["selected_batch_count"] == 1
    assert report["status"] == "completed_batch_complete_with_gaps"
    assert report["batch_execution_succeeded"] is True
    assert report["updated_count"] == 1
    assert report["remaining_temporal_gap_count"] == 1
    assert report["remaining_unresolved_gap_count"] == 1


def test_patch_limit_without_batch_mode_blocks_before_any_mutation():
    store, candidate = _seed_store()
    second = _add_gap_chunk(store, chunk_id="patch-limit-second", text="second temporal gap")
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=1,
        max_runtime_seconds=30,
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        source_file_limit=10,
        source_entry_limit=100,
        target_document_limit=10,
        patch_limit=1,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
    )

    assert plan["status"] == "dry_run_patch_limit_exceeded"
    assert report["status"] == "blocked_patch_limit"
    assert report["mutation_performed"] is False
    assert store.get(candidate["_id"])["observed_at_start"] == ""
    assert store.get(second["_id"])["observed_at_start"] == ""


def test_candidate_collection_parses_only_snapshot_locator_targets(tmp_path, monkeypatch):
    selected = tmp_path / "a-selected.jsonl"
    irrelevant_malformed = tmp_path / "z-irrelevant.jsonl"
    selected.write_text("valid selected fixture", encoding="utf-8")
    irrelevant_malformed.write_text("malformed source must remain unread", encoding="utf-8")
    parsed_paths = []

    def parse_selected(provider, path, *, project, source_locator_hash):
        parsed_paths.append((Path(path), source_locator_hash))
        return ParsedTranscript(
            session=TranscriptSession(
                session_id_hash=SESSION_HASH,
                provider=provider,
                project=project,
                started_at="2026-07-01T10:00:00Z",
            ),
            turns=[
                TranscriptTurn("turn-a", SESSION_HASH, 1, "user", "2026-07-01T10:00:01Z", "hello"),
                TranscriptTurn("turn-b", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:02Z", "world"),
            ],
            tool_events=[],
            parser_warnings=[],
            source_status="source_locator_private_spool_only",
        )

    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(temporal_repair, "parse_transcript_source", parse_selected)

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=2,
        source_entry_limit=100,
        required_source_locator_hashes={build_source_locator_hash(str(selected))},
    )

    assert timed_out is False
    assert [path for path, _locator_hash in parsed_paths] == [selected]
    assert report["source_file_count"] == 1
    assert report["unmatched_source_locator_hash_count"] == 0
    assert report["source_scan_complete"] is True
    assert report["required_target_scan_satisfied"] is True
    assert report["source_tree_scan_complete"] is True
    assert report["source_scan_truncated"] is False
    assert report["parser_error_count"] == 0
    assert len(documents) == 1


def test_candidate_collection_without_locator_filter_parses_all_bounded_provider_sources(
    tmp_path, monkeypatch
):
    first = tmp_path / "a-first.jsonl"
    second = tmp_path / "z-second.jsonl"
    first.write_text("first fixture", encoding="utf-8")
    second.write_text("second fixture", encoding="utf-8")
    parsed_paths = []

    def parse_source(provider, path, *, project, source_locator_hash):
        parsed_paths.append((Path(path), source_locator_hash))
        return ParsedTranscript(
            session=TranscriptSession(
                session_id_hash=SESSION_HASH,
                provider=provider,
                project=project,
                started_at="2026-07-01T10:00:00Z",
            ),
            turns=[
                TranscriptTurn("turn-a", SESSION_HASH, 1, "user", "2026-07-01T10:00:01Z", "hello"),
                TranscriptTurn("turn-b", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:02Z", "world"),
            ],
            tool_events=[],
            parser_warnings=[],
            source_status="source_locator_private_spool_only",
        )

    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(temporal_repair, "parse_transcript_source", parse_source)

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=2,
        source_entry_limit=100,
        required_source_locator_hashes=None,
    )

    assert timed_out is False
    assert [path for path, _locator_hash in parsed_paths] == [first, second]
    assert report["source_file_count"] == 2
    assert report["source_scan_complete"] is True
    assert report["source_tree_scan_complete"] is True
    assert len(documents) == 2


def test_candidate_collection_times_out_during_locator_filter_before_source_parsing(tmp_path, monkeypatch):
    selected = tmp_path / "selected.jsonl"
    selected.write_text("unread fixture", encoding="utf-8")
    monkeypatch.setattr(
        temporal_repair,
        "parse_transcript_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source parse must not run")),
    )
    clock = iter((0.0, 31.0))

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        source_entry_limit=100,
        required_source_locator_hashes={build_source_locator_hash(str(selected))},
        started=0.0,
        max_runtime_seconds=30,
        monotonic=lambda: next(clock),
    )

    assert documents == []
    assert timed_out is True
    assert report["parsed_source_count"] == 0
    assert report["unmatched_source_locator_hash_count"] == 1


def test_candidate_collection_checks_deadline_for_non_provider_entries_before_parsing(
    tmp_path, monkeypatch
):
    for index in range(2):
        (tmp_path / f"ignored-{index}.txt").write_text("not a provider source", encoding="utf-8")
    monkeypatch.setattr(
        temporal_repair,
        "parse_transcript_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source parse must not run")),
    )
    clock = iter((0.0, 0.0, 31.0))

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        source_entry_limit=100,
        required_source_locator_hashes={"sha256:" + "a" * 64},
        started=0.0,
        max_runtime_seconds=30,
        monotonic=lambda: next(clock),
    )

    assert documents == []
    assert timed_out is True
    assert report["source_entry_count"] == 1
    assert report["discovered_source_file_count"] == 0
    assert report["parsed_source_count"] == 0


def test_candidate_collection_times_out_inside_flat_directory_scan_before_sorting(
    tmp_path, monkeypatch
):
    scan_count = 0

    class FakeEntry:
        def __init__(self, index: int):
            self.path = str(tmp_path / f"ignored-{index:05d}.txt")

        def is_dir(self, *, follow_symlinks: bool) -> bool:
            assert follow_symlinks is False
            return False

    class FakeScanner:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal scan_count
            scan_count += 1
            return FakeEntry(scan_count)

    monkeypatch.setattr(temporal_repair.os, "scandir", lambda _path: FakeScanner())

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        source_entry_limit=100,
        required_source_locator_hashes={"sha256:" + "a" * 64},
        started=0.0,
        max_runtime_seconds=30,
        monotonic=lambda: 31.0 if scan_count >= 2 else 0.0,
    )

    assert documents == []
    assert timed_out is True
    assert scan_count <= 2
    assert report["source_entry_count"] <= 2


def test_candidate_collection_uses_safe_finite_default_for_none(tmp_path):
    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        source_entry_limit=None,
        required_source_locator_hashes={"sha256:" + "a" * 64},
    )

    assert documents == []
    assert timed_out is False
    assert report["source_entry_limit"] == temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT
    assert temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT == 10_000


@pytest.mark.parametrize(
    ("provider", "files", "links"),
    [
        (
            "codex",
            [("nested/valid.jsonl", True), ("uppercase.JSONL", False)],
            [("linked.jsonl", "nested/valid.jsonl", False)],
        ),
        (
            "gemini",
            [
                ("project/chats/valid.jsonl", True),
                ("project/chats/native.json", True),
                ("project/chats/uppercase.JSONL", False),
                ("parent/project/chats/nested.jsonl", False),
            ],
            [("project/chats/linked.jsonl", "project/chats/valid.jsonl", True)],
        ),
        (
            "antigravity",
            [
                ("project/.system_generated/nested/valid.jsonl", True),
                ("project/.system_generated/nested/uppercase.JSONL", False),
                ("project/ordinary/invalid.jsonl", False),
            ],
            [
                (
                    "project/.system_generated/nested/linked.jsonl",
                    "project/.system_generated/nested/valid.jsonl",
                    True,
                )
            ],
        ),
        (
            "grok",
            [("session/updates.jsonl", True), ("session/UPDATES.JSONL", False)],
            [("linked/updates.jsonl", "session/updates.jsonl", False)],
        ),
    ],
)
def test_bounded_provider_iterator_matches_migration_predicates(tmp_path, provider, files, links):
    accepted: set[Path] = set()
    rejected: set[Path] = set()
    for relative, expected in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        (accepted if expected else rejected).add(path)
    for relative, target, expected in links:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(tmp_path / target)
        (accepted if expected else rejected).add(path)

    actual = set(
        temporal_repair._iter_provider_files(provider, tmp_path, source_entry_limit=100)
    )

    assert actual == set(enumerate_provider_files(provider, tmp_path))
    assert accepted <= actual
    assert rejected.isdisjoint(actual)


def test_candidate_collection_stops_at_source_scan_limit_before_parsing(tmp_path, monkeypatch):
    ignored = tmp_path / "a-ignored.jsonl"
    another_ignored = tmp_path / "b-ignored.jsonl"
    ignored.write_text("ignored source", encoding="utf-8")
    another_ignored.write_text("another ignored source", encoding="utf-8")
    monkeypatch.setattr(
        temporal_repair,
        "parse_transcript_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source parse must not run")),
    )

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        source_entry_limit=100,
        required_source_locator_hashes={"sha256:" + "a" * 64},
    )

    assert documents == []
    assert timed_out is False
    assert report["source_file_limit_exceeded"] is True
    assert report["source_file_count"] == 0
    assert report["parsed_source_count"] == 0


def test_candidate_collection_skips_scan_without_required_locator_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        temporal_repair,
        "_iter_source_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source scan must not run")),
    )

    documents, report, timed_out = collect_historical_candidates(
        provider=PROVIDER,
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        required_source_locator_hashes=set(),
    )

    assert documents == []
    assert timed_out is False
    assert report["source_file_count"] == 0
    assert report["required_source_locator_hash_count"] == 0
    assert report["source_entry_limit"] == temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT


def test_wrong_session_candidate_is_not_planned_for_snapshot_target():
    store, candidate = _seed_store()
    wrong_session_candidate = dict(candidate)
    wrong_session_candidate["session_id_hash"] = "sha256:" + "f" * 64

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[wrong_session_candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["session_identity_conflict_count"] == 1
    assert report["error_count"] == 1


def test_legacy_target_without_locator_plans_only_a_valid_replacement_locator():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["legacy_target_count"] == 1
    assert report["planned_update_count"] == 1
    assert report["target_locator_missing_count"] == 0
    assert report["content_conflict_count"] == 0
    assert report["status"] == "dry_run"


def test_legacy_target_requires_explicit_identity_match_opt_in():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["match_strategy"] == "exact_source_locator"
    assert report["legacy_identity_match_required_count"] == 1
    assert report["planned_update_count"] == 0
    assert report["mutation_performed"] is False
    assert report["status"] == "dry_run_with_gaps"


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "other-project"),
        ("session_id_hash", "sha256:" + "e" * 64),
        ("content_hash", "sha256:" + "e" * 64),
    ],
)
def test_legacy_identity_mismatch_never_plans_a_replacement_locator(field, value):
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    candidate[field] = value

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["legacy_candidate_missing_count"] == 1
    assert report["status"] == "dry_run_with_gaps"


def test_legacy_target_project_mismatch_is_not_identity_matched():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    target = store.get(candidate["_id"])
    assert target is not None
    target = dict(target)
    target["project"] = "other-project"

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        snapshot_documents=[target],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["snapshot_integrity_error_count"] == 1
    assert report["status"] == "dry_run_with_gaps"


def test_legacy_invalid_native_interval_never_plans_a_replacement_locator():
    store, candidate = _seed_store(source_locator_hash="")
    candidate.update(
        {
            "source_locator_hash": SOURCE_LOCATOR_HASH,
            "observed_at_start": "2026-07-01T10:00:05Z",
            "observed_at_end": "2026-07-01T10:00:01Z",
        }
    )

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["legacy_candidate_missing_count"] == 1
    assert report["status"] == "dry_run_with_gaps"


def test_legacy_identity_requires_one_target_and_one_candidate():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    second_candidate = dict(candidate)
    second_candidate["_id"] = f"{candidate['_id']}-second"
    second_candidate["source_locator_hash"] = "sha256:" + "e" * 64

    candidate_report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second_candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    target = store.get(candidate["_id"])
    assert target is not None
    second_target = dict(target)
    second_target.update({"_id": f"{target['_id']}-second", "_rev": "1-second"})
    target_report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        snapshot_documents=[target, second_target],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert candidate_report["planned_update_count"] == 0
    assert candidate_report["legacy_candidate_ambiguous_count"] == 1
    assert target_report["planned_update_count"] == 0
    assert target_report["legacy_target_ambiguous_count"] == 2


def test_normal_locator_target_never_uses_legacy_identity_fallback():
    store, candidate = _seed_store()
    alternate_locator_candidate = dict(candidate)
    alternate_locator_candidate["_id"] = f"{candidate['_id']}-alternate"
    alternate_locator_candidate["source_locator_hash"] = "sha256:" + "e" * 64

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[alternate_locator_candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["match_strategy"] == "legacy_identity_match"
    assert report["legacy_target_count"] == 0
    assert report["planned_update_count"] == 0
    assert report["content_conflict_count"] == 1


def test_plan_digest_binds_match_strategy_and_legacy_source_error_aggregate():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    exact_locator_plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )
    legacy_plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )
    legacy_error_plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        legacy_source_error_count=1,
        **_limits(),
    )

    assert exact_locator_plan["plan_digest"] != legacy_plan["plan_digest"]
    assert legacy_plan["plan_digest"] != legacy_error_plan["plan_digest"]
    assert legacy_error_plan["planned_update_count"] == 0
    assert legacy_error_plan["legacy_source_error_count"] == 1
    assert legacy_error_plan["legacy_source_error_blocked_count"] == 1
    assert legacy_error_plan["error_count"] == 2
    assert legacy_error_plan["gap_count"] == 2
    assert legacy_error_plan["status"] == "dry_run_with_gaps"

    executed = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        legacy_source_error_count=1,
        execute=True,
        expected_plan_digest=legacy_error_plan["plan_digest"],
        **_limits(),
    )

    assert executed["updated_count"] == 0
    assert executed["mutation_performed"] is False
    assert executed["status"] == "completed_with_errors"
    assert executed["error_count"] >= 1
    assert executed["gap_count"] >= 1


def test_legacy_source_errors_exclude_legacy_items_but_allow_exact_locator_batch():
    store, normal_candidate = _seed_store()
    legacy_candidate = _add_gap_chunk(
        store,
        chunk_id="legacy-source-error-target",
        text="legacy source error target",
        source_locator_hash="",
    )
    legacy_candidate["source_locator_hash"] = "sha256:" + "e" * 64

    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[normal_candidate, legacy_candidate],
        max_runtime_seconds=30,
        batch_limit=1,
        legacy_identity_match=True,
        legacy_source_error_count=1,
        **_limits(),
    )
    executed = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[normal_candidate, legacy_candidate],
        max_runtime_seconds=30,
        batch_limit=1,
        legacy_identity_match=True,
        legacy_source_error_count=1,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    exact_target = store.get(normal_candidate["_id"])
    legacy_target = store.get(legacy_candidate["_id"])
    assert exact_target is not None and legacy_target is not None
    assert plan["planned_update_count"] == 1
    assert plan["batch_ready"] is True
    assert plan["legacy_source_error_blocked_count"] == 1
    assert plan["error_count"] == 2
    assert plan["gap_count"] == 2
    assert executed["updated_count"] == 1
    assert executed["batch_execution_succeeded"] is True
    assert executed["status"] == "completed_batch_complete_with_gaps"
    assert executed["error_count"] == 3
    assert executed["gap_count"] == 3
    assert exact_target["source_locator_hash"] == SOURCE_LOCATOR_HASH
    assert legacy_target["source_locator_hash"] == ""
    assert legacy_target["observed_at_start"] == ""


def test_legacy_source_error_reports_a_blocker_for_each_legacy_target():
    store, first_candidate = _seed_store(source_locator_hash="")
    first_candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    second_candidate = _add_gap_chunk(
        store,
        chunk_id="second-legacy-source-error-target",
        text="second legacy source error target",
        source_locator_hash="",
    )
    second_candidate["source_locator_hash"] = "sha256:" + "e" * 64

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[first_candidate, second_candidate],
        max_runtime_seconds=30,
        batch_limit=1,
        legacy_identity_match=True,
        legacy_source_error_count=1,
        **_limits(),
    )

    assert report["legacy_target_count"] == 2
    assert report["legacy_source_error_count"] == 1
    assert report["legacy_source_error_blocked_count"] == 2
    assert report["planned_update_count"] == 0
    assert report["selected_batch_count"] == 0
    assert report["batch_ready"] is False
    assert report["unrepairable_gap_count"] == 3
    assert report["error_count"] == 3
    assert report["gap_count"] == 3
    assert report["status"] == "dry_run_with_gaps"


def test_legacy_target_execute_binds_empty_expected_locator_to_replacement_locator():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    patch_calls = []

    def patch_with_locator_cas(
        *,
        doc_id,
        expected_content_hash,
        expected_rev,
        observed_at_start,
        observed_at_end,
        expected_source_locator_hash,
        replacement_source_locator_hash,
    ):
        patch_calls.append(
            {
                "expected_source_locator_hash": expected_source_locator_hash,
                "replacement_source_locator_hash": replacement_source_locator_hash,
            }
        )
        current = store.get(doc_id)
        assert current is not None
        if current["source_locator_hash"] != expected_source_locator_hash:
            raise SourceStoreConflict("simulated locator CAS conflict")
        updated = dict(current)
        for key in ("_rev", "idempotency_key", "payload_hash"):
            updated.pop(key, None)
        updated["observed_at_start"] = observed_at_start
        updated["observed_at_end"] = observed_at_end
        updated["source_locator_hash"] = replacement_source_locator_hash
        return store.put(updated)

    store.patch_observed_time_if_content_hash = patch_with_locator_cas  # type: ignore[method-assign]
    coverage_before = store.get(coverage_manifest_doc_id(SESSION_HASH))
    assert coverage_before is not None
    _mark_projection_projected(store)
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        legacy_identity_match=True,
        **_limits(),
    )

    current = store.get(candidate["_id"])
    assert current is not None
    assert patch_calls == [
        {
            "expected_source_locator_hash": "",
            "replacement_source_locator_hash": SOURCE_LOCATOR_HASH,
        }
    ]
    assert report["updated_count"] == 1
    assert report["write_conflict_count"] == 0
    assert current["source_locator_hash"] == SOURCE_LOCATOR_HASH
    coverage_after = store.get(coverage_manifest_doc_id(SESSION_HASH))
    projection = store.get(projection_state_doc_id(SESSION_HASH))
    assert coverage_after is not None and projection is not None
    assert report["coverage_recomputed_session_count"] == 1
    assert report["projection_pending_session_count"] == 1
    assert coverage_after["source_hash"] != coverage_before["source_hash"]
    assert projection["projection_status"] == ProjectionStatus.PENDING


def test_legacy_uncertain_ack_without_replacement_locator_fails_closed():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH

    def patch_then_lose_acknowledgement(
        *,
        doc_id,
        expected_content_hash,
        expected_rev,
        observed_at_start,
        observed_at_end,
        expected_source_locator_hash,
        replacement_source_locator_hash,
    ):
        del expected_content_hash, expected_rev, replacement_source_locator_hash
        current = store.get(doc_id)
        assert current is not None
        assert current["source_locator_hash"] == expected_source_locator_hash == ""
        updated = dict(current)
        for key in ("_rev", "idempotency_key", "payload_hash"):
            updated.pop(key, None)
        updated["observed_at_start"] = observed_at_start
        updated["observed_at_end"] = observed_at_end
        store.put(updated)
        raise TimeoutError("private transport acknowledgement detail")

    store.patch_observed_time_if_content_hash = patch_then_lose_acknowledgement  # type: ignore[method-assign]
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        legacy_identity_match=True,
        **_limits(),
    )

    current = store.get(candidate["_id"])
    assert current is not None
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False
    assert report["write_error_count"] == 1
    assert current["source_locator_hash"] == ""
    assert "private transport acknowledgement detail" not in json.dumps(report)


def test_legacy_readback_without_replacement_locator_fails_postcheck():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    original_patch = store.patch_observed_time_if_content_hash

    def patch_without_locator_replacement(
        *,
        doc_id,
        expected_content_hash,
        expected_rev,
        observed_at_start,
        observed_at_end,
        expected_source_locator_hash,
        replacement_source_locator_hash,
    ):
        assert expected_source_locator_hash == ""
        assert replacement_source_locator_hash == SOURCE_LOCATOR_HASH
        return original_patch(
            doc_id=doc_id,
            expected_content_hash=expected_content_hash,
            expected_rev=expected_rev,
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
        )

    store.patch_observed_time_if_content_hash = patch_without_locator_replacement  # type: ignore[method-assign]
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        legacy_identity_match=True,
        **_limits(),
    )

    current = store.get(candidate["_id"])
    assert current is not None
    assert report["updated_count"] == 1
    assert report["write_conflict_count"] == 1
    assert report["status"] == "completed_with_errors"
    assert current["source_locator_hash"] == ""


def test_legacy_target_duplicate_candidate_records_are_ambiguous_even_when_locator_and_interval_match(
):
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    duplicate_candidate = dict(candidate)

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, duplicate_candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["legacy_candidate_ambiguous_count"] == 1
    assert report["status"] == "dry_run_with_gaps"


def test_legacy_target_invalid_replacement_locator_fails_closed():
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = "not-a-valid-locator"

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        legacy_identity_match=True,
        **_limits(),
    )

    assert report["planned_update_count"] == 0
    assert report["replacement_locator_invalid_count"] == 1
    assert report["status"] == "dry_run_with_gaps"


def test_generator_snapshot_uses_materialized_snapshot_count():
    store, candidate = _seed_store()
    snapshot = (document for document in [store.get(candidate["_id"])])

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        snapshot_documents=snapshot,
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["snapshot_document_count"] == 1
    assert report["planned_update_count"] == 1


def test_batch_execute_timeout_clears_ready_and_fails_closed():
    store, candidate = _seed_store()
    before = store.get(candidate["_id"])
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        patch_limit=1,
        batch_limit=1,
        max_runtime_seconds=30,
        source_file_limit=1,
        source_entry_limit=100,
        target_document_limit=1,
    )
    clock = iter((0.0, 0.0, 0.0, 31.0))

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        patch_limit=1,
        batch_limit=1,
        max_runtime_seconds=30,
        source_file_limit=1,
        source_entry_limit=100,
        target_document_limit=1,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        started=0.0,
        monotonic=lambda: next(clock),
    )

    assert report["status"] == "aborted_timeout"
    assert report["timed_out"] is True
    assert report["batch_ready"] is False
    assert report["batch_execution_succeeded"] is False
    assert store.get(candidate["_id"]) == before


def test_partial_session_cas_conflict_still_refreshes_mutated_session_derived_state():
    store, candidate = _seed_store()
    second = _add_gap_chunk(store, chunk_id="partial-second", text="partial repair second")
    original_patch = store.patch_observed_time_if_content_hash

    def patch_with_second_conflict(**kwargs):
        if kwargs["doc_id"] == second["_id"]:
            raise SourceStoreConflict("simulated concurrent change")
        return original_patch(**kwargs)

    store.patch_observed_time_if_content_hash = patch_with_second_conflict  # type: ignore[method-assign]
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        max_runtime_seconds=30,
        **_limits(),
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    assert report["updated_count"] == 1
    assert report["partial_session_count"] == 1
    assert report["coverage_recomputed_session_count"] == 1
    assert report["projection_pending_session_count"] == 1
    assert report["remaining_temporal_gap_count"] == 1


def test_postcheck_failure_after_successful_cas_preserves_mutation_and_invalidation():
    store, candidate = _seed_store()
    original_patch = store.patch_observed_time_if_content_hash
    original_get = store.get
    postcheck_armed = False

    def patch_then_arm_postcheck(**kwargs):
        nonlocal postcheck_armed
        revision = original_patch(**kwargs)
        postcheck_armed = True
        return revision

    def get_with_one_stale_postcheck(document_id):
        nonlocal postcheck_armed
        current = original_get(document_id)
        if postcheck_armed and document_id == candidate["_id"] and current is not None:
            postcheck_armed = False
            stale = dict(current)
            stale["observed_at_start"] = ""
            stale["observed_at_end"] = ""
            return stale
        return current

    store.patch_observed_time_if_content_hash = patch_then_arm_postcheck  # type: ignore[method-assign]
    store.get = get_with_one_stale_postcheck  # type: ignore[method-assign]
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    assert report["status"] == "completed_with_errors"
    assert report["updated_count"] == 1
    assert report["mutation_performed"] is True
    assert report["write_conflict_count"] == 1
    assert report["partial_session_count"] == 1
    assert report["coverage_recomputed_session_count"] == 1
    assert report["projection_pending_session_count"] == 1
    assert store.get(projection_state_doc_id(SESSION_HASH))["projection_status"] == ProjectionStatus.PENDING


def test_transport_exception_after_temporal_write_is_reconciled_and_invalidates_projection():
    store, candidate = _seed_store()
    _mark_projection_projected(store)
    original_patch = store.patch_observed_time_if_content_hash
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    def patch_then_lose_acknowledgement(**kwargs):
        original_patch(**kwargs)
        raise TimeoutError("private transport acknowledgement detail")

    store.patch_observed_time_if_content_hash = patch_then_lose_acknowledgement  # type: ignore[method-assign]
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    current = store.get(candidate["_id"])
    projection = store.get(projection_state_doc_id(SESSION_HASH))
    assert current is not None and projection is not None
    assert report["status"] == "completed"
    assert report["updated_count"] == 1
    assert report["mutation_performed"] is True
    assert report["write_error_count"] == 0
    assert report["coverage_recomputed_session_count"] == 1
    assert report["projection_pending_session_count"] == 1
    assert report["remaining_temporal_gap_count"] == 0
    assert current["observed_at_start"] == candidate["observed_at_start"]
    assert current["observed_at_end"] == candidate["observed_at_end"]
    assert projection["projection_status"] == ProjectionStatus.PENDING
    assert "private transport acknowledgement detail" not in json.dumps(report)


def test_transport_exception_before_temporal_write_does_not_report_false_mutation():
    store, candidate = _seed_store()
    _mark_projection_projected(store)
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    def fail_before_patch(**_kwargs):
        raise TimeoutError("private transport pre-write detail")

    store.patch_observed_time_if_content_hash = fail_before_patch  # type: ignore[method-assign]
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    current = store.get(candidate["_id"])
    projection = store.get(projection_state_doc_id(SESSION_HASH))
    assert current is not None and projection is not None
    assert report["status"] == "completed_with_errors"
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False
    assert report["write_error_count"] == 1
    assert report["coverage_recomputed_session_count"] == 0
    assert report["projection_pending_session_count"] == 0
    assert report["remaining_temporal_gap_count"] == 1
    assert current["observed_at_start"] == ""
    assert current["observed_at_end"] == ""
    assert projection["projection_status"] == ProjectionStatus.PROJECTED
    assert "private transport pre-write detail" not in json.dumps(report)


def test_transport_exception_readback_revision_lineage_mismatch_fails_closed():
    store, candidate = _seed_store()
    original_get = store.get
    readback_armed = False
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    def fail_and_arm_mismatched_readback(**_kwargs):
        nonlocal readback_armed
        readback_armed = True
        raise TimeoutError("private transport mismatch detail")

    def get_with_one_mismatched_readback(document_id):
        nonlocal readback_armed
        current = original_get(document_id)
        if readback_armed and document_id == candidate["_id"] and current is not None:
            readback_armed = False
            mismatch = dict(current)
            mismatch["observed_at_start"] = candidate["observed_at_start"]
            mismatch["observed_at_end"] = candidate["observed_at_end"]
            return mismatch
        return current

    store.patch_observed_time_if_content_hash = fail_and_arm_mismatched_readback  # type: ignore[method-assign]
    store.get = get_with_one_mismatched_readback  # type: ignore[method-assign]
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    assert report["status"] == "completed_with_errors"
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False
    assert report["write_error_count"] == 1
    assert report["coverage_recomputed_session_count"] == 0
    assert report["projection_pending_session_count"] == 0
    assert original_get(candidate["_id"])["observed_at_start"] == ""
    assert "private transport mismatch detail" not in json.dumps(report)


def test_transport_exception_readback_failure_fails_closed():
    store, candidate = _seed_store()
    original_get = store.get
    readback_armed = False
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        **_limits(),
    )

    def fail_and_arm_readback_error(**_kwargs):
        nonlocal readback_armed
        readback_armed = True
        raise TimeoutError("private transport write detail")

    def get_with_one_readback_error(document_id):
        nonlocal readback_armed
        if readback_armed and document_id == candidate["_id"]:
            readback_armed = False
            raise RuntimeError("private readback detail")
        return original_get(document_id)

    store.patch_observed_time_if_content_hash = fail_and_arm_readback_error  # type: ignore[method-assign]
    store.get = get_with_one_readback_error  # type: ignore[method-assign]
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        **_limits(),
    )

    assert report["status"] == "completed_with_errors"
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False
    assert report["write_error_count"] == 1
    assert report["coverage_recomputed_session_count"] == 0
    assert report["projection_pending_session_count"] == 0
    assert original_get(candidate["_id"])["observed_at_start"] == ""
    assert "private readback detail" not in json.dumps(report)


def test_planning_timeout_is_a_nonzero_blocked_gap():
    store, candidate = _seed_store()
    clock = iter((0.0, 31.0))

    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate],
        max_runtime_seconds=30,
        monotonic=lambda: next(clock),
        started=0.0,
        **_limits(),
    )

    assert report["status"] == "aborted_timeout"
    assert report["timed_out"] is True
    assert report["error_count"] > 0
    assert report["gap_count"] > 0
    assert report["mutation_performed"] is False


def test_initial_snapshot_failure_returns_sanitized_fail_closed_report():
    class SnapshotFailureStore(InMemoryCouchDBSourceStore):
        def find_by_type(self, *_args, **_kwargs):
            raise RuntimeError("private snapshot transport detail")

    report = repair_historical_temporal_gaps(
        source_store=SnapshotFailureStore(),
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[],
        max_runtime_seconds=30,
        **_limits(),
    )

    assert report["status"] == "blocked"
    assert report["error"] == "snapshot_read_failed"
    assert report["error_count"] == 1
    assert report["gap_count"] == 1
    assert "private snapshot transport detail" not in json.dumps(report)


def test_gemini_json_uses_private_fixture_conversion_instead_of_fixture_parser_error(tmp_path):
    source = tmp_path / PROJECT / "chats" / "native.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"sessionId":"gemini-temporal","messages":['
        '{"role":"user","content":"hello","timestamp":"2026-07-01T10:00:01Z"},'
        '{"role":"model","content":"world","timestamp":"2026-07-01T10:00:02Z"}]}',
        encoding="utf-8",
    )

    documents, report, timed_out = collect_historical_candidates(
        provider="gemini",
        project=PROJECT,
        source_root=tmp_path,
        source_file_limit=1,
        source_entry_limit=100,
    )

    assert timed_out is False
    assert report["parser_error_count"] == 0
    assert report["parsed_source_count"] == 1
    assert len(documents) == 1


def test_cli_skips_proven_nontarget_redaction_leak_and_discards_partial_candidates(
    monkeypatch, capsys, tmp_path
):
    target_source = tmp_path / "a-target.jsonl"
    leaking_source = tmp_path / "b-unrelated.jsonl"
    target_source.write_text("target fixture", encoding="utf-8")
    leaking_source.write_text("unrelated fixture", encoding="utf-8")
    target_chunk = _temporal_chunk(session_id_hash=SESSION_HASH, chunk_id="target")
    partial_chunk = _temporal_chunk(
        session_id_hash="sha256:" + "e" * 64, chunk_id="unrelated-partial"
    )
    leaking_chunk = _temporal_chunk(
        session_id_hash="sha256:" + "e" * 64, chunk_id="unrelated-leaking"
    )
    snapshot = build_conversation_chunk_document(chunk=target_chunk)
    snapshot.update({"observed_at_start": "", "observed_at_end": ""})
    store = InMemoryCouchDBSourceStore()
    store.put(snapshot)
    target_locator_hash = build_source_locator_hash(str(target_source))
    leaking_locator_hash = build_source_locator_hash(str(leaking_source))
    original_build = temporal_repair.build_conversation_chunk_document

    def parse_source(_provider, path, *, project, source_locator_hash):
        assert project == PROJECT
        if Path(path) == target_source:
            assert source_locator_hash == target_locator_hash
            return _parsed_temporal_source(SESSION_HASH)
        assert Path(path) == leaking_source
        assert source_locator_hash == leaking_locator_hash
        return _parsed_temporal_source("sha256:" + "e" * 64)

    def chunks_for_source(parsed):
        if parsed.session.session_id_hash == SESSION_HASH:
            return [target_chunk]
        return [partial_chunk, leaking_chunk]

    def raise_for_later_unrelated_chunk(*, chunk, source_locator_hash):
        if chunk.chunk_id == leaking_chunk.chunk_id:
            raise SourceRedactionLeak("synthetic redaction leak")
        return original_build(chunk=chunk, source_locator_hash=source_locator_hash)

    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(temporal_repair, "parse_transcript_source", parse_source)
    monkeypatch.setattr(temporal_repair, "build_transcript_chunks", chunks_for_source)
    monkeypatch.setattr(
        temporal_repair, "build_conversation_chunk_document", raise_for_later_unrelated_chunk
    )

    rc = main(
        _cli_args(
            tmp_path,
            source_file_limit=2,
            target_document_limit=1,
            patch_limit=1,
            batch_limit=1,
            legacy_identity_match=True,
        )
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 0
    assert report["status"] == "dry_run_batch_ready"
    assert report["batch_ready"] is True
    assert report["selected_batch_count"] == 1
    assert report["historical_candidate_count"] == 1
    assert report["parser_error_count"] == 0
    assert report["non_target_source_redaction_skip_count"] == 1
    assert "synthetic redaction leak" not in output
    assert str(tmp_path) not in output


@pytest.mark.parametrize("matches_target", ["locator", "session"])
def test_cli_target_redaction_leak_blocks_without_a_plan(monkeypatch, capsys, tmp_path, matches_target):
    source = tmp_path / "target.jsonl"
    source.write_text("target fixture", encoding="utf-8")
    source_locator_hash = build_source_locator_hash(str(source))
    target_session_id_hash = SESSION_HASH
    source_session_id_hash = (
        "sha256:" + "e" * 64 if matches_target == "locator" else target_session_id_hash
    )
    snapshot_chunk = _temporal_chunk(
        session_id_hash=target_session_id_hash, chunk_id=f"target-{matches_target}"
    )
    snapshot = build_conversation_chunk_document(
        chunk=snapshot_chunk,
        source_locator_hash=source_locator_hash if matches_target == "locator" else "",
    )
    snapshot.update({"observed_at_start": "", "observed_at_end": ""})
    store = InMemoryCouchDBSourceStore()
    store.put(snapshot)
    leaking_chunk = _temporal_chunk(
        session_id_hash=source_session_id_hash, chunk_id=f"leaking-{matches_target}"
    )

    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(
        temporal_repair,
        "parse_transcript_source",
        lambda *_args, **_kwargs: _parsed_temporal_source(source_session_id_hash),
    )
    monkeypatch.setattr(temporal_repair, "build_transcript_chunks", lambda _parsed: [leaking_chunk])
    monkeypatch.setattr(
        temporal_repair,
        "build_conversation_chunk_document",
        lambda **_kwargs: (_ for _ in ()).throw(SourceRedactionLeak("synthetic redaction leak")),
    )

    rc = main(_cli_args(tmp_path))

    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 1
    if matches_target == "locator":
        assert report["status"] == "blocked"
        assert report["error"] == "historical_source_parse_error"
        assert report["parser_error_count"] == 1
        assert "plan_digest" not in report
    else:
        assert report["status"] == "dry_run_with_gaps"
        assert report["legacy_identity_match_required_count"] == 1
        assert report["parser_error_count"] == 0
        assert "plan_digest" in report
    assert report["non_target_source_redaction_skip_count"] == 0
    assert "synthetic redaction leak" not in output


@pytest.mark.parametrize("invalid_target_session_id_hash", ["", "invalid"])
def test_cli_incomplete_target_session_identity_blocks_nontarget_redaction_skip(
    monkeypatch, capsys, tmp_path, invalid_target_session_id_hash
):
    source = tmp_path / "unrelated.jsonl"
    source.write_text("unrelated fixture", encoding="utf-8")
    snapshot_chunk = _temporal_chunk(session_id_hash=SESSION_HASH, chunk_id="incomplete-target")
    snapshot = build_conversation_chunk_document(chunk=snapshot_chunk)
    snapshot.update(
        {
            "session_id_hash": invalid_target_session_id_hash,
            "observed_at_start": "",
            "observed_at_end": "",
        }
    )
    store = InMemoryCouchDBSourceStore()
    store.put(snapshot)
    unrelated_session_id_hash = "sha256:" + "e" * 64
    leaking_chunk = _temporal_chunk(
        session_id_hash=unrelated_session_id_hash, chunk_id="unrelated-source-leak"
    )

    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(
        temporal_repair,
        "parse_transcript_source",
        lambda *_args, **_kwargs: _parsed_temporal_source(unrelated_session_id_hash),
    )
    monkeypatch.setattr(temporal_repair, "build_transcript_chunks", lambda _parsed: [leaking_chunk])
    monkeypatch.setattr(
        temporal_repair,
        "build_conversation_chunk_document",
        lambda **_kwargs: (_ for _ in ()).throw(SourceRedactionLeak("synthetic redaction leak")),
    )

    rc = main(_cli_args(tmp_path))

    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 1
    assert report["status"] == "dry_run_with_gaps"
    assert report["snapshot_integrity_error_count"] == 1
    assert report["parser_error_count"] == 0
    assert report["non_target_source_redaction_skip_count"] == 0
    assert "plan_digest" in report
    assert "synthetic redaction leak" not in output


@pytest.mark.parametrize("failure_stage", ["parse", "materialize_without_session"])
def test_cli_redaction_leak_with_missing_parsed_session_fails_closed(
    monkeypatch, capsys, tmp_path, failure_stage
):
    source = tmp_path / "source.jsonl"
    source.write_text("fixture", encoding="utf-8")
    snapshot_chunk = _temporal_chunk(session_id_hash=SESSION_HASH, chunk_id="target")
    snapshot = build_conversation_chunk_document(chunk=snapshot_chunk)
    snapshot.update({"observed_at_start": "", "observed_at_end": ""})
    store = InMemoryCouchDBSourceStore()
    store.put(snapshot)
    leaking_chunk = _temporal_chunk(
        session_id_hash="sha256:" + "e" * 64, chunk_id="leaking-source"
    )

    def parse_source(*_args, **_kwargs):
        if failure_stage == "parse":
            raise SourceRedactionLeak("synthetic redaction leak")
        return object()

    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(temporal_repair, "parse_transcript_source", parse_source)
    monkeypatch.setattr(
        temporal_repair,
        "build_transcript_chunks",
        lambda _parsed: [leaking_chunk],
    )
    monkeypatch.setattr(
        temporal_repair,
        "build_conversation_chunk_document",
        lambda **_kwargs: (_ for _ in ()).throw(SourceRedactionLeak("synthetic redaction leak")),
    )

    rc = main(_cli_args(tmp_path))

    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 1
    assert report["status"] == "dry_run_with_gaps"
    assert report["legacy_identity_match_required_count"] == 1
    assert report["parser_error_count"] == 0
    assert report["non_target_source_redaction_skip_count"] == 0
    assert "plan_digest" in report
    assert "synthetic redaction leak" not in output
    assert str(tmp_path) not in output


def test_cli_opt_out_mixed_scope_scans_only_normal_locator_targets(
    monkeypatch, capsys, tmp_path
):
    legacy_source = tmp_path / "a-legacy.jsonl"
    normal_source = tmp_path / "b-normal.jsonl"
    legacy_source.write_text("legacy fixture", encoding="utf-8")
    normal_source.write_text("normal fixture", encoding="utf-8")
    normal_target_session_id_hash = "sha256:" + "c" * 64
    unrelated_session_id_hash = "sha256:" + "e" * 64
    legacy_chunk = _temporal_chunk(session_id_hash=SESSION_HASH, chunk_id="legacy-target")
    normal_target_chunk = _temporal_chunk(
        session_id_hash=normal_target_session_id_hash, chunk_id="normal-target"
    )
    normal_source_locator_hash = build_source_locator_hash(str(normal_source))
    legacy_snapshot = build_conversation_chunk_document(chunk=legacy_chunk)
    normal_snapshot = build_conversation_chunk_document(
        chunk=normal_target_chunk, source_locator_hash=normal_source_locator_hash
    )
    legacy_snapshot.update({"observed_at_start": "", "observed_at_end": ""})
    normal_snapshot.update({"observed_at_start": "", "observed_at_end": ""})
    store = InMemoryCouchDBSourceStore()
    store.put(legacy_snapshot)
    store.put(normal_snapshot)
    leaking_chunk = _temporal_chunk(
        session_id_hash=unrelated_session_id_hash, chunk_id="normal-source-leak"
    )
    parsed_paths = []
    original_build = temporal_repair.build_conversation_chunk_document
    original_collect = temporal_repair.collect_historical_candidates

    def parse_source(_provider, path, *, project, source_locator_hash):
        assert project == PROJECT
        parsed_paths.append(Path(path))
        if Path(path) == legacy_source:
            return _parsed_temporal_source(SESSION_HASH)
        assert Path(path) == normal_source
        assert source_locator_hash == normal_source_locator_hash
        return _parsed_temporal_source(unrelated_session_id_hash)

    def chunks_for_source(parsed):
        if parsed.session.session_id_hash == SESSION_HASH:
            return [legacy_chunk]
        return [leaking_chunk]

    def raise_for_normal_target_locator(*, chunk, source_locator_hash):
        if chunk.chunk_id == leaking_chunk.chunk_id:
            raise SourceRedactionLeak("synthetic redaction leak")
        return original_build(chunk=chunk, source_locator_hash=source_locator_hash)

    def record_normal_locator_scan(**kwargs):
        assert kwargs["required_source_locator_hashes"] == {normal_source_locator_hash}
        return original_collect(**kwargs)

    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
    monkeypatch.setattr(temporal_repair, "parse_transcript_source", parse_source)
    monkeypatch.setattr(temporal_repair, "build_transcript_chunks", chunks_for_source)
    monkeypatch.setattr(
        temporal_repair, "build_conversation_chunk_document", raise_for_normal_target_locator
    )
    monkeypatch.setattr(temporal_repair, "collect_historical_candidates", record_normal_locator_scan)

    rc = main(
        _cli_args(
            tmp_path,
            source_file_limit=2,
            target_document_limit=2,
            patch_limit=2,
        )
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 1
    assert parsed_paths == [normal_source]
    assert report["status"] == "blocked"
    assert report["error"] == "historical_source_parse_error"
    assert report["parser_error_count"] == 1
    assert report["non_target_source_redaction_skip_count"] == 0
    assert "plan_digest" not in report
    assert "synthetic redaction leak" not in output


def test_cli_snapshots_target_before_parsing_and_passes_only_target_locator_hashes(monkeypatch, capsys, tmp_path):
    collection = temporal_repair._collection_counts(source_file_count=1, parser_error_count=1)
    store, candidate = _seed_store()
    calls = []
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")

    original_find_by_type = store.find_by_type

    def record_snapshot(*args, **kwargs):
        calls.append("snapshot")
        assert "source_locator_hash" in kwargs["fields"]
        return original_find_by_type(*args, **kwargs)

    def record_collection(**kwargs):
        calls.append("parse")
        assert kwargs["required_source_locator_hashes"] == {candidate["source_locator_hash"]}
        return [], collection, False

    store.find_by_type = record_snapshot  # type: ignore[method-assign]
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(temporal_repair, "collect_historical_candidates", record_collection)

    rc = temporal_repair.main(_cli_args(tmp_path))

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked"
    assert report["error"] == "historical_source_parse_error"
    assert report["error_count"] == 1
    assert report["gap_count"] == 1
    assert report["parser_error_count"] == 1
    assert calls == ["snapshot", "parse"]


def test_cli_legacy_locator_target_requests_full_bounded_source_scan(monkeypatch, capsys, tmp_path):
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    collection = temporal_repair._collection_counts(
        discovered_source_file_count=1,
        source_file_count=1,
        source_scan_complete=True,
        source_tree_scan_complete=True,
        required_target_scan_satisfied=True,
        parsed_source_count=1,
    )
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)

    def record_collection(**kwargs):
        assert kwargs["required_source_locator_hashes"] is None
        return [candidate], collection, False

    monkeypatch.setattr(temporal_repair, "collect_historical_candidates", record_collection)

    rc = temporal_repair.main(_cli_args(tmp_path, legacy_identity_match=True))

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "dry_run"
    assert report["legacy_target_count"] == 1
    assert report["planned_update_count"] == 1


def test_cli_legacy_identity_match_is_bound_to_approval_argv_and_plan_digest(
    monkeypatch, capsys, tmp_path
):
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    collection = temporal_repair._collection_counts(
        discovered_source_file_count=1,
        source_file_count=1,
        source_scan_complete=True,
        source_tree_scan_complete=True,
        required_target_scan_satisfied=True,
        parsed_source_count=1,
    )
    target = temporal_repair._resolve_target({"COUCHDB_URL": "https://repair-test.invalid"})
    approval_argv: list[list[str]] = []
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        temporal_repair,
        "collect_historical_candidates",
        lambda **_kwargs: ([candidate], collection, False),
    )

    def approve(*_args, command_argv, **_kwargs):
        approval_argv.append(command_argv)
        return {
            "target": {"target_fingerprints": target.target_fingerprints},
            "timeout_seconds": 30,
        }

    monkeypatch.setattr(temporal_repair, "validate_memory_enqueue_approval", approve)
    plan_args = _cli_args(tmp_path, batch_limit=1, legacy_identity_match=True)
    assert main(plan_args) == 0
    plan = json.loads(capsys.readouterr().out)

    assert main(
        [
            *_cli_args(tmp_path, batch_limit=1),
            "--execute",
            "--expected-plan-digest",
            plan["plan_digest"],
            "--approval",
            "approved-for-test",
        ]
    ) == 1
    drifted = json.loads(capsys.readouterr().out)

    assert main(
        [
            *plan_args,
            "--execute",
            "--expected-plan-digest",
            plan["plan_digest"],
            "--approval",
            "approved-for-test",
        ]
    ) == 0
    executed = json.loads(capsys.readouterr().out)

    assert drifted["status"] == "blocked_plan_drift"
    assert drifted["mutation_performed"] is False
    assert executed["updated_count"] == 1
    assert "--legacy-identity-match" in approval_argv[-1]


def test_cli_legacy_only_opt_out_skips_scan_and_opt_in_reports_source_error_targets(
    monkeypatch, capsys, tmp_path
):
    store, candidate = _seed_store(source_locator_hash="")
    candidate["source_locator_hash"] = SOURCE_LOCATOR_HASH
    collection = temporal_repair._collection_counts(
        discovered_source_file_count=2,
        source_file_count=2,
        source_scan_complete=True,
        source_tree_scan_complete=True,
        required_target_scan_satisfied=True,
        parsed_source_count=1,
        parser_error_count=1,
    )
    no_scan_collection = temporal_repair._collection_counts(
        required_hashes=set(),
        source_scan_complete=True,
        required_target_scan_satisfied=True,
    )
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)

    def record_collection(**kwargs):
        if kwargs["required_source_locator_hashes"] == set():
            return [], no_scan_collection, False
        assert kwargs["required_source_locator_hashes"] is None
        return [candidate], collection, False

    monkeypatch.setattr(temporal_repair, "collect_historical_candidates", record_collection)

    default_rc = temporal_repair.main(_cli_args(tmp_path, batch_limit=1))
    default_report = json.loads(capsys.readouterr().out)
    opt_in_rc = temporal_repair.main(
        _cli_args(tmp_path, batch_limit=1, legacy_identity_match=True)
    )
    opt_in_report = json.loads(capsys.readouterr().out)

    assert default_rc == 1
    assert default_report["status"] == "dry_run_with_gaps"
    assert default_report["legacy_identity_match_required_count"] == 1
    assert default_report["source_file_count"] == 0
    assert "parser_error_count" in default_report
    assert opt_in_rc == 1
    assert opt_in_report["status"] == "dry_run_with_gaps"
    assert opt_in_report["planned_update_count"] == 0
    assert opt_in_report["selected_batch_count"] == 0
    assert opt_in_report["batch_ready"] is False
    assert opt_in_report["legacy_source_error_count"] == 1
    assert opt_in_report["legacy_source_error_blocked_count"] == 1
    assert opt_in_report["error_count"] == 2
    assert opt_in_report["gap_count"] == 2
    assert "historical_source_parse_error" not in opt_in_report


def test_cli_batch_ready_with_unrepairable_gap_returns_nonzero_without_locator_hash(monkeypatch, capsys, tmp_path):
    store, candidate = _seed_store()
    unrepairable = _add_gap_chunk(
        store,
        chunk_id="cli-unrepairable-target",
        text="cli unrepairable target",
    )
    mismatched_candidate = dict(unrepairable)
    mismatched_candidate["content_hash"] = "sha256:" + "e" * 64
    collection = temporal_repair._collection_counts(
        discovered_source_file_count=1,
        source_file_count=1,
        parsed_source_count=1,
    )
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        temporal_repair,
        "collect_historical_candidates",
        lambda **_kwargs: ([candidate, mismatched_candidate], collection, False),
    )

    rc = main(
        _cli_args(
            tmp_path,
            source_file_limit=2,
            target_document_limit=2,
            patch_limit=2,
            batch_limit=1,
        )
    )

    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 1
    assert report["status"] == "dry_run_batch_ready_with_gaps"
    assert report["unrepairable_gap_count"] == 1
    assert candidate["source_locator_hash"] not in output


def test_cli_batch_execute_timeout_returns_nonzero(monkeypatch, capsys, tmp_path):
    store, candidate = _seed_store()
    collection = temporal_repair._collection_counts(
        discovered_source_file_count=1,
        source_file_count=1,
        source_scan_complete=True,
        source_tree_scan_complete=True,
        required_target_scan_satisfied=True,
        parsed_source_count=1,
    )
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    target = temporal_repair._resolve_target({"COUCHDB_URL": "https://repair-test.invalid"})
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        temporal_repair,
        "validate_memory_enqueue_approval",
        lambda *_args, **_kwargs: {"target": {"target_fingerprints": target.target_fingerprints}, "timeout_seconds": 30},
    )
    monkeypatch.setattr(
        temporal_repair,
        "collect_historical_candidates",
        lambda **_kwargs: ([candidate], collection, False),
    )
    monkeypatch.setattr(
        temporal_repair,
        "repair_historical_temporal_gaps",
        lambda **_kwargs: {
            "status": "aborted_timeout",
            "timed_out": True,
            "batch_ready": False,
            "batch_execution_succeeded": False,
            "write_conflict_count": 0,
            "write_error_count": 0,
            "error_count": 1,
        },
    )

    rc = main(
        [
            *_cli_args(tmp_path, batch_limit=1),
            "--execute",
            "--expected-plan-digest", "sha256:" + "a" * 64,
            "--approval", "approved-for-test",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "aborted_timeout"


def test_source_file_limit_reports_truncation_before_any_couchdb_mutation(tmp_path, monkeypatch, capsys):
    source_hashes = {}
    for name in ("first", "second"):
        source = tmp_path / f"{name}.jsonl"
        source.write_text(
            '{"session_id":"' + name + '","cwd":"/workspace/neurons",'
            '"type":"user","timestamp":"2026-07-01T10:00:01Z",'
            '"content":"hello"}\n',
            encoding="utf-8",
        )
        source_hashes[name] = build_source_locator_hash(str(source))
    store, _candidate = _seed_store(source_locator_hash=source_hashes["first"])
    _add_gap_chunk(
        store,
        chunk_id="source-limit-second",
        text="second source limit target",
        source_locator_hash=source_hashes["second"],
    )
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)

    assert main(_cli_args(tmp_path, target_document_limit=2)) == 1

    output = capsys.readouterr().out
    assert '"error": "source_file_limit_exceeded"' in output
    assert '"source_file_truncated": true' in output


def test_source_entry_limit_stops_non_provider_tree_before_any_couchdb_mutation(
    tmp_path, monkeypatch, capsys
):
    for index in range(2):
        (tmp_path / f"ignored-{index}.txt").write_text("not a provider source", encoding="utf-8")
    store, candidate = _seed_store()
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)

    assert main(_cli_args(tmp_path, source_entry_limit=1)) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "source_entry_limit_exceeded"
    assert report["source_entry_limit"] == 1
    assert report["source_entry_count"] == 2
    assert report["source_entry_limit_exceeded"] is True
    assert store.get(candidate["_id"])["observed_at_start"] == ""


def test_finite_source_limits_have_stable_cli_outcome_when_creation_order_changes(
    tmp_path, monkeypatch, capsys
):
    cases = (
        ("source_entry_limit_exceeded", 1, 1, "target.jsonl", "ignored.txt"),
        ("source_file_limit_exceeded", 1, 2, "a-target.jsonl", "z-extra.jsonl"),
    )
    for expected_error, source_file_limit, source_entry_limit, target_name, other_name in cases:
        outcomes = []
        for index, creation_order in enumerate(((target_name, other_name), (other_name, target_name))):
            source_root = tmp_path / f"sources-{expected_error}-{index}"
            source_root.mkdir()
            for name in creation_order:
                (source_root / name).write_text("fixture\n", encoding="utf-8")
            selected = source_root / target_name
            parsed = ParsedTranscript(
                session=TranscriptSession(
                    session_id_hash=SESSION_HASH,
                    provider=PROVIDER,
                    project=PROJECT,
                    started_at="2026-07-01T10:00:00Z",
                ),
                turns=[
                    TranscriptTurn("turn-a", SESSION_HASH, 1, "user", "2026-07-01T10:00:01Z", "hello"),
                    TranscriptTurn("turn-b", SESSION_HASH, 2, "assistant", "2026-07-01T10:00:02Z", "world"),
                ],
                tool_events=[],
                parser_warnings=[],
                source_status="source_locator_private_spool_only",
            )
            target = build_conversation_chunk_document(
                chunk=build_transcript_chunks(parsed)[0],
                source_locator_hash=build_source_locator_hash(str(selected)),
            )
            target["observed_at_start"] = ""
            target["observed_at_end"] = ""
            store = InMemoryCouchDBSourceStore()
            store.put(target)
            monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
            monkeypatch.setattr(
                temporal_repair,
                "CouchDBHttpSourceStore",
                lambda _store=store, **_kwargs: _store,
            )
            monkeypatch.setattr(temporal_repair, "_source_project", lambda *_args: PROJECT)
            monkeypatch.setattr(
                temporal_repair,
                "parse_transcript_source",
                lambda *_args, _parsed=parsed, **_kwargs: _parsed,
            )

            rc = main(
                _cli_args(
                    source_root,
                    source_file_limit=source_file_limit,
                    source_entry_limit=source_entry_limit,
                    target_document_limit=1,
                    patch_limit=1,
                    batch_limit=1,
                )
            )
            report = json.loads(capsys.readouterr().out)
            outcomes.append((rc, report["status"], report.get("error")))

        assert outcomes == [(1, "blocked", expected_error), (1, "blocked", expected_error)]


def test_cli_rejects_nonpositive_source_entry_limit(capsys, tmp_path):
    assert main(_cli_args(tmp_path, source_entry_limit=0)) == 2

    assert json.loads(capsys.readouterr().out)["error"] == "invalid_bounds"


def test_cli_uses_safe_finite_default_when_source_entry_limit_is_omitted(
    capsys, monkeypatch, tmp_path
):
    store = InMemoryCouchDBSourceStore()
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        temporal_repair,
        "_iter_source_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source scan must not run")),
    )

    assert main(_cli_args(tmp_path, source_entry_limit=None)) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run"
    assert report["source_entry_limit"] == temporal_repair.DEFAULT_SOURCE_ENTRY_LIMIT
    assert report["plan_digest"].startswith("sha256:")


def test_cli_execute_empty_batch_is_a_successful_noop(capsys, monkeypatch, tmp_path):
    store = InMemoryCouchDBSourceStore()
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")
    target = temporal_repair._resolve_target({"COUCHDB_URL": "https://repair-test.invalid"})
    monkeypatch.setattr(temporal_repair, "CouchDBHttpSourceStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        temporal_repair,
        "validate_memory_enqueue_approval",
        lambda *_args, **_kwargs: {
            "target": {"target_fingerprints": target.target_fingerprints},
            "timeout_seconds": 30,
        },
    )
    args = _cli_args(tmp_path, source_entry_limit=None, batch_limit=1)
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)

    assert main(
        [
            *args,
            "--execute",
            "--expected-plan-digest", plan["plan_digest"],
            "--approval", "approved-for-test",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["selected_batch_count"] == 0
    assert report["error_count"] == 0
    assert report["mutation_performed"] is False


def test_command_is_registered_as_approval_gated_metadata_repair():
    assert COMMAND_HANDLERS["couchdb-historical-temporal-repair"] is main
    assert COMMAND_METADATA["couchdb-historical-temporal-repair"] == {
        "runtime_category": "human_gated_metadata_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    }


def test_cli_execute_requires_approval_before_source_or_couchdb_access(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")

    assert main(
        [
            "--provider", PROVIDER,
            "--project", PROJECT,
            "--source-root", str(tmp_path),
            "--source-file-limit", "1",
            "--source-entry-limit", "100",
            "--target-document-limit", "1",
            "--patch-limit", "1",
            "--max-runtime-seconds", "30",
            "--execute",
            "--expected-plan-digest", "sha256:" + "a" * 64,
        ]
    ) == 2

    assert '"error": "approval_rejected"' in capsys.readouterr().out

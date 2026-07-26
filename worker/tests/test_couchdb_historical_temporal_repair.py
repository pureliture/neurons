from __future__ import annotations

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.couchdb_source.document_model import (
    ProjectionStatus,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_projection_state_document,
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


def _limits() -> dict[str, int]:
    return {
        "source_file_limit": 10,
        "target_document_limit": 10,
        "patch_limit": 10,
    }


def _seed_store() -> tuple[InMemoryCouchDBSourceStore, dict]:
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
    chunk = build_conversation_chunk_document(chunk=missing)
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


def _add_gap_chunk(store: InMemoryCouchDBSourceStore, *, chunk_id: str, text: str) -> dict:
    chunk = build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id=chunk_id,
            session_id_hash=SESSION_HASH,
            provider=PROVIDER,
            project=PROJECT,
            turn_start_index=3,
            turn_end_index=3,
            text=text,
        )
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
        target_document_limit=10,
        patch_limit=11,
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
    assert execute["status"] == "blocked_plan_drift"
    assert store.get(candidate["_id"])["observed_at_start"] == ""


def test_patch_limit_blocks_before_any_mutation():
    store, candidate = _seed_store()
    second = _add_gap_chunk(store, chunk_id="patch-limit-second", text="second temporal gap")
    report = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        source_file_limit=10,
        target_document_limit=10,
        patch_limit=1,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest="",
    )

    # The bad digest gate wins first, so obtain the actual digest then prove the
    # separate patch bound rejects without invoking a source patch.
    plan = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        source_file_limit=10,
        target_document_limit=10,
        patch_limit=1,
        max_runtime_seconds=30,
    )
    bounded = repair_historical_temporal_gaps(
        source_store=store,
        provider=PROVIDER,
        project=PROJECT,
        historical_documents=[candidate, second],
        source_file_limit=10,
        target_document_limit=10,
        patch_limit=1,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
    )

    assert report["status"] == "blocked_plan_drift"
    assert bounded["status"] == "blocked_patch_limit"
    assert bounded["mutation_performed"] is False
    assert store.get(candidate["_id"])["observed_at_start"] == ""
    assert store.get(second["_id"])["observed_at_start"] == ""


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
    )

    assert timed_out is False
    assert report["parser_error_count"] == 0
    assert report["parsed_source_count"] == 1
    assert len(documents) == 1


def test_source_file_limit_reports_truncation_before_any_couchdb_mutation(tmp_path, monkeypatch, capsys):
    for name in ("first", "second"):
        source = tmp_path / f"{name}.jsonl"
        source.write_text(
            '{"session_id":"' + name + '","cwd":"/workspace/neurons",'
            '"type":"user","timestamp":"2026-07-01T10:00:01Z",'
            '"content":"hello"}\n',
            encoding="utf-8",
        )
    monkeypatch.setenv("COUCHDB_URL", "https://repair-test.invalid")

    assert main(
        [
            "--provider", PROVIDER,
            "--project", PROJECT,
            "--source-root", str(tmp_path),
            "--source-file-limit", "1",
            "--target-document-limit", "1",
            "--patch-limit", "1",
            "--max-runtime-seconds", "30",
        ]
    ) == 1

    output = capsys.readouterr().out
    assert '"error": "source_file_limit_exceeded"' in output
    assert '"source_file_truncated": true' in output


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
            "--target-document-limit", "1",
            "--patch-limit", "1",
            "--max-runtime-seconds", "30",
            "--execute",
            "--expected-plan-digest", "sha256:" + "a" * 64,
        ]
    ) == 2

    assert '"error": "approval_rejected"' in capsys.readouterr().out

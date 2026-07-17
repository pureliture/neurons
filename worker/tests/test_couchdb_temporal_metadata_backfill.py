from __future__ import annotations

import hashlib
import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.couchdb_source.document_model import (
    ProjectionStatus,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_projection_state_document,
    build_source_revision_token,
    build_transcript_session_document,
    conversation_chunk_doc_id,
    coverage_manifest_doc_id,
    projection_state_doc_id,
    session_doc_id,
    sha256_hash,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.build_cli import _select_sessions_needing_projection
from agent_knowledge.llm_brain_core.couchdb_projection_cli import _select_sessions
from agent_knowledge.rag_ingress.state_db import (
    CommandResultSpec,
    DeliveryJobSpec,
    RAGIngressStateDB,
)
from agent_knowledge.rag_ingress import temporal_metadata_backfill as backfill_module
from agent_knowledge.rag_ingress.temporal_metadata_backfill import (
    BACKFILL_OPERATION,
    backfill_temporal_metadata,
    main,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


PROJECT = "neurons"
SESSION_HASH = sha256_hash("private-session")
CHUNK_ID = "chunk_00"
OBSERVED_START = "2026-07-09T10:00:00Z"
OBSERVED_END = "2026-07-09T10:30:00Z"


def _state_db(tmp_path: Path) -> RAGIngressStateDB:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return RAGIngressStateDB(private / "state.sqlite")


def test_resolved_backfill_target_repr_does_not_disclose_raw_target_or_password() -> None:
    target = backfill_module._resolve_backfill_target(
        {
            "COUCHDB_URL": "https://private-backfill.invalid",
            "COUCHDB_DB": "private_backfill_source",
            "COUCHDB_USER": "private-user",
            "COUCHDB_PASSWORD": "backfill-password-marker",
        }
    )

    rendered = repr(target)
    for private_value in (
        "private-backfill.invalid",
        "private_backfill_source",
        "private-user",
        "backfill-password-marker",
    ):
        assert private_value not in rendered


def _payload(
    *,
    project: str = PROJECT,
    session_hash: str = SESSION_HASH,
    chunk_id: str = CHUNK_ID,
    observed_start: str = OBSERVED_START,
    observed_end: str = OBSERVED_END,
    body: str = "redacted temporal source body",
    idempotency_key: str = "",
) -> dict:
    metadata = {
        "type": "conversation_chunk",
        "project": project,
        "provider": "codex",
        "session_id_hash": session_hash,
        "chunk_id": chunk_id,
        "observed_at_start": observed_start,
        "observed_at_end": observed_end,
    }
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {"provider": "codex", "project": project},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "body": body,
                "metadata": metadata,
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "targetProfile": "index-transcript-memory",
        "kind": "conversation_chunk",
        "idempotencyKey": idempotency_key or "wire-key-" + chunk_id,
    }


def _record_payload(
    state_db: RAGIngressStateDB,
    payload: dict,
    *,
    delivery_status: str = "succeeded",
    delivery_target_profile: str = "index-transcript-memory",
) -> None:
    assert state_db.record_delivery_payload(payload) == "recorded"
    key = str(payload["idempotencyKey"])
    payload_hash = str(payload["contentHash"])
    suffix = hashlib.sha256(key.encode()).hexdigest()[:12]
    job_id = f"job-{suffix}"
    state_db.command_transaction().execute(
        command_id=f"command-{suffix}",
        command_type="transcript_ingest",
        idempotency_key=key,
        payload_hash=payload_hash,
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[
            DeliveryJobSpec(
                job_id=job_id,
                target_profile=delivery_target_profile,
                document_kind="conversation_chunk",
                idempotency_key=key,
                payload_hash=payload_hash,
            )
        ],
    )
    if delivery_status != "pending":
        state_db.record_delivery_evidence(job_id, status=delivery_status)


def _source_store(*, with_temporal_metadata: bool = False) -> InMemoryCouchDBSourceStore:
    store = InMemoryCouchDBSourceStore()
    start = OBSERVED_START if with_temporal_metadata else ""
    end = OBSERVED_END if with_temporal_metadata else ""
    session = TranscriptSession(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        started_at=start,
        ended_at=end,
        observed_at_start=start,
        observed_at_end=end,
    )
    chunk = TranscriptChunk.from_text(
        chunk_id=CHUNK_ID,
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        turn_start_index=0,
        turn_end_index=1,
        text="redacted temporal source body",
        observed_at_start=start,
        observed_at_end=end,
    )
    chunk_doc = build_conversation_chunk_document(chunk=chunk)
    store.put(build_transcript_session_document(session=session))
    store.put(chunk_doc)
    coverage = build_coverage_manifest_document(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        conversation_revision_tokens=[
            build_source_revision_token(chunk_doc, material_hash_field="content_hash")
        ],
        tool_evidence_coverage_hashes=[],
        observed_at_start=start,
        observed_at_end=end,
    )
    store.put(coverage)
    current_session = store.get(session_doc_id(SESSION_HASH))
    assert current_session is not None
    current_session["source_hash"] = coverage["source_hash"]
    store.put(current_session)
    store.put(
        build_projection_state_document(
            session_id_hash=SESSION_HASH,
            provider="codex",
            project=PROJECT,
            projection_status=ProjectionStatus.PROJECTED,
            source_hash=coverage["source_hash"],
            projected_source_hash=coverage["source_hash"],
            active_content_hash=sha256_hash("old materialization"),
        )
    )
    return store


def _add_source_session(
    store: InMemoryCouchDBSourceStore,
    *,
    session_hash: str,
    chunk_id: str,
    body: str = "redacted temporal source body",
) -> None:
    session = TranscriptSession(
        session_id_hash=session_hash,
        provider="codex",
        project=PROJECT,
        started_at="",
    )
    chunk = TranscriptChunk.from_text(
        chunk_id=chunk_id,
        session_id_hash=session_hash,
        provider="codex",
        project=PROJECT,
        turn_start_index=0,
        turn_end_index=1,
        text=body,
    )
    chunk_doc = build_conversation_chunk_document(chunk=chunk)
    store.put(build_transcript_session_document(session=session))
    store.put(chunk_doc)
    coverage = build_coverage_manifest_document(
        session_id_hash=session_hash,
        provider="codex",
        project=PROJECT,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        conversation_revision_tokens=[
            build_source_revision_token(chunk_doc, material_hash_field="content_hash")
        ],
        tool_evidence_coverage_hashes=[],
    )
    store.put(coverage)
    store.put(
        build_projection_state_document(
            session_id_hash=session_hash,
            provider="codex",
            project=PROJECT,
            projection_status=ProjectionStatus.PROJECTED,
            source_hash=coverage["source_hash"],
            projected_source_hash=coverage["source_hash"],
            active_content_hash=sha256_hash("old materialization " + chunk_id),
        )
    )


def test_dry_run_is_default_and_reports_hashed_bounded_plan_without_mutation(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    store = _source_store()
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
    )

    assert report["dry_run"] is True
    assert report["status"] == "dry_run"
    assert report["scanned_count"] == 1
    assert report["planned_update_count"] == 1
    assert report["mutation_performed"] is False
    assert report["plan_digest"].startswith("sha256:")
    assert report["project_scope_hash"].startswith("sha256:")
    assert store.all_docs() == before
    encoded = json.dumps(report, sort_keys=True)
    assert SESSION_HASH not in encoded
    assert CHUNK_ID not in encoded
    assert "redacted temporal source body" not in encoded


def test_never_delivered_payload_is_not_a_temporal_repair_candidate(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload(), delivery_status="pending")
    store = _source_store()
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
    )

    assert report["planned_update_count"] == 0
    assert report["total_remaining_update_count"] == 0
    assert report["delivery_not_succeeded_count"] == 1
    assert report["gap_count"] == 1
    assert report["status"] == "dry_run_with_gaps"
    assert report["mutation_performed"] is False
    assert store.all_docs() == before


@pytest.mark.parametrize(
    ("proof_kind", "expected_counter"),
    [
        ("hash", "delivery_hash_mismatch_count"),
        ("scope", "delivery_scope_mismatch_count"),
    ],
)
def test_delivery_proof_mismatch_fails_closed_before_temporal_repair(
    tmp_path,
    proof_kind,
    expected_counter,
):
    state_db = _state_db(tmp_path)
    payload = _payload()
    _record_payload(
        state_db,
        payload,
        delivery_target_profile=(
            "wrong-target" if proof_kind == "scope" else "index-transcript-memory"
        ),
    )
    if proof_kind == "hash":
        with state_db.connect() as connection:
            connection.execute(
                "UPDATE delivery_jobs SET payload_hash = ? WHERE idempotency_key = ?",
                (sha256_hash("different-delivery"), payload["idempotencyKey"]),
            )
    store = _source_store()
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
    )

    assert report["planned_update_count"] == 0
    assert report[expected_counter] == 1
    assert report["error_count"] == 1
    assert report["status"] == "dry_run_with_errors"
    assert report["mutation_performed"] is False
    assert store.all_docs() == before


def test_apply_preserves_body_and_content_hash_refreshes_coverage_and_invalidates_projections(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    store = _source_store()
    chunk_id = conversation_chunk_doc_id(SESSION_HASH, CHUNK_ID)
    before_chunk = store.get(chunk_id)
    before_coverage = store.get(coverage_manifest_doc_id(SESSION_HASH))
    assert before_chunk is not None and before_coverage is not None

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    after_chunk = store.get(chunk_id)
    after_session = store.get(session_doc_id(SESSION_HASH))
    after_coverage = store.get(coverage_manifest_doc_id(SESSION_HASH))
    after_projection = store.get(projection_state_doc_id(SESSION_HASH))
    assert after_chunk is not None
    assert after_session is not None
    assert after_coverage is not None
    assert after_projection is not None
    assert after_chunk["observed_at_start"] == OBSERVED_START
    assert after_chunk["observed_at_end"] == OBSERVED_END
    assert after_chunk["body"] == before_chunk["body"]
    assert after_chunk["content_hash"] == before_chunk["content_hash"]
    assert after_session["observed_at_start"] == OBSERVED_START
    assert after_session["observed_at_end"] == OBSERVED_END
    assert after_coverage["source_hash"] != before_coverage["source_hash"]
    assert after_projection["projection_status"] == ProjectionStatus.PENDING
    assert after_projection["source_hash"] == after_coverage["source_hash"]
    assert after_projection["projected_source_hash"] == before_coverage["source_hash"]
    assert report["updated_count"] == 1
    assert report["source_hash_changed_session_count"] == 1
    assert report["session_projection_invalidated_count"] == 1
    assert report["graph_currentness_invalidated_count"] == 1
    assert report["mutation_performed"] is True
    assert [
        row["session_id_hash"]
        for row in _select_sessions_needing_projection(store, limit=10, project=PROJECT)
    ] == [SESSION_HASH]

    class _OldGraphProjectionState:
        def list_projected_source_hash_sets(self, *args, **kwargs):
            del args, kwargs
            return {SESSION_HASH.replace(":", "_"): {before_coverage["source_hash"]}}

    assert [
        row["session_id_hash"]
        for row in _select_sessions(
            store,
            project=PROJECT,
            provider="",
            limit=10,
            projection_state_store=_OldGraphProjectionState(),
            extraction_level="episodic",
        )
    ] == [SESSION_HASH]


def test_exact_duplicate_is_noop_and_does_not_dirty_projection(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    store = _source_store(with_temporal_metadata=True)
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert report["duplicate_count"] == 1, json.dumps(report, sort_keys=True)
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False
    assert store.all_docs() == before
    projection = store.get(projection_state_doc_id(SESSION_HASH))
    assert projection is not None
    assert projection["projection_status"] == ProjectionStatus.PROJECTED


@pytest.mark.parametrize(
    ("observed_start", "observed_end"),
    [
        (OBSERVED_START, ""),
        ("not-a-timestamp", OBSERVED_END),
        ("2026-07-10T10:00:00Z", "2026-07-09T10:00:00Z"),
    ],
)
def test_missing_or_invalid_temporal_metadata_fails_closed_without_source_mutation(
    tmp_path, observed_start, observed_end
):
    state_db = _state_db(tmp_path)
    _record_payload(
        state_db,
        _payload(observed_start=observed_start, observed_end=observed_end),
    )
    store = _source_store()
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert report["metadata_error_count"] == 1
    assert report["planned_update_count"] == 0
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False
    assert report["status"] == "completed_with_errors"
    assert store.all_docs() == before


class _FailCoverageOnceStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_coverage_once = False

    def put(self, document: dict):
        if document.get("doc_type") == "coverage_manifest" and self.fail_coverage_once:
            self.fail_coverage_once = False
            raise RuntimeError("injected aggregate write failure")
        return super().put(document)


class _UncertainChunkWriteStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.raise_after_temporal_write = False

    def put(self, document: dict):
        result = super().put(document)
        if (
            document.get("doc_type") == "conversation_chunk"
            and document.get("observed_at_start")
            and self.raise_after_temporal_write
        ):
            self.raise_after_temporal_write = False
            raise RuntimeError("injected uncertain write acknowledgement")
        return result


class _ConcurrentContentChangeStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.inject_change = False

    def patch_observed_time_if_content_hash(
        self,
        *,
        doc_id: str,
        expected_content_hash: str,
        expected_rev: str,
        observed_at_start: str,
        observed_at_end: str,
    ):
        if self.inject_change:
            self.inject_change = False
            current = self.get(doc_id)
            assert current is not None
            for key in ("_rev", "idempotency_key", "payload_hash"):
                current.pop(key, None)
            current["body"] = "concurrently delivered newer body"
            current["content_hash"] = sha256_hash(current["body"])
            super().put(current)
        return super().patch_observed_time_if_content_hash(
            doc_id=doc_id,
            expected_content_hash=expected_content_hash,
            expected_rev=expected_rev,
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
        )


class _ConcurrentSameContentRevisionStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.inject_change = False

    def patch_observed_time_if_content_hash(
        self,
        *,
        doc_id: str,
        expected_content_hash: str,
        expected_rev: str,
        observed_at_start: str,
        observed_at_end: str,
    ):
        if self.inject_change:
            self.inject_change = False
            current = self.get(doc_id)
            assert current is not None
            for key in ("_rev", "idempotency_key", "payload_hash"):
                current.pop(key, None)
            current["observed_at_start"] = "2026-07-16T10:00:00Z"
            current["observed_at_end"] = "2026-07-16T10:30:00Z"
            super().put(current)
        return super().patch_observed_time_if_content_hash(
            doc_id=doc_id,
            expected_content_hash=expected_content_hash,
            expected_rev=expected_rev,
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
        )


def test_partial_write_is_reported_as_mutation_and_idempotent_retry_reconciles(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    seeded = _source_store()
    store = _FailCoverageOnceStore()
    for document in seeded.all_docs():
        store.put(document)
    store.fail_coverage_once = True

    failed = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert failed["chunk_metadata_write_count"] == 1
    assert failed["updated_count"] == 0
    assert failed["partial_reconciliation_count"] == 1
    assert failed["mutation_performed"] is True
    assert failed["write_error_count"] == 1

    recovered = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert recovered["planned_update_count"] == 1
    assert recovered["chunk_metadata_write_count"] == 0
    assert recovered["updated_count"] == 1
    assert recovered["partial_reconciliation_count"] == 0
    assert recovered["mutation_performed"] is True
    projection = store.get(projection_state_doc_id(SESSION_HASH))
    coverage = store.get(coverage_manifest_doc_id(SESSION_HASH))
    assert projection is not None and coverage is not None
    assert projection["projection_status"] == ProjectionStatus.PENDING
    assert projection["source_hash"] == coverage["source_hash"]


def test_uncertain_write_acknowledgement_is_explicit_and_retryable(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    seeded = _source_store()
    store = _UncertainChunkWriteStore()
    for document in seeded.all_docs():
        store.put(document)
    store.raise_after_temporal_write = True

    uncertain = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert uncertain["mutation_uncertain"] is True
    assert uncertain["mutation_uncertain_count"] == 1
    assert uncertain["partial_reconciliation_count"] == 1
    assert uncertain["write_error_count"] == 1
    assert uncertain["status"] == "completed_with_errors"

    recovered = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )
    assert recovered["updated_count"] == 1
    assert recovered["mutation_uncertain"] is False


def test_concurrent_content_change_is_not_overwritten_by_stale_planned_document(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    seeded = _source_store()
    store = _ConcurrentContentChangeStore()
    for document in seeded.all_docs():
        store.put(document)
    store.inject_change = True

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    chunk = store.get(conversation_chunk_doc_id(SESSION_HASH, CHUNK_ID))
    assert chunk is not None
    assert chunk["body"] == "concurrently delivered newer body"
    assert chunk["observed_at_start"] == ""
    assert report["write_conflict_count"] == 1
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False


def test_concurrent_same_content_revision_change_also_fails_cas(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    seeded = _source_store()
    store = _ConcurrentSameContentRevisionStore()
    for document in seeded.all_docs():
        store.put(document)
    store.inject_change = True

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    chunk = store.get(conversation_chunk_doc_id(SESSION_HASH, CHUNK_ID))
    assert chunk is not None
    assert chunk["observed_at_start"] == "2026-07-16T10:00:00Z"
    assert report["write_conflict_count"] == 1
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False


def test_reconciliation_only_plan_rechecks_temporal_revision_before_aggregate_write(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    seeded = _source_store()
    doc_id = conversation_chunk_doc_id(SESSION_HASH, CHUNK_ID)
    chunk = seeded.get(doc_id)
    assert chunk is not None
    seeded.patch_observed_time_if_content_hash(
        doc_id=doc_id,
        expected_content_hash=chunk["content_hash"],
        expected_rev=chunk["_rev"],
        observed_at_start=OBSERVED_START,
        observed_at_end=OBSERVED_END,
    )
    # Deliberately leave coverage/session/projection on the old source revision.
    store = _ConcurrentSameContentRevisionStore()
    for document in seeded.all_docs():
        store.put(document)
    store.inject_change = True

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    current = store.get(doc_id)
    assert current is not None
    assert current["observed_at_start"] == "2026-07-16T10:00:00Z"
    assert report["write_conflict_count"] == 1
    assert report["updated_count"] == 0


def test_project_scope_limit_and_finite_timeout_are_mandatory_bounded_inputs(tmp_path):
    state_db = _state_db(tmp_path)
    store = _source_store()

    for project, limit, timeout in (
        ("", 10, 30),
        (PROJECT, 0, 30),
        (PROJECT, 10, 0),
        (PROJECT, 10, float("nan")),
        (PROJECT, 10, float("inf")),
        (PROJECT, 10, float("-inf")),
    ):
        try:
            backfill_temporal_metadata(
                state_db=state_db,
                source_store=store,
                project=project,
                limit=limit,
                max_runtime_seconds=timeout,
            )
        except ValueError as exc:
            assert str(exc)
        else:
            raise AssertionError("unbounded temporal metadata backfill must fail closed")


def test_max_runtime_aborts_before_any_mutation(tmp_path):
    state_db = _state_db(tmp_path)
    store = InMemoryCouchDBSourceStore()
    for index in range(2):
        session_hash = sha256_hash(f"timeout-session-{index}")
        chunk_id = f"timeout_chunk_{index}"
        _record_payload(
            state_db,
            _payload(session_hash=session_hash, chunk_id=chunk_id),
        )
        _add_source_session(store, session_hash=session_hash, chunk_id=chunk_id)
    ticks = iter((0.0, 0.0, 31.0))

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
        monotonic=lambda: next(ticks, 31.0),
    )

    assert report["status"] == "aborted_timeout"
    assert report["timed_out"] is True
    assert report["abort_count"] == 1
    assert report["scanned_count"] == 1
    assert report["updated_count"] == 0
    assert report["mutation_performed"] is False


def test_completed_first_batch_does_not_starve_later_payload_rows(tmp_path):
    state_db = _state_db(tmp_path)
    store = InMemoryCouchDBSourceStore()
    session_hashes = [sha256_hash(f"private-session-{index}") for index in range(3)]
    for index, session_hash in enumerate(session_hashes):
        chunk_id = f"chunk_{index:02d}"
        _record_payload(
            state_db,
            _payload(session_hash=session_hash, chunk_id=chunk_id),
        )
        _add_source_session(store, session_hash=session_hash, chunk_id=chunk_id)

    first = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=2,
        max_runtime_seconds=30,
        execute=True,
    )
    second = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=2,
        max_runtime_seconds=30,
        execute=True,
    )

    assert first["updated_count"] == 2
    assert second["scanned_count"] == 3
    assert second["duplicate_count"] == 2
    assert second["updated_count"] == 1
    for index, session_hash in enumerate(session_hashes):
        stored = store.get(conversation_chunk_doc_id(session_hash, f"chunk_{index:02d}"))
        assert stored is not None
        assert stored["observed_at_start"] == OBSERVED_START


def test_wire_content_revision_must_match_existing_couchdb_chunk(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload(body="authoritative retained wire body"))
    store = InMemoryCouchDBSourceStore()
    _add_source_session(
        store,
        session_hash=SESSION_HASH,
        chunk_id=CHUNK_ID,
        body="different current couchdb body",
    )
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert report["content_conflict_count"] == 1
    assert report["planned_update_count"] == 0
    assert report["mutation_performed"] is False
    assert store.all_docs() == before


def test_current_content_revision_selects_only_its_bound_temporal_metadata(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(
        state_db,
        _payload(
            body="older source revision",
            observed_start="2026-07-09T10:00:00Z",
            observed_end="2026-07-09T10:30:00Z",
            idempotency_key="wire-old",
        ),
    )
    _record_payload(
        state_db,
        _payload(
            body="current source revision",
            observed_start="2026-07-15T10:00:00Z",
            observed_end="2026-07-15T10:30:00Z",
            idempotency_key="wire-current",
        ),
    )
    store = InMemoryCouchDBSourceStore()
    _add_source_session(
        store,
        session_hash=SESSION_HASH,
        chunk_id=CHUNK_ID,
        body="current source revision",
    )

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    stored = store.get(conversation_chunk_doc_id(SESSION_HASH, CHUNK_ID))
    assert stored is not None
    assert stored["observed_at_start"] == "2026-07-15T10:00:00Z"
    assert report["superseded_content_count"] == 1
    assert report["updated_count"] == 1
    assert report["content_conflict_count"] == 0


def test_conflicting_temporal_metadata_for_same_content_revision_fails_closed(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(
        state_db,
        _payload(idempotency_key="wire-a"),
    )
    _record_payload(
        state_db,
        _payload(
            observed_start="2026-07-10T10:00:00Z",
            observed_end="2026-07-10T10:30:00Z",
            idempotency_key="wire-b",
        ),
    )
    store = _source_store()
    before = store.all_docs()

    report = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=10,
        max_runtime_seconds=30,
        execute=True,
    )

    assert report["wire_conflict_count"] == 1
    assert report["planned_update_count"] == 0
    assert report["mutation_performed"] is False
    assert store.all_docs() == before


def _write_approval(
    tmp_path: Path,
    argv: list[str],
    *,
    target_fingerprints: dict[str, str] | None = None,
) -> Path:
    payload = {
        "schema_version": "agent_knowledge_live_approval.v1",
        "operation": BACKFILL_OPERATION,
        "operator_approval": {"approved": True},
        "redaction_required": True,
        "timeout_seconds": 30,
        "rollback_or_abort_criteria": ["abort on any source write error"],
        "command": {"argv": argv},
    }
    if target_fingerprints is not None:
        payload["target"] = {"target_fingerprints": target_fingerprints}
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_bounded_plan_digest_binds_planned_subset_and_blocks_batch_replay(tmp_path):
    state_db = _state_db(tmp_path)
    second_session = sha256_hash("private-session-two")
    _record_payload(state_db, _payload())
    _record_payload(
        state_db,
        _payload(
            session_hash=second_session,
            chunk_id="chunk_01",
            idempotency_key="wire-key-two",
        ),
    )
    store = _source_store()
    _add_source_session(
        store,
        session_hash=second_session,
        chunk_id="chunk_01",
    )

    first_plan = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=1,
        max_runtime_seconds=30,
    )
    assert first_plan["planned_update_count"] == 1
    assert first_plan["total_remaining_update_count"] == 2
    first_digest = first_plan["plan_digest"]

    first_execute = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=1,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=first_digest,
    )
    assert first_execute["status"] == "completed"
    assert first_execute["updated_count"] == 1

    second_plan = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=1,
        max_runtime_seconds=30,
    )
    assert second_plan["planned_update_count"] == 1
    assert second_plan["total_remaining_update_count"] == 1
    assert second_plan["plan_digest"] != first_digest

    before_replay = store.all_docs()
    replay = backfill_temporal_metadata(
        state_db=state_db,
        source_store=store,
        project=PROJECT,
        limit=1,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=first_digest,
    )
    assert replay["status"] == "blocked_plan_drift"
    assert replay["mutation_performed"] is False
    assert store.all_docs() == before_replay


def test_cli_defaults_to_dry_run_and_live_requires_exact_argv_approval(tmp_path):
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    store = _source_store()
    dry_argv = [
        "--state-db",
        str(state_db.path),
        "--project",
        PROJECT,
        "--limit",
        "10",
        "--max-runtime-seconds",
        "30",
    ]
    state_files_before = {
        path.name: path.stat().st_mtime_ns for path in state_db.path.parent.iterdir()
    }

    with (
        patch(
            "agent_knowledge.rag_ingress.temporal_metadata_backfill.CouchDBHttpSourceStore",
            return_value=store,
        ),
        patch.dict(
            os.environ,
            {
                "COUCHDB_URL": "http://example.invalid",
                "COUCHDB_DB": "source",
                "COUCHDB_USER": "user",
                "COUCHDB_PASSWORD": "password",
            },
        ),
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(dry_argv) == 0
        dry_report = json.loads(output.getvalue())
    assert dry_report["dry_run"] is True
    assert dry_report["mutation_performed"] is False
    assert {
        path.name: path.stat().st_mtime_ns for path in state_db.path.parent.iterdir()
    } == state_files_before

    expected_plan_digest = dry_report["plan_digest"]
    with patch("sys.stdout", StringIO()) as output:
        assert main([*dry_argv, "--execute"]) == 2
        missing_plan = json.loads(output.getvalue())
    assert missing_plan["error"] == "expected_plan_digest_invalid"
    assert missing_plan["mutation_performed"] is False

    live_without_approval = [
        *dry_argv,
        "--execute",
        "--expected-plan-digest",
        expected_plan_digest,
    ]
    with patch("sys.stdout", StringIO()) as output:
        assert main(live_without_approval) == 2
        rejected = json.loads(output.getvalue())
    assert rejected["error"] == "approval_rejected"
    assert rejected["mutation_performed"] is False

    drift_argv = [
        *dry_argv,
        "--execute",
        "--expected-plan-digest",
        "sha256:" + ("0" * 64),
        "--approval",
        "PLACEHOLDER",
    ]
    drift_approval = _write_approval(
        tmp_path,
        drift_argv,
        target_fingerprints=dry_report["target_fingerprints"],
    )
    drift_argv[-1] = str(drift_approval)
    drift_approval = _write_approval(
        tmp_path,
        drift_argv,
        target_fingerprints=dry_report["target_fingerprints"],
    )
    before_drift = store.all_docs()
    with (
        patch(
            "agent_knowledge.rag_ingress.temporal_metadata_backfill.CouchDBHttpSourceStore",
            return_value=store,
        ),
        patch.dict(
            os.environ,
            {
                "COUCHDB_URL": "http://example.invalid",
                "COUCHDB_DB": "source",
                "COUCHDB_USER": "user",
                "COUCHDB_PASSWORD": "password",
            },
        ),
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(drift_argv) == 1
        drifted = json.loads(output.getvalue())
    assert drifted["status"] == "blocked_plan_drift"
    assert drifted["mutation_performed"] is False
    assert store.all_docs() == before_drift

    approval_argv = [
        *dry_argv,
        "--execute",
        "--expected-plan-digest",
        expected_plan_digest,
        "--approval",
        "PLACEHOLDER",
    ]
    approval = _write_approval(
        tmp_path,
        approval_argv,
        target_fingerprints=dry_report["target_fingerprints"],
    )
    approval_argv[-1] = str(approval)
    # The approval must bind the actual argv, including its own path.
    approval = _write_approval(
        tmp_path,
        approval_argv,
        target_fingerprints=dry_report["target_fingerprints"],
    )
    live_argv = approval_argv
    with (
        patch(
            "agent_knowledge.rag_ingress.temporal_metadata_backfill.CouchDBHttpSourceStore",
            return_value=store,
        ),
        patch.dict(
            os.environ,
            {
                "COUCHDB_URL": "http://example.invalid",
                "COUCHDB_DB": "source",
                "COUCHDB_USER": "user",
                "COUCHDB_PASSWORD": "password",
            },
        ),
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(live_argv) == 0
        applied = json.loads(output.getvalue())
    assert applied["dry_run"] is False
    assert applied["updated_count"] == 1
    assert applied["mutation_performed"] is True


def test_cli_rejects_resolved_couchdb_target_drift_before_constructing_writable_store(
    tmp_path: Path,
) -> None:
    state_db = _state_db(tmp_path)
    _record_payload(state_db, _payload())
    store = _source_store()
    argv = [
        "--state-db",
        str(state_db.path),
        "--project",
        PROJECT,
        "--limit",
        "10",
        "--max-runtime-seconds",
        "30",
    ]
    primary_env = {
        "COUCHDB_URL": "https://primary-couchdb.invalid",
        "COUCHDB_DB": "primary_source",
        "COUCHDB_USER": "user",
        "COUCHDB_PASSWORD": "password",
    }
    with (
        patch(
            "agent_knowledge.rag_ingress.temporal_metadata_backfill.CouchDBHttpSourceStore",
            return_value=store,
        ),
        patch.dict(os.environ, primary_env, clear=False),
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(argv) == 0
    plan = json.loads(output.getvalue())
    assert set(plan["target_fingerprints"]) == {"couchdb_source"}
    assert plan["target_fingerprints"]["couchdb_source"].startswith("sha256:")
    assert "primary-couchdb" not in json.dumps(plan, sort_keys=True)

    approval_argv = [
        *argv,
        "--execute",
        "--expected-plan-digest",
        plan["plan_digest"],
        "--approval",
        "PLACEHOLDER",
    ]
    approval = _write_approval(
        tmp_path,
        approval_argv,
        target_fingerprints=plan["target_fingerprints"],
    )
    approval_argv[-1] = str(approval)
    _write_approval(
        tmp_path,
        approval_argv,
        target_fingerprints=plan["target_fingerprints"],
    )

    with (
        patch.dict(
            os.environ,
            {**primary_env, "COUCHDB_DB": "drifted_source"},
            clear=False,
        ),
        patch(
            "agent_knowledge.rag_ingress.temporal_metadata_backfill.CouchDBHttpSourceStore",
            side_effect=AssertionError("must not construct writable store after target drift"),
        ) as source_store,
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(approval_argv) == 2
    report = json.loads(output.getvalue())
    assert report["error"] == "approval_rejected"
    assert report["mutation_performed"] is False
    source_store.assert_not_called()


def test_neuron_knowledge_routes_backfill_as_approval_gated_runtime_command():
    assert COMMAND_HANDLERS["couchdb-temporal-metadata-backfill"] is main
    assert COMMAND_METADATA["couchdb-temporal-metadata-backfill"] == {
        "runtime_category": "human_gated_metadata_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    }

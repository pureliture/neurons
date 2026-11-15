"""End-to-end integration of the embedding + build candidates (synthetic only).

This file must be run from the integration clone and imports both implementations:
- local_embedding streaming client / LocalEmbeddingOutboxWorker (embedding candidate)
- session-memory selector / materializer / dead-letter lookup (build candidate)
No DB, network, DML, or production data.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import (
    ProjectionDeadLetterLookupError,
    _select_sessions_needing_projection,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
    project_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.local_embedding.canonical import split_utf8_chunks_v1
from agent_knowledge.local_embedding.client import CanonicalLocalEmbeddingClient
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker
from agent_knowledge.postgres_store.pgvector_store import LocalOutboxJob
from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    LOCAL_EMBEDDING_DEAD_LETTER,
    PG_INPUT_POLICY_VERSION,
)
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
)


# ---------------------------------------------------------------------------
# Integrated fake: outbox state used by both candidates
# ---------------------------------------------------------------------------
class _Row(dict):
    pass


class _IntegratedStore:
    """Stateful outbox/SLE model with the latest-row dead-letter contract."""

    def __init__(self, *, session_id_hash: str, chunk_id: str, content_hash: str):
        self.session_id_hash = session_id_hash
        self.chunk_id = chunk_id
        self.content_hash = content_hash
        self.rows: list[_Row] = []
        self.sle_state = ""
        self.sle_vector: list[float] | None = None
        self.lookup_error: Exception | None = None
        self.next_id = 1

    def add(self, *, status: str, input_hash: str = "sha256:" + "b" * 64) -> _Row:
        row = _Row(
            outbox_id=self.next_id,
            target_type="session_chunk",
            target_id=self.chunk_id,
            profile_id=LOCAL_EMBEDDING_PROFILE_ID,
            source_content_hash=self.content_hash,
            input_hash=input_hash,
            payload_text="payload",
            incarnation=1,
            status=status,
            fencing_token=0,
            worker_id=None,
            lease_until=0.0,
            retry_count=0,
        )
        self.next_id += 1
        self.rows.append(row)
        return row

    def _latest(self, target_id: str) -> _Row | None:
        rows = [row for row in self.rows if row["target_id"] == target_id]
        return max(rows, key=lambda row: row["outbox_id"]) if rows else None

    # build-candidate lookup contract
    def has_dead_letter_local_outbox_for_session(self, *, session_id_hash, profile_id):
        if self.lookup_error is not None:
            raise self.lookup_error
        if session_id_hash != self.session_id_hash or profile_id != LOCAL_EMBEDDING_PROFILE_ID:
            return False
        latest = self._latest(self.chunk_id)
        return bool(
            latest
            and latest["status"] == "dead_letter"
            and latest["source_content_hash"] == self.content_hash
        )

    def has_dead_letter_local_outbox(
        self, *, target_type, target_id, profile_id, source_content_hash="", input_hash=""
    ):
        latest = self._latest(target_id)
        return bool(
            latest
            and latest["status"] == "dead_letter"
            and latest["target_type"] == target_type
            and latest["profile_id"] == profile_id
            and (not source_content_hash or latest["source_content_hash"] == source_content_hash)
            and (not input_hash or latest["input_hash"] == input_hash)
        )

    # embedding-candidate worker contract
    def claim_local_outbox_leases(self, *, worker_id, batch_size, lease_seconds, **_kwargs):
        now = time.monotonic()
        claimed = []
        for row in sorted(self.rows, key=lambda r: r["outbox_id"]):
            claimable = row["status"] in {"queued", "failed"} or (
                row["status"] == "processing" and row["lease_until"] < now
            )
            if not claimable:
                continue
            row["status"] = "processing"
            row["fencing_token"] += 1
            row["worker_id"] = worker_id
            row["lease_until"] = now + lease_seconds
            claimed.append(self._job(row))
            if len(claimed) >= batch_size:
                break
        return claimed

    def _job(self, row: _Row) -> LocalOutboxJob:
        return LocalOutboxJob(
            outbox_id=row["outbox_id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            profile_id=row["profile_id"],
            source_content_hash=row["source_content_hash"],
            input_hash=row["input_hash"],
            payload_text=row["payload_text"],
            incarnation=row["incarnation"],
            status=row["status"],
            fencing_token=row["fencing_token"],
            worker_id=row["worker_id"],
            lease_until=None,
        )

    def renew_local_outbox_lease(self, *, outbox_id, worker_id, fencing_token, lease_seconds):
        row = next(r for r in self.rows if r["outbox_id"] == outbox_id)
        if (
            row["status"] == "processing"
            and row["worker_id"] == worker_id
            and row["fencing_token"] == fencing_token
            and row["lease_until"] > time.monotonic()
        ):
            row["lease_until"] = time.monotonic() + lease_seconds
            return True
        return False

    def complete_local_outbox_cas(self, *, outbox_id, worker_id, fencing_token, vector, **_kwargs):
        row = next(r for r in self.rows if r["outbox_id"] == outbox_id)
        if (
            row["status"] != "processing"
            or row["worker_id"] != worker_id
            or row["fencing_token"] != fencing_token
            or row["lease_until"] <= time.monotonic()
        ):
            return False
        row["status"] = "completed"
        self.sle_state = "ready"
        self.sle_vector = list(vector)
        return True

    def mark_local_outbox_failed(
        self, *, outbox_id, worker_id, fencing_token, error_code, retryable, **_kwargs
    ):
        row = next(r for r in self.rows if r["outbox_id"] == outbox_id)
        if (
            row["status"] != "processing"
            or row["worker_id"] != worker_id
            or row["fencing_token"] != fencing_token
            or row["lease_until"] <= time.monotonic()
        ):
            raise ValueError("lease mismatch")
        row["status"] = "dead_letter" if not retryable else "failed"
        row["retry_count"] += 1
        row["last_error"] = error_code
        self.sle_state = "failed"
        return row["status"]

    def expire(self) -> None:
        for row in self.rows:
            if row["status"] == "processing":
                row["lease_until"] = 0.0

    def requeue(self, row: _Row) -> None:
        row["status"] = "queued"
        row["lease_until"] = 0.0
        row["worker_id"] = None


# ---------------------------------------------------------------------------
# CouchDB session/state helpers
# ---------------------------------------------------------------------------
def _seed_session(store: InMemoryCouchDBSourceStore, *, raw_id: str) -> str:
    provider, project = "claude", "neurons"
    sid = dm.build_session_id_hash(provider, raw_id)
    session = TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-06-17T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_00", session_id_hash=sid, provider=provider, project=project,
        turn_start_index=0, turn_end_index=0, text="user asked; assistant answered",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=provider, project=project,
        conversation_chunk_count=1, tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        tool_evidence_coverage_hashes=[],
        conversation_revision_tokens=[
            dm.build_source_revision_token(chunk_doc, material_hash_field="content_hash")
        ],
        observed_at_start=session.started_at, observed_at_end=session.started_at,
        project_authority={"project": project, "ambiguous": False, "eligible_for_retirement": True},
    ))
    return sid


def _put_terminal_state(store, sid: str, source_hash: str) -> None:
    state = dm.build_projection_state_document(
        session_id_hash=sid, provider="claude", project="neurons",
        projection_status=dm.ProjectionStatus.PENDING,
    )
    state["source_hash"] = source_hash
    state["backend_receipts"] = {}
    state["projection_attempt"] = {
        "postgres_pgvector_local": {
            "status": dm.ProjectionStatus.FAILED,
            "reason": LOCAL_EMBEDDING_DEAD_LETTER,
            "source_hash": source_hash,
            "recorded_at": "2026-11-15T00:00:00+00:00",
            "policy_version": PG_INPUT_POLICY_VERSION,
        }
    }
    state["failure_source_hash"] = source_hash
    store.put(state)


def _select(store, lookup):
    return _select_sessions_needing_projection(
        store, limit=0, backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        representation="session_v1", dead_letter_lookup=lookup,
    )


class _SuccessProjector:
    embedding_profile = LOCAL_EMBEDDING_PROFILE_ID

    def project(self, *, target_profile, document):
        return "local-ref"

    def close(self):
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_import_paths_are_repo_lib():
    import agent_knowledge
    import agent_knowledge.couchdb_source.build_cli as build_cli
    import agent_knowledge.local_embedding.client as client
    import agent_knowledge.postgres_store.outbox_worker as worker

    repo_root = Path(__file__).resolve().parents[2]
    lib_root = (repo_root / "worker" / "lib").resolve()
    assert lib_root.is_dir(), lib_root
    for module in (agent_knowledge, build_cli, client, worker):
        module_path = Path(module.__file__).resolve()
        assert lib_root in module_path.parents, (module.__name__, module_path, lib_root)

    # Both candidate features must be present in the imported modules.
    assert "dead_letter_lookup" in inspect.signature(
        build_cli._select_sessions_needing_projection
    ).parameters
    assert hasattr(client.CanonicalLocalEmbeddingClient, "_embed_documents_streaming")


def test_oversize_streaming_then_terminal_failure_suppresses_and_requeue_unblocks():
    # 1) Embedding candidate: a >524288-byte document streams via 8 KiB chunks.
    calls: list[list[str]] = []

    def fake_batch(batch):
        calls.append(list(batch))
        return [[0.1] * 256 for _ in batch], len(batch)

    client = CanonicalLocalEmbeddingClient(base_url="http://127.0.0.1:1/v1", max_retries=0)
    client._send_chunk_batch_sync = fake_batch
    text = "가" * 200_000  # 600,000 UTF-8 bytes
    result = client.embed_document(text)
    assert result.chunk_count == len(split_utf8_chunks_v1(text))
    assert all(len(chunk.encode("utf-8")) <= 8192 for batch in calls for chunk in batch)

    # 2) Build candidate: a terminal local failure is dead-lettered, then suppressed.
    couch = InMemoryCouchDBSourceStore()
    sid = _seed_session(couch, raw_id="integration-transition")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=couch
    )
    _put_terminal_state(couch, sid, source_hash)

    def terminal_embed(_text):
        from agent_knowledge.local_embedding.client import LocalEmbeddingClientError
        raise LocalEmbeddingClientError("synthetic_terminal", retryable=False)

    state = _IntegratedStore(
        session_id_hash=sid, chunk_id="c" * 64,
        content_hash="sha256:" + "a" * 64,
    )
    row = state.add(status="queued")
    worker = LocalEmbeddingOutboxWorker(
        store=state, embedding_fn=terminal_embed, batch_size=1, lease_seconds=1
    )
    worker.run_once()
    assert row["status"] == "dead_letter"
    assert state.sle_state == "failed"
    assert _select(couch, state) == []

    # 3) Requeue -> processing -> ready releases the selector.
    state.requeue(row)
    assert [x["session_id_hash"] for x in _select(couch, state)] == [sid]

    worker_ok = LocalEmbeddingOutboxWorker(
        store=state, embedding_fn=lambda _text: [0.2] * 256,
        batch_size=1, lease_seconds=1,
    )
    worker_ok.run_once()
    assert row["status"] == "completed"
    assert state.sle_state == "ready" and len(state.sle_vector or []) == 256

    # 4) A successful materializer/projector clears the failure attempt.
    materialized = materialize_session_memory(session_id_hash=sid, store=couch)
    projected = project_session_memory(
        materialized=materialized, store=couch,
        projector=_SuccessProjector(), backend="postgres_pgvector",
    )
    assert projected["status"] == dm.ProjectionStatus.PROJECTED
    assert _select(couch, state) == []


def test_stale_lease_reclaim_is_single_writer_and_unblocks_selector():
    couch = InMemoryCouchDBSourceStore()
    sid = _seed_session(couch, raw_id="integration-lease")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=couch
    )
    _put_terminal_state(couch, sid, source_hash)
    state = _IntegratedStore(
        session_id_hash=sid, chunk_id="d" * 64,
        content_hash="sha256:" + "e" * 64,
    )
    state.add(status="queued")

    worker_a = LocalEmbeddingOutboxWorker(
        store=state, embedding_fn=lambda _t: [0.3] * 256,
        batch_size=1, lease_seconds=1, worker_id="a",
    )
    jobs_a = state.claim_local_outbox_leases(
        worker_id="a", batch_size=1, lease_seconds=1
    )
    assert len(jobs_a) == 1 and jobs_a[0].fencing_token == 1
    state.expire()
    jobs_b = state.claim_local_outbox_leases(
        worker_id="b", batch_size=1, lease_seconds=1
    )
    assert len(jobs_b) == 1 and jobs_b[0].fencing_token == 2
    assert state.complete_local_outbox_cas(
        outbox_id=jobs_b[0].outbox_id, worker_id="b",
        fencing_token=jobs_b[0].fencing_token, vector=[0.4] * 256,
    )
    assert not state.complete_local_outbox_cas(
        outbox_id=jobs_a[0].outbox_id, worker_id="a",
        fencing_token=jobs_a[0].fencing_token, vector=[0.9] * 256,
    )
    assert state.sle_vector == [0.4] * 256
    # Completed latest row releases the terminal block; the session is selected
    # so a normal projection can consume the ready vector.
    assert [x["session_id_hash"] for x in _select(couch, state)] == [sid]

    materialized = materialize_session_memory(session_id_hash=sid, store=couch)
    project_session_memory(
        materialized=materialized, store=couch,
        projector=_SuccessProjector(), backend="postgres_pgvector",
    )
    assert _select(couch, state) == []


def test_db_outage_raises_explicit_lookup_error():
    couch = InMemoryCouchDBSourceStore()
    sid = _seed_session(couch, raw_id="integration-outage")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=couch
    )
    _put_terminal_state(couch, sid, source_hash)
    state = _IntegratedStore(
        session_id_hash=sid, chunk_id="f" * 64,
        content_hash="sha256:" + "1" * 64,
    )
    state.add(status="dead_letter")
    state.lookup_error = RuntimeError("synthetic db outage")

    with pytest.raises(ProjectionDeadLetterLookupError):
        _select(couch, state)

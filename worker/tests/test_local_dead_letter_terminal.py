"""Terminal local dead-letter classification and receipt lifecycle (synthetic only)."""

from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
    project_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    LOCAL_EMBEDDING_DEAD_LETTER,
    EmbeddingChunksPending,
    LocalEmbeddingTerminalFailure,
)
from agent_knowledge.rag_ingress.pg_representation import derive_local_pg_chunk_id
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
)


def _seed_session(store: InMemoryCouchDBSourceStore, *, raw_id: str = "terminal") -> str:
    provider, project = "claude", "neurons"
    sid = dm.build_session_id_hash(provider, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=provider,
        project=project,
        started_at="2026-06-17T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_00",
        session_id_hash=sid,
        provider=provider,
        project=project,
        turn_start_index=0,
        turn_end_index=0,
        text="user asked; assistant answered",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=provider,
            project=project,
            conversation_chunk_count=1,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[chunk_doc["content_hash"]],
            tool_evidence_coverage_hashes=[],
            conversation_revision_tokens=[
                dm.build_source_revision_token(
                    chunk_doc, material_hash_field="content_hash"
                )
            ],
            observed_at_start=session.started_at,
            observed_at_end=session.started_at,
            project_authority={
                "project": project,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    return sid


class _LookupStore:
    def __init__(self, *, terminal: bool = False, error: Exception | None = None):
        self.terminal = terminal
        self.error = error
        self.calls: list[dict] = []

    def has_dead_letter_local_outbox(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.terminal


class _FailingProjector:
    embedding_profile = LOCAL_EMBEDDING_PROFILE_ID

    def project(self, *, target_profile, document):
        raise LocalEmbeddingTerminalFailure()

    def close(self):
        return None


class _SuccessProjector:
    embedding_profile = LOCAL_EMBEDDING_PROFILE_ID

    def project(self, *, target_profile, document):
        return "local-ref"

    def close(self):
        return None


def _projector_with(store) -> PgSessionMemoryProjector:
    projector = object.__new__(PgSessionMemoryProjector)
    projector._store = store
    projector.last_local_representation_receipt = object()
    return projector


def test_terminal_dead_letter_raises_terminal_not_pending():
    store = _LookupStore(terminal=True)
    projector = _projector_with(store)

    with pytest.raises(LocalEmbeddingTerminalFailure) as excinfo:
        projector._raise_local_pending_or_terminal(
            target_id="a" * 64,
            content_hash="sha256:" + "b" * 64,
            input_hash="sha256:" + "c" * 64,
        )

    assert excinfo.value.code == LOCAL_EMBEDDING_DEAD_LETTER
    assert projector.last_local_representation_receipt is None
    assert store.calls == [
        {
            "target_type": "session_chunk",
            "target_id": "a" * 64,
            "profile_id": LOCAL_EMBEDDING_PROFILE_ID,
            "source_content_hash": "sha256:" + "b" * 64,
            "input_hash": "sha256:" + "c" * 64,
        }
    ]


def test_non_dead_letter_failure_stays_pending():
    projector = _projector_with(_LookupStore(terminal=False))

    with pytest.raises(EmbeddingChunksPending):
        projector._raise_local_pending_or_terminal(
            target_id="a" * 64,
            content_hash="sha256:" + "b" * 64,
            input_hash="sha256:" + "c" * 64,
        )


def test_missing_lookup_is_explicit_not_pending():
    projector = _projector_with(object())

    with pytest.raises(RuntimeError, match="dead-letter lookup unavailable"):
        projector._raise_local_pending_or_terminal(
            target_id="a" * 64,
            content_hash="sha256:" + "b" * 64,
            input_hash="sha256:" + "c" * 64,
        )


def test_materializer_records_terminal_failure_and_success_clears_it():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store)
    materialized = materialize_session_memory(session_id_hash=sid, store=store)
    assert materialized.source_hash
    state_id = dm.projection_state_doc_id(sid)

    failed = project_session_memory(
        materialized=materialized,
        store=store,
        projector=_FailingProjector(),
        backend="postgres_pgvector",
    )

    assert failed["status"] == dm.ProjectionStatus.FAILED
    assert failed["reason"] == LOCAL_EMBEDDING_DEAD_LETTER
    state = store.get(state_id)
    attempt = state["projection_attempt"]["postgres_pgvector_local"]
    assert attempt["status"] == dm.ProjectionStatus.FAILED
    assert attempt["reason"] == LOCAL_EMBEDDING_DEAD_LETTER
    assert attempt["source_hash"] == materialized.source_hash
    assert state.get("failure_source_hash") == materialized.source_hash

    succeeded = project_session_memory(
        materialized=materialized,
        store=store,
        projector=_SuccessProjector(),
        backend="postgres_pgvector",
    )

    assert succeeded["status"] == dm.ProjectionStatus.PROJECTED
    state = store.get(state_id)
    attempt = state["projection_attempt"]["postgres_pgvector_local"]
    assert attempt["status"] == dm.ProjectionStatus.PROJECTED
    assert state.get("failure_source_hash") in (None, "")
    receipt = state["backend_receipts"]["postgres_pgvector_local"][
        LOCAL_EMBEDDING_PROFILE_ID
    ]
    assert receipt["projected_source_hash"] == materialized.source_hash


def test_local_receipt_identifier_is_deterministic_without_receipt():
    args = {
        "project": "neurons",
        "provider": "claude",
        "session_id_hash": "sha256:" + "1" * 64,
        "source_hash": "sha256:" + "2" * 64,
        "content_hash": "sha256:" + "3" * 64,
        "embedding_profile": LOCAL_EMBEDDING_PROFILE_ID,
    }
    first = derive_local_pg_chunk_id(**args)
    second = derive_local_pg_chunk_id(**args)
    assert first == second
    assert len(first) == 64 and all(ch in "0123456789abcdef" for ch in first)
    assert derive_local_pg_chunk_id(**{**args, "content_hash": "sha256:" + "4" * 64}) != first
    assert derive_local_pg_chunk_id(**{**args, "source_hash": "sha256:" + "5" * 64}) != first
    with pytest.raises(ValueError):
        derive_local_pg_chunk_id(**{**args, "source_hash": "bad"})

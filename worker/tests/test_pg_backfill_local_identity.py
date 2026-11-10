"""Local-profile PG identity conflict resolution tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from agent_knowledge.couchdb_source.document_model import (
    INDEX_RECALL_PROFILE,
    sha256_hash,
)
from agent_knowledge.local_embedding.canonical import (
    canonical_input_hash_v1,
    canonical_session_input_v1,
    compute_input_hash,
)
from agent_knowledge.model_connectors import (
    DEFAULT_EMBEDDING_PROFILE_ID,
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_PROFILE_ID,
)
from agent_knowledge.postgres_store.pgvector_store import SessionChunk
from agent_knowledge.rag_ingress.pg_backfill import (
    PgSessionMemoryProjector,
    _derive_pg_chunk_id_impl,
)
from agent_knowledge.rag_ingress.pg_embedding_chunks import CHUNKED_PROFILE
from agent_knowledge.rag_ingress.pg_representation import (
    build_local_segment_receipt_v4,
    derive_local_pg_chunk_id,
    valid_local_pg_receipt_metadata,
)
from agent_knowledge.session_memory.chunk_overlap import ChunkView


class LocalEmbedProvider:
    size = LOCAL_EMBEDDING_DIM
    model = LOCAL_EMBEDDING_MODEL


class SegmentEmbedProvider(LocalEmbedProvider):
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [0.25] * self.size


class InMemoryLocalPgStore:
    def __init__(self) -> None:
        self.rows: dict[str, SessionChunk] = {}
        self.local_embeddings: dict[str, dict] = {}
        self.insert_count = 0

    @contextmanager
    def transaction(self):
        yield self

    def acquire_session_identity_lock(self, **_kwargs) -> None:
        return None

    def find_session_chunks_by_natural_scope(
        self,
        *,
        session_id_hash: str,
        project: str,
        provider: str,
        content_hash: str,
        **_kwargs,
    ) -> list[SessionChunk]:
        return [
            row
            for row in self.rows.values()
            if (
                row.session_id_hash,
                row.project,
                row.provider,
                row.content_hash,
            )
            == (session_id_hash, project, provider, content_hash)
        ]

    def get_chunk(self, chunk_id: str, **_kwargs) -> SessionChunk | None:
        return self.rows.get(chunk_id)

    def _enqueue_local_outbox_on(self, *_args, **_kwargs) -> None:
        return None

    def insert_chunk_with_local_outbox(
        self,
        chunk: SessionChunk,
        **_kwargs,
    ) -> tuple[str, None]:
        self.rows[chunk.chunk_id] = chunk
        self.insert_count += 1
        self._set_ready_local_embedding(chunk)
        return chunk.chunk_id, None

    def get_local_embedding_row(
        self,
        *,
        target_type: str,
        target_id: str,
        embedding_profile: str,
    ) -> dict | None:
        assert target_type == "session_chunk"
        assert embedding_profile == LOCAL_EMBEDDING_PROFILE_ID
        return self.local_embeddings.get(target_id)

    def seed_row(self, row: SessionChunk) -> None:
        self.rows[row.chunk_id] = row
        self._set_ready_local_embedding(row)

    def _set_ready_local_embedding(self, row: SessionChunk) -> None:
        input_hash = canonical_input_hash_v1(
            canonical_session_input_v1(row.content_markdown)
        )
        self.local_embeddings[row.chunk_id] = {
            "state": "ready",
            "source_content_hash": row.content_hash,
            "embedding_profile": LOCAL_EMBEDDING_PROFILE_ID,
            "content_hash": row.content_hash,
            "input_hash": input_hash,
            "embedding_state": "ready",
            "embedding": [0.0] * LOCAL_EMBEDDING_DIM,
        }


def _document(body: str = "synthetic local memory") -> dict[str, str]:
    return {
        "body": body,
        "content_hash": sha256_hash(body),
        "session_id_hash": sha256_hash("synthetic-session"),
        "source_hash": sha256_hash("synthetic-source"),
        "project": "synthetic-project",
        "provider": "synthetic-provider",
    }


def _row(
    chunk_id: str,
    document: dict[str, str],
    *,
    body: str | None = None,
) -> SessionChunk:
    return SessionChunk(
        chunk_id=chunk_id,
        session_id_hash=document["session_id_hash"],
        project=document["project"],
        provider=document["provider"],
        chunk_index=0,
        content_markdown=body if body is not None else document["body"],
        content_hash=document["content_hash"],
        embedding_state="ready",
        embedding=[0.0] * LOCAL_EMBEDDING_DIM,
        embedding_model=LOCAL_EMBEDDING_MODEL,
        embedding_revision=1,
        observed_at=None,
    )


def _identity_ids(document: dict[str, str]) -> tuple[str, str, str]:
    legacy_v1 = _derive_pg_chunk_id_impl(
        project=document["project"],
        provider=document["provider"],
        session_id_hash=document["session_id_hash"],
        source_hash=document["source_hash"],
        content_hash=document["content_hash"],
        embedding_profile=DEFAULT_EMBEDDING_PROFILE_ID,
    )
    legacy_v2 = _derive_pg_chunk_id_impl(
        project=document["project"],
        provider=document["provider"],
        session_id_hash=document["session_id_hash"],
        source_hash=document["source_hash"],
        content_hash=document["content_hash"],
        embedding_profile=CHUNKED_PROFILE,
    )
    local_v1 = derive_local_pg_chunk_id(
        project=document["project"],
        provider=document["provider"],
        session_id_hash=document["session_id_hash"],
        source_hash=document["source_hash"],
        content_hash=document["content_hash"],
    )
    return legacy_v1, legacy_v2, local_v1


def test_segment_projection_reuses_ready_rows_and_only_embeds_changed_tail(monkeypatch):
    class SegmentStore(InMemoryLocalPgStore):
        def insert_chunk_with_local_outbox(self, chunk: SessionChunk, **_kwargs):
            self.rows[chunk.chunk_id] = chunk
            self.insert_count += 1
            self.local_embeddings[chunk.chunk_id] = {
                "state": "pending",
                "source_content_hash": chunk.content_hash,
                "input_hash": compute_input_hash(chunk.content_markdown),
                "vector": None,
            }
            return chunk.chunk_id, self.insert_count

    class Worker:
        def __init__(self, *, store, embedding_fn, **_kwargs):
            self.store = store
            self.embedding_fn = embedding_fn
            self.last_batch_failed = False

        def run_once(self):
            pending = next(
                (
                    chunk_id
                    for chunk_id, local in self.store.local_embeddings.items()
                    if local["state"] == "pending"
                ),
                None,
            )
            if pending is None:
                return 0
            text = self.store.rows[pending].content_markdown
            self.store.local_embeddings[pending].update(
                state="ready",
                vector=self.embedding_fn(text),
            )
            return 1

    monkeypatch.setattr(
        "agent_knowledge.postgres_store.outbox_worker.LocalEmbeddingOutboxWorker",
        Worker,
    )

    def view(index: int, text: str) -> ChunkView:
        return ChunkView(
            content_hash=sha256_hash(f"view-{index}-{text}"),
            turn_start_index=index,
            turn_end_index=index,
            part_index=1,
            part_count=1,
            char_start=0,
            char_end=len(text),
            redaction_version="test",
            text=text,
        )

    views = [view(index, chr(96 + index) * 70) for index in range(1, 4)]
    body = "synthetic session body with turns one two three"
    document = {
        **_document(body),
        "conversation_views": tuple(views),
        "tool_evidence_bodies": (),
    }
    store = SegmentStore()
    provider = SegmentEmbedProvider()
    projector = PgSessionMemoryProjector(
        store,
        provider,
        representation="segment_v1",
        segment_max_bytes=350,
    )

    projector.project(target_profile=INDEX_RECALL_PROFILE, document=document)
    original_rows = deepcopy(store.rows)
    original_ids = set(store.rows)
    assert projector.last_segments_total == 2
    assert projector.last_segments_reused == 0
    assert projector.last_segments_embedded == 2
    assert store.insert_count == 2
    assert provider.calls == 2

    projector.project(target_profile=INDEX_RECALL_PROFILE, document=document)
    assert projector.last_segments_reused == 2
    assert projector.last_segments_embedded == 0
    assert store.insert_count == 2
    assert provider.calls == 2
    assert store.rows == original_rows

    appended_document = {
        **document,
        "body": body + " and appended fourth turn",
        "content_hash": sha256_hash(body + " and appended fourth turn"),
        "source_hash": sha256_hash("synthetic-source-after-append"),
        "conversation_views": (*views, view(4, "d" * 70)),
    }
    projector.project(target_profile=INDEX_RECALL_PROFILE, document=appended_document)
    assert projector.last_segments_total == 2
    assert projector.last_segments_reused == 1
    assert projector.last_segments_embedded == 1
    assert store.insert_count == 3
    assert provider.calls == 3
    assert all(store.rows[chunk_id] == original_rows[chunk_id] for chunk_id in original_ids)


def test_segment_receipt_v4_binds_ordered_segment_ids():
    segment_ids = ["seg_" + "a" * 60, "seg_" + "b" * 60]
    digest = sha256_hash(
        json.dumps(segment_ids, ensure_ascii=False, separators=(",", ":"))
    )
    receipt = build_local_segment_receipt_v4(
        project="synthetic-project",
        provider="synthetic-provider",
        session_id_hash=sha256_hash("synthetic-session"),
        active_content_hash=sha256_hash("source-body"),
        projected_source_hash=sha256_hash("source-revision"),
        representation_content_hash=sha256_hash("source-body"),
        session_memory_knowledge_id=segment_ids[0],
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        segment_ids=segment_ids,
        segment_content_hashes=[
            sha256_hash("segment a"),
            sha256_hash("segment b"),
        ],
        segment_set_digest=digest,
    )

    assert valid_local_pg_receipt_metadata(receipt)
    assert receipt["representation"] == "segment_v1"
    assert receipt["segment_ids"] == segment_ids
    assert receipt["segment_content_hashes"] == [
        sha256_hash("segment a"),
        sha256_hash("segment b"),
    ]
    assert receipt["segment_set_digest"] == digest
    assert receipt["session_memory_knowledge_id"] == segment_ids[0]
    assert not valid_local_pg_receipt_metadata(
        {**receipt, "segment_content_hashes": [sha256_hash("segment a")]}
    )
    assert not valid_local_pg_receipt_metadata(
        {
            **receipt,
            "segment_content_hashes": [sha256_hash("segment a"), "invalid"],
        }
    )
    assert not valid_local_pg_receipt_metadata(
        {
            **receipt,
            "segment_content_hashes": [
                sha256_hash("different segment a"),
                sha256_hash("segment b"),
            ],
        }
    )


@pytest.mark.parametrize(
    ("scenario", "expected_resolution", "expected_bypassed", "expected_inserts"),
    [
        ("large_nonderivable", "new_local_v1_over_unverifiable_legacy", 1, 1),
        ("multiple_natural", "new_local_v1_over_unverifiable_legacy", 2, 1),
        ("multiple_exact", "new_local_v1_over_unverifiable_legacy", 2, 1),
        ("local_and_legacy", "local_v1_preferred", 1, 0),
        ("exact_and_extra", "new_local_v1_over_unverifiable_legacy", 2, 1),
    ],
)
def test_ambiguous_local_identity_uses_or_creates_local_v1(
    scenario: str,
    expected_resolution: str,
    expected_bypassed: int,
    expected_inserts: int,
    capsys,
) -> None:
    body = "long " * 1800 if scenario == "large_nonderivable" else "synthetic local memory"
    document = _document(body)
    store = InMemoryLocalPgStore()
    legacy_v1, legacy_v2, local_v1 = _identity_ids(document)

    if scenario == "large_nonderivable":
        store.seed_row(_row("natural-large", document))
    elif scenario == "multiple_natural":
        store.seed_row(_row("natural-a", document))
        store.seed_row(_row("natural-b", document))
    elif scenario == "multiple_exact":
        store.seed_row(_row(legacy_v1, document))
        store.seed_row(_row(legacy_v2, document))
    elif scenario == "local_and_legacy":
        store.seed_row(_row(local_v1, document))
        store.seed_row(_row(legacy_v1, document))
    elif scenario == "exact_and_extra":
        store.seed_row(_row(legacy_v1, document))
        store.seed_row(_row("natural-extra", document))

    legacy_snapshot = {
        chunk_id: deepcopy(row)
        for chunk_id, row in store.rows.items()
        if chunk_id != local_v1
    }
    projector = PgSessionMemoryProjector(store, LocalEmbedProvider())

    first_id = projector.project(target_profile="session-memory", document=document)
    first_log = capsys.readouterr().err.strip()
    assert first_id == local_v1
    assert projector.last_local_identity_scheme == "local_v1"
    assert projector.last_local_identity_resolution == expected_resolution
    assert projector.last_local_identity_bypassed_rows == expected_bypassed
    assert store.insert_count == expected_inserts
    assert first_log == (
        "[pg-identity] "
        f"resolution={expected_resolution} bypassed={expected_bypassed}"
    )
    for chunk_id, row in legacy_snapshot.items():
        assert store.rows[chunk_id] == row

    second_id = projector.project(target_profile="session-memory", document=document)
    second_log = capsys.readouterr().err.strip()
    assert second_id == first_id
    assert projector.last_local_identity_resolution == "local_v1_preferred"
    assert projector.last_local_identity_bypassed_rows == expected_bypassed
    assert second_log == (
        "[pg-identity] "
        f"resolution=local_v1_preferred bypassed={expected_bypassed}"
    )
    assert store.insert_count == expected_inserts
    for chunk_id, row in legacy_snapshot.items():
        assert store.rows[chunk_id] == row
    receipt = projector.last_local_representation_receipt
    assert receipt is not None
    assert valid_local_pg_receipt_metadata(
        receipt,
        store=store,
        require_ready_vector=True,
    )


def test_nonchosen_body_mismatch_does_not_block_exact_legacy_reuse() -> None:
    document = _document()
    store = InMemoryLocalPgStore()
    legacy_v1, _, _local_v1 = _identity_ids(document)
    bad_row = _row("natural-bad", document, body="different body")
    exact_row = _row(legacy_v1, document)
    store.seed_row(bad_row)
    store.seed_row(exact_row)
    projector = PgSessionMemoryProjector(store, LocalEmbedProvider())

    chunk_id = projector.project(target_profile="session-memory", document=document)

    assert chunk_id == legacy_v1
    assert projector.last_local_identity_scheme == "legacy_exact"
    assert projector.last_local_identity_bypassed_rows == 1
    assert projector.last_local_identity_resolution == ""
    assert store.rows["natural-bad"] == bad_row


def test_chosen_local_v1_body_mismatch_still_fails_closed() -> None:
    document = _document()
    store = InMemoryLocalPgStore()
    _legacy_v1, _legacy_v2, local_v1 = _identity_ids(document)
    bad_local_row = _row(local_v1, document, body="different body")
    store.seed_row(bad_local_row)
    projector = PgSessionMemoryProjector(store, LocalEmbedProvider())

    with pytest.raises(ValueError, match="row scope or body mismatch"):
        projector.project(target_profile="session-memory", document=document)

    assert store.rows[local_v1] == bad_local_row


def test_small_single_verified_natural_row_remains_legacy_reuse() -> None:
    document = _document()
    store = InMemoryLocalPgStore()
    store.seed_row(_row("natural-verified", document))
    projector = PgSessionMemoryProjector(store, LocalEmbedProvider())

    chunk_id = projector.project(target_profile="session-memory", document=document)

    assert chunk_id == "natural-verified"
    assert projector.last_local_identity_scheme == "legacy_reuse"
    assert projector.last_local_identity_resolution == ""
    assert projector.last_local_identity_bypassed_rows == 0
    assert store.insert_count == 0
    assert projector.last_local_representation_receipt is not None
    assert valid_local_pg_receipt_metadata(
        projector.last_local_representation_receipt,
        store=store,
        require_ready_vector=True,
    )


def test_no_rows_creates_local_v1_without_conflict_resolution() -> None:
    document = _document()
    store = InMemoryLocalPgStore()
    projector = PgSessionMemoryProjector(store, LocalEmbedProvider())

    chunk_id = projector.project(target_profile="session-memory", document=document)

    assert chunk_id == _identity_ids(document)[2]
    assert projector.last_local_identity_scheme == "local_v1"
    assert projector.last_local_identity_resolution == ""
    assert projector.last_local_identity_bypassed_rows == 0
    assert store.insert_count == 1


def test_ambiguous_rows_still_require_source_hash_for_local_v1() -> None:
    document = _document()
    document["source_hash"] = ""
    store = InMemoryLocalPgStore()
    store.seed_row(_row("natural-a", document))
    store.seed_row(_row("natural-b", document))
    projector = PgSessionMemoryProjector(store, LocalEmbedProvider())

    with pytest.raises(ValueError, match="source_hash is required"):
        projector.project(target_profile="session-memory", document=document)

    assert store.insert_count == 0

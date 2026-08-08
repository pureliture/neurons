"""Focused active-source-revision ingress regression tests."""

from __future__ import annotations

import hashlib
import os

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import _select_sessions_needing_projection
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    activate_source_revision,
    build_revision_scoped_source_documents,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import (
    InMemoryCouchDBSourceStore,
    SourceStoreConflict,
)
from agent_knowledge.rag_ingress.backfill_apply import apply_backfill_to_state_db
from agent_knowledge.rag_ingress.couchdb_delivery_backend import (
    CouchDBDeliveryBackend,
    _chunk_documents_match,
)
from agent_knowledge.rag_ingress.couchdb_retired_index_bridge import (
    CouchDBRetiredIndexBridgeAdapter,
)
from agent_knowledge.rag_ingress.delivery_executor import (
    DeliveryExecutor,
    DeliveryJobView,
    DeliveryOutcomeUncertain,
)
from agent_knowledge.rag_ingress.delivery_reconcile import DeliveryReconciler
from agent_knowledge.rag_ingress.server_runtime import document_from_ingress_payload
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from agent_knowledge.session_memory.transcript_model import TranscriptChunk


SESSION_ID_HASH = dm.sha256_hash("codex:active-pointer-ingress")
PROVIDER = "codex"
PROJECT = "neurons"


def _payload(
    *,
    idempotency_key: str,
    chunk_id: str,
    body: str,
) -> dict:
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {
            "host": "test-host",
            "producer": "test",
            "provider": PROVIDER,
            "project": PROJECT,
        },
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": "session.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {
                    "type": "conversation_chunk",
                    "session_id_hash": SESSION_ID_HASH,
                    "chunk_id": chunk_id,
                    "provider": PROVIDER,
                    "project": PROJECT,
                    "turn_start_index": 0,
                    "turn_end_index": 1,
                    "part_index": 1,
                    "part_count": 1,
                    "char_start": 0,
                    "char_end": len(body),
                },
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "targetProfile": "couchdb-transcript-source",
        "kind": "conversation_chunk",
        "idempotencyKey": idempotency_key,
    }


def _state_db(tmp_path) -> RAGIngressStateDB:
    private = tmp_path / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    return RAGIngressStateDB(private / "state.sqlite")


def _job(state_db: RAGIngressStateDB, idempotency_key: str) -> DeliveryJobView:
    row = state_db.get_row("delivery_jobs", "idempotency_key", idempotency_key)
    assert row is not None
    return DeliveryJobView.from_row(row)


def _mark_projected(store: InMemoryCouchDBSourceStore, *, source_hash: str) -> None:
    state_id = dm.projection_state_doc_id(SESSION_ID_HASH)
    state = store.get(state_id)
    assert state is not None
    state.update(
        {
            "projection_status": dm.ProjectionStatus.PROJECTED,
            "active_content_hash": dm.sha256_hash("materialized-session-memory"),
            "source_hash": source_hash,
            "projected_source_hash": source_hash,
        }
    )
    store.put(state)


def _active_pointer(store: InMemoryCouchDBSourceStore) -> dict:
    pointer = store.get(dm.active_source_revision_pointer_doc_id(SESSION_ID_HASH))
    assert pointer is not None
    return pointer


def _orphan_chunk_document(
    *,
    chunk_id: str,
    body: str,
    provider: str = PROVIDER,
    project: str = PROJECT,
    redaction_version: str = "redaction.v2",
    source_status: str = "source_locator_private_spool_only",
) -> dict:
    chunk = TranscriptChunk(
        chunk_id=chunk_id,
        session_id_hash=SESSION_ID_HASH,
        provider=provider,
        project=project,
        turn_start_index=0,
        turn_end_index=1,
        redacted_text=body,
        content_hash=dm.sha256_hash(body),
        redaction_version=redaction_version,
        source_status=source_status,
        part_index=1,
        part_count=1,
        char_start=0,
        char_end=len(body),
    )
    return dm.build_conversation_chunk_document(chunk=chunk, source_locator_hash="")


def _activate_corrective_current_source(
    store: InMemoryCouchDBSourceStore,
    *,
    chunk_id: str,
    body: str,
    provider: str = PROVIDER,
    project: str = PROJECT,
):
    """Activate a corrective current copy that supersedes one raw chunk id."""

    session = store.get(dm.session_doc_id(SESSION_ID_HASH))
    assert session is not None
    corrective_chunk = _orphan_chunk_document(
        chunk_id=chunk_id,
        body=body,
        provider=provider,
        project=project,
    )
    current_documents = build_revision_scoped_source_documents(
        documents=(session, corrective_chunk),
        source_snapshot_hash=dm.sha256_hash("corrective-current-source"),
        scope_kind="current",
    )
    for document in current_documents:
        store.put_if_absent(document)
    activated = activate_source_revision(
        store=store,
        session_id_hash=SESSION_ID_HASH,
        source_document_ids=tuple(document["_id"] for document in current_documents),
    )
    current_chunk = next(
        document
        for document in current_documents
        if document["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK
    )
    return activated, current_chunk


def _force_delete_for_corrupt_active_revision_test(
    store: InMemoryCouchDBSourceStore,
    document_id: str,
) -> None:
    """Bypass only the in-memory delete guard to model external corruption."""

    assert store._docs.pop(document_id, None) is not None  # noqa: SLF001


class _FailOnceCoverageStore(InMemoryCouchDBSourceStore):
    """Inject one post-pointer coverage failure without changing source writes."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_next_coverage_write = False

    def fail_next_coverage_write(self) -> None:
        self._fail_next_coverage_write = True

    def put(self, document: dict):
        if (
            self._fail_next_coverage_write
            and str(document.get("doc_type") or "") == dm.SourceDocType.COVERAGE_MANIFEST
        ):
            self._fail_next_coverage_write = False
            raise SourceStoreConflict("injected active coverage failure")
        return super().put(document)


class _OriginDriftAtPointerCasStore(InMemoryCouchDBSourceStore):
    """Mutate one mutable origin immediately before a successor pointer CAS."""

    def __init__(self, origin_document_id: str) -> None:
        super().__init__()
        self._origin_document_id = origin_document_id
        self._inject_at_pointer_cas = False

    def inject_origin_drift_at_next_pointer_cas(self) -> None:
        self._inject_at_pointer_cas = True

    def put_if_revision(self, document: dict, *, expected_rev: str):
        if (
            self._inject_at_pointer_cas
            and str(document.get("doc_type") or "") == dm.SourceDocType.ACTIVE_SOURCE_REVISION
        ):
            self._inject_at_pointer_cas = False
            origin = self.get(self._origin_document_id)
            assert origin is not None
            changed = dict(origin)
            changed["body"] = "origin changed during successor pointer transition"
            changed["content_hash"] = dm.sha256_hash(changed["body"])
            # Model an external CouchDB writer after the store's preflight
            # guard but before the pointer CAS; the local guard cannot make
            # those separate remote documents atomic.
            changed["_rev"] = "999-origin-drift"
            self._docs[self._origin_document_id] = changed
        return super().put_if_revision(document, expected_rev=expected_rev)


class _FailOncePointerCasStore(InMemoryCouchDBSourceStore):
    """Reject one successor-pointer CAS after the new chunk becomes an orphan."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_next_pointer_cas = False

    def fail_next_pointer_cas(self) -> None:
        self._fail_next_pointer_cas = True

    def put_if_revision(self, document: dict, *, expected_rev: str):
        if (
            self._fail_next_pointer_cas
            and str(document.get("doc_type") or "") == dm.SourceDocType.ACTIVE_SOURCE_REVISION
        ):
            self._fail_next_pointer_cas = False
            raise SourceStoreConflict("injected active pointer CAS failure")
        return super().put_if_revision(document, expected_rev=expected_rev)


class _InitialActivationInterleavingStore(InMemoryCouchDBSourceStore):
    """Run one legacy ingress after member staging but before initial pointer CAS."""

    def __init__(self) -> None:
        super().__init__()
        self._on_first_member_staged = None
        self._member_interleaving_fired = False

    def set_on_first_member_staged(self, callback) -> None:
        self._on_first_member_staged = callback

    def put_if_absent(self, document: dict):
        revision = super().put_if_absent(document)
        if (
            not self._member_interleaving_fired
            and str(document.get("doc_type") or "") == dm.SourceDocType.SOURCE_REVISION_MEMBER
            and self._on_first_member_staged is not None
        ):
            self._member_interleaving_fired = True
            self._on_first_member_staged()
        return revision


class _PointerPublishBeforeLegacyChunkWriteStore(InMemoryCouchDBSourceStore):
    """Publish an initial pointer after ingress reads absence, before its raw write."""

    def __init__(self) -> None:
        super().__init__()
        self._before_next_chunk_write = None

    def set_before_next_chunk_write(self, callback) -> None:
        self._before_next_chunk_write = callback

    def _publish_before_chunk_write(self, document: dict) -> None:
        callback = self._before_next_chunk_write
        if (
            callback is not None
            and str(document.get("doc_type") or "") == dm.SourceDocType.CONVERSATION_CHUNK
        ):
            self._before_next_chunk_write = None
            callback()

    def put(self, document: dict):
        self._publish_before_chunk_write(document)
        return super().put(document)

    def put_if_absent(self, document: dict):
        self._publish_before_chunk_write(document)
        return super().put_if_absent(document)


class _ConcurrentChunkPayloadCollisionStore(InMemoryCouchDBSourceStore):
    """Create a conflicting chunk after the delivery pre-read, before insert."""

    def __init__(self) -> None:
        super().__init__()
        self._collision_document: dict | None = None

    def inject_next_chunk_collision(self, document: dict) -> None:
        self._collision_document = document

    def put_if_absent(self, document: dict):
        collision = self._collision_document
        if (
            collision is not None
            and str(document.get("doc_type") or "") == dm.SourceDocType.CONVERSATION_CHUNK
            and str(document.get("_id") or "") == str(collision.get("_id") or "")
        ):
            self._collision_document = None
            super().put_if_absent(collision)
        return super().put_if_absent(document)


def _assert_active_currentness_recovered(store: InMemoryCouchDBSourceStore) -> None:
    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    coverage = store.get(dm.coverage_manifest_doc_id(SESSION_ID_HASH))
    projection = store.get(dm.projection_state_doc_id(SESSION_ID_HASH))

    assert coverage is not None
    assert coverage["source_hash"] == resolved.source_hash
    assert coverage["active_source_manifest_id"] == resolved.manifest_id
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == resolved.source_hash


def test_delivery_backend_rotates_active_pointer_for_distinct_chunk_not_duplicate(tmp_path) -> None:
    first = _payload(
        idempotency_key="delivery-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    second = _payload(
        idempotency_key="delivery-second",
        chunk_id="second-chunk",
        body="Second distinct active source chunk.",
    )
    state_db = _state_db(tmp_path)
    assert apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)[
        "conflict_count"
    ] == 0
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "delivery-first"))
    first_resolved = activate_source_revision(
        store=store,
        session_id_hash=SESSION_ID_HASH,
    )
    active_evidence = backend.find_by_natural_key(
        "delivery-first",
        _job(state_db, "delivery-first").payload_hash,
    )
    assert active_evidence is not None
    assert active_evidence.status == "succeeded"
    _mark_projected(store, source_hash=first_resolved.source_hash)
    pointer_before_duplicate = _active_pointer(store)
    session_before_distinct = store.get(dm.session_doc_id(SESSION_ID_HASH))
    assert session_before_distinct is not None

    backend.submit(_job(state_db, "delivery-first"))

    assert _active_pointer(store) == pointer_before_duplicate
    assert _select_sessions_needing_projection(store, limit=0) == []
    duplicate_projection = store.get(dm.projection_state_doc_id(SESSION_ID_HASH))
    assert duplicate_projection is not None
    assert duplicate_projection["projection_status"] == dm.ProjectionStatus.PROJECTED
    assert duplicate_projection["projected_source_hash"] == first_resolved.source_hash

    backend.submit(_job(state_db, "delivery-second"))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    projection = store.get(dm.projection_state_doc_id(SESSION_ID_HASH))
    assert projection is not None
    assert _active_pointer(store)["active_revision"] != pointer_before_duplicate["active_revision"]
    assert store.get(dm.session_doc_id(SESSION_ID_HASH)) == session_before_distinct
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == resolved.source_hash
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "second-chunk",
    }
    manifest = store.get(resolved.manifest_id or "")
    assert manifest is not None
    active_source_ids = {
        document["_id"]
        for document in (
            *resolved.sessions,
            *resolved.conversation_chunks,
            *resolved.tool_evidence_bundles,
        )
    }
    assert {member["source_document_id"] for member in manifest["members"]} == active_source_ids
    assert all(
        document.get("source_snapshot_schema_version")
        for document in (
            *resolved.sessions,
            *resolved.conversation_chunks,
            *resolved.tool_evidence_bundles,
        )
    )
    assert active_source_ids.isdisjoint(
        {
            dm.session_doc_id(SESSION_ID_HASH),
            dm.conversation_chunk_doc_id(SESSION_ID_HASH, "first-chunk"),
            dm.conversation_chunk_doc_id(SESSION_ID_HASH, "second-chunk"),
        }
    )
    assert [row["session_id_hash"] for row in _select_sessions_needing_projection(store, limit=0)] == [
        SESSION_ID_HASH
    ]


def test_initial_activation_staging_does_not_make_legacy_ingress_uncertain(tmp_path) -> None:
    first = _payload(
        idempotency_key="initial-interleaving-first",
        chunk_id="first-chunk",
        body="First source before initial activation.",
    )
    second = _payload(
        idempotency_key="initial-interleaving-second",
        chunk_id="second-chunk",
        body="Distinct ingress while activation stages members.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = _InitialActivationInterleavingStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)
    interleaved_deliveries = []

    backend.submit(_job(state_db, "initial-interleaving-first"))
    store.set_on_first_member_staged(
        lambda: interleaved_deliveries.append(
            backend.submit(_job(state_db, "initial-interleaving-second"))
        )
    )

    activated = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "second-chunk",
    }
    assert resolved.is_legacy_unpinned is False
    assert resolved.source_hash == activated.source_hash
    assert all(
        document.get("source_snapshot_schema_version")
        for document in resolved.conversation_chunks
    )
    assert [delivery.status for delivery in interleaved_deliveries] == ["succeeded"]


def test_legacy_ingress_repairs_initial_pointer_published_before_chunk_write(tmp_path) -> None:
    first = _payload(
        idempotency_key="late-pointer-first",
        chunk_id="first-chunk",
        body="First source before initial activation.",
    )
    second = _payload(
        idempotency_key="late-pointer-second",
        chunk_id="second-chunk",
        body="Distinct source written after initial pointer publication.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = _PointerPublishBeforeLegacyChunkWriteStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "late-pointer-first"))
    store.set_before_next_chunk_write(
        lambda: activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    )

    evidence = backend.submit(_job(state_db, "late-pointer-second"))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert evidence.status == "succeeded"
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "second-chunk",
    }
    _assert_active_currentness_recovered(store)


def test_legacy_retired_bridge_repairs_initial_pointer_published_before_chunk_write() -> None:
    first = _payload(
        idempotency_key="late-pointer-bridge-first",
        chunk_id="first-chunk",
        body="First bridge source before initial activation.",
    )
    second = _payload(
        idempotency_key="late-pointer-bridge-second",
        chunk_id="second-chunk",
        body="Distinct bridge source written after initial pointer publication.",
    )
    store = _PointerPublishBeforeLegacyChunkWriteStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)

    adapter.submit_document(document_from_ingress_payload(first))
    store.set_before_next_chunk_write(
        lambda: activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    )

    result = adapter.submit_document(document_from_ingress_payload(second))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert result.status == "submitted"
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "second-chunk",
    }
    _assert_active_currentness_recovered(store)


def test_retired_index_bridge_rotates_active_pointer_for_distinct_chunk_not_duplicate() -> None:
    first = _payload(
        idempotency_key="bridge-first",
        chunk_id="first-chunk",
        body="First active bridge chunk.",
    )
    second = _payload(
        idempotency_key="bridge-second",
        chunk_id="second-chunk",
        body="Second distinct bridge chunk.",
    )
    store = InMemoryCouchDBSourceStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)

    adapter.submit_document(document_from_ingress_payload(first))
    first_resolved = activate_source_revision(
        store=store,
        session_id_hash=SESSION_ID_HASH,
    )
    _mark_projected(store, source_hash=first_resolved.source_hash)
    pointer_before_duplicate = _active_pointer(store)
    session_before_distinct = store.get(dm.session_doc_id(SESSION_ID_HASH))
    assert session_before_distinct is not None

    adapter.submit_document(document_from_ingress_payload(first))

    assert _active_pointer(store) == pointer_before_duplicate
    assert _select_sessions_needing_projection(store, limit=0) == []

    adapter.submit_document(document_from_ingress_payload(second))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    projection = store.get(dm.projection_state_doc_id(SESSION_ID_HASH))
    assert projection is not None
    assert _active_pointer(store)["active_revision"] != pointer_before_duplicate["active_revision"]
    assert store.get(dm.session_doc_id(SESSION_ID_HASH)) == session_before_distinct
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == resolved.source_hash
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "second-chunk",
    }


def test_delivery_retry_after_origin_drift_activates_current_successor(tmp_path) -> None:
    first = _payload(
        idempotency_key="delivery-origin-drift-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    second = _payload(
        idempotency_key="delivery-origin-drift-second",
        chunk_id="second-chunk",
        body="Second active source chunk.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = _OriginDriftAtPointerCasStore(
        dm.conversation_chunk_doc_id(SESSION_ID_HASH, "first-chunk")
    )
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "delivery-origin-drift-first"))
    activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    store.inject_origin_drift_at_next_pointer_cas()

    with pytest.raises(DeliveryOutcomeUncertain):
        backend.submit(_job(state_db, "delivery-origin-drift-second"))

    stale = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert stale.conversation_chunks[0]["body"] != "origin changed during successor pointer transition"

    backend.submit(_job(state_db, "delivery-origin-drift-second"))

    recovered = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert recovered.source_hash != stale.source_hash
    assert {
        document["body"] for document in recovered.conversation_chunks
    } >= {"origin changed during successor pointer transition"}
    _assert_active_currentness_recovered(store)


def test_retired_bridge_retry_after_origin_drift_activates_current_successor() -> None:
    first = _payload(
        idempotency_key="bridge-origin-drift-first",
        chunk_id="first-chunk",
        body="First active bridge chunk.",
    )
    second = _payload(
        idempotency_key="bridge-origin-drift-second",
        chunk_id="second-chunk",
        body="Second active bridge chunk.",
    )
    store = _OriginDriftAtPointerCasStore(
        dm.conversation_chunk_doc_id(SESSION_ID_HASH, "first-chunk")
    )
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)

    adapter.submit_document(document_from_ingress_payload(first))
    activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    store.inject_origin_drift_at_next_pointer_cas()

    with pytest.raises(SourceStoreConflict, match="origin changed during activation"):
        adapter.submit_document(document_from_ingress_payload(second))

    stale = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert stale.conversation_chunks[0]["body"] != "origin changed during successor pointer transition"

    adapter.submit_document(document_from_ingress_payload(second))

    recovered = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert recovered.source_hash != stale.source_hash
    assert {
        document["body"] for document in recovered.conversation_chunks
    } >= {"origin changed during successor pointer transition"}
    _assert_active_currentness_recovered(store)


def test_delivery_backend_duplicate_retry_repairs_active_currentness_after_coverage_failure(
    tmp_path,
) -> None:
    first = _payload(
        idempotency_key="delivery-retry-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    second = _payload(
        idempotency_key="delivery-retry-second",
        chunk_id="second-chunk",
        body="Second active source chunk.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = _FailOnceCoverageStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "delivery-retry-first"))
    initial = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    _mark_projected(store, source_hash=initial.source_hash)
    store.fail_next_coverage_write()

    with pytest.raises(DeliveryOutcomeUncertain):
        backend.submit(_job(state_db, "delivery-retry-second"))

    backend.submit(_job(state_db, "delivery-retry-second"))

    _assert_active_currentness_recovered(store)


def test_retired_index_bridge_duplicate_retry_repairs_active_currentness_after_coverage_failure() -> None:
    first = _payload(
        idempotency_key="bridge-retry-first",
        chunk_id="first-chunk",
        body="First active bridge chunk.",
    )
    second = _payload(
        idempotency_key="bridge-retry-second",
        chunk_id="second-chunk",
        body="Second active bridge chunk.",
    )
    store = _FailOnceCoverageStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)

    adapter.submit_document(document_from_ingress_payload(first))
    initial = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    _mark_projected(store, source_hash=initial.source_hash)
    store.fail_next_coverage_write()

    with pytest.raises(SourceStoreConflict):
        adapter.submit_document(document_from_ingress_payload(second))

    adapter.submit_document(document_from_ingress_payload(second))

    _assert_active_currentness_recovered(store)


def test_delivery_backend_rejects_invalid_active_pointer_before_new_chunk_write(tmp_path) -> None:
    first = _payload(
        idempotency_key="invalid-pointer-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    second = _payload(
        idempotency_key="invalid-pointer-second",
        chunk_id="second-chunk",
        body="Second distinct active source chunk.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)
    backend.submit(_job(state_db, "invalid-pointer-first"))
    resolved = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert resolved.manifest_id is not None
    manifest = store.get(resolved.manifest_id)
    assert manifest is not None
    _force_delete_for_corrupt_active_revision_test(store, manifest["members"][0]["member_id"])
    documents_before = store.all_docs()

    with pytest.raises(DeliveryOutcomeUncertain):
        backend.submit(_job(state_db, "invalid-pointer-second"))

    assert store.all_docs() == documents_before
    assert store.get(dm.conversation_chunk_doc_id(SESSION_ID_HASH, "second-chunk")) is None


def test_delivery_reconciler_does_not_promote_stale_reference_when_active_pointer_is_invalid(
    tmp_path,
) -> None:
    first = _payload(
        idempotency_key="invalid-pointer-reconcile-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "invalid-pointer-reconcile-first"))
    resolved = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert resolved.manifest_id is not None
    manifest = store.get(resolved.manifest_id)
    assert manifest is not None
    _force_delete_for_corrupt_active_revision_test(store, manifest["members"][0]["member_id"])

    job = _job(state_db, "invalid-pointer-reconcile-first")
    assert state_db.record_failed_retryable_attempt(
        job.job_id,
        dataset_ref="couchdb:couchdb",
        document_ref=dm.session_doc_id(SESSION_ID_HASH),
        run="stale-couchdb-reference",
        max_attempts=4,
    ) == "failed_retryable"

    assert DeliveryReconciler(state_db=state_db, backend=backend).reconcile_once(
        job.job_id,
        max_attempts=4,
    ) == "failed_retryable"
    reconciled = state_db.get_delivery_job(job.job_id)
    assert reconciled["index_target_id"] == ""
    assert reconciled["index_document_id"] == ""
    assert reconciled["index_run_id"] == "active_pointer_control_unresolved"


def test_delivery_backend_quarantines_active_chunk_payload_identity_mismatch(tmp_path) -> None:
    first = _payload(
        idempotency_key="payload-identity-first",
        chunk_id="shared-chunk",
        body="Original active source chunk.",
    )
    conflicting = _payload(
        idempotency_key="payload-identity-conflict",
        chunk_id="shared-chunk",
        body="Conflicting content for the same chunk id.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, conflicting], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "payload-identity-first"))
    activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    documents_before = store.all_docs()
    evidence = backend.find_by_natural_key(
        "payload-identity-conflict",
        _job(state_db, "payload-identity-conflict").payload_hash,
    )

    outcome = DeliveryExecutor(
        state_db=state_db,
        backend=backend,
        lease_owner="payload-identity-worker",
    ).execute_once(_job(state_db, "payload-identity-conflict").job_id)

    job = state_db.get_row(
        "delivery_jobs", "idempotency_key", "payload-identity-conflict"
    )
    assert outcome == "quarantined"
    assert evidence is not None
    assert evidence.status == "payload_integrity_mismatch"
    assert evidence.run == "chunk_id_payload_mismatch"
    assert job is not None
    assert job["status"] == "quarantined"
    assert job["last_error_class"] == "delivery_payload_integrity_mismatch"
    assert job["index_run_id"] == "chunk_id_payload_mismatch"
    assert store.all_docs() == documents_before


def test_delivery_backend_quarantines_racing_active_chunk_payload_identity_mismatch(tmp_path) -> None:
    first = _payload(
        idempotency_key="racing-payload-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    incoming = _payload(
        idempotency_key="racing-payload-incoming",
        chunk_id="racing-chunk",
        body="Expected content for the raced chunk.",
    )
    conflicting_chunk = _orphan_chunk_document(
        chunk_id="racing-chunk",
        body="Conflicting content inserted by another writer.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, incoming], dry_run=False)
    store = _ConcurrentChunkPayloadCollisionStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "racing-payload-first"))
    active_before = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    store.inject_next_chunk_collision(conflicting_chunk)

    outcome = DeliveryExecutor(
        state_db=state_db,
        backend=backend,
        lease_owner="racing-payload-worker",
    ).execute_once(_job(state_db, "racing-payload-incoming").job_id)

    job = state_db.get_row("delivery_jobs", "idempotency_key", "racing-payload-incoming")
    assert outcome == "quarantined"
    assert job is not None
    assert job["status"] == "quarantined"
    assert job["last_error_class"] == "delivery_payload_integrity_mismatch"
    assert job["index_run_id"] == "chunk_id_payload_mismatch"
    assert store.get(conflicting_chunk["_id"])["body"] == conflicting_chunk["body"]
    assert resolve_active_source_revision(
        store=store, session_id_hash=SESSION_ID_HASH
    ).source_hash == active_before.source_hash


def test_delivery_reconciler_does_not_succeed_for_unactivated_active_pointer_orphan(tmp_path) -> None:
    first = _payload(
        idempotency_key="orphan-reconcile-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    second = _payload(
        idempotency_key="orphan-reconcile-second",
        chunk_id="second-chunk",
        body="Chunk left orphaned after activation CAS loss.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = _FailOncePointerCasStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "orphan-reconcile-first"))
    activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    store.fail_next_pointer_cas()
    second_job = _job(state_db, "orphan-reconcile-second")

    assert DeliveryExecutor(
        state_db=state_db,
        backend=backend,
        lease_owner="orphan-reconcile-worker",
    ).execute_once(second_job.job_id) == "replayable"
    assert state_db.record_failed_retryable_attempt(
        second_job.job_id,
        dataset_ref="couchdb:couchdb",
        document_ref=dm.session_doc_id(SESSION_ID_HASH),
        run="stale-couchdb-reference",
        max_attempts=4,
    ) == "failed_retryable"
    assert DeliveryReconciler(state_db=state_db, backend=backend).reconcile_once(
        second_job.job_id,
        max_attempts=4,
    ) == "failed_retryable"
    reconciled = state_db.get_delivery_job(second_job.job_id)
    assert reconciled["status"] == "failed_retryable"
    assert reconciled["index_target_id"] == ""
    assert reconciled["index_document_id"] == ""
    assert reconciled["index_run_id"] == "active_pointer_member_missing"


@pytest.mark.parametrize(
    ("field", "foreign_value"),
    [
        ("session_id_hash", dm.sha256_hash("foreign-session")),
        ("chunk_id", "foreign-chunk"),
        ("provider", "foreign-provider"),
        ("project", "foreign-project"),
        ("redaction_version", "redaction.foreign"),
        ("source_status", "foreign-source-status"),
    ],
)
def test_chunk_identity_match_requires_authoritative_scope_fields(
    field: str,
    foreign_value: str,
) -> None:
    expected = _orphan_chunk_document(
        chunk_id="identity-chunk",
        body="Same body must not erase source scope identity.",
    )
    foreign = dict(expected)
    foreign[field] = foreign_value

    assert not _chunk_documents_match(foreign, expected)


def test_delivery_executor_quarantines_foreign_scope_active_pointer_orphan(tmp_path) -> None:
    first = _payload(
        idempotency_key="foreign-orphan-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    incoming = _payload(
        idempotency_key="foreign-orphan-incoming",
        chunk_id="foreign-orphan-chunk",
        body="Same chunk body under a foreign semantic scope.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, incoming], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "foreign-orphan-first"))
    active_before = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    store.put(
        _orphan_chunk_document(
            chunk_id="foreign-orphan-chunk",
            body="Same chunk body under a foreign semantic scope.",
            provider="foreign-provider",
            project="foreign-project",
        )
    )

    outcome = DeliveryExecutor(
        state_db=state_db,
        backend=backend,
        lease_owner="foreign-orphan-worker",
    ).execute_once(_job(state_db, "foreign-orphan-incoming").job_id)

    job = state_db.get_row("delivery_jobs", "idempotency_key", "foreign-orphan-incoming")
    assert outcome == "quarantined"
    assert job is not None
    assert job["status"] == "quarantined"
    assert job["last_error_class"] == "delivery_payload_integrity_mismatch"
    assert job["index_run_id"] == "chunk_id_payload_mismatch"
    assert resolve_active_source_revision(
        store=store, session_id_hash=SESSION_ID_HASH
    ).source_hash == active_before.source_hash


def test_delivery_backend_reuses_exact_orphan_chunk_in_next_active_revision(tmp_path) -> None:
    first = _payload(
        idempotency_key="orphan-first",
        chunk_id="first-chunk",
        body="First active source chunk.",
    )
    second = _payload(
        idempotency_key="orphan-second",
        chunk_id="orphan-chunk",
        body="Existing orphan source chunk.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[first, second], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)
    backend.submit(_job(state_db, "orphan-first"))
    activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    pointer_before = _active_pointer(store)
    store.put(_orphan_chunk_document(chunk_id="orphan-chunk", body="Existing orphan source chunk."))

    backend.submit(_job(state_db, "orphan-second"))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert _active_pointer(store)["active_revision"] != pointer_before["active_revision"]
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "orphan-chunk",
    }


def test_retired_index_bridge_reuses_exact_orphan_chunk_in_next_active_revision() -> None:
    first = _payload(
        idempotency_key="bridge-orphan-first",
        chunk_id="first-chunk",
        body="First active bridge chunk.",
    )
    second = _payload(
        idempotency_key="bridge-orphan-second",
        chunk_id="orphan-chunk",
        body="Existing orphan bridge chunk.",
    )
    store = InMemoryCouchDBSourceStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)
    adapter.submit_document(document_from_ingress_payload(first))
    activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    pointer_before = _active_pointer(store)
    store.put(_orphan_chunk_document(chunk_id="orphan-chunk", body="Existing orphan bridge chunk."))

    adapter.submit_document(document_from_ingress_payload(second))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert _active_pointer(store)["active_revision"] != pointer_before["active_revision"]
    assert {document["chunk_id"] for document in resolved.conversation_chunks} == {
        "first-chunk",
        "orphan-chunk",
    }


def test_retired_index_bridge_rejects_invalid_active_pointer_before_new_chunk_write() -> None:
    first = _payload(
        idempotency_key="invalid-bridge-first",
        chunk_id="first-chunk",
        body="First active bridge chunk.",
    )
    second = _payload(
        idempotency_key="invalid-bridge-second",
        chunk_id="second-chunk",
        body="Second distinct bridge chunk.",
    )
    store = InMemoryCouchDBSourceStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)
    adapter.submit_document(document_from_ingress_payload(first))
    resolved = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert resolved.manifest_id is not None
    manifest = store.get(resolved.manifest_id)
    assert manifest is not None
    _force_delete_for_corrupt_active_revision_test(store, manifest["members"][0]["member_id"])
    documents_before = store.all_docs()

    with pytest.raises(SourceRevisionResolutionError):
        adapter.submit_document(document_from_ingress_payload(second))

    assert store.all_docs() == documents_before
    assert store.get(dm.conversation_chunk_doc_id(SESSION_ID_HASH, "second-chunk")) is None


def test_delivery_replay_of_corrective_current_chunk_is_idempotent(tmp_path) -> None:
    raw = _payload(
        idempotency_key="current-delivery",
        chunk_id="current-chunk",
        body="Corrective current source content.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[raw], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "current-delivery"))
    activated, current_chunk = _activate_corrective_current_source(
        store,
        chunk_id="current-chunk",
        body="Corrective current source content.",
    )
    _mark_projected(store, source_hash=activated.source_hash)
    pointer_before = _active_pointer(store)

    evidence = backend.submit(_job(state_db, "current-delivery"))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert evidence.status == "succeeded"
    assert _active_pointer(store) == pointer_before
    assert resolved.conversation_chunks == (store.get(current_chunk["_id"]),)
    assert (
        resolved.conversation_chunks[0]["supersedes_source_document_hash"]
        == dm.sha256_hash(dm.conversation_chunk_doc_id(SESSION_ID_HASH, "current-chunk"))
    )
    assert _select_sessions_needing_projection(store, limit=0) == []


def test_retired_bridge_replay_of_corrective_current_chunk_is_idempotent() -> None:
    raw = _payload(
        idempotency_key="current-bridge",
        chunk_id="current-chunk",
        body="Corrective current source content.",
    )
    store = InMemoryCouchDBSourceStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)

    adapter.submit_document(document_from_ingress_payload(raw))
    activated, current_chunk = _activate_corrective_current_source(
        store,
        chunk_id="current-chunk",
        body="Corrective current source content.",
    )
    _mark_projected(store, source_hash=activated.source_hash)
    pointer_before = _active_pointer(store)

    result = adapter.submit_document(document_from_ingress_payload(raw))

    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    assert result.status == "submitted"
    assert _active_pointer(store) == pointer_before
    assert resolved.conversation_chunks == (store.get(current_chunk["_id"]),)
    assert _select_sessions_needing_projection(store, limit=0) == []


def test_delivery_rejects_mismatched_corrective_current_chunk(tmp_path) -> None:
    raw = _payload(
        idempotency_key="current-delivery-mismatch",
        chunk_id="current-chunk",
        body="Original replay content.",
    )
    state_db = _state_db(tmp_path)
    apply_backfill_to_state_db(state_db=state_db, payloads=[raw], dry_run=False)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=state_db, store=store)

    backend.submit(_job(state_db, "current-delivery-mismatch"))
    _activate_corrective_current_source(
        store,
        chunk_id="current-chunk",
        body="Corrected content conflicts with replay.",
    )
    pointer_before = _active_pointer(store)

    evidence = backend.submit(_job(state_db, "current-delivery-mismatch"))

    assert evidence.status == "payload_integrity_mismatch"
    assert _active_pointer(store) == pointer_before


def test_retired_bridge_rejects_mismatched_corrective_current_chunk() -> None:
    raw = _payload(
        idempotency_key="current-bridge-mismatch",
        chunk_id="current-chunk",
        body="Original replay content.",
    )
    store = InMemoryCouchDBSourceStore()
    adapter = CouchDBRetiredIndexBridgeAdapter(store=store)

    adapter.submit_document(document_from_ingress_payload(raw))
    _activate_corrective_current_source(
        store,
        chunk_id="current-chunk",
        body="Corrected content conflicts with replay.",
    )
    pointer_before = _active_pointer(store)

    with pytest.raises(SourceStoreConflict):
        adapter.submit_document(document_from_ingress_payload(raw))

    assert _active_pointer(store) == pointer_before

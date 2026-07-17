"""Draft delete-lifecycle seam for the Qdrant searchable mirror.

These tests exercise the additive ``delete_document`` / ``delete_by_natural_key``
seam on :class:`QdrantDoclingMirrorAdapter` using the reusable in-memory fake
client. The seam is code-only and is NOT wired into any live GC/retirement route;
the tests assert the contract a future GC chokepoint would rely on (idempotent
delete, natural-key resolution, collection isolation).
"""

from __future__ import annotations

import pytest

from agent_knowledge.rag_ingress.retired_index_bridge import BackendDocumentHandle, IndexStatus
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    FOUNDATION_DIRECT_WRITE_CONTRACT,
    DEFAULT_COLLECTION_NAME,
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
    point_id_for_natural_key,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _adapter(client: InMemoryQdrantClient | None = None):
    client = client or InMemoryQdrantClient()
    client.create_collection(
        DEFAULT_COLLECTION_NAME,
        vectors_config={"size": 32, "distance": "Cosine"},
    )
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=32),
    )
    return adapter, client


def _doc(
    *,
    body: str = "Decision: the local ledger is the canonical authority; the mirror is searchable only.",
    profile: str = "transcript-memory",
):
    return build_rag_ready_document(
        target_profile=profile,
        document_kind="conversation_chunk",
        source_namespace="workspace-neurons",
        source_alias="sessions/example.md",
        privacy_class="private",
        body=body,
        filename="example.md",
        metadata={"project": "neurons", "provider": "claude"},
    )


def test_delete_document_removes_point_and_is_idempotent():
    adapter, client = _adapter()
    doc = _doc()
    result = adapter.submit_document(doc)
    handle = BackendDocumentHandle(dataset_ref=result.dataset_ref, document_ref=result.document_ref)

    assert adapter.document_status(handle) == IndexStatus.INDEXED
    assert client.point_count(DEFAULT_COLLECTION_NAME) == 1

    deletion = adapter.delete_document(handle)
    assert deletion.status == "deleted"
    assert deletion.existed is True
    assert client.point_count(DEFAULT_COLLECTION_NAME) == 0
    assert adapter.document_status(handle) == IndexStatus.UNKNOWN

    again = adapter.delete_document(handle)
    assert again.status == "absent"
    assert again.existed is False
    # public-safe serialization path
    serialized = deletion.to_dict()
    assert serialized["status"] == "deleted"
    assert serialized["existed"] is True
    assert "document_ref" in serialized


def test_delete_by_natural_key_resolves_and_removes():
    adapter, _ = _adapter()
    doc = _doc()
    adapter.submit_document(doc)

    assert (
        adapter.find_by_natural_key(
            target_profile=doc.target_profile,
            idempotency_key=doc.idempotency_key,
            payload_hash=doc.content_hash,
        )
        is not None
    )

    deletion = adapter.delete_by_natural_key(
        target_profile=doc.target_profile,
        idempotency_key=doc.idempotency_key,
        content_hash=doc.content_hash,
    )
    assert deletion.status == "deleted"
    assert (
        adapter.find_by_natural_key(
            target_profile=doc.target_profile,
            idempotency_key=doc.idempotency_key,
            payload_hash=doc.content_hash,
        )
        is None
    )


def test_delete_by_natural_key_absent_is_safe_noop():
    adapter, _ = _adapter()
    target_profile = "transcript-memory"
    idempotency_key = "workspace-neurons:conversation_chunk:sha256:deadbeefdeadbeef"
    content_hash = "sha256:deadbeefdeadbeef"

    deletion = adapter.delete_by_natural_key(
        target_profile=target_profile,
        idempotency_key=idempotency_key,
        content_hash=content_hash,
    )
    assert deletion.status == "absent"
    assert deletion.existed is False
    assert deletion.document_ref == point_id_for_natural_key(
        target_profile=target_profile,
        idempotency_key=idempotency_key,
        content_hash=content_hash,
    )


def test_delete_missing_ok_false_raises():
    adapter, _ = _adapter()
    with pytest.raises(ValueError):
        adapter.delete_by_natural_key(
            target_profile="transcript-memory",
            idempotency_key="workspace-neurons:conversation_chunk:sha256:absent",
            content_hash="sha256:absent",
            missing_ok=False,
        )


def test_delete_absent_missing_ok_does_not_use_write_transport():
    adapter, _ = _adapter()

    class FailOnDeleteTransport:
        def delete_points(self, **kwargs):
            raise AssertionError("absent delete must not reach the transport")

    adapter._write_transport = FailOnDeleteTransport()
    deletion = adapter.delete_document(
        BackendDocumentHandle(
            dataset_ref=f"qdrant:{DEFAULT_COLLECTION_NAME}",
            document_ref="confirmed-absent-point",
        )
    )

    assert deletion.status == "absent"
    assert deletion.existed is False


def test_delete_document_collection_mismatch_is_no_op():
    adapter, client = _adapter()
    result = adapter.submit_document(_doc())
    bad = BackendDocumentHandle(dataset_ref="qdrant:some_other_collection", document_ref=result.document_ref)

    deletion = adapter.delete_document(bad)
    assert deletion.status == "collection_mismatch"
    assert deletion.existed is False
    assert client.point_count(DEFAULT_COLLECTION_NAME) == 1


def test_query_reflects_deletion_with_reusable_fake_client():
    adapter, _ = _adapter()
    keep = _doc(body="alpha decision about ledger authority and recall")
    drop = _doc(body="beta note about searchable mirror parity gating")
    adapter.submit_document(keep)
    drop_result = adapter.submit_document(drop)

    hits = adapter.query_mirror_candidates("decision", target_profile="transcript-memory", limit=5)
    assert any(hit["content_hash"] == keep.content_hash for hit in hits)

    adapter.delete_document(
        BackendDocumentHandle(dataset_ref=drop_result.dataset_ref, document_ref=drop_result.document_ref)
    )
    hits_after = adapter.query_mirror_candidates("note", target_profile="transcript-memory", limit=5)
    assert all(hit["content_hash"] != drop.content_hash for hit in hits_after)

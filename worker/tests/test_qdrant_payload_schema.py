"""M2: top-level filter payload fields + payload index declaration.

RetiredIndexBridge-parity filter keys (result_type, project, provider, session_id_hash) are
promoted from the nested metadata dict to top-level payload, payload indexes are
declared at collection-create time, and multi-field filtering (notably
privacy_class) works through query_mirror_candidates.
"""

from __future__ import annotations

from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    FOUNDATION_DIRECT_WRITE_CONTRACT,
    DEFAULT_COLLECTION_NAME,
    PAYLOAD_INDEX_FIELDS,
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _adapter(client: InMemoryQdrantClient | None = None):
    client = client or InMemoryQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=32),
    )
    return adapter, client


def _doc(*, body: str, privacy: str = "private", result_type: str = "approved_memory_card", project: str = "neurons"):
    return build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/example.md",
        privacy_class=privacy,
        body=body,
        filename="example.md",
        metadata={"project": project, "provider": "claude", "session_id_hash": "abc123", "result_type": result_type},
    )


def test_adapter_construction_never_declares_payload_indexes():
    client = InMemoryQdrantClient()
    _adapter(client)
    declared = client.payload_indexes(DEFAULT_COLLECTION_NAME)
    assert declared == []


def test_adapter_close_closes_client_and_embedding_provider():
    events: list[str] = []

    class _Client(InMemoryQdrantClient):
        def close(self):
            events.append("client")

    class _Embedding:
        @property
        def size(self):
            return 32

        def embed(self, text: str):
            return [0.0] * 32

        def close(self):
            events.append("embedding")

    QdrantDoclingMirrorAdapter(
        client=_Client(),
        direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=_Embedding(),
    ).close()

    assert events == ["client", "embedding"]


def test_filter_fields_promoted_to_top_level_payload():
    client = InMemoryQdrantClient()
    adapter, _ = _adapter(client)
    doc = _doc(body="decision about ledger authority")
    result = adapter.submit_document(doc)
    points = client.retrieve(collection_name=DEFAULT_COLLECTION_NAME, ids=[result.document_ref], with_payload=True)
    payload = points[0]["payload"]
    assert payload["result_type"] == "approved_memory_card"
    assert payload["project"] == "neurons"
    assert payload["provider"] == "claude"
    assert payload["session_id_hash"] == "abc123"
    assert payload["privacy_class"] == "private"


def test_result_type_falls_back_to_document_kind_when_absent():
    client = InMemoryQdrantClient()
    adapter, _ = _adapter(client)
    doc = build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/x.md",
        privacy_class="private",
        body="no explicit result_type in metadata",
        filename="x.md",
        metadata={"project": "neurons"},
    )
    result = adapter.submit_document(doc)
    points = client.retrieve(collection_name=DEFAULT_COLLECTION_NAME, ids=[result.document_ref], with_payload=True)
    assert points[0]["payload"]["result_type"] == "approved_memory_card"


def test_query_filters_by_privacy_class():
    adapter, _ = _adapter()
    adapter.submit_document(_doc(body="alpha private decision", privacy="private"))
    adapter.submit_document(_doc(body="beta public decision", privacy="public"))

    private_hits = adapter.query_mirror_candidates(
        "decision", target_profile="derived-memory-items", filters={"privacy_class": "private"}, limit=5
    )
    assert private_hits
    assert all(hit["content_hash"] for hit in private_hits)
    # the public doc must never appear under a private filter
    public_doc = _doc(body="beta public decision", privacy="public")
    assert all(hit["content_hash"] != public_doc.content_hash for hit in private_hits)


def test_query_filter_shape_includes_privacy_class_clause():
    # Assert the SERVER-side query_filter (not just the client-side re-check) carries
    # the privacy_class condition, so a backend that honors the filter is scoped.
    client = InMemoryQdrantClient()
    adapter, _ = _adapter(client)
    adapter.submit_document(_doc(body="x decision", privacy="private"))
    adapter.query_mirror_candidates(
        "decision", target_profile="derived-memory-items", privacy_class="private", limit=5
    )
    query_filter = client.last_query_filter
    must = query_filter["must"] if isinstance(query_filter, dict) else query_filter.must
    keys = {}
    for condition in must:
        if isinstance(condition, dict):
            keys[condition["key"]] = condition["match"]["value"]
        else:
            keys[condition.key] = condition.match.value
    assert keys.get("privacy_class") == "private"
    assert keys.get("target_profile") == "derived-memory-items"


def test_fully_unscoped_query_is_refused():
    adapter, _ = _adapter()
    adapter.submit_document(_doc(body="anything"))
    import pytest

    with pytest.raises(ValueError):
        adapter.query_mirror_candidates("anything")  # no target_profile, no privacy_class, no filters


def test_filters_cannot_override_explicit_target_profile():
    adapter, _ = _adapter()
    adapter.submit_document(_doc(body="anything"))
    import pytest

    with pytest.raises(ValueError):
        adapter.query_mirror_candidates(
            "anything",
            target_profile="derived-memory-items",
            filters={"target_profile": "some-other-profile"},
        )


def test_query_filters_by_result_type_and_project():
    adapter, _ = _adapter()
    keep = _doc(body="gamma card about authority", result_type="approved_memory_card", project="neurons")
    other = _doc(body="delta snapshot about authority", result_type="project_context_snapshot", project="other")
    adapter.submit_document(keep)
    adapter.submit_document(other)

    hits = adapter.query_mirror_candidates(
        "authority",
        target_profile="derived-memory-items",
        filters={"result_type": "approved_memory_card", "project": "neurons"},
        limit=5,
    )
    assert any(hit["content_hash"] == keep.content_hash for hit in hits)
    assert all(hit["content_hash"] != other.content_hash for hit in hits)

"""M4: Qdrant hit ledger-join authority gate.

Mirror hits are never authoritative on their own. The join resolves each
candidate against canonical authority (content_hash -> knowledge_items),
flips resolved hits to authority_join_status='resolved', and drops/flags
unresolved ones -- never promoting an unresolved hit to authority.
"""

from __future__ import annotations

from agent_knowledge.rag_ingress.index_backend import IndexStatus
from agent_knowledge.rag_ingress.qdrant_authority_join import (
    LedgerContentHashAuthorityResolver,
    join_mirror_hits_to_authority,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


class _FakeLedger:
    """Duck-typed ledger exposing only get_by_content_hash."""

    def __init__(self, by_hash: dict[str, dict]):
        self._by_hash = by_hash

    def get_by_content_hash(self, content_hash: str):
        return self._by_hash.get(content_hash)


def _hit(content_hash: str, *, target_profile: str = "derived-memory-items") -> dict:
    return {
        "result_type": "searchable_mirror",
        "authority": "searchable_runtime_mirror",
        "target_profile": target_profile,
        "source_ref": "workspace:approved_memory_card:" + content_hash,
        "content_hash": content_hash,
        "summary": "candidate",
        "canonical_resolution_required": True,
        "authority_join_status": "not_checked",
    }


class _AllowResolver:
    def resolve(self, hit):
        return {"status": "active", "currentness": "current"}


class _DenyResolver:
    def resolve(self, hit):
        return None


def test_resolved_hit_is_flipped_to_authoritative():
    [joined] = join_mirror_hits_to_authority([_hit("sha256:a")], resolver=_AllowResolver())
    assert joined["authority_join_status"] == "resolved"
    assert joined["canonical_resolution_required"] is False
    assert joined["authority"] == "local_ledger"
    assert joined["authority_currentness"] == "current"


def test_unresolved_hit_is_dropped_by_default():
    out = join_mirror_hits_to_authority([_hit("sha256:a")], resolver=_DenyResolver())
    assert out == []


def test_unresolved_hit_kept_flagged_when_not_dropping():
    [flagged] = join_mirror_hits_to_authority(
        [_hit("sha256:a")], resolver=_DenyResolver(), drop_unresolved=False
    )
    assert flagged["authority_join_status"] == "unresolved"
    assert flagged["canonical_resolution_required"] is True


def test_ledger_resolver_requires_authorized_status():
    resolver = LedgerContentHashAuthorityResolver(
        _FakeLedger(
            {
                "sha256:active": {"status": "active"},
                "sha256:prepared": {"status": "prepared"},
            }
        )
    )
    assert resolver.resolve(_hit("sha256:active")) is not None
    # not yet indexed/active -> not authoritative
    assert resolver.resolve(_hit("sha256:prepared")) is None
    # absent content_hash -> None
    assert resolver.resolve(_hit("sha256:missing")) is None
    assert resolver.resolve(_hit("")) is None


def test_end_to_end_query_then_authority_join():
    client = InMemoryQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=32),
    )
    doc = build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/x.md",
        privacy_class="private",
        body="decision about ledger authority and recall",
        filename="x.md",
        metadata={"project": "neurons"},
    )
    result = adapter.submit_document(doc)
    assert result.status == IndexStatus.INDEXED

    hits = adapter.query_mirror_candidates("decision", target_profile="derived-memory-items", limit=5)
    assert hits and all(h["canonical_resolution_required"] for h in hits)

    ledger = _FakeLedger({doc.content_hash: {"status": "indexed", "currentness": "current"}})
    joined = join_mirror_hits_to_authority(hits, resolver=LedgerContentHashAuthorityResolver(ledger))
    assert joined
    assert all(h["authority_join_status"] == "resolved" for h in joined)
    assert all(h["canonical_resolution_required"] is False for h in joined)

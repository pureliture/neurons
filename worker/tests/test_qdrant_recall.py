"""M8 Qdrant-backed brain.query recall: archive/evidence lanes filled from the
Qdrant mirror (authority-joined via CouchDB projection-state), project-scoped. Pure
compute -- in-memory CouchDB + Qdrant fakes, no network.
"""

from __future__ import annotations

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_and_project,
    materialize_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import store_tool_evidence_bundles
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
    ToolEvidenceSummaryRecord,
)
from agent_knowledge.session_memory.brain_query import run_brain_query_v2
from agent_knowledge.rag_ingress.qdrant_backfill import backfill_session_memory
from agent_knowledge.rag_ingress.qdrant_recall import build_qdrant_brain_query_search
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient

VECTOR_SIZE = 64


def _sid(raw="r1", provider="codex"):
    return dm.build_session_id_hash(provider, raw)


def _seed_projected(store, sid, *, project="neurons", chunk_texts=("alpha apple distinctive token", "beta gamma topic body")):
    session = TranscriptSession(session_id_hash=sid, provider="codex", project=project, started_at="2026-06-17T01:00:00Z")
    store.put(dm.build_transcript_session_document(session=session))
    conv = []
    for i, t in enumerate(chunk_texts):
        ch = TranscriptChunk.from_text(chunk_id=f"c{i}", session_id_hash=sid, provider="codex", project=project, turn_start_index=i, turn_end_index=i, text=t)
        d = dm.build_conversation_chunk_document(chunk=ch)
        store.put(d)
        conv.append(d["content_hash"])
    store.put(dm.build_coverage_manifest_document(session_id_hash=sid, provider="codex", project=project, conversation_chunk_count=len(chunk_texts), tool_evidence_bundle_count=0, conversation_content_hashes=conv, tool_evidence_coverage_hashes=[]))
    store_tool_evidence_bundles([ToolEvidenceSummaryRecord(session_id_hash=sid, provider="codex", project=project, category="test_result", outcome="pass", tool_name="bash", command_summary="x", redacted_summary="12 passed", evidence_index=0)], store=store)

    class _P:
        def project(self, *, target_profile, document):
            return "mem_" + str(document.get("content_hash", "")).split(":")[-1][:12]

    materialize_and_project(session_id_hash=sid, store=store, projector=_P())


def _adapter(client):
    return QdrantDoclingMirrorAdapter(client=client, collection_name="recall_mirror", normalizer=PassthroughMarkdownNormalizer(), embedding_provider=HashEmbeddingProvider(size=VECTOR_SIZE))


def _world():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected(store, sid)
    client = InMemoryQdrantClient()
    adapter = _adapter(client)
    backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    return store, adapter, mat


def test_qdrant_recall_returns_brain_query_item_shape():
    store, adapter, mat = _world()
    search = build_qdrant_brain_query_search(adapter=adapter, store=store)
    results = search(mat.body, "/project/neurons")
    assert results
    item = results[0]
    assert item["content_hash"] == mat.content_hash
    assert item["result_type"] == "session_memory"
    assert item["card_type"] == ""  # -> archive lane
    assert item["currentness"] == "current"
    assert item["memory_id"]  # populated from the mirror payload


def test_qdrant_recall_project_filter_drops_other_project():
    store, adapter, mat = _world()
    search = build_qdrant_brain_query_search(adapter=adapter, store=store)
    # wrong project -> authority resolver drops every hit
    assert search(mat.body, "/project/other-project") == []


def test_brain_query_archive_lane_filled_by_qdrant_recall():
    store, adapter, mat = _world()
    search = build_qdrant_brain_query_search(adapter=adapter, store=store)

    class _ReadModel:
        def get_card_meta(self, card_id):
            return None

        def list_recent_cards(self, *, project, limit):
            return []

        def list_project_card_counts(self):
            return []

    resp = run_brain_query_v2(
        read_model=_ReadModel(), brain_id="/project/neurons", query=mat.body, index_search=search
    )
    assert resp["audit"]["index_bound"] is True
    # the session-memory hit lands in the archive lane (no ledger card to dedup against)
    assert any(it.get("content_hash") == mat.content_hash for it in resp["archive"])

"""Parity-soak runner (CouchDB-native): non-empty primary + backfilled mirror
passes; a mirror hit that does not authority-join fails; empty/partial primary is
REJECTED (vacuous-recall guard), not a green pass. Pure compute -- in-memory CouchDB
+ Qdrant fakes, no network.
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
from agent_knowledge.rag_ingress.qdrant_backfill import backfill_session_memory
from agent_knowledge.rag_ingress.qdrant_backfill_parity import (
    PARITY_SCHEMA,
    build_authority_joined_mirror_fetch,
    run_parity_soak,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient

VECTOR_SIZE = 64


# --------------------------------------------------------------- CouchDB seeding

def _sid(provider="codex", raw="sess-1") -> str:
    return dm.build_session_id_hash(provider, raw)


def _record(sid, index, *, summary="12 passed", provider="codex", project="neurons"):
    return ToolEvidenceSummaryRecord(
        session_id_hash=sid,
        provider=provider,
        project=project,
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="uv run pytest -q",
        redacted_summary=summary,
        evidence_index=index,
    )


def _seed_session(store, sid, *, provider="codex", project="neurons", chunk_texts=("user asked", "assistant answered")):
    session = TranscriptSession(
        session_id_hash=sid, provider=provider, project=project, started_at="2026-06-17T01:00:00Z"
    )
    store.put(dm.build_transcript_session_document(session=session))
    conv_hashes = []
    for i, text in enumerate(chunk_texts):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{i:02d}",
            session_id_hash=sid,
            provider=provider,
            project=project,
            turn_start_index=i,
            turn_end_index=i,
            text=text,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        store.put(doc)
        conv_hashes.append(doc["content_hash"])
    cov = dm.build_coverage_manifest_document(
        session_id_hash=sid,
        provider=provider,
        project=project,
        conversation_chunk_count=len(chunk_texts),
        tool_evidence_bundle_count=0,
        conversation_content_hashes=conv_hashes,
        tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False, "eligible_for_retirement": True},
    )
    store.put(cov)


class _Projector:
    def project(self, *, target_profile, document):
        return "mem_" + str(document.get("content_hash", "")).split(":")[-1][:12]


def _seed_projected_session(store, sid, **kw):
    _seed_session(store, sid, **kw)
    store_tool_evidence_bundles([_record(sid, 0)], store=store)
    materialize_and_project(session_id_hash=sid, store=store, projector=_Projector())


def _adapter(client, *, collection="parity_mirror"):
    return QdrantDoclingMirrorAdapter(
        client=client,
        collection_name=collection,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=VECTOR_SIZE),
    )


def _build_world():
    """Two PROJECTED session-memories backfilled into the Qdrant mirror."""
    store = InMemoryCouchDBSourceStore()
    sid_a = _sid(raw="alpha")
    sid_b = _sid(raw="beta")
    _seed_projected_session(store, sid_a, chunk_texts=("alpha apple", "alpha topic one"))
    _seed_projected_session(store, sid_b, chunk_texts=("beta banana", "beta topic two"))
    client = InMemoryQdrantClient()
    adapter = _adapter(client)
    backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    ch_a = materialize_session_memory(session_id_hash=sid_a, store=store).content_hash
    ch_b = materialize_session_memory(session_id_hash=sid_b, store=store).content_hash
    return store, adapter, ch_a, ch_b


def _mirror_fetch_for(store, adapter):
    return build_authority_joined_mirror_fetch(
        mirror_query=lambda q: adapter.query_mirror_candidates(
            q, target_profile="session-memory", limit=10
        ),
        store=store,
    )


def test_nonempty_primary_backfilled_mirror_passes():
    store, adapter, ch_a, ch_b = _build_world()

    # primary = authoritative RetiredIndexBridge recall (authority-joined hits with content_hash)
    primary_for = {
        "alpha apple": [{"content_hash": ch_a, "canonical_resolution_required": False}],
        "beta banana": [{"content_hash": ch_b, "canonical_resolution_required": False}],
    }

    result = run_parity_soak(
        ["alpha apple", "beta banana"],
        primary_fetch=lambda q: primary_for.get(q, []),
        mirror_fetch=_mirror_fetch_for(store, adapter),
        k=10,
        min_mean_recall_at_k=0.95,
    )
    assert result.schema_version == PARITY_SCHEMA
    assert result.rejected is False
    assert result.passed is True
    assert result.report["mismatch_count"] == 0


def test_unresolvable_mirror_hit_fails_parity():
    store, adapter, ch_a, _ = _build_world()
    primary_for = {"alpha apple": [{"content_hash": ch_a, "canonical_resolution_required": False}]}

    # mirror returns a hit for a session whose content_hash is NOT the projected one,
    # so the CouchDB authority-join drops it -> mirror has 0 hits -> mismatch.
    def mirror_query(query):
        return [
            {
                "session_id_hash": _sid(raw="alpha"),
                "content_hash": "sha256:" + "f" * 64,
                "canonical_resolution_required": True,
            }
        ]

    mirror_fetch = build_authority_joined_mirror_fetch(mirror_query=mirror_query, store=store)
    result = run_parity_soak(
        ["alpha apple"],
        primary_fetch=lambda q: primary_for.get(q, []),
        mirror_fetch=mirror_fetch,
        k=10,
        min_mean_recall_at_k=0.95,
    )
    assert result.rejected is False  # primary is non-empty
    assert result.passed is False
    assert result.report["mismatch_count"] == 1


def test_empty_primary_is_rejected_not_green():
    store, adapter, _, _ = _build_world()
    result = run_parity_soak(
        ["query one", "query two"],
        primary_fetch=lambda q: [],
        mirror_fetch=_mirror_fetch_for(store, adapter),
        k=10,
        min_mean_recall_at_k=0.95,
        min_nonempty_fraction=0.5,
    )
    assert result.rejected is True
    assert result.passed is False
    assert result.rejected_reason == "insufficient_primary_coverage"
    assert result.nonempty_primary_count == 0


def test_partial_primary_below_floor_is_rejected():
    store, adapter, ch_a, _ = _build_world()
    primary_for = {"q1": [{"content_hash": ch_a, "canonical_resolution_required": False}]}
    result = run_parity_soak(
        ["q1", "q2", "q3", "q4"],
        primary_fetch=lambda q: primary_for.get(q, []),
        mirror_fetch=_mirror_fetch_for(store, adapter),
        k=10,
        min_mean_recall_at_k=0.95,
        min_nonempty_fraction=0.5,
    )
    assert result.rejected is True
    assert result.rejected_reason == "insufficient_primary_coverage"


def test_empty_cohort_is_rejected():
    result = run_parity_soak(
        [], primary_fetch=lambda q: [], mirror_fetch=lambda q: [], k=10, min_mean_recall_at_k=0.95
    )
    assert result.rejected is True
    assert result.passed is False

"""CouchDB-native Qdrant session-memory backfill core.

Source/authority = CouchDB projection_state (PROJECTED), NOT the ledger. Covers:
content_hash verbatim (== materialized.content_hash), authority-join via the
CouchDB projection-state resolver, enumeration of only PROJECTED sessions,
read-only materialize (no store.put during backfill), idempotency (incl. across
the forward sink), dry-run, mirror-only, reversibility, and the embedding-dim
guard. Pure compute -- in-memory fakes only, no network.
"""

from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_and_project,
    materialize_session_memory,
    project_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import store_tool_evidence_bundles
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
    ToolEvidenceSummaryRecord,
)
from agent_knowledge.rag_ingress.qdrant_authority_join import join_mirror_hits_to_authority
from agent_knowledge.rag_ingress.qdrant_couchdb_authority import (
    CouchDBProjectionStateAuthorityResolver,
)
from agent_knowledge.rag_ingress.qdrant_backfill import (
    BACKFILL_SCHEMA,
    SESSION_MEMORY_PRIVACY_CLASS,
    EmbeddingDimMismatch,
    QdrantSessionMemoryMirrorSink,
    backfill_session_memory,
    build_session_memory_mirror_document,
    iter_projected_session_memories,
    rollback_submitted,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
    point_id_for_natural_key,
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
    chunk_docs = []
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
        chunk_docs.append(doc)
        conv_hashes.append(doc["content_hash"])
    cov = dm.build_coverage_manifest_document(
        session_id_hash=sid,
        provider=provider,
        project=project,
        conversation_chunk_count=len(chunk_texts),
        tool_evidence_bundle_count=0,
        conversation_content_hashes=conv_hashes,
        tool_evidence_coverage_hashes=[],
        conversation_revision_tokens=[
            dm.build_source_revision_token(doc, material_hash_field="content_hash")
            for doc in chunk_docs
        ],
        observed_at_start=session.started_at,
        observed_at_end=session.started_at,
        project_authority={"project": project, "ambiguous": False, "eligible_for_retirement": True},
    )
    store.put(cov)


def _seed_projected_session(store, sid, **kw):
    """Seed a session and run it through projection so projection_state=PROJECTED."""
    _seed_session(store, sid, **kw)
    store_tool_evidence_bundles([_record(sid, 0)], store=store)

    class _Projector:
        def project(self, *, target_profile, document):
            return "mem_" + str(document.get("content_hash", "")).split(":")[-1][:12]

    materialize_and_project(session_id_hash=sid, store=store, projector=_Projector())
    return store.get(dm.projection_state_doc_id(sid))


def _adapter(client=None, *, collection="test_mirror", size=VECTOR_SIZE):
    client = client or InMemoryQdrantClient()
    return QdrantDoclingMirrorAdapter(
        client=client,
        collection_name=collection,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=size),
    )


# ----------------------------------------- 0. active_content_hash on projection_state

def test_active_content_hash_populated_on_projected():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    state = _seed_projected_session(store, sid)
    assert state["projection_status"] == dm.ProjectionStatus.PROJECTED
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    assert state["active_content_hash"] == mat.content_hash
    assert state["active_content_hash"]  # non-empty


def test_active_content_hash_empty_on_failure_path():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    # coverage claims 2 chunks but only 1 stored -> materialization loss -> FAILED
    _seed_session(store, sid, chunk_texts=("only one",))
    cov = dm.build_coverage_manifest_document(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        conversation_chunk_count=2,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=["sha256:" + "a" * 64, "sha256:" + "b" * 64],
        tool_evidence_coverage_hashes=[],
    )
    store.put(cov)
    mat = materialize_session_memory(session_id_hash=sid, store=store)

    class _Projector:
        def project(self, *, target_profile, document):
            return "ref"

    project_session_memory(materialized=mat, store=store, projector=_Projector())
    state = store.get(dm.projection_state_doc_id(sid))
    assert state["projection_status"] == dm.ProjectionStatus.FAILED
    assert state.get("active_content_hash", "") == ""


# --------------------------------------------- 1. content_hash verbatim (materialized)

def test_mask_and_store_makes_body_public_safe_keeps_content_hash():
    from agent_knowledge.public_safe_util import ensure_public_safe
    from agent_knowledge.rag_ingress.server_runtime import public_ingress_leak_violations
    ch = "sha256:" + "a" * 64
    body = (
        "ran https://user:pass@host/x and ~/notes then regex \\bword and "
        "raw_transcript C:\\tmp RETIRED_INDEX_BRIDGE_API_KEY=zzz Bearer abc.def end"
    )
    doc = build_session_memory_mirror_document(
        session_id_hash="sha256:" + "0" * 64, provider="codex", project="neurons",
        content_hash=ch, body=body,
    )
    # content_hash stays the verbatim authority key (NOT recomputed from masked body)
    assert doc.content_hash == ch
    # masked body passes BOTH mirror guards: the fail-closed pre-check (704) AND
    # ensure_public_safe (706); and concrete leak tails are gone.
    assert public_ingress_leak_violations(doc.body) == []
    ensure_public_safe(doc.body, "b")  # must not raise
    for leak in ("user:pass", "raw_transcript", "abc.def", "/notes"):
        assert leak not in doc.body


def test_content_hash_is_materialized_value_verbatim():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    mat = materialize_session_memory(session_id_hash=sid, store=store)

    document = build_session_memory_mirror_document(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        content_hash=mat.content_hash,
        body=mat.body,
    )
    assert document.content_hash == mat.content_hash
    assert document.privacy_class == SESSION_MEMORY_PRIVACY_CLASS == "private"
    assert document.idempotency_key == f"codex:session_memory:{mat.content_hash}"


# --------------------------------------- 2. authority-join via CouchDB resolver

def test_backfilled_point_authority_joins_via_couchdb_resolver():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    client = InMemoryQdrantClient()
    adapter = _adapter(client)

    backfill_session_memory(store=store, adapter=adapter, dry_run=False)

    mat = materialize_session_memory(session_id_hash=sid, store=store)
    # query using a token from the body so the hash-embedding scores it
    hits = adapter.query_mirror_candidates("session-memory", target_profile="session-memory", limit=10)
    assert hits
    assert hits[0]["session_id_hash"] == sid
    assert hits[0]["content_hash"] == mat.content_hash

    resolved = join_mirror_hits_to_authority(
        hits, resolver=CouchDBProjectionStateAuthorityResolver(store), drop_unresolved=True
    )
    assert resolved
    assert resolved[0]["authority_join_status"] == "resolved"
    assert resolved[0]["content_hash"] == mat.content_hash


def test_resolver_drops_not_projected():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    # flip the state to FAILED -> must not resolve
    failed = dm.build_projection_state_document(
        session_id_hash=sid, provider="codex", project="neurons",
        projection_status=dm.ProjectionStatus.FAILED,
    )
    store.put(failed)
    hit = {"session_id_hash": sid, "content_hash": mat.content_hash}
    assert CouchDBProjectionStateAuthorityResolver(store).resolve(hit) is None


def test_resolver_drops_content_hash_mismatch():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    hit = {"session_id_hash": sid, "content_hash": "sha256:" + "f" * 64}  # wrong hash
    assert CouchDBProjectionStateAuthorityResolver(store).resolve(hit) is None


def test_resolver_drops_missing_projection_state():
    store = InMemoryCouchDBSourceStore()
    hit = {"session_id_hash": _sid(provider="ghost", raw="none"), "content_hash": "sha256:" + "1" * 64}
    assert CouchDBProjectionStateAuthorityResolver(store).resolve(hit) is None


def test_resolver_legacy_doc_without_active_content_hash_fails_closed():
    store = InMemoryCouchDBSourceStore()
    sid = _sid(raw="legacy")
    legacy = dm.build_projection_state_document(
        session_id_hash=sid, provider="codex", project="neurons",
        projection_status=dm.ProjectionStatus.PROJECTED,
        active_content_hash="sha256:" + "f" * 64,
    )
    legacy["active_content_hash"] = ""  # legacy: field empty on already-stored docs
    assert str(legacy.get("active_content_hash", "")) == ""
    store.put(legacy)
    record = CouchDBProjectionStateAuthorityResolver(store).resolve(
        {"session_id_hash": sid, "content_hash": "sha256:" + "c" * 64}
    )
    assert record is None


def test_resolver_returns_no_privacy_level_key():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    record = CouchDBProjectionStateAuthorityResolver(store).resolve(
        {"session_id_hash": sid, "content_hash": mat.content_hash}
    )
    assert record is not None
    assert "privacy_level" not in record  # privacy not a CouchDB-source concept
    assert record["provider"] == "codex"


# ------------------------------------------- 3. enumerate only PROJECTED sessions

def test_iter_enumerates_only_projected_sessions():
    store = InMemoryCouchDBSourceStore()
    sid_ok = _sid(raw="ok")
    sid_failed = _sid(raw="failed")
    _seed_projected_session(store, sid_ok)
    # a FAILED projection_state must be excluded
    store.put(
        dm.build_projection_state_document(
            session_id_hash=sid_failed, provider="codex", project="neurons",
            projection_status=dm.ProjectionStatus.FAILED,
        )
    )
    enumerated = list(iter_projected_session_memories(store))
    sids = {d["session_id_hash"] for d in enumerated}
    assert sid_ok in sids
    assert sid_failed not in sids
    assert len(enumerated) == 1


# ------------------------------------- 4. read-only materialize during backfill

def test_backfill_does_not_write_store():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    before = {d["_id"]: d.get("_rev") for d in store.all_docs()}

    client = InMemoryQdrantClient()
    backfill_session_memory(store=store, adapter=_adapter(client), dry_run=False)

    after = {d["_id"]: d.get("_rev") for d in store.all_docs()}
    assert before == after  # no doc created or revised by the backfill


def test_backfill_content_hash_matches_materialized():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    client = InMemoryQdrantClient()
    report = backfill_session_memory(store=store, adapter=_adapter(client), dry_run=False)
    assert report.submitted_count == 1
    assert report.submitted[0]["content_hash"] == mat.content_hash


# ------------------------------------------------- 5. idempotency (and forward parity)

def test_backfill_twice_does_not_duplicate_points():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    client = InMemoryQdrantClient()
    adapter = _adapter(client)

    backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    assert client.point_count("test_mirror") == 1


def test_qdrant_projector_canonical_write_no_retired_index_bridge():
    # C: builder projects session-memory straight to Qdrant (retired-index-bridge-free), records
    # PROJECTED + active_content_hash, returns a qdrant_sm ref; a later backfill of
    # the same session is idempotent (same point_id).
    from agent_knowledge.rag_ingress.qdrant_backfill import (
        QdrantSessionMemoryMirrorSink,
        QdrantSessionMemoryProjector,
    )
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_session(store, sid)
    store_tool_evidence_bundles([_record(sid, 0)], store=store)
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    client = InMemoryQdrantClient()
    adapter = _adapter(client)
    projector = QdrantSessionMemoryProjector(QdrantSessionMemoryMirrorSink(adapter))

    result = project_session_memory(materialized=mat, store=store, projector=projector)
    assert result["status"] == "projected"
    assert result["ref"].startswith("qdrant_sm:")
    assert client.point_count("test_mirror") == 1
    state = store.get(dm.projection_state_doc_id(sid))
    assert state["projection_status"] == dm.ProjectionStatus.PROJECTED
    assert state["active_content_hash"] == mat.content_hash

    backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    assert client.point_count("test_mirror") == 1  # idempotent, same point


def test_qdrant_projector_failure_marks_projection_failed():
    # Qdrant is the CANONICAL target here: a submit failure must PROPAGATE so the
    # projection is recorded FAILED (retried), never a false PROJECTED.
    from agent_knowledge.rag_ingress.qdrant_backfill import QdrantSessionMemoryProjector
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_session(store, sid)
    store_tool_evidence_bundles([_record(sid, 0)], store=store)
    mat = materialize_session_memory(session_id_hash=sid, store=store)

    class _BoomSink:
        def submit(self, **kwargs):
            raise RuntimeError("qdrant down")

    result = project_session_memory(
        materialized=mat, store=store, projector=QdrantSessionMemoryProjector(_BoomSink())
    )
    assert result["status"] == dm.ProjectionStatus.FAILED
    state = store.get(dm.projection_state_doc_id(sid))
    assert state["projection_status"] == dm.ProjectionStatus.FAILED
    assert state.get("active_content_hash", "") == ""


def test_forward_and_backfill_produce_same_point_id():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_session(store, sid)
    store_tool_evidence_bundles([_record(sid, 0)], store=store)
    client = InMemoryQdrantClient()
    adapter = _adapter(client)

    # forward path: project with the mirror sink attached
    sink = QdrantSessionMemoryMirrorSink(adapter)

    class _Projector:
        def project(self, *, target_profile, document):
            return "mem_fwd"

    materialize_and_project(session_id_hash=sid, store=store, projector=_Projector(), mirror_sink=sink)
    forward_ids = set(client._collections["test_mirror"].keys())
    assert len(forward_ids) == 1

    # backfill path on the same projected session -> same point id (idempotent)
    backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    after_ids = set(client._collections["test_mirror"].keys())
    assert after_ids == forward_ids  # no new point; identical deterministic id

    # and that id is exactly the natural-key point id for the materialized hash
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    expected = point_id_for_natural_key(
        target_profile="session-memory",
        idempotency_key=f"codex:session_memory:{mat.content_hash}",
        content_hash=mat.content_hash,
    )
    assert forward_ids == {expected}


# ------------------------------------------------------------------ 6. dry-run

def test_dry_run_writes_nothing_but_reports_plan():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    client = InMemoryQdrantClient()
    report = backfill_session_memory(store=store, adapter=_adapter(client), dry_run=True)
    assert client.point_count("test_mirror") == 0
    assert report.dry_run is True
    assert report.submitted_count == 1
    assert report.schema_version == BACKFILL_SCHEMA


# ----------------------------------------------------------------- 7. mirror-only

def test_mirror_only_no_store_write_no_dual_write():
    import agent_knowledge.rag_ingress.qdrant_backfill as backfill_mod

    assert not hasattr(backfill_mod, "MirrorDualWriteBackend")
    src = open(backfill_mod.__file__, encoding="utf-8").read()
    assert "MirrorDualWriteBackend" not in src
    # no canonical-write verbs: never put/delete CouchDB, never call RetiredIndexBridge retrieve
    for forbidden in ("store.put(", "store.delete(", ".retrieve(", "MirrorDualWriteBackend("):
        assert forbidden not in src


# -------------------------------------------------------------- 8. reversibility

def test_backfill_then_rollback_leaves_zero_points():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    client = InMemoryQdrantClient()
    adapter = _adapter(client)

    report = backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    assert client.point_count("test_mirror") == 1
    rollback = rollback_submitted(adapter=adapter, submitted=report.submitted)
    assert client.point_count("test_mirror") == 0
    assert rollback.deleted_count == 1


def test_rollback_absent_point_is_status_absent_not_raise():
    client = InMemoryQdrantClient()
    adapter = _adapter(client)
    ch = "sha256:" + "9" * 64
    triple = {
        "target_profile": "session-memory",
        "idempotency_key": f"codex:session_memory:{ch}",
        "content_hash": ch,
    }
    report = rollback_submitted(adapter=adapter, submitted=[triple])
    assert report.deleted_count == 0
    assert report.absent_count == 1
    assert report.statuses[0]["status"] == "absent"


# ----------------------------------------------------------- 9. embedding-dim guard

def test_embedding_dim_mismatch_raises_before_write():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    client = InMemoryQdrantClient()
    client.create_collection("dim_mirror", vectors_config={"size": 128, "distance": "Cosine"})
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="dim_mirror",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=64),
        ensure_collection=False,
    )
    assert adapter.embedding_size == 64
    assert adapter.collection_vector_size() == 128
    with pytest.raises(EmbeddingDimMismatch):
        backfill_session_memory(store=store, adapter=adapter, dry_run=False)
    assert client.point_count("dim_mirror") == 0


# ---------------------------------------------------------------- limit / resume

def test_limit_caps_submitted_points():
    store = InMemoryCouchDBSourceStore()
    for i in range(3):
        _seed_projected_session(store, _sid(raw=f"s{i}"))
    client = InMemoryQdrantClient()
    report = backfill_session_memory(store=store, adapter=_adapter(client), dry_run=False, limit=2)
    assert report.submitted_count == 2
    assert report.candidate_count == 3  # full corpus still counted
    assert client.point_count("test_mirror") == 2


def test_resume_skips_already_submitted():
    store = InMemoryCouchDBSourceStore()
    sid = _sid()
    _seed_projected_session(store, sid)
    mat = materialize_session_memory(session_id_hash=sid, store=store)
    client = InMemoryQdrantClient()
    report = backfill_session_memory(
        store=store, adapter=_adapter(client), dry_run=False, already_submitted={mat.content_hash}
    )
    assert report.submitted_count == 0
    assert client.point_count("test_mirror") == 0

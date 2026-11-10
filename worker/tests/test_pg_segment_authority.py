import json

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.rag_ingress.pg_representation import (
    build_local_segment_receipt_v4,
)
from agent_knowledge.rag_ingress.couchdb_authority import (
    CouchDBProjectionStateAuthorityResolver,
)
from test_couchdb_build_cli import _build_synthetic_session


def test_segment_authority_allows_receipt_ids_and_rejects_out_of_set_ids():
    source = InMemoryCouchDBSourceStore()
    session_id_hash = _build_synthetic_session(
        source,
        provider="synthetic-provider",
        project="synthetic-project",
        raw_id="segment-authority",
    )
    materialized = materialize_session_memory(
        session_id_hash=session_id_hash,
        store=source,
    )
    segment_ids = ["seg_" + "a" * 60, "seg_" + "b" * 60]
    receipt = build_local_segment_receipt_v4(
        project=materialized.project,
        provider=materialized.provider,
        session_id_hash=materialized.session_id_hash,
        active_content_hash=materialized.content_hash,
        projected_source_hash=materialized.source_hash,
        representation_content_hash=materialized.content_hash,
        session_memory_knowledge_id=segment_ids[0],
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        segment_ids=segment_ids,
        segment_content_hashes=[
            sha256_hash("synthetic segment body"),
            sha256_hash("synthetic segment body 2"),
        ],
        segment_set_digest=sha256_hash(
            json.dumps(segment_ids, ensure_ascii=False, separators=(",", ":"))
        ),
    )
    receipt.update(
        projection_status="projected",
        materialized_at="2026-01-01T00:00:00+00:00",
        failure_reason="",
    )
    state = dm.build_projection_state_document(
        session_id_hash=session_id_hash,
        provider=materialized.provider,
        project=materialized.project,
        projection_status=dm.ProjectionStatus.PROJECTED,
        session_memory_knowledge_id=segment_ids[0],
        active_content_hash=materialized.content_hash,
    )
    state["source_hash"] = materialized.source_hash
    state["projected_source_hash"] = materialized.source_hash
    state["backend_receipts"] = {
        "postgres_pgvector_local": {
            LOCAL_EMBEDDING_PROFILE_ID: receipt,
        }
    }
    source.put(state)
    authority = CouchDBProjectionStateAuthorityResolver(
        source,
        backend="postgres_pgvector_local",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    allowed = authority.resolve(
        {
            "session_id_hash": session_id_hash,
            "memory_id": segment_ids[1],
            "content_hash": sha256_hash("synthetic segment body 2"),
            "provider": materialized.provider,
            "project": materialized.project,
        }
    )
    session_receipt = authority.resolve_session_segments(
        {
            "session_id_hash": session_id_hash,
            "provider": materialized.provider,
            "project": materialized.project,
        }
    )
    old_revision = authority.resolve(
        {
            "session_id_hash": session_id_hash,
            "memory_id": "seg_" + "c" * 60,
            "content_hash": sha256_hash("old revision"),
            "provider": materialized.provider,
            "project": materialized.project,
        }
    )
    mismatched_content = authority.resolve(
        {
            "session_id_hash": session_id_hash,
            "memory_id": segment_ids[1],
            "content_hash": sha256_hash("different segment body"),
            "provider": materialized.provider,
            "project": materialized.project,
        }
    )
    other_session = authority.resolve(
        {
            "session_id_hash": sha256_hash("other-session"),
            "memory_id": segment_ids[1],
            "content_hash": sha256_hash("other session"),
            "provider": materialized.provider,
            "project": materialized.project,
        }
    )

    assert allowed is not None
    assert allowed["representation"] == "segment_v1"
    assert allowed["segment_ids"] == segment_ids
    assert session_receipt is not None
    assert session_receipt["segment_ids"] == segment_ids
    assert session_receipt["segment_content_hashes"] == receipt["segment_content_hashes"]
    assert old_revision is None
    assert mismatched_content is None
    assert other_session is None

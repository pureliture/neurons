"""Local A/B receipt tracer: real isolated SQL, synthetic source/embedding only."""
from dataclasses import replace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.model_connectors import DEFAULT_EMBEDDING_PROFILE_ID
from agent_knowledge.rag_ingress import pg_backfill
from agent_knowledge.rag_ingress.pg_recall import build_pg_brain_query_search_from_env
from agent_knowledge.rag_ingress.qdrant_backfill import public_safe_mask_body
from test_couchdb_build_cli import _build_synthetic_session
from test_pg_backfill import CountingEmbedProvider, SyntheticEmbedProvider


@pytest.fixture
def representation_lane(isolated_pg_store, monkeypatch):
    couch = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(couch, provider="codex", project="representation-test", raw_id="masked-session")
    chunk = next(d for d in couch.find_by_session(session_id_hash=sid) if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " synthetic raw_transcript terminology"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    couch.put(chunk)
    embed = CountingEmbedProvider()
    monkeypatch.setattr("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore", lambda **kw: couch)
    monkeypatch.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: SyntheticEmbedProvider())
    search = build_pg_brain_query_search_from_env({"NEURON_LBRAIN_PGVECTOR_DSN": isolated_pg_store.dsn, "COUCHDB_URL": "http://synthetic.invalid"})
    return dict(sql=isolated_pg_store, couch=couch, sid=sid, embed=embed, query=lambda: search("session context", "/project/representation-test"))


def materialize(lane):
    return materialize_session_memory(session_id_hash=lane["sid"], store=lane["couch"])


def receipt(lane):
    return lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))["backend_receipts"]["postgres_pgvector"]


def publish(lane, **kwargs):
    return pg_backfill.project_pg_representation(
        materialized=kwargs.pop("materialized", materialize(lane)),
        source_store=lane["couch"], sql_store=lane["sql"], embed_provider=lane["embed"],
        body=kwargs.pop("body", public_safe_mask_body(materialize(lane).body)), **kwargs,
    )


@pytest.mark.parametrize("field,value", [
    ("active_content_hash", dm.sha256_hash("wrong A")),
    ("representation_content_hash", dm.sha256_hash("wrong B")),
    ("session_memory_knowledge_id", "wrong-id"),
    ("project", "wrong-project"),
    ("embedding_profile", "wrong-profile"),
    ("provenance_digest", dm.sha256_hash("wrong-provenance")),
])
def test_same_revision_retry_repairs_wrong_mapping(representation_lane, field, value):
    lane = representation_lane
    original = materialize(lane)
    first = publish(lane, materialized=original)
    expected = receipt(lane)
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    state["backend_receipts"]["postgres_pgvector"][field] = value
    lane["couch"].put(state)
    assert lane["query"]() == []
    assert publish(lane, materialized=original) == first
    assert receipt(lane) == expected
    assert lane["embed"].calls == 1
    assert len(lane["query"]()) == 1


@pytest.mark.parametrize("version", [None, True, "1", 0, 3, {}, []])
def test_unknown_receipt_versions_never_fall_back_to_v1(representation_lane, version):
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_and_project
    lane = representation_lane
    result = materialize_and_project(session_id_hash=lane["sid"], store=lane["couch"],
        projector=pg_backfill.PgSessionMemoryProjector(lane["sql"], lane["embed"]), backend="postgres_pgvector")
    assert result["projection"]["status"] == "projected"
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    state["backend_receipts"]["postgres_pgvector"]["receipt_version"] = version
    lane["couch"].put(state)
    assert lane["query"]() == []


@pytest.mark.parametrize("version", [1, 2])
def test_unknown_metadata_rejected(representation_lane, version):
    lane = representation_lane
    if version == 2:
        publish(lane)
    else:
        from agent_knowledge.couchdb_source.session_memory_materializer import materialize_and_project
        materialize_and_project(session_id_hash=lane["sid"], store=lane["couch"],
            projector=pg_backfill.PgSessionMemoryProjector(lane["sql"], lane["embed"]), backend="postgres_pgvector")
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    state["backend_receipts"]["postgres_pgvector"]["unverified_provenance"] = "unknown"
    lane["couch"].put(state)
    assert lane["query"]() == []


def test_failed_new_revision_preserves_previous_healthy_receipt(representation_lane):
    from agent_knowledge.couchdb_source.session_memory_materializer import project_session_memory
    lane = representation_lane
    publish(lane)
    previous = receipt(lane)
    chunk = next(d for d in lane["couch"].find_by_session(session_id_hash=lane["sid"]) if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " new revision"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    lane["couch"].put(chunk)
    class FailingProjector:
        def project(self, **kwargs):
            raise RuntimeError("synthetic failure")
    result = project_session_memory(materialized=materialize(lane), store=lane["couch"],
        projector=FailingProjector(), backend="postgres_pgvector")
    assert result["status"] == "failed"
    assert receipt(lane) == previous
    assert lane["query"]() == []


def test_self_consistent_forged_authority_hash_is_not_authority(representation_lane):
    from agent_knowledge.rag_ingress.pg_representation import mapping_digest
    lane = representation_lane
    publish(lane)
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    mapping = state["backend_receipts"]["postgres_pgvector"]
    mapping["active_content_hash"] = dm.sha256_hash("forged authority")
    mapping["provenance_digest"] = mapping_digest(mapping)
    lane["couch"].put(state)
    assert lane["query"]() == []


def test_unreceipted_legacy_vector_is_not_certified_as_v2(representation_lane):
    from agent_knowledge.postgres_store.pgvector_store import SessionChunk
    lane = representation_lane
    original = materialize(lane)
    body = public_safe_mask_body(original.body)
    legacy = SessionChunk(chunk_id="legacy-unverified", session_id_hash=lane["sid"],
        project=original.project, provider=original.provider, content_markdown=body,
        content_hash=dm.sha256_hash(body), embedding_state="ready", embedding=[0.5] * 3072)
    lane["sql"].insert_chunk(legacy)
    result = publish(lane)
    assert result["ref"] != legacy.chunk_id
    assert lane["embed"].calls == 1
    assert lane["sql"].get_chunk(legacy.chunk_id).embedding == legacy.embedding


def test_same_body_new_revision_needs_receipt_renewal_not_embedding(representation_lane):
    lane = representation_lane
    first = publish(lane)
    before = lane["sql"].get_chunk(first["ref"])
    original = materialize(lane)
    chunk = next(d for d in lane["couch"].find_by_session(session_id_hash=lane["sid"]) if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["observed_at_end"] = "2026-06-18T00:00:00Z"
    lane["couch"].put(chunk)
    current = materialize(lane)
    assert current.body == original.body and current.source_hash != original.source_hash
    assert lane["query"]() == []
    second = publish(lane)
    assert second["status"] == "projected" and second["ref"] != first["ref"]
    assert lane["embed"].calls == 1
    assert [h["memory_id"] for h in lane["query"]()] == [second["ref"]]
    after = lane["sql"].get_chunk(first["ref"])
    assert after == before
    assert lane["sql"].get_chunk(second["ref"]).embedding == before.embedding


@pytest.mark.parametrize("field,value", [
    ("body", "arbitrary body"), ("content_hash", dm.sha256_hash("wrong")),
    ("source_hash", dm.sha256_hash("wrong")), ("project", "wrong"),
    ("provider", "wrong"), ("fully_materialized", False),
    ("conversation_chunk_count", 99), ("tool_evidence_bundle_count", 99),
])
def test_writer_rejects_incomplete_or_forged_materialization(representation_lane, field, value):
    lane = representation_lane
    with pytest.raises(ValueError, match="PG representation verification failed"):
        publish(lane, materialized=replace(materialize(lane), **{field: value}))
    assert lane["embed"].calls == 0
    assert lane["query"]() == []


def test_writer_rejects_arbitrary_transformed_body(representation_lane):
    lane = representation_lane
    with pytest.raises(ValueError, match="PG representation verification failed"):
        publish(lane, body="unrelated but well hashed body")
    assert lane["embed"].calls == 0
    assert lane["query"]() == []


@pytest.mark.parametrize("field,value", [
    ("content_hash", dm.sha256_hash("wrong")), ("content_markdown", "tampered body"),
    ("session_id_hash", dm.sha256_hash("wrong")), ("provider", "wrong"),
    ("project", "other-project"), ("embedding_model", "wrong"),
    ("embedding_state", "failed"), ("embedding", None),
])
def test_corrupt_sql_collision_never_overwritten(representation_lane, field, value):
    lane = representation_lane
    result = publish(lane)
    row = lane["sql"].get_chunk(result["ref"])
    setattr(row, field, value)
    lane["sql"].insert_chunk(row)
    previous = lane["sql"].get_chunk(row.chunk_id)
    assert lane["query"]() == []
    with pytest.raises(ValueError, match="PG representation row mismatch"):
        publish(lane)
    assert lane["sql"].get_chunk(row.chunk_id) == previous
    assert lane["embed"].calls == 1


@pytest.mark.parametrize("kind", ["orphan", "qdrant-only"])
def test_unreceipted_sql_is_invisible(representation_lane, kind):
    lane = representation_lane
    publish(lane)
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    previous = state.pop("backend_receipts")["postgres_pgvector"]
    if kind == "qdrant-only":
        state.update(previous)
    lane["couch"].put(state)
    assert lane["query"]() == []


@pytest.mark.parametrize("phase", ["before", "after", "conflict"])
def test_source_change_during_commit_never_authorizes_old_sql(representation_lane, monkeypatch, phase):
    from agent_knowledge.couchdb_source.source_store import SourceStoreConflict
    lane = representation_lane
    put = lane["couch"].put_if_revision
    def change():
        chunk = next(d for d in lane["couch"].find_by_session(session_id_hash=lane["sid"]) if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
        chunk["body"] += " changed"
        chunk["content_hash"] = dm.sha256_hash(chunk["body"])
        lane["couch"].put(chunk)
    def race(doc, **kw):
        if phase in ("before", "conflict"):
            change()
        if phase == "conflict":
            raise SourceStoreConflict("synthetic conflict")
        result = put(doc, **kw)
        if phase == "after":
            change()
        return result
    with monkeypatch.context() as m:
        m.setattr(lane["couch"], "put_if_revision", race)
        assert publish(lane) == {"status": "failed", "reason": "source_revision_changed", "ref": ""}
    assert lane["query"]() == []
    assert publish(lane)["status"] == "projected"
    assert len(lane["query"]()) == 1


@pytest.mark.parametrize("field,value", [
    ("receipt_version", 999), ("receipt_version", "2"),
    ("representation_kind", "unknown"), ("provenance_digest", "malformed"),
    ("embedding_profile", "unknown"), ("representation_content_hash", "sha256:bad"),
    ("session_id_hash", dm.sha256_hash("wrong")),
])
def test_v2_malformed_metadata_rejected(representation_lane, field, value):
    lane = representation_lane
    publish(lane)
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    state["backend_receipts"]["postgres_pgvector"][field] = value
    lane["couch"].put(state)
    assert lane["query"]() == []


@pytest.mark.parametrize("version", [1, 2])
def test_same_revision_unknown_metadata_retry_repairs_receipt(representation_lane, version):
    from agent_knowledge.couchdb_source.session_memory_materializer import project_session_memory
    lane = representation_lane
    original = materialize(lane)
    def run():
        if version == 2:
            return publish(lane, materialized=original)
        return project_session_memory(materialized=original, store=lane["couch"],
            projector=pg_backfill.PgSessionMemoryProjector(lane["sql"], lane["embed"]), backend="postgres_pgvector")
    first = run()
    expected = receipt(lane)
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    state["backend_receipts"]["postgres_pgvector"]["unknown"] = "unverified"
    lane["couch"].put(state)
    assert lane["query"]() == []
    assert run() == first
    assert receipt(lane) == expected
    assert len(lane["query"]()) == 1


def test_receipt_commit_rejects_unbound_representation_metadata(representation_lane):
    from agent_knowledge.couchdb_source.session_memory_materializer import _commit_projection_state_if_source_current
    lane = representation_lane
    publish(lane)
    previous = receipt(lane)
    malformed = dict(previous, active_content_hash=dm.sha256_hash("wrong"))
    with pytest.raises(ValueError, match="PG representation receipt mismatch"):
        _commit_projection_state_if_source_current(materialized=materialize(lane), store=lane["couch"],
            projection_status="projected", ref=previous["session_memory_knowledge_id"], backend="postgres_pgvector",
            representation_receipt=malformed)
    assert receipt(lane) == previous


def test_concurrent_sql_insert_cannot_be_overwritten(representation_lane, monkeypatch):
    from psycopg.errors import UniqueViolation
    lane = representation_lane
    insert = lane["sql"].insert_chunk
    def race(row, *args, **kwargs):
        # A writer outside this lane does not honor its advisory lock.
        insert(replace(row, content_markdown="collision", content_hash=dm.sha256_hash("collision")))
        return insert(row, *args, **kwargs)
    monkeypatch.setattr(lane["sql"], "insert_chunk", race)
    with pytest.raises(UniqueViolation):
        publish(lane)
    rows = lane["sql"].search_session_chunks(SyntheticEmbedProvider().embed("x"), project="representation-test")
    assert len(rows) == 1 and rows[0]["content_markdown"] == "collision"
    assert lane["query"]() == []


@pytest.mark.parametrize("failure", ["embedding", "dimension", "readback", "receipt_cas"])
def test_local_writer_failure_does_not_replace_healthy_receipt(representation_lane, monkeypatch, failure):
    lane = representation_lane
    publish(lane)
    previous = receipt(lane)
    chunk = next(d for d in lane["couch"].find_by_session(session_id_hash=lane["sid"]) if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " new body"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    lane["couch"].put(chunk)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")
    with monkeypatch.context() as m:
        if failure == "embedding":
            m.setattr(lane["embed"], "embed", fail)
        elif failure == "dimension":
            m.setattr(lane["embed"], "embed", lambda text: [1.0])
        elif failure == "receipt_cas":
            m.setattr(lane["couch"], "put_if_revision", fail)
        else:
            get = lane["sql"].get_chunk
            def bad_readback(chunk_id, conn=None):
                if conn is None:
                    return None
                return get(chunk_id, conn=conn)
            m.setattr(lane["sql"], "get_chunk", bad_readback)
        with pytest.raises((ValueError, RuntimeError)):
            publish(lane)
    assert receipt(lane) == previous
    assert lane["query"]() == []
    assert publish(lane)["status"] == "projected"
    assert len(lane["query"]()) == 1


@pytest.mark.parametrize("version", [None, 1])
def test_legacy_a_equals_b_receipt_remains_visible(representation_lane, version):
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_and_project
    lane = representation_lane
    materialize_and_project(session_id_hash=lane["sid"], store=lane["couch"],
        projector=pg_backfill.PgSessionMemoryProjector(lane["sql"], lane["embed"]), backend="postgres_pgvector")
    if version is not None:
        state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
        state["backend_receipts"]["postgres_pgvector"]["receipt_version"] = version
        lane["couch"].put(state)
    mapping = receipt(lane)
    assert [h["content_hash"] for h in lane["query"]()] == [mapping["active_content_hash"]]


@pytest.mark.parametrize("field", ["materialized_at", "failure_reason"])
def test_malformed_nonidentity_metadata_rejected(representation_lane, field):
    lane = representation_lane
    publish(lane)
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    state["backend_receipts"]["postgres_pgvector"][field] = {"invalid": True}
    lane["couch"].put(state)
    assert lane["query"]() == []


def test_masked_representation_sql_receipt_recall(representation_lane):
    lane = representation_lane
    original = materialize(lane)
    assert hasattr(pg_backfill, "project_pg_representation"), "verified representation writer is missing"
    result = publish(lane)
    assert result["status"] == "projected"
    mapping = receipt(lane)
    row = lane["sql"].get_chunk(result["ref"])
    assert mapping["receipt_version"] == 2
    assert mapping["representation_kind"] == "qdrant_public_safe_mask.v1"
    assert mapping["embedding_profile"] == DEFAULT_EMBEDDING_PROFILE_ID
    assert mapping["active_content_hash"] == original.content_hash
    assert mapping["projected_source_hash"] == original.source_hash
    assert mapping["representation_content_hash"] == row.content_hash == dm.sha256_hash(row.content_markdown)
    assert row.content_hash != original.content_hash
    assert row.content_markdown == public_safe_mask_body(original.body)
    assert original.body == materialize(lane).body
    assert [(h["memory_id"], h["summary"], h["content_hash"]) for h in lane["query"]()] == [(row.chunk_id, row.content_markdown, row.content_hash)]

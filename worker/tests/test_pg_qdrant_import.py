"""Local legacy-vector import: synthetic authority and real isolated PostgreSQL."""
from copy import deepcopy
from importlib import import_module

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.pg_backfill import _derive_pg_chunk_id_impl
from agent_knowledge.rag_ingress.qdrant_authority_join import join_mirror_hits_to_authority
from agent_knowledge.rag_ingress.qdrant_backfill import public_safe_mask_body
from agent_knowledge.rag_ingress.qdrant_couchdb_authority import CouchDBProjectionStateAuthorityResolver
from agent_knowledge.rag_ingress.qdrant_docling_mirror import _validate_mirror_text
from test_couchdb_build_cli import _build_synthetic_session


def importer():
    from agent_knowledge import rag_ingress
    import pkgutil
    assert "pg_qdrant_import" in {m.name for m in pkgutil.iter_modules(rag_ingress.__path__)}, "local verified importer missing"
    return import_module("agent_knowledge.rag_ingress.pg_qdrant_import")


@pytest.fixture
def lane(isolated_pg_store):
    source = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(source, provider="codex", project="import-test", raw_id="synthetic-import", body="synthetic raw_transcript terminology")
    chunk = next(d for d in source.find_by_session(session_id_hash=sid) if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " synthetic raw_transcript terminology"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    source.put(chunk)
    current = materialize_session_memory(session_id_hash=sid, store=source)
    body = _validate_mirror_text(public_safe_mask_body(current.body))
    return dict(source=source, sql=isolated_pg_store, current=current, point={
        "id": "00000000-0000-0000-0000-000000000001",
        "payload": {"document_kind": "session_memory", "result_type": "session_memory",
                    "target_profile": "session-memory", "session_id_hash": sid,
                    "project": current.project, "provider": current.provider,
                    "content_hash": current.content_hash, "text": body},
        "vector": [0.123456] * 3072,
    })


def run(lane, **overrides):
    api = importer()
    params = dict(point=lane["point"], collection=api.LegacyCollection("synthetic-collection", 3072, "Cosine"),
                  attestation=api.OperatorEmbeddingAttestation(collection="synthetic-collection", model="gemini-embedding-2", dimension=3072,
                                                              confirmed=True, evidence_ref="synthetic-operator-confirmation"),
                  project="import-test", provider="codex", session_id_hash=lane["current"].session_id_hash,
                  source_store=lane["source"], sql_store=lane["sql"])
    params.update(overrides)
    return api.import_qdrant_point(**params)


def state(lane):
    return lane["source"].get(dm.projection_state_doc_id(lane["current"].session_id_hash))


def counts(lane):
    with lane["sql"].transaction() as conn:
        return tuple(conn.execute("SELECT count(*) FROM " + table).fetchone()["count"]
                     for table in ("session_memory_chunks", "embedding_outbox"))


def recall(lane):
    rows = lane["sql"].search_session_chunks(lane["point"]["vector"], project="import-test")
    hits = [dict(session_id_hash=r["session_id_hash"], content_hash=r["content_hash"],
                 memory_id=r["chunk_id"], provider=r["provider"], project=r["project"],
                 summary=r["content_markdown"]) for r in rows]
    return join_mirror_hits_to_authority(hits, resolver=CouchDBProjectionStateAuthorityResolver(
        lane["source"], filters={"project": "import-test"}, backend="postgres_pgvector"), drop_unresolved=True)


def test_masked_point_copied_to_scoped_sql_receipt_and_recall(lane):
    result = run(lane)
    assert result["status"] == "projected"
    row = lane["sql"].get_chunk(result["ref"])
    receipt = state(lane)["backend_receipts"]["postgres_pgvector"]
    assert receipt["receipt_version"] == 2
    assert receipt["active_content_hash"] == lane["current"].content_hash
    assert receipt["representation_content_hash"] == row.content_hash == dm.sha256_hash(lane["point"]["payload"]["text"])
    assert row.content_hash != receipt["active_content_hash"]
    assert row.chunk_id == _derive_pg_chunk_id_impl(project="import-test", provider="codex",
        session_id_hash=lane["current"].session_id_hash, source_hash=lane["current"].source_hash, content_hash=row.content_hash)
    assert row.embedding == pytest.approx(lane["point"]["vector"], rel=0.0005, abs=3e-8)
    assert row.content_markdown == lane["point"]["payload"]["text"]
    assert [h["memory_id"] for h in recall(lane)] == [result["ref"]]
    assert counts(lane) == (1, 0)


@pytest.mark.parametrize("field,value", [
    ("embedding_model", "wrong-model"), ("model", "wrong-model"),
    ("content_hash", dm.sha256_hash("wrong source")),
    ("project", "other-project"), ("provider", "other-provider"),
    ("session_id_hash", dm.sha256_hash("other-session")),
    ("source_hash", dm.sha256_hash("other-revision")),
    ("text", "different public-safe body"), ("text", None),
    ("document_kind", "knowledge"), ("target_profile", "other-target"),
    ("result_type", "knowledge"),
])
def test_payload_mismatch_rejected_without_writes(lane, field, value):
    lane["point"]["payload"][field] = value
    with pytest.raises(ValueError, match="^Qdrant import rejected: [a-z_]+$"):
        run(lane)
    assert counts(lane) == (0, 0) and state(lane) is None


@pytest.mark.parametrize("kind", ["missing", "unconfirmed", "model", "collection", "dimension", "evidence"])
def test_attestation_is_required_and_bound(lane, kind):
    api = importer()
    values = dict(collection="synthetic-collection", model="gemini-embedding-2", dimension=3072,
                  confirmed=True, evidence_ref="synthetic-confirmation")
    changes = {"unconfirmed": {"confirmed": False}, "model": {"model": "wrong"},
               "collection": {"collection": "wrong"}, "dimension": {"dimension": 1536},
               "evidence": {"evidence_ref": ""}}
    values.update(changes.get(kind, {}))
    attestation = None if kind == "missing" else api.OperatorEmbeddingAttestation(**values)
    with pytest.raises(ValueError, match="^Qdrant import rejected: attestation$"):
        run(lane, attestation=attestation)
    assert counts(lane) == (0, 0) and state(lane) is None


@pytest.mark.parametrize("dimension,distance", [(1536, "Cosine"), (3072, "Dot"), ("3072", "Cosine")])
def test_collection_contract_rejected(lane, dimension, distance):
    api = importer()
    with pytest.raises(ValueError, match="^Qdrant import rejected: collection$"):
        run(lane, collection=api.LegacyCollection("synthetic-collection", dimension, distance))
    assert counts(lane) == (0, 0) and state(lane) is None


@pytest.mark.parametrize("vector", [[1.0], [float("nan")] * 3072, [float("inf")] * 3072,
                                    [1e10] * 3072, [True] * 3072, ["0.1"] * 3072,
                                    [0.0] * 3072, {"named": [0.1] * 3072}, None])
def test_invalid_vector_rejected_before_sql(lane, vector):
    lane["point"]["vector"] = vector
    with pytest.raises(ValueError, match="^Qdrant import rejected: vector$"):
        run(lane)
    assert counts(lane) == (0, 0) and state(lane) is None


@pytest.mark.parametrize("metadata", [{"model": "wrong"}, {"embedding_model": "wrong"},
                                     {"project": "wrong"}, {"provider": "wrong"},
                                     {"session_id_hash": dm.sha256_hash("wrong")}, "invalid"])
def test_conflicting_nested_metadata_rejected(lane, metadata):
    lane["point"]["payload"]["metadata"] = metadata
    with pytest.raises(ValueError, match="^Qdrant import rejected: [a-z_]+$"):
        run(lane)
    assert counts(lane) == (0, 0) and state(lane) is None


@pytest.mark.parametrize("point", [None, [], {"payload": None}, {"payload": "synthetic sensitive value"}])
def test_malformed_point_has_source_free_error(lane, point):
    with pytest.raises(ValueError, match="^Qdrant import rejected: payload$") as exc:
        run(lane, point=point)
    assert exc.value.__suppress_context__ or exc.value.__context__ is None
    assert counts(lane) == (0, 0) and state(lane) is None


def test_double_rounding_uses_postgresql_insert_conversion(lane):
    import struct
    from agent_knowledge.postgres_store.pgvector_store import _parse_vector, _vector_literal

    value = 1.00048828126
    lane["point"]["vector"] = [value] * 3072
    with lane["sql"].transaction() as conn:
        converted = _parse_vector(conn.execute("SELECT %s::halfvec AS embedding",
            (_vector_literal(lane["point"]["vector"]),)).fetchone()["embedding"])
    assert converted == [1.0] * 3072
    assert struct.unpack("e", struct.pack("e", value))[0] == 1.0009765625
    first = run(lane)
    assert lane["sql"].get_chunk(first["ref"]).embedding == converted
    assert run(lane, dry_run=True) == {"status": "validated", "reason": "", "ref": ""}
    assert run(lane) == first
    assert counts(lane) == (1, 0)


def test_changed_adapter_vector_rejected_before_insert(lane, monkeypatch):
    api = importer()
    # This different value rounds to the same halfvec as the supplied input;
    # pre-insert validation must still reject an altered original vector.
    lane["point"]["vector"] = [1.0] * 3072
    monkeypatch.setattr(api.VerifiedLegacyVector, "embed", lambda self, body: [1.0001] * 3072)
    with pytest.raises(ValueError, match="^Qdrant import rejected: projection$"):
        run(lane)
    assert counts(lane) == (0, 0) and state(lane) is None and recall(lane) == []


def test_duplicate_import_preserves_sql_vector_and_receipt(lane):
    first = run(lane)
    row = lane["sql"].get_chunk(first["ref"])
    receipt = deepcopy(state(lane))
    assert run(lane) == first
    assert lane["sql"].get_chunk(first["ref"]) == row
    assert state(lane) == receipt
    assert counts(lane) == (1, 0)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("expected,existing", [
    (0.123456, 0.75), (1.0, 0.99951171875),
    (1.00048828126, 1.0009765625),  # SQL float32 rounding differs from Python binary16.
])
def test_existing_different_vector_rejected_without_overwrite(lane, dry_run, expected, existing):
    from agent_knowledge.postgres_store.pgvector_store import SessionChunk
    lane["point"]["vector"] = [expected] * 3072
    current = lane["current"]
    body = lane["point"]["payload"]["text"]
    chunk_id = _derive_pg_chunk_id_impl(project=current.project, provider=current.provider,
        session_id_hash=current.session_id_hash, source_hash=current.source_hash, content_hash=dm.sha256_hash(body))
    lane["sql"].insert_chunk(SessionChunk(chunk_id=chunk_id, session_id_hash=current.session_id_hash,
        project=current.project, provider=current.provider, content_markdown=body,
        content_hash=dm.sha256_hash(body), embedding_state="ready", embedding=[existing] * 3072))
    row = lane["sql"].get_chunk(chunk_id)
    with pytest.raises(ValueError, match="^Qdrant import rejected: projection$"):
        run(lane, dry_run=dry_run)
    assert lane["sql"].get_chunk(chunk_id) == row
    assert state(lane) is None and recall(lane) == []
    assert counts(lane) == (1, 0)


class AlteredReadbackStore:
    """Injected store fault; underlying writes and readback still execute real SQL."""
    def __init__(self, store, value=0.75):
        self.store = store
        self.value = value

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get_chunk(self, chunk_id, conn=None):
        row = self.store.get_chunk(chunk_id, conn=conn)
        if row is not None and conn is None:
            row.embedding = [self.value] * 3072
        return row


@pytest.mark.parametrize("expected,swapped", [(0.123456, 0.75), (1.0, 0.99951171875)])
def test_wrong_readback_vector_never_gets_receipt(lane, expected, swapped):
    lane["point"]["vector"] = [expected] * 3072
    with pytest.raises(ValueError, match="^Qdrant import rejected: projection$"):
        run(lane, sql_store=AlteredReadbackStore(lane["sql"], swapped))
    assert counts(lane) == (1, 0)
    assert state(lane) is None and recall(lane) == []


def change_source(lane):
    chunk = next(d for d in lane["source"].find_by_session(session_id_hash=lane["current"].session_id_hash)
                 if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " changed source"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    lane["source"].put(chunk)


@pytest.mark.parametrize("kind", ["changed", "missing", "incomplete", "wrong_scope"])
def test_noncurrent_or_incomplete_authority_never_imports(lane, kind):
    if kind == "changed":
        change_source(lane)
    elif kind == "missing":
        lane["source"] = InMemoryCouchDBSourceStore()
    elif kind == "incomplete":
        coverage = next(d for d in lane["source"].find_by_session(session_id_hash=lane["current"].session_id_hash)
                        if d["doc_type"] == dm.SourceDocType.COVERAGE_MANIFEST)
        coverage["conversation_chunk_count"] = 99
        lane["source"].put(coverage)
    else:
        lane["point"]["payload"]["project"] = "other"
    with pytest.raises(ValueError, match="^Qdrant import rejected: (source|scope)$"):
        run(lane, **({"project": "other"} if kind == "wrong_scope" else {}))
    assert counts(lane) == (0, 0) and state(lane) is None


class RacingSource:
    def __init__(self, lane, phase):
        self.lane, self.phase = lane, phase

    def __getattr__(self, name):
        return getattr(self.lane["source"], name)

    def put_if_revision(self, doc, **kwargs):
        from agent_knowledge.couchdb_source.source_store import SourceStoreConflict
        if self.phase in ("before", "conflict"):
            change_source(self.lane)
        if self.phase == "conflict":
            raise SourceStoreConflict("synthetic conflict")
        if self.phase == "failure":
            raise RuntimeError("synthetic sensitive source detail")
        result = self.lane["source"].put_if_revision(doc, **kwargs)
        if self.phase == "after":
            change_source(self.lane)
        return result


@pytest.mark.parametrize("phase", ["before", "after", "conflict", "failure"])
def test_raced_source_or_failed_receipt_keeps_orphan_invisible(lane, phase):
    if phase == "failure":
        with pytest.raises(ValueError, match="^Qdrant import rejected: projection$"):
            run(lane, source_store=RacingSource(lane, phase))
    else:
        assert run(lane, source_store=RacingSource(lane, phase)) == {
            "status": "failed", "reason": "source_revision_changed", "ref": ""}
    assert counts(lane) == (1, 0)
    assert recall(lane) == []
    if phase == "failure":
        assert state(lane) is None


def test_local_vector_adapter_cannot_embed_another_body():
    api = importer()
    provider = api.VerifiedLegacyVector("validated body", (0.25,) * 3072)
    assert provider.embed("validated body") == [0.25] * 3072
    with pytest.raises(ValueError, match="^legacy vector body mismatch$"):
        provider.embed("different body")


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("original,changed", [(0.123456, 0.75), (1.0, 0.99951171875)])
def test_same_body_new_source_revision_reuses_only_matching_vector(lane, dry_run, original, changed):
    lane["point"]["vector"] = [original] * 3072
    first = run(lane)
    previous = deepcopy(state(lane))
    chunk = next(d for d in lane["source"].find_by_session(session_id_hash=lane["current"].session_id_hash)
                 if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["observed_at_end"] = "2026-06-18T00:00:00Z"
    lane["source"].put(chunk)
    assert recall(lane) == []
    current = materialize_session_memory(session_id_hash=lane["current"].session_id_hash, store=lane["source"])
    assert current.content_hash == lane["current"].content_hash
    assert current.source_hash != lane["current"].source_hash
    row = lane["sql"].get_chunk(first["ref"])
    lane["point"]["vector"] = [changed] * 3072
    with pytest.raises(ValueError, match="^Qdrant import rejected: projection$"):
        run(lane, dry_run=dry_run, source_store=ReadOnlySource(lane["source"]))
    assert state(lane) == previous and counts(lane) == (1, 0)
    assert lane["sql"].get_chunk(first["ref"]) == row and recall(lane) == []
    lane["point"]["vector"] = [original] * 3072
    assert run(lane, dry_run=True, source_store=ReadOnlySource(lane["source"])) == {
        "status": "validated", "reason": "", "ref": ""}
    assert state(lane) == previous and counts(lane) == (1, 0)
    second = run(lane)
    assert second["status"] == "projected" and second["ref"] != first["ref"]
    assert [h["memory_id"] for h in recall(lane)] == [second["ref"]]


class ReadOnlySource:
    """Fail immediately on any source mutation, even a no-op write."""
    def __init__(self, source):
        self.source = source

    def __getattr__(self, name):
        if name not in {"get", "find_by_session", "find_by_doc_type"}:
            raise AssertionError("dry-run attempted a source mutation")
        return getattr(self.source, name)


def test_dry_run_validates_but_never_writes(lane):
    before = deepcopy(lane["point"])
    assert run(lane, dry_run=True, source_store=ReadOnlySource(lane["source"])) == {
        "status": "validated", "reason": "", "ref": ""}
    assert lane["point"] == before
    assert counts(lane) == (0, 0) and state(lane) is None
    lane["point"]["payload"]["content_hash"] = dm.sha256_hash("stale")
    with pytest.raises(ValueError, match="Qdrant import rejected: source"):
        run(lane, dry_run=True)
    assert counts(lane) == (0, 0) and state(lane) is None

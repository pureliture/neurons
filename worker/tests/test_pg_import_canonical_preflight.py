"""정규 청크 ID와 승인 후 대상 변화 회귀: 합성 로컬 SQL만 사용한다."""
from copy import deepcopy

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.postgres_store.pgvector_store import SessionChunk
from agent_knowledge.rag_ingress import pg_backfill
from agent_knowledge.rag_ingress import pg_qdrant_import_cli as cli
from agent_knowledge.rag_ingress.pg_qdrant_import import LegacyCollection, OperatorEmbeddingAttestation
from test_pg_qdrant_import_cli import actual_sql, approve, invoke, lane  # noqa: F401


def canonical_row(boundary):
    point = boundary.points[0]
    current = materialize_session_memory(session_id_hash=boundary.sid, store=boundary.source)
    content_hash = dm.sha256_hash(point["payload"]["text"])
    return SessionChunk(
        chunk_id=pg_backfill._derive_pg_chunk_id_impl(
            project=current.project, provider=current.provider, session_id_hash=boundary.sid,
            source_hash=current.source_hash, content_hash=content_hash),
        session_id_hash=boundary.sid, project=current.project, provider=current.provider,
        content_markdown=point["payload"]["text"], content_hash=content_hash,
        embedding_state="ready", embedding=point["vector"],
    )


def test_actual_sql_preflight_reads_the_importer_canonical_key(lane, actual_sql, monkeypatch):
    boundary, _, _ = lane
    store, conn, _ = actual_sql
    boundary.sql = store
    row = canonical_row(boundary)
    store.insert_chunk(row, insert_only=True)
    conn.commit()
    original = store.get_chunk
    reads = []

    def read(chunk_id, **kwargs):
        result = original(chunk_id, **kwargs)
        reads.append((chunk_id, result))
        return result

    monkeypatch.setattr(store, "get_chunk", read)
    result = cli.inspect_point(boundary, boundary.points[0], LegacyCollection("synthetic", 3072, "Cosine"),
                               OperatorEmbeddingAttestation("synthetic", "gemini-embedding-2", 3072, True, "synthetic"))
    assert result["status"] == "validated"
    assert reads and all(key == row.chunk_id and value is not None for key, value in reads)


def test_batch_derives_only_prefixed_canonical_content_hashes(lane, monkeypatch):
    boundary, _, _ = lane
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    boundary.points[1]["vector"][0] = 0.25
    original = pg_backfill._derive_pg_chunk_id_impl
    hashes = []

    def derive(**kwargs):
        hashes.append(kwargs["content_hash"])
        return original(**kwargs)

    monkeypatch.setattr(pg_backfill, "_derive_pg_chunk_id_impl", derive)
    entries = cli.inspect_batch(boundary, boundary.points, LegacyCollection("synthetic", 3072, "Cosine"),
                                OperatorEmbeddingAttestation("synthetic", "gemini-embedding-2", 3072, True, "synthetic"))
    assert {entry["status"] for entry in entries} == {"canonical_vector_conflict"}
    assert len(hashes) == 6  # target lookup, real importer, batch identity for each point
    assert set(hashes) == {dm.sha256_hash(boundary.points[0]["payload"]["text"])}


@pytest.mark.parametrize("drift", ["create", "delete"])
def test_actual_sql_target_presence_drift_rejected_before_any_write(lane, actual_sql, capsys, drift):
    boundary, _, _ = lane
    store, conn, _ = actual_sql
    boundary.sql = store
    row = canonical_row(boundary)
    if drift == "delete":
        store.insert_chunk(row, insert_only=True)
        conn.commit()
    assert invoke(lane, capsys)[0] == 0
    approve(lane)
    if drift == "create":
        store.insert_chunk(row, insert_only=True)
    else:
        conn.execute("DELETE FROM session_memory_chunks WHERE chunk_id = %s", (row.chunk_id,))
    conn.commit()
    # SQL trigger observes real DML, not a key-insensitive fake store.
    conn.execute("CREATE TEMP TABLE preflight_writes (operation text)")
    conn.execute("""CREATE FUNCTION pg_temp.record_preflight_write() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO preflight_writes VALUES (TG_OP); RETURN NULL; END $$""")
    conn.execute("""CREATE TRIGGER preflight_write_audit AFTER INSERT OR UPDATE OR DELETE
        ON session_memory_chunks FOR EACH ROW EXECUTE FUNCTION pg_temp.record_preflight_write()""")
    conn.commit()
    receipt_before = deepcopy(boundary.source.get(dm.projection_state_doc_id(boundary.sid)))
    source_writes = []
    original_put = boundary.source.put

    def put(*args, **kwargs):
        source_writes.append(True)
        return original_put(*args, **kwargs)

    boundary.source.put = put
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 1 and report == {"status": "rejected", "mutation_started": False, "applied": 0}
    assert conn.execute("SELECT count(*) AS n FROM preflight_writes").fetchone()["n"] == 0
    assert source_writes == []
    assert boundary.source.get(dm.projection_state_doc_id(boundary.sid)) == receipt_before
    assert (store.get_chunk(row.chunk_id) is not None) == (drift == "create")

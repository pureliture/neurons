"""Cross-writer absence fences: real isolated SQL, synthetic vectors only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from time import monotonic

import psycopg
import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
from agent_knowledge.rag_ingress.pg_qdrant_import import (
    LegacyCollection, OperatorEmbeddingAttestation, import_qdrant_point,
)
from agent_knowledge.rag_ingress.qdrant_backfill import public_safe_mask_body
from test_couchdb_build_cli import _build_synthetic_session


class LocalVector:
    model = "gemini-embedding-2"
    size = 3072

    def embed(self, body):
        return [0.5] * self.size


@pytest.fixture
def lane(isolated_pg_store):
    source = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(source, provider="codex", project="lock-test",
                                   raw_id="synthetic-lock", body="synthetic safe text")
    current = materialize_session_memory(session_id_hash=sid, store=source)
    assert public_safe_mask_body(current.body) == current.body
    normal_store = PgVectorStore(dsn=isolated_pg_store.dsn)
    document = {key: getattr(current, key) for key in (
        "body", "content_hash", "session_id_hash", "project", "provider", "source_hash",
    )}

    def import_vector():
        return import_qdrant_point(
            point={"id": "synthetic-point", "payload": {
                "document_kind": "session_memory", "target_profile": "session-memory",
                "result_type": "session_memory", "session_id_hash": sid,
                "project": current.project, "provider": current.provider,
                "content_hash": current.content_hash, "text": current.body,
            }, "vector": [0.25] * 3072},
            collection=LegacyCollection("synthetic-collection", 3072, "Cosine"),
            attestation=OperatorEmbeddingAttestation(
                "synthetic-collection", "gemini-embedding-2", 3072, True, "synthetic-evidence",
            ),
            project=current.project, provider=current.provider, session_id_hash=sid,
            source_store=source, sql_store=isolated_pg_store,
        )

    def normal():
        return PgSessionMemoryProjector(normal_store, LocalVector()).project(
            target_profile="session-memory", document=document,
        )

    return dict(sql=isolated_pg_store, normal_store=normal_store, source=source,
                current=current, document=document, importer=import_vector, normal=normal)


def overlap_absent_writers(monkeypatch, first_store, first, second_store, second):
    """Hold first before INSERT; let second reach its real SQL absence fence.

    Unfixed writers can pass both absence reads. Pause that stale INSERT until
    the first writer has published its receipt, making vector loss deterministic.
    Fixed writers instead wait on PostgreSQL's advisory lock before those reads.
    No SQL read, lock, insert, commit, or returned result is simulated.
    """
    first_at_insert, release_first = Event(), Event()
    second_at_insert, release_second = Event(), Event()
    second_pid = []
    original_first_insert = first_store.insert_chunk
    original_second_insert = second_store.insert_chunk
    original_transaction = second_store.transaction

    def first_insert(*args, **kwargs):
        first_at_insert.set()
        assert release_first.wait(8), "first writer was not released"
        return original_first_insert(*args, **kwargs)

    def second_insert(*args, **kwargs):
        second_at_insert.set()
        assert release_second.wait(8), "second writer was not released"
        return original_second_insert(*args, **kwargs)

    @contextmanager
    def second_transaction():
        with original_transaction() as conn:
            second_pid[:] = [conn.info.backend_pid]
            yield conn

    monkeypatch.setattr(first_store, "insert_chunk", first_insert)
    monkeypatch.setattr(second_store, "insert_chunk", second_insert)
    monkeypatch.setattr(second_store, "transaction", second_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        try:
            assert first_at_insert.wait(5), "first writer did not reach absent INSERT"
            second_future = pool.submit(second)
            deadline = monotonic() + 5
            with psycopg.connect(first_store.dsn, autocommit=True) as observer:
                while not second_at_insert.is_set():
                    if second_future.done():
                        second_future.result()  # Surface unexpected errors, not a timeout.
                        pytest.fail("second writer finished before the first absent INSERT")
                    if second_pid:
                        row = observer.execute(
                            "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                            (second_pid[0],),
                        ).fetchone()
                        if row == ("Lock", "advisory"):
                            break
                    assert monotonic() < deadline, "second writer reached neither lock nor INSERT"
                    second_at_insert.wait(0.01)
            release_first.set()
            first_result = first_future.result(timeout=5)
            release_second.set()
            try:
                second_result = second_future.result(timeout=5)
            except ValueError as error:
                second_result = error
            return first_result, second_result
        finally:
            release_first.set()
            release_second.set()


@pytest.mark.parametrize("new_revision", [False, True], ids=["same-id", "same-body-new-revision"])
def test_import_winner_keeps_vector_receipt_and_normal_reuses(lane, monkeypatch, new_revision):
    if new_revision:
        lane["document"]["source_hash"] = dm.sha256_hash("synthetic next source revision")
    imported, normal_ref = overlap_absent_writers(
        monkeypatch, lane["sql"], lane["importer"], lane["normal_store"], lane["normal"],
    )
    assert imported["status"] == "projected"
    row = lane["sql"].get_chunk(imported["ref"])
    assert row.embedding == [0.25] * 3072, "normal writer overwrote the imported vector"
    assert normal_ref == imported["ref"], "normal writer did not reuse the winning ready identity"
    receipt = lane["source"].get(dm.projection_state_doc_id(lane["current"].session_id_hash))[
        "backend_receipts"]["postgres_pgvector"]
    assert receipt["receipt_version"] == 2
    assert receipt["projection_status"] == "projected"
    assert receipt["session_memory_knowledge_id"] == imported["ref"]
    assert receipt["projected_source_hash"] == lane["current"].source_hash
    assert receipt["active_content_hash"] == receipt["representation_content_hash"] == row.content_hash
    with lane["sql"].transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM embedding_outbox").fetchone()["n"] == 0
    # Public importer retry proves the saved vector/receipt remain usable.
    assert lane["importer"]() == imported


def test_normal_winner_is_not_replaced_or_certified_by_conflicting_import(lane, monkeypatch):
    normal_ref, rejected = overlap_absent_writers(
        monkeypatch, lane["normal_store"], lane["normal"], lane["sql"], lane["importer"],
    )
    assert isinstance(rejected, ValueError)
    assert str(rejected) == "Qdrant import rejected: projection"
    assert lane["sql"].get_chunk(normal_ref).embedding == [0.5] * 3072
    assert lane["source"].get(dm.projection_state_doc_id(lane["current"].session_id_hash)) is None
    with lane["sql"].transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM embedding_outbox").fetchone()["n"] == 0

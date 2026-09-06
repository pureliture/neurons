"""Contract and opt-in live integration tests for the rationalized PG store.

The live tests intentionally require ``LBRAIN_TEST_PG_DSN``. A reachable
PostgreSQL port is not enough evidence that it is a disposable test database,
so the default worker suite never mutates an ambient database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import uuid

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.postgres_store.dual_read_shadow import DualReadShadowHarness
from agent_knowledge.postgres_store.outbox_worker import OutboxWorker
from agent_knowledge.model_connectors import DEFAULT_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    OutboxJob,
    PgVectorStore,
    SessionChunk,
    make_dummy_vector,
)
from agent_knowledge.session_memory.brain_steward import BrainStewardService


PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
live_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)


def _hash(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _card(memory_id: str, *, summary: str = "candidate summary") -> MemoryCard:
    now = datetime.now(timezone.utc)
    return MemoryCard(
        memory_id=memory_id,
        project="lbrain-pg-contract",
        card_type="decision",
        title="PG candidate",
        summary=summary,
        typed_payload={"decision": summary},
        content_hash=_hash(memory_id),
        valid_from=now,
        created_at=now,
        updated_at=now,
    )


def test_store_has_no_in_memory_compatibility_path():
    with pytest.raises(ValueError, match="in-memory"):
        PgVectorStore(use_in_memory=True)
    with pytest.raises(ValueError, match="DSN or psycopg connection"):
        PgVectorStore()


def test_store_connection_failure_is_fail_closed(monkeypatch):
    import psycopg

    def fail_connect(*_args, **_kwargs):
        raise psycopg.OperationalError("synthetic connection failure")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    store = PgVectorStore(dsn="postgresql://invalid.example.invalid/never")
    with pytest.raises(ConnectionError, match="PostgreSQL connection failed"):
        store.get_card("missing")
    assert not hasattr(store, "cards")
    assert not hasattr(store, "outbox")


def test_schema_is_readable_without_claiming_execution():
    schema = PgVectorStore.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS memory_cards" in schema
    assert "CREATE TABLE IF NOT EXISTS embedding_outbox" in schema
    assert "content_hash VARCHAR(71) NOT NULL" in schema
    assert "gemini-embedding-2" in schema
    assert "halfvec(3072)" in schema
    assert "halfvec_cosine_ops" in schema
    assert "token_count INT NOT NULL DEFAULT 0" in schema
    assert "CREATE TABLE IF NOT EXISTS graph_projection_outbox" in schema
    assert "cas_skipped" in schema
    assert "FOR UPDATE SKIP LOCKED" not in schema


def test_candidate_upsert_sql_has_authority_collision_guard():
    from agent_knowledge.postgres_store.pgvector_store import _CARD_UPSERT_SQL

    assert "ON CONFLICT (memory_id) DO UPDATE" in _CARD_UPSERT_SQL
    assert "lifecycle_state NOT IN ('accepted', 'human_accepted', 'auto_accepted')" in _CARD_UPSERT_SQL


class _BenchmarkQdrant:
    def __init__(self, ids: list[str] | None = None, error: Exception | None = None):
        self.ids = ids or []
        self.error = error

    def search(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return [{"id": item} for item in self.ids]

    def query_points(self, **_kwargs):
        # New Qdrant SDK seam used by DualReadShadowHarness._query_qdrant.
        if self.error is not None:
            raise self.error

        class _Point:
            def __init__(self, memory_id: str):
                self.payload = {"memory_id": memory_id}

        class _Response:
            def __init__(self, points):
                self.points = points

        return _Response([_Point(item) for item in self.ids])


class _BenchmarkPg:
    def __init__(self, ids: list[str] | None = None, error: Exception | None = None):
        self.ids = ids or []
        self.error = error

    def hybrid_search(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return [{"memory_id": item} for item in self.ids]


def test_shadow_benchmark_does_not_hide_backend_errors_or_empty_fixtures():
    harness = DualReadShadowHarness(
        qdrant_client=_BenchmarkQdrant(error=TimeoutError("qdrant timeout")),
        pg_store=_BenchmarkPg(ids=["memory-1"]),
        minimum_queries_for_cutover=1,
    )

    result = harness.execute_query([0.1] * 3072)
    # Backend exceptions are normalized to reason codes (redaction contract),
    # never raw exception text — but the failure must stay explicit.
    assert result.qdrant_error == "qdrant_query_failed"
    assert result.recall_at_k == 0.0
    assert result.discrepancies

    with pytest.raises(ValueError, match="at least one query fixture"):
        harness.run_benchmark([])


def test_shadow_benchmark_requires_cutover_sample_size():
    harness = DualReadShadowHarness(
        qdrant_client=_BenchmarkQdrant(ids=["memory-1"]),
        pg_store=_BenchmarkPg(ids=["memory-1"]),
    )
    summary = harness.run_benchmark([[0.1] * 3072])
    assert summary.benchmark_valid is True
    assert summary.sample_size_gate_passed is False
    assert summary.overall_gate_passed is False
    assert summary.evidence_class == "test_harness"


def test_live_cutover_evidence_rejects_dict_qdrant_double():
    with pytest.raises(ValueError, match="in-memory Qdrant"):
        DualReadShadowHarness(
            qdrant_client=type("DictQdrant", (), {"vectors": {}})(),
            pg_store=_BenchmarkPg(ids=["memory-1"]),
            evidence_class="live_cutover",
        )


class _OutboxProbe:
    def __init__(self):
        self.job = OutboxJob(
            outbox_id=7,
            target_type="memory_card",
            target_id="memory-7",
            content_hash="sha256:probe",
            payload_text="probe",
        )
        self.cas_kwargs = None
        self.renew_kwargs = None

    def claim_outbox_leases(self, **_kwargs):
        return [self.job]

    def cas_update_embedding(self, **kwargs):
        self.cas_kwargs = kwargs
        return True

    def renew_outbox_lease(self, **kwargs):
        self.renew_kwargs = kwargs
        return True

    def mark_outbox_failed(self, **_kwargs):
        raise AssertionError("failure path should not be used")


def test_outbox_worker_fences_cas_and_renews_through_store_contract():
    store = _OutboxProbe()
    worker = OutboxWorker(
        store=store,
        worker_id="worker-probe",
        embedding_fn=lambda _text: [0.1] * 3072,
    )

    assert worker.run_once() == 1
    assert store.cas_kwargs["worker_id"] == "worker-probe"
    assert worker.renew_lease(7) is True
    assert store.renew_kwargs == {
        "outbox_id": 7,
        "worker_id": "worker-probe",
        "lease_seconds": 30,
    }


def test_outbox_worker_requires_explicit_embedding_provider():
    with pytest.raises(ValueError, match="embedding_fn is required"):
        OutboxWorker(store=_OutboxProbe())


def test_embedding_profile_id_is_shared_by_model_connector_contract():
    assert DEFAULT_EMBEDDING_PROFILE_ID == "lbrain-memory-gemini-embedding-2-v1"


def test_outbox_ownerless_mutations_fail_before_database_access():
    store = PgVectorStore(dsn="postgresql://invalid.example.invalid/never")
    with pytest.raises(ValueError, match="requires worker_id"):
        store.mark_outbox_completed(1)
    with pytest.raises(ValueError, match="requires worker_id"):
        store.mark_outbox_failed(1, "synthetic")
    with pytest.raises(ValueError, match="requires worker_id"):
        store.cas_update_embedding(1, "card", "hash", make_dummy_vector(1))


@live_pg
def test_live_candidate_and_outbox_are_read_back_from_same_postgres(tmp_path: Path):
    """M1 acceptance: card and outbox commit together and are SQL-readable."""

    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    memory_id = f"m1_{suffix}"
    try:
        card = _card(memory_id)
        store.upsert_card(card)

        stored = store.get_card(memory_id)
        assert stored is not None
        assert stored.content_hash == card.content_hash
        jobs = store.list_outbox_jobs(status="queued")
        job = next(job for job in jobs if job.target_id == memory_id)
        assert job.target_type == "memory_card"
        assert job.content_hash == card.content_hash
        assert job.payload_text == card.summary
        assert job.status == "queued"
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id = %s", (memory_id,))
                cur.execute("DELETE FROM memory_cards WHERE memory_id = %s", (memory_id,))


@live_pg
def test_live_candidate_and_outbox_roll_back_as_one_transaction(tmp_path: Path):
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    memory_id = f"m1_rollback_{suffix}"
    card = _card(memory_id)
    with pytest.raises(RuntimeError, match="force rollback"):
        with store.transaction() as conn:
            store.upsert_card(card, conn=conn)
            assert store.get_card(memory_id, conn=conn) is not None
            assert any(job.target_id == memory_id for job in store.list_outbox_jobs(conn=conn))
            raise RuntimeError("force rollback")
    assert store.get_card(memory_id) is None
    assert all(job.target_id != memory_id for job in store.list_outbox_jobs())


@live_pg
def test_live_candidate_cannot_overwrite_accepted_card(tmp_path: Path):
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    memory_id = f"m1_accepted_{suffix}"
    accepted = _card(memory_id, summary="accepted authority")
    accepted.lifecycle_state = "human_accepted"
    accepted.authorization_status = "active"
    try:
        store.insert_card(accepted)
        proposal = _card(memory_id, summary="racing proposal")
        with pytest.raises(ValueError, match="accepted"):
            store.upsert_card(proposal)
        stored = store.get_card(memory_id)
        assert stored is not None
        assert stored.lifecycle_state == "human_accepted"
        assert stored.summary == "accepted authority"
        assert all(job.target_id != memory_id for job in store.list_outbox_jobs(status="queued"))
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id = %s", (memory_id,))
                cur.execute("DELETE FROM memory_cards WHERE memory_id = %s", (memory_id,))


@live_pg
def test_live_brain_steward_candidate_uses_pg_store(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = Ledger(private / "legacy.sqlite")
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    source_hash = _hash("source-" + suffix)
    source_span = {
        "source_ref": {"source_id": "src_m1"},
        "span_ref": {"span_id": "span_m1"},
        "content_hash": source_hash,
        "card_type": "decision",
        "scope": "project",
        "project": "lbrain-pg-contract",
        "provider": "codex",
        "title": "PG candidate",
        "redacted_summary": "candidate persisted in PostgreSQL",
        "typed_payload": {
            "decision": "PostgreSQL",
            "rationale": "transactional candidate path",
            "alternatives": [],
            "consequence": "durable outbox",
            "authority_ref": "m1-test",
        },
        "confidence": 0.9,
        "confidence_basis": "integration test",
    }
    result = BrainStewardService(ledger, pgvector_store=store).candidate_create(
        source_span=source_span,
    )
    memory_id = str(result["memory_id"])
    try:
        stored = store.get_card(memory_id)
        assert stored is not None
        assert stored.lifecycle_state == "candidate"
        assert stored.authorization_status == "disabled"
        assert any(job.target_id == memory_id for job in store.list_outbox_jobs())
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id = %s", (memory_id,))
                cur.execute("DELETE FROM memory_cards WHERE memory_id = %s", (memory_id,))


@live_pg
def test_live_outbox_worker_dual_cas_updates_card_and_session_chunk():
    """M2 acceptance: one real worker embeds both supported target tables."""

    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    card_id = f"m2_card_{suffix}"
    chunk_id = f"m2_chunk_{suffix}"
    card = _card(card_id, summary="accepted card receives embedding-only write-back")
    card.lifecycle_state = "human_accepted"
    card.authorization_status = "active"
    chunk = SessionChunk(
        chunk_id=chunk_id,
        session_id_hash=_hash("session-" + suffix),
        project="lbrain-pg-contract",
        provider="codex",
        content_markdown="session chunk receives the same CAS-protected embedding",
        content_hash=_hash("chunk-" + suffix),
        embedding_state="pending",
    )
    try:
        store.upsert_card(card)
        store.insert_chunk(chunk)

        worker = OutboxWorker(
            store=store,
            worker_id="m2-dual-cas-worker",
            embedding_fn=lambda _text: make_dummy_vector(20260905),
        )
        assert worker.run_once() == 2

        stored_card = store.get_card(card_id)
        stored_chunk = store.get_chunk(chunk_id)
        assert stored_card is not None
        assert stored_card.embedding_state == "ready"
        assert stored_card.embedding is not None
        assert stored_card.lifecycle_state == "human_accepted"
        assert stored_card.authorization_status == "active"
        assert stored_card.summary == card.summary
        assert stored_chunk is not None
        assert stored_chunk.embedding_state == "ready"
        assert stored_chunk.embedding is not None
        jobs = store.list_outbox_jobs()
        assert {job.target_id for job in jobs if job.target_id in {card_id, chunk_id}} == {
            card_id,
            chunk_id,
        }
        assert all(
            job.status == "completed"
            for job in jobs
            if job.target_id in {card_id, chunk_id}
        )
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id IN (%s, %s)", (card_id, chunk_id))
                cur.execute("DELETE FROM session_memory_chunks WHERE chunk_id = %s", (chunk_id,))
                cur.execute("DELETE FROM memory_cards WHERE memory_id = %s", (card_id,))


@pytest.mark.parametrize("target_type", ["memory_card", "session_chunk"])
@live_pg
def test_live_dual_cas_stale_hash_is_terminal_noop(target_type: str):
    """M2 acceptance: stale card and chunk jobs never write a vector."""

    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    target_id = f"m2_stale_{target_type}_{suffix}"
    old_hash = _hash("old-" + suffix)
    new_hash = _hash("new-" + suffix)
    try:
        if target_type == "memory_card":
            store.insert_card(
                _card(target_id, summary="card content changed before embedding",)
            )
            with store.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE memory_cards SET content_hash = %s WHERE memory_id = %s",
                        (old_hash, target_id),
                    )
            store.enqueue_outbox(target_type, target_id, old_hash, "old card content")
            with store.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE memory_cards SET content_hash = %s WHERE memory_id = %s",
                        (new_hash, target_id),
                    )
        else:
            store.insert_chunk(
                SessionChunk(
                    chunk_id=target_id,
                    session_id_hash=_hash("session-" + suffix),
                    project="lbrain-pg-contract",
                    content_markdown="new chunk content",
                    content_hash=old_hash,
                    embedding_state="pending",
                )
            )
            with store.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE session_memory_chunks SET content_hash = %s WHERE chunk_id = %s",
                        (new_hash, target_id),
                    )

        worker = OutboxWorker(
            store=store,
            worker_id="m2-stale-worker",
            embedding_fn=lambda _text: make_dummy_vector(17),
        )
        assert worker.run_once() == 1
        job = next(job for job in store.list_outbox_jobs() if job.target_id == target_id)
        assert job.status == "cas_skipped"
        assert job.last_error == "CAS skip: content_hash mismatch"
        if target_type == "memory_card":
            stored = store.get_card(target_id)
        else:
            stored = store.get_chunk(target_id)
        assert stored is not None
        assert stored.embedding is None
        assert stored.embedding_state == "pending"
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id = %s", (target_id,))
                cur.execute("DELETE FROM session_memory_chunks WHERE chunk_id = %s", (target_id,))
                cur.execute("DELETE FROM memory_cards WHERE memory_id = %s", (target_id,))


@live_pg
def test_live_outbox_claims_are_non_overlapping_and_expired_owner_is_fenced():
    """M2 acceptance: SKIP LOCKED partitions work and stale owners cannot finish it."""

    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    card_ids = [f"m2_lease_{suffix}_{index}" for index in range(2)]
    try:
        for card_id in card_ids:
            store.upsert_card(_card(card_id, summary=f"lease test {card_id}"))

        stores = [PgVectorStore(dsn=PG_DSN), PgVectorStore(dsn=PG_DSN)]
        claim_args = [
            (stores[0], "m2-lease-worker-a"),
            (stores[1], "m2-lease-worker-b"),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: args[0].claim_outbox_leases(
                        worker_id=args[1], batch_size=1, lease_seconds=30
                    ),
                    claim_args,
                )
            )
        claimed = [jobs[0] for jobs in results]
        assert {job.target_id for job in claimed} == set(card_ids)
        assert {job.worker_id for job in claimed} == {
            "m2-lease-worker-a",
            "m2-lease-worker-b",
        }

        reclaimed_job = claimed[0]
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE embedding_outbox SET lease_until = NOW() - INTERVAL '1 second' WHERE outbox_id = %s",
                    (reclaimed_job.outbox_id,),
                )
        assert (
            stores[0].renew_outbox_lease(
                reclaimed_job.outbox_id,
                "m2-lease-worker-a",
                lease_seconds=30,
            )
            is False
        )
        with pytest.raises(ValueError, match="lease owner mismatch"):
            stores[0].mark_outbox_completed(
                reclaimed_job.outbox_id,
                worker_id="m2-lease-worker-a",
            )

        recovery_store = PgVectorStore(dsn=PG_DSN)
        recovered = recovery_store.claim_outbox_leases(
            worker_id="m2-lease-recovery",
            batch_size=1,
            lease_seconds=30,
        )
        assert len(recovered) == 1
        assert recovered[0].outbox_id == reclaimed_job.outbox_id
        with pytest.raises(ValueError, match="lease owner mismatch"):
            stores[0].cas_update_embedding(
                reclaimed_job.target_type,
                reclaimed_job.target_id,
                reclaimed_job.content_hash,
                make_dummy_vector(101),
                outbox_id=reclaimed_job.outbox_id,
                worker_id="m2-lease-worker-a",
            )

        recovery_worker = OutboxWorker(
            store=recovery_store,
            worker_id="m2-lease-recovery",
            embedding_fn=lambda _text: make_dummy_vector(2026),
        )
        assert recovery_worker.process_job(recovered[0]) is True
        assert recovery_store.get_outbox_job(reclaimed_job.outbox_id).status == "completed"
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id LIKE %s", (f"m2_lease_{suffix}_%",))
                cur.execute("DELETE FROM memory_cards WHERE memory_id LIKE %s", (f"m2_lease_{suffix}_%",))


@live_pg
def test_live_duplicate_cas_commits_once_and_old_dead_letter_preserves_new_content():
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    target_id = f"m2_duplicate_{uuid.uuid4().hex[:12]}"
    card = _card(target_id)
    try:
        store.upsert_card(card)
        job = store.claim_outbox_leases("same-owner", batch_size=1)[0]

        def finish(_index):
            try:
                return store.cas_update_embedding(
                    job.outbox_id, target_id, job.content_hash,
                    make_dummy_vector(1), worker_id="same-owner",
                )
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(finish, range(2))) == [False, True]
        assert store.get_card(target_id).embedding_revision == 2
        stale_id = store.enqueue_outbox("memory_card", target_id, _hash("old"), "old")
        store.claim_outbox_leases("failure-owner", batch_size=1)
        store.mark_outbox_failed(stale_id, "synthetic", max_retries=1, worker_id="failure-owner")
        assert store.get_outbox_job(stale_id).status == "dead_letter"
        assert store.get_card(target_id).embedding_state == "ready"
    finally:
        with store.transaction() as conn:
            conn.execute("DELETE FROM embedding_outbox WHERE target_id = %s", (target_id,))
            conn.execute("DELETE FROM memory_cards WHERE memory_id = %s", (target_id,))


@live_pg
def test_live_graph_join_and_vector_search_enforce_authority_filters():
    from dataclasses import replace
    from datetime import timedelta

    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    prefix = f"m3_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    base = _card(prefix + "_visible", summary="transaction vector search")
    base.lifecycle_state = "human_accepted"
    base.authorization_status = "active"
    base.embedding = make_dummy_vector(7)
    base.embedding_state = "ready"
    cards = [base,
             replace(base, memory_id=prefix + "_other", project="other-project"),
             replace(base, memory_id=prefix + "_candidate", lifecycle_state="candidate"),
             replace(base, memory_id=prefix + "_disabled", authorization_status="disabled"),
             replace(base, memory_id=prefix + "_stale", currentness="stale"),
             replace(base, memory_id=prefix + "_future", valid_from=now + timedelta(days=1)),
             replace(base, memory_id=prefix + "_past", valid_from=now - timedelta(days=2),
                     valid_to=now - timedelta(days=1))]
    try:
        for card in cards:
            store.insert_card(card)
        ids = [card.memory_id for card in cards]
        joined = store.list_authorized_cards(project=base.project, memory_ids=ids)
        assert [card["memory_id"] for card in joined] == [base.memory_id]
        for text_query in (None, "transaction"):
            results = store.hybrid_search(project=base.project, query_vector=base.embedding,
                                          text_query=text_query)
            assert [row["memory_id"] for row in results] == [base.memory_id]
        assert store.graph_projection_health(base.project)["unprojected"] is True
        with pytest.raises(ValueError, match="as_of"):
            store.hybrid_search(project=base.project, query_vector=base.embedding, as_of="bad-time")
    finally:
        with store.transaction() as conn:
            conn.execute("DELETE FROM memory_cards WHERE memory_id = ANY(%s)", ([c.memory_id for c in cards],))

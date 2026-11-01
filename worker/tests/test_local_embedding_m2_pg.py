"""M2/M3 isolated PostgreSQL/pgvector verification for the local Model2Vec profile.

Requires `LBRAIN_TEST_PG_DSN` (see `conftest.isolated_pg_store`) and the pinned real Model2Vec
snapshot (embeddings come from the real HTTP service through `CanonicalLocalEmbeddingClient`).
No paid embedding endpoint is reachable from these tests.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.local_embedding import (
    LOCAL_EMBEDDING_PROFILE_ID,
    CanonicalLocalEmbeddingClient,
    LocalEmbeddingServiceApplication,
    LocalModel2VecEngine,
    backfill_pg_local_embeddings,
    create_local_embedding_http_server,
    evaluate_cutover_readiness,
    inspect_pg_local_embedding_state,
)
from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker
from agent_knowledge.postgres_store.pgvector_store import MemoryCard, SessionChunk

_MODEL_DIR = Path(
    os.environ.get("NEURON_LOCAL_EMBEDDING_MODEL_DIR")
    or (
        Path.home()
        / ".hermes/profiles/atlas/cache/scratch/model2vec-mac-test-hf/hub/models--minishlab--potion-multilingual-128M/snapshots/73908c3438cf03b6a01bcb9611d62b23d0726f08"
    )
)
LEGACY_DIM = 3072


@pytest.fixture(scope="module")
def local_client():
    if not _MODEL_DIR.is_dir():
        pytest.skip("Pinned real Model2Vec snapshot not present on this host")
    engine = LocalModel2VecEngine.from_model_dir(_MODEL_DIR)
    server = create_local_embedding_http_server(
        application=LocalEmbeddingServiceApplication(engine=engine),
        bind_host="127.0.0.1",
        port=0,
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = CanonicalLocalEmbeddingClient(base_url=f"http://{host}:{port}", use_openai_sdk=True)
    try:
        yield client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _embed(client: CanonicalLocalEmbeddingClient):
    return lambda text: client.embed_document(text).vector


def _card(memory_id: str, summary: str, *, project: str = "m2pg", **kw) -> MemoryCard:
    now = datetime.now(timezone.utc)
    defaults = dict(
        memory_id=memory_id,
        project=project,
        card_type="decision",
        title=f"title {memory_id}",
        summary=summary,
        typed_payload={},
        lifecycle_state="human_accepted",
        authorization_status="active",
        currentness="current",
        content_hash=sha256_hash(summary),
        valid_from=now - timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    defaults.update(kw)
    return MemoryCard(**defaults)


def _chunk(chunk_id: str, text: str, *, project: str = "m2pg") -> SessionChunk:
    return SessionChunk(
        chunk_id=chunk_id,
        session_id_hash=sha256_hash("sess-" + chunk_id),
        project=project,
        provider="test",
        content_markdown=text,
        token_count=len(text.split()),
        content_hash=sha256_hash(text),
        observed_at=datetime.now(timezone.utc),
    )


def _rejected(store, **kwargs) -> bool:
    """CAS write-back is rejected: False (target deleted/changed) or ValueError (lease/fence not owned)."""
    try:
        return store.complete_local_outbox_cas(**kwargs) is False
    except ValueError:
        return True


def _local(store):
    store.embedding_profile = LOCAL_EMBEDDING_PROFILE_ID
    return store


def _legacy_vec(seed: float = 0.01) -> list[float]:
    return [seed] * LEGACY_DIM


def _count(store, sql: str, params=()) -> int:
    with store._scope(write=False) as db:
        with db.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(next(iter(row.values())) if isinstance(row, dict) else row[0])


def _run_all(store, client, *, worker_id="w-main") -> int:
    worker = LocalEmbeddingOutboxWorker(
        store=store, worker_id=worker_id, embedding_fn=_embed(client), batch_size=16
    )
    total = 0
    for _ in range(10):
        n = worker.run_once()
        if not n:
            break
        total += n
    return total


def test_profile_separation_preserves_legacy_gemini_vector(isolated_pg_store, local_client):
    store = isolated_pg_store
    legacy = _card("pg-legacy-1", "기존 Gemini 3072차원 벡터가 있는 카드", embedding=_legacy_vec(), embedding_state="ready")
    store.upsert_card(legacy)
    before = store.get_card("pg-legacy-1")
    assert before is not None and before.embedding is not None and len(before.embedding) == LEGACY_DIM

    _local(store)
    store.enqueue_local_outbox("memory_card", "pg-legacy-1", legacy.content_hash, legacy.summary)
    assert _run_all(store, local_client) == 1

    row = store.get_local_embedding_row(target_type="memory_card", target_id="pg-legacy-1")
    assert row is not None and row["state"] == "ready" and len(row["vector"]) == 256
    after = store.get_card("pg-legacy-1")
    assert after is not None and after.embedding is not None
    assert len(after.embedding) == LEGACY_DIM
    assert list(after.embedding) == list(before.embedding)
    assert after.embedding_state == "ready"


def test_local_search_never_mixes_dimensions_or_scopes(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    cards = [
        _card("pg-s-active", "Mac mini 로컬 Model2Vec 임베딩 서비스로 Gemini 비용을 제거한다"),
        _card("pg-s-disabled", "Mac mini 로컬 Model2Vec 임베딩 비활성 후보", authorization_status="disabled", lifecycle_state="candidate"),
        _card("pg-s-superseded", "Mac mini 로컬 Model2Vec 임베딩 구버전 결정", currentness="superseded"),
        _card("pg-s-other-project", "Mac mini 로컬 Model2Vec 임베딩 타 프로젝트", project="someone-else"),
        _card(
            "pg-s-future",
            "Mac mini 로컬 Model2Vec 임베딩 미래 유효 카드",
            valid_from=datetime.now(timezone.utc) + timedelta(days=30),
        ),
    ]
    for c in cards:
        store.upsert_card(c)
    _run_all(store, local_client)

    qv = local_client.embed_document("Mac mini 로컬 Model2Vec 임베딩").vector
    hits = store.hybrid_search_local(project="m2pg", query_vector=qv, limit=10)
    ids = [h["memory_id"] for h in hits]
    assert ids == ["pg-s-active"], ids

    with pytest.raises(ValueError):
        store.hybrid_search_local(project="m2pg", query_vector=[0.1] * LEGACY_DIM, limit=5)


def test_stale_result_blocked_after_source_change(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    cand = dict(lifecycle_state="candidate", authorization_status="disabled")
    card = _card("pg-stale-1", "첫 번째 버전 본문", **cand)
    store.upsert_card(card)
    stale_jobs = store.claim_local_outbox_leases("w-old", batch_size=4, lease_seconds=30)
    assert len(stale_jobs) == 1

    changed = _card("pg-stale-1", "두 번째 버전 본문 (원본 변경)", **cand)
    store.upsert_card(changed)

    stale = stale_jobs[0]
    assert _rejected(
        store,
        outbox_id=stale.outbox_id,
        worker_id="w-old",
        fencing_token=stale.fencing_token,
        vector=local_client.embed_document(stale.payload_text).vector,
    )
    assert _count(
        store,
        "SELECT COUNT(*) FROM card_local_embeddings WHERE memory_id=%s AND state='ready' AND source_content_hash=%s",
        ("pg-stale-1", card.content_hash),
    ) == 0

    _run_all(store, local_client, worker_id="w-new")
    row = store.get_local_embedding_row(target_type="memory_card", target_id="pg-stale-1")
    assert row is not None and row["source_content_hash"] == changed.content_hash


def test_lease_expiry_fences_old_worker(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    store.upsert_card(_card("pg-lease-1", "lease fencing 검증 카드"))
    (job_a,) = store.claim_local_outbox_leases("w-a", batch_size=1, lease_seconds=1)
    time.sleep(1.3)
    (job_b,) = store.claim_local_outbox_leases("w-b", batch_size=1, lease_seconds=30)
    assert job_b.outbox_id == job_a.outbox_id
    assert job_b.fencing_token > job_a.fencing_token

    vec = local_client.embed_document(job_a.payload_text).vector
    assert _rejected(
        store,
        outbox_id=job_a.outbox_id, worker_id="w-a", fencing_token=job_a.fencing_token, vector=vec
    )
    assert store.complete_local_outbox_cas(
        outbox_id=job_b.outbox_id, worker_id="w-b", fencing_token=job_b.fencing_token, vector=vec
    )
    assert _rejected(
        store,
        outbox_id=job_b.outbox_id, worker_id="w-b", fencing_token=job_b.fencing_token, vector=vec
    )


def test_delete_then_recreate_blocks_zombie_vector(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    card = _card("pg-zombie-1", "삭제 후 재생성될 카드")
    store.upsert_card(card)
    (job,) = store.claim_local_outbox_leases("w-z", batch_size=1, lease_seconds=30)

    store.delete_card("pg-zombie-1")
    assert store.get_card("pg-zombie-1") is None
    vec = local_client.embed_document(job.payload_text).vector
    assert _rejected(
        store,
        outbox_id=job.outbox_id, worker_id="w-z", fencing_token=job.fencing_token, vector=vec
    )
    assert _count(store, "SELECT COUNT(*) FROM card_local_embeddings WHERE memory_id=%s", ("pg-zombie-1",)) == 0

    store.upsert_card(_card("pg-zombie-1", "삭제 후 재생성될 카드"))
    jobs = store.list_local_outbox_jobs(status="queued")
    recreated = [j for j in jobs if j.target_id == "pg-zombie-1"]
    assert len(recreated) == 1 and recreated[0].incarnation > job.incarnation
    assert _rejected(
        store,
        outbox_id=job.outbox_id, worker_id="w-z", fencing_token=job.fencing_token, vector=vec
    )
    _run_all(store, local_client, worker_id="w-z2")
    row = store.get_local_embedding_row(target_type="memory_card", target_id="pg-zombie-1")
    assert row is not None and row["state"] == "ready"


def test_concurrent_workers_claim_disjoint_jobs(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    for i in range(12):
        store.upsert_card(_card(f"pg-conc-{i}", f"동시 처리 카드 {i}"))
    results: dict[str, list[int]] = {}

    def claim(name: str):
        results[name] = [j.outbox_id for j in store.claim_local_outbox_leases(name, batch_size=6, lease_seconds=30)]

    threads = [threading.Thread(target=claim, args=(f"w-{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [i for ids in results.values() for i in ids]
    assert len(all_ids) == len(set(all_ids)) == 12


def test_dead_letter_requeue_is_bounded_to_transient_errors(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    store.upsert_card(_card("pg-dl-transient", "일시 장애 카드"))
    store.upsert_card(_card("pg-dl-terminal", "영구 오류 카드"))
    jobs = {j.target_id: j for j in store.claim_local_outbox_leases("w-dl", batch_size=4, lease_seconds=30)}
    for tid, code in (("pg-dl-transient", "retryable_error:transport_unavailable"), ("pg-dl-terminal", "terminal_error:ValueError")):
        j = jobs[tid]
        state = store.mark_local_outbox_failed(
            outbox_id=j.outbox_id, worker_id="w-dl", fencing_token=j.fencing_token,
            error_code=code, retryable=False, max_retries=1,
        )
        assert state == "dead_letter"
    assert store.requeue_local_dead_letters() == 1
    assert store.requeue_local_dead_letters() == 0  # idempotent
    dead = {j.target_id for j in store.list_local_outbox_jobs(status="dead_letter")}
    queued = {j.target_id for j in store.list_local_outbox_jobs(status="queued")}
    assert dead == {"pg-dl-terminal"}
    assert "pg-dl-transient" in queued
    _run_all(store, local_client, worker_id="w-dl2")
    assert store.get_local_embedding_row(target_type="memory_card", target_id="pg-dl-transient")["state"] == "ready"


def test_session_chunks_local_profile_and_legacy_column_untouched(isolated_pg_store, local_client):
    store = _local(isolated_pg_store)
    text = "세션 메모리에서 Model2Vec 256차원 로컬 임베딩 검색이 동작한다."
    chunk_id, outbox_id = store.insert_chunk_with_local_outbox(_chunk("pg-chunk-1", text))
    assert chunk_id == "pg-chunk-1" and outbox_id > 0
    other_id, _ = store.insert_chunk_with_local_outbox(_chunk("pg-chunk-2", "전혀 다른 주제: 요리 레시피와 파스타 삶는 시간", project="m2pg"))
    _run_all(store, local_client)

    qv = local_client.embed_document("세션 메모리 로컬 임베딩 검색").vector
    hits = store.search_session_chunks_local(qv, project="m2pg", limit=2)
    assert hits and hits[0]["chunk_id"] == "pg-chunk-1"
    assert store.search_session_chunks_local(qv, project="other-project", limit=5) == []
    row = store.get_chunk("pg-chunk-1")
    assert row is not None and row.embedding is None  # legacy 3072 column never written by local profile


def test_backfill_is_bounded_resumable_and_idempotent(isolated_pg_store, local_client):
    legacy_store = isolated_pg_store  # legacy profile: cards exist without local derived rows
    for i in range(7):
        legacy_store.upsert_card(
            _card(f"pg-bf-{i}", f"백필 대상 카드 {i}", embedding=_legacy_vec(0.02 + i / 1000), embedding_state="ready")
        )
    store = _local(legacy_store)

    before = inspect_pg_local_embedding_state(store)
    assert before.eligible_cards == 7 and before.missing_cards == 7
    assert evaluate_cutover_readiness(pg_progress=before).ready is False

    first = backfill_pg_local_embeddings(
        store, _embed(local_client), batch_size=3, max_batches=1, enqueue_limit_per_kind=7
    )
    assert first.completed == 3
    mid = inspect_pg_local_embedding_state(store)
    assert 0 < mid.ready_cards < 7

    second = backfill_pg_local_embeddings(
        store, _embed(local_client), batch_size=3, max_batches=5, enqueue_limit_per_kind=7
    )
    assert second.enqueued_cards == 0  # already enqueued: idempotent
    done = inspect_pg_local_embedding_state(store)
    assert done.ready_cards == 7 and done.missing_cards == 0
    assert evaluate_cutover_readiness(pg_progress=done).ready is True

    third = backfill_pg_local_embeddings(store, _embed(local_client), enqueue_limit_per_kind=7)
    assert third.claimed == 0 and third.enqueued_cards == 0

    # legacy vectors are untouched
    for i in range(7):
        c = store.get_card(f"pg-bf-{i}")
        assert c is not None and c.embedding is not None and len(c.embedding) == LEGACY_DIM
    assert done.legacy_card_embeddings_preserved == 7


def test_backfill_skips_unauthorized_and_deleted_cards(isolated_pg_store, local_client):
    store = isolated_pg_store
    store.upsert_card(_card("pg-bfx-ok", "정상 카드"))
    store.upsert_card(_card("pg-bfx-disabled", "비활성 카드", authorization_status="disabled", lifecycle_state="candidate"))
    store.upsert_card(_card("pg-bfx-deleted", "삭제될 카드"))
    _local(store)
    store.delete_card("pg-bfx-deleted")
    backfill_pg_local_embeddings(store, _embed(local_client), enqueue_limit_per_kind=20)
    assert store.get_local_embedding_row(target_type="memory_card", target_id="pg-bfx-ok") is not None
    assert store.get_local_embedding_row(target_type="memory_card", target_id="pg-bfx-disabled") is None
    assert store.get_local_embedding_row(target_type="memory_card", target_id="pg-bfx-deleted") is None

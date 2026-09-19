"""Tier 5 Adversarial Coverage & Security Hardening Test Suite.

Extends E2E verification with extreme adversarial scenarios:
1. Malicious wire payloads & injection attempts.
2. Progressive truncation under extreme memory/payload stress.
3. High-concurrency CAS race conditions across 10 mutating threads & 5 workers.
4. Deep cyclic & diamond DAG provenance security under temporal filtering.
5. High-scale Qdrant to PostgreSQL migration with dual-read shadow verification.
6. Absolute candidate proposal isolation against context leakage.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import uuid
import pytest
from qdrant_client import QdrantClient, models

from agent_knowledge.ledger import Ledger
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.mcp_tools import (
    list_public_agent_tools,
    list_admin_tools,
    BRAIN_RESOLVE_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
)
from agent_knowledge.mcp_jsonrpc import (
    handle_jsonrpc_message,
    handle_admin_jsonrpc_message,
)
from agent_knowledge.llm_brain_core.slim_serializer import (
    SlimSerializer,
)
from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    MemoryEdge,
    SessionChunk,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.outbox_worker import (
    OutboxWorker,
    generate_deterministic_embedding,
)
from agent_knowledge.postgres_store.migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
)
from agent_knowledge.postgres_store.dual_read_shadow import (
    DualReadShadowHarness,
)

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
live_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)


def _service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    ledger = Ledger(private / "ledger.sqlite")
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


# ==============================================================================
# 1. Adversarial Security & JSON-RPC Injection Guard
# ==============================================================================

def test_tier5_adversarial_admin_tool_disguise_injection(tmp_path: Path):
    """Attempts to invoke admin tools with disguised method names and payloads fail-closed."""
    service = _service(tmp_path)
    malicious_methods = [
        "memory_candidate_approve",
        "agent_memory_admin.memory_candidate_approve",
        "../agent_memory_admin/memory_candidate_approve",
        "brain_permission_sensitive_audit_probe",
        "brain_corpus_ingest_plan",
        "brain_object_decision_commit",
        "memory_supersede_commit",
        "__proto__",
        "constructor",
    ]

    for method in malicious_methods:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": method, "arguments": {"memory_id": "target_123"}},
        }
        res = handle_jsonrpc_message(req, service, surface="agent")
        assert "error" in res, f"Method {method} should be rejected on agent endpoint"
        assert res["error"]["code"] in (-32601, -32600, -32602)


def test_tier5_adversarial_proposal_tampering_isolation(tmp_path: Path):
    """Agent cannot inject active authorization_status or approved lifecycle into candidate creation."""
    service = _service(tmp_path)

    req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Malicious Bypass Attempt",
                "summary": "Attempting to write authoritative active state directly",
                "typed_payload": {
                    "decision": "Bypass security",
                    "rationale": "testing",
                    "alternatives": ["none"],
                    "consequence": "security test",
                    "authority_ref": "sec_test",
                },
                "content_hash": "sha256:" + "a" * 64,
                "source_ref": {"source_id": "malicious_actor"},
                "span_ref": {"span_id": "span_1"},
                "proposer": "codex",
                "authorization_status": "active",  # Malicious override
                "lifecycle_state": "human_accepted",  # Malicious override
            },
        },
    }
    res = handle_jsonrpc_message(req, service, surface="agent")
    assert "result" in res
    result_data = res["result"]["structuredContent"]

    # Even if attacker passed active/human_accepted, system MUST force candidate/disabled
    assert result_data["authorization_status"] == "disabled"
    assert result_data["lifecycle_state"] == "candidate"
    assert result_data["proposal_write_performed"] is True



# ==============================================================================
# 2. Extreme Wire Budget & Truncation Resilience
# ==============================================================================

def test_tier5_adversarial_extreme_payload_truncation():
    """Enforces <= 3.0 KB hard limit even when given 100 large cards with 10 KB each."""
    huge_cards = [
        {
            "memory_id": f"huge_card_{i}",
            "card_type": "decision",
            "title": f"Huge Decision {i} " * 20,
            "summary": f"Very long summary text exceeding normal budgets {i} " * 50,
            "typed_payload": {
                "decision": f"Huge decision text {i} " * 20,
                "rationale": f"Rationale {i} " * 20,
            },
            "currentness": "current",
            "confidence": 1.0,
            "content_hash": f"sha256:{i:064d}",
        }
        for i in range(100)
    ]

    serialized = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=huge_cards[:50],
        preferences=huge_cards[50:],
        recent_context="Long recent context " * 50,
        guardrails=["Guardrail " * 20 for _ in range(20)],
    )

    raw_json = json.dumps(serialized, ensure_ascii=False)
    payload_size = len(raw_json.encode("utf-8"))

    # Hard ceiling strictly enforced <= 3072 bytes (3.0 KB)
    assert payload_size <= 3072, f"Payload {payload_size} bytes exceeded hard ceiling 3072 bytes"
    assert serialized["schema_version"] == "lbrain_slim_context.v1"



# ==============================================================================
# 3. High-Concurrency CAS Race Stress (10 Mutating Threads vs 5 Workers)
# ==============================================================================

def _mutate_card(store, target_id, content_hash):
    card = store.get_card(target_id)
    assert card is not None, f"missing mutation target: {target_id}"
    card.content_hash = content_hash
    card.summary = f"Revision {content_hash}"
    card.embedding_state = "pending"
    card.embedding = None
    card.updated_at = datetime.now(timezone.utc)
    store.upsert_card(card)


@live_pg
def test_tier5_mutator_ready_revision_enqueues_new_job(isolated_pg_store):
    store = isolated_pg_store
    store.upsert_card(MemoryCard(
        memory_id="revision", project="test", card_type="decision", title="old",
        summary="old", content_hash="sha256:old", embedding_state="ready",
        embedding=make_dummy_vector(1),
    ))
    assert store.list_outbox_jobs() == []
    _mutate_card(store, "revision", "sha256:new")
    jobs = store.list_outbox_jobs()
    assert len(jobs) == 1, "every changed ready revision must enqueue an embedding job"
    assert (jobs[0].target_id, jobs[0].content_hash, jobs[0].status) == ("revision", "sha256:new", "queued")
    card = store.get_card("revision")
    assert card.embedding_state == "pending"
    assert card.embedding is None


@live_pg
@pytest.mark.parametrize("participant", ["worker", "mutator"])
def test_tier5_stress_propagates_participant_exception(isolated_pg_store, monkeypatch, participant):
    def fail(*args, **kwargs):
        raise RuntimeError(f"injected {participant} failure")

    if participant == "worker":
        monkeypatch.setattr(OutboxWorker, "run_once", fail)
    else:
        monkeypatch.setitem(globals(), "_mutate_card", fail)
    with pytest.raises(RuntimeError, match=f"injected {participant} failure"):
        test_tier5_adversarial_high_concurrency_cas_stress(isolated_pg_store)


@live_pg
@pytest.mark.parametrize("max_retries", [1, 5], ids=["dead_letter", "failed"])
def test_tier5_stress_rejects_embedding_failure(isolated_pg_store, monkeypatch, max_retries):
    def fail_embedding(text):
        raise RuntimeError("injected embedding failure")

    original_init = OutboxWorker.__init__

    def init(worker, *args, **kwargs):
        original_init(worker, *args, **kwargs, max_retries=max_retries)

    monkeypatch.setattr(OutboxWorker, "__init__", init)
    monkeypatch.setitem(globals(), "generate_deterministic_embedding", fail_embedding)
    with pytest.raises(AssertionError, match="unsuccessful outbox"):
        test_tier5_adversarial_high_concurrency_cas_stress(isolated_pg_store)


@live_pg
def test_tier5_stress_rejects_zero_worker_progress(isolated_pg_store, monkeypatch):
    monkeypatch.setattr(OutboxWorker, "run_once", lambda self: 0)
    with pytest.raises(AssertionError, match="workers made no progress"):
        test_tier5_adversarial_high_concurrency_cas_stress(isolated_pg_store)


@live_pg
def test_tier5_adversarial_high_concurrency_cas_stress(isolated_pg_store):
    """10 mutator threads updating cards concurrently with 5 outbox worker threads."""
    store = isolated_pg_store

    tag = f"t5_stress_{uuid.uuid4().hex[:8]}"

    # Create 20 initial cards
    for i in range(20):
        store.upsert_card(
            MemoryCard(
                memory_id=f"{tag}_card_{i}",
                project="neurons",
                card_type="decision",
                title=f"Initial Title {i}",
                summary=f"Initial Summary {i}",
                content_hash=f"sha256:{hashlib.sha256(f'{tag}_init_{i}'.encode()).hexdigest()}",
                embedding_state="pending",
            )
        )

    # 5 worker threads with injected deterministic embedding provider
    workers = [
        OutboxWorker(
            store,
            worker_id=f"{tag}_worker_{w}",
            batch_size=4,
            lease_seconds=5,
            poll_interval_seconds=0.05,
            embedding_fn=generate_deterministic_embedding,
        )
        for w in range(5)
    ]

    # Five synchronized rounds exercise all 15 participants without timing sleeps.
    rounds = threading.Barrier(15, timeout=20)

    def worker_work(worker):
        processed = 0
        for _ in range(5):
            rounds.wait()
            processed += worker.run_once()
            rounds.wait()
        return processed

    def mutator_work(thread_idx):
        mutations = 0
        for step in range(5):
            rounds.wait()
            target_id = f"{tag}_card_{(thread_idx + step) % 20}"
            _mutate_card(store, target_id, f"sha256:{hashlib.sha256(f'{tag}_mutated_{thread_idx}_{step}'.encode()).hexdigest()}")
            mutations += 1
            rounds.wait()
        return mutations

    def guarded(fn, arg):
        try:
            return fn(arg)
        except BaseException:
            rounds.abort()
            raise

    errors = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        worker_futures = [pool.submit(guarded, worker_work, w) for w in workers]
        mutator_futures = [pool.submit(guarded, mutator_work, m) for m in range(10)]
        for future in as_completed(worker_futures + mutator_futures, timeout=90):
            try:
                future.result()
            except BaseException as exc:
                errors.append(exc)
    if errors:
        # Barrier cancellation is secondary: surface the actual participant failure.
        raise next((exc for exc in errors if not isinstance(exc, threading.BrokenBarrierError)), errors[0])
    assert [future.result() for future in mutator_futures] == [5] * 10
    assert all(future.result() > 0 for future in worker_futures), "workers made no progress"

    # Each committed revision owns one job, including all 50 mutations.
    jobs = store.list_outbox_jobs()
    assert len(jobs) == 70, "expected 20 initial + 50 mutated revision jobs"
    expected_revisions = {
        (f"{tag}_card_{i}", f"sha256:{hashlib.sha256(f'{tag}_init_{i}'.encode()).hexdigest()}")
        for i in range(20)
    } | {
        (f"{tag}_card_{(m + step) % 20}", f"sha256:{hashlib.sha256(f'{tag}_mutated_{m}_{step}'.encode()).hexdigest()}")
        for m in range(10) for step in range(5)
    }
    assert {(job.target_id, job.content_hash) for job in jobs} == expected_revisions

    # Participants have stopped: finish only this fixture's bounded queue.
    for _ in range(70):
        if workers[0].run_once() == 0:
            break
    jobs = store.list_outbox_jobs()
    unsuccessful = [(job.outbox_id, job.status, job.last_error) for job in jobs
                    if job.status not in {"completed", "cas_skipped"}]
    assert not unsuccessful, f"unsuccessful outbox: {unsuccessful}"
    assert all(job.retry_count == 0 for job in jobs)

    for i in range(20):
        card = store.get_card(f"{tag}_card_{i}")
        assert card is not None
        assert card.embedding_state == "ready"
        assert card.embedding is not None and len(card.embedding) == 3072
        current_jobs = [job for job in jobs if job.target_id == card.memory_id
                        and job.content_hash == card.content_hash]
        assert len(current_jobs) == 1
        assert current_jobs[0].status == "completed"
        assert current_jobs[0].last_error is None
        assert card.embedding == pytest.approx(
            generate_deterministic_embedding(current_jobs[0].payload_text), abs=1e-4,
        )


# ==============================================================================
# 4. Multi-Hop Cycle & Diamond DAG Provenance Traversal
# ==============================================================================

@live_pg
def test_tier5_adversarial_deep_cyclic_provenance_dag():
    """Traverses complex multi-hop DAG with diamond convergence, cycles, and temporal filters."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()

    tag = f"t5_dag_{uuid.uuid4().hex[:8]}"
    node_map = {n: f"{tag}_{n}" for n in ["R", "A1", "A2", "B", "C", "D", "E"]}

    try:
        # Create nodes: R -> A1, R -> A2; A1 -> B, A2 -> B (diamond); B -> C -> D -> E -> R (cycle!)
        for n, nid in node_map.items():
            store.insert_card(
                MemoryCard(
                    memory_id=nid,
                    project="neurons",
                    card_type="decision",
                    title=f"Node {n}",
                    summary=f"Summary {n}",
                    content_hash=f"sha256:{hashlib.sha256(nid.encode()).hexdigest()}",
                    lifecycle_state="human_accepted",
                    authorization_status="active",
                    currentness="current",
                    valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                )
            )

        edges = [
            ("R", "A1", "derived_from", datetime(2026, 8, 1, tzinfo=timezone.utc)),
            ("R", "A2", "derived_from", datetime(2026, 8, 1, tzinfo=timezone.utc)),
            ("A1", "B", "supports", datetime(2026, 8, 5, tzinfo=timezone.utc)),
            ("A2", "B", "supports", datetime(2026, 8, 5, tzinfo=timezone.utc)),
            ("B", "C", "supersedes", datetime(2026, 8, 10, tzinfo=timezone.utc)),
            ("C", "D", "derived_from", datetime(2026, 8, 15, tzinfo=timezone.utc)),
            ("D", "E", "derived_from", datetime(2026, 8, 20, tzinfo=timezone.utc)),
            ("E", "R", "contradicts", datetime(2026, 8, 25, tzinfo=timezone.utc)),  # Cycle!
        ]

        for src, dst, rel, valid_from in edges:
            src_id = node_map[src]
            dst_id = node_map[dst]
            store.insert_edge(
                MemoryEdge(
                    src_id=src_id,
                    dst_id=dst_id,
                    rel_type=rel,
                    valid_from=valid_from,
                    provenance_hash=f"sha256:{hashlib.sha256(f'{src_id}_{dst_id}'.encode()).hexdigest()}",
                )
            )

        # 1. Full traversal from root R (max_depth=5)
        results = store.traverse_provenance_dag(root_memory_id=node_map["R"], max_depth=5)
        assert len(results) > 0
        assert max(r["depth"] for r in results) <= 5

        # 2. Point-in-time traversal as of 2026-08-08 (should only include R->A1, R->A2, A1->B, A2->B)
        pit_results = store.traverse_provenance_dag(
            root_memory_id=node_map["R"],
            max_depth=5,
            as_of="2026-08-08T00:00:00Z",
        )
        pit_dsts = {r["dst_id"] for r in pit_results}
        assert node_map["A1"] in pit_dsts
        assert node_map["A2"] in pit_dsts
        assert node_map["B"] in pit_dsts
        assert node_map["C"] not in pit_dsts  # Created after 2026-08-08
        assert node_map["D"] not in pit_dsts
    finally:
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_edges WHERE src_id LIKE %s OR dst_id LIKE %s", (f"{tag}%", f"{tag}%"))
                cur.execute("DELETE FROM graph_projection_outbox WHERE source_id LIKE %s", (f"{tag}%",))
                cur.execute("DELETE FROM memory_cards WHERE memory_id LIKE %s", (f"{tag}%",))


# ==============================================================================
# 5. Full End-to-End Migration & Dual-Read Cutover Verification
# ==============================================================================

@live_pg
def test_tier5_adversarial_full_backfill_and_dual_read_cutover():
    """Simulates full zero-downtime cutover: migrate 100 items and verify Recall@5 >= 0.95."""
    project = f"t5_cutover_{uuid.uuid4().hex[:8]}"

    source_qdrant = QdrantClient(":memory:")
    source_qdrant.create_collection(
        "session_chunks",
        vectors_config=models.VectorParams(size=3072, distance=models.Distance.COSINE),
    )
    source_qdrant.create_collection(
        "memory_cards",
        vectors_config=models.VectorParams(size=3072, distance=models.Distance.COSINE),
    )

    points = []
    for i in range(100):
        vec = make_dummy_vector(i * 7)
        card_id = f"{project}_card_{i}"
        payload = {
            "memory_id": card_id,
            "project": project,
            "card_type": "decision" if i % 2 == 0 else "preference",
            "title": f"Migrated Knowledge {i}",
            "summary": f"Summary for knowledge point {i}",
            "typed_payload": {"idx": i},
            "content_hash": f"sha256:{hashlib.sha256(card_id.encode()).hexdigest()}",
            "lifecycle_state": "human_accepted",
            "authorization_status": "active",
            "currentness": "current",
            "confidence": 0.95,
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": None,
            "source_ref": [f"ref_{i}"],
            "embedding_model": "gemini-embedding-2",
        }
        points.append(models.PointStruct(id=i + 1, vector=vec, payload=payload))
    source_qdrant.upsert("memory_cards", points)

    pg_target = PgVectorStore(dsn=PG_DSN)
    pg_target.execute_ddl()

    try:
        # Step 1: Run Backfill Migration
        migrator = QdrantToPostgresMigrator(
            qdrant_client=source_qdrant,
            target_store=pg_target,
            batch_size=25,
        )
        summary = migrator.run_full_migration(project=project)
        assert summary.total_migrated == 100
        migrated_cards = pg_target.list_authorized_cards(project=project)
        assert len(migrated_cards) == 100

        # Step 2: Run Dual-Read Shadow Benchmark
        harness = DualReadShadowHarness(
            qdrant_client=source_qdrant,
            pg_store=pg_target,
            default_limit=5,
            recall_gate_threshold=0.95,
            p95_latency_gate_ms=20.0,
            evidence_class="test_harness",
        )
        benchmark_queries = [make_dummy_vector(q * 11) for q in range(50)]
        bench_res = harness.run_benchmark(benchmark_queries, project=project)

        assert bench_res.mean_recall_at_k >= 0.95
        assert bench_res.recall_gate_passed is True
        assert bench_res.p95_pgvector_latency_ms <= 20.0
        assert bench_res.overall_gate_passed is False
        assert "test_harness_not_cutover_evidence" in bench_res.cutover_blockers

        # Step 3: Verify Cutover Search on Target Store
        resolved = pg_target.hybrid_search(
            project=project,
            query_vector=make_dummy_vector(0),
            limit=5,
        )
        assert len(resolved) == 5
        assert resolved[0]["similarity_score"] >= 0.99
    finally:
        with pg_target.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM embedding_outbox WHERE target_id LIKE %s", (f"{project}%",))
                cur.execute("DELETE FROM graph_projection_outbox WHERE source_id LIKE %s", (f"{project}%",))
                cur.execute("DELETE FROM memory_cards WHERE project = %s", (project,))

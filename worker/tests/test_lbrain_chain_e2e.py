"""Isolated-PG full chain acceptance: session chunk -> candidate -> approval -> embedding -> recall.

This is the local product-acceptance gate for the LBrain work-memory chain:

1. a redacted session chunk is mined into a ``needs_review`` steward proposal;
2. the candidate stays invisible to public authority reads until approval;
3. the restricted approval commits accepted+current authority and enqueues the
   embedding outbox job in the same transaction;
4. the outbox worker consumes the job with a deterministic test-only embedder;
5. a fresh store+resolver instance ("new session") recalls the memory in list
   and query modes, and other projects cannot see it.

Multi-chunk sessions must yield one candidate per chunk (batch mining maps each
item back to its own chunk), and every approved memory must be recallable.

No model APIs and no Neo4j: the miner completion and the embedding provider are
deterministic local test doubles; PostgreSQL is the disposable isolated instance
from ``LBRAIN_TEST_PG_DSN``.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.postgres_store.outbox_worker import (
    OutboxWorker,
    generate_deterministic_embedding,
)
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore, VECTOR_DIMENSION
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.session_memory.candidate_intake_cli import (
    CandidateIntakeSettings,
    run_candidate_intake,
)


class _NoSqliteLedger:
    """The SQLite card lane must never be touched; PostgreSQL is the authority."""

    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def _chunk(project: str, marker: str = "1") -> dict:
    body = f"Decided: keep PostgreSQL as the authority store for LBrain work memory (session {marker})."
    return {
        "content": body,
        "content_hash": "sha256:" + hashlib.sha256((project + "|" + marker + "|" + body).encode("utf-8")).hexdigest(),
        "metadata": {
            "knowledge_id": f"chain-e2e-session-{marker}",
            "project": project,
            "provider": "codex",
        },
    }


def _completion(messages) -> str:
    content = str((messages or [{}])[-1].get("content") or "")
    count = max(1, content.count("### SESSION "))
    return json.dumps(
        [
            {
                "card_type": "decision",
                "title": f"Authority store {index + 1}",
                "statement": f"PostgreSQL is the authority store for LBrain work memory (item {index + 1}).",
                "typed_payload": {
                    "decision": f"Use PostgreSQL as the authority store (item {index + 1})",
                    "rationale": "One authority store keeps recall deterministic",
                    "alternatives": [],
                    "consequence": "The graph stays a derived index",
                    "authority_ref": "chain-e2e-design-note",
                },
            }
            for index in range(count)
        ]
    )


def _collect_authority_items(
    resolver: GraphFirstResolver,
    *,
    project: str,
    mode: str = "list",
    query: str = "",
    expected: int = 1,
    timeout: float = 15.0,
) -> list:
    """Collect all authority items, across wire-budget pages, for a fresh session.

    Two local-environment protections, both product-invisible on CI:

    * the public resolver trims each response to a ~3 KB wire budget and returns
      ``next_cursor`` for continuation, so all pages must be collected;
    * fresh approvals stamp ``valid_from`` from the database clock and a local
      Docker Desktop PostgreSQL can run its clock slightly ahead of the host,
      so reads are retried in a bounded window until the expected cards appear.
      A card that never appears still fails.
    """
    deadline = time.monotonic() + timeout
    while True:
        collected: list = []
        cursor = None
        for _ in range(10):
            result = resolver.resolve(project=project, mode=mode, query=query, limit=5, cursor=cursor)
            collected.extend(result.get("items", []))
            cursor = result.get("next_cursor")
            if not cursor or len(collected) >= expected:
                break
        if len(collected) >= expected or time.monotonic() >= deadline:
            return collected
        time.sleep(0.25)


def test_session_chunk_becomes_recallable_memory(isolated_pg_store):
    store = isolated_pg_store
    project = "chain-e2e-" + uuid.uuid4().hex[:10]
    steward = BrainStewardService(_NoSqliteLedger(), pgvector_store=store, allow_restricted=True)

    # 1. session chunk -> needs_review proposal (no authority yet)
    report, _state = run_candidate_intake(
        settings=CandidateIntakeSettings(project=project, enable_proposals=True),
        chunks=[_chunk(project)],
        completion_fn=_completion,
        steward=steward,
        dry_run=False,
        checkpoint={"version": 1, "projects": {}},
    )
    assert report.status == "completed"
    assert report.proposed_count == 1
    assert report.duplicate_count == 0
    assert report.skipped_count == 0

    queue = steward.review_queue_list(project=project)
    assert queue["count"] == 1
    candidate_id = queue["items"][0]["memory_id"]

    # The candidate is not authoritative: public reads exclude it.
    assert GraphFirstResolver(store).resolve(project=project, mode="list")["items"] == []
    assert (
        GraphFirstResolver(store, None, generate_deterministic_embedding)
        .resolve(project=project, mode="query", query="PostgreSQL authority store")["items"]
        == []
    )

    # 2. restricted approval -> accepted+current authority, audit, embedding enqueued
    steward.candidate_approve(
        candidate_memory_id=candidate_id,
        approved_by="chain-e2e-operator",
        decision_id="chain-e2e-approve-1",
    )
    approved = store.get_card(candidate_id)
    assert approved is not None
    assert approved.authorization_status == "active"
    assert approved.lifecycle_state in ("accepted", "human_accepted")
    assert store.get_steward_decision("chain-e2e-approve-1")["memory_id"] == candidate_id
    assert steward.review_queue_list(project=project)["count"] == 0

    queued = [job for job in store.list_outbox_jobs() if job.target_id == candidate_id]
    assert len(queued) == 1
    assert queued[0].status == "queued"

    # 3. embedding consumption with the test-only deterministic provider
    worker = OutboxWorker(
        store=store, worker_id="chain-e2e-worker", embedding_fn=generate_deterministic_embedding
    )
    assert worker.run_once() == 1
    assert worker.run_once() == 0

    ready = store.get_card(candidate_id)
    assert ready.embedding_state == "ready"
    assert ready.embedding is not None
    assert len(ready.embedding) == VECTOR_DIMENSION

    # 4. a fresh store+resolver ("new session") recalls the memory
    fresh_store = PgVectorStore(dsn=store.dsn)
    fresh = GraphFirstResolver(fresh_store, None, generate_deterministic_embedding)
    listed = _collect_authority_items(fresh, project=project, expected=1)
    assert [item["id"] for item in listed] == [candidate_id]

    recalled = fresh.resolve(project=project, mode="query", query="PostgreSQL authority store")
    assert [item["id"] for item in recalled["items"]] == [candidate_id]
    assert "PostgreSQL" in recalled["decisions"][0]["decision"]

    context = GraphFirstResolver(fresh_store).resolve(project=project, mode="context", query="")
    assert candidate_id in [item["id"] for item in context["items"]]

    # 5. project scoping stays intact for a new session
    unrelated = GraphFirstResolver(fresh_store).resolve(project="unrelated-project", mode="list")
    assert unrelated["items"] == []


def test_multi_chunk_session_creates_one_candidate_per_chunk(isolated_pg_store):
    store = isolated_pg_store
    project = "chain-multi-" + uuid.uuid4().hex[:10]
    steward = BrainStewardService(_NoSqliteLedger(), pgvector_store=store, allow_restricted=True)

    # 1. three session chunks -> three needs_review candidates (batched mining)
    report, _state = run_candidate_intake(
        settings=CandidateIntakeSettings(project=project, enable_proposals=True),
        chunks=[_chunk(project, marker) for marker in ("1", "2", "3")],
        completion_fn=_completion,
        steward=steward,
        dry_run=False,
        checkpoint={"version": 1, "projects": {}},
    )
    assert report.status == "completed"
    assert report.proposed_count == 3
    assert report.duplicate_count == 0
    assert report.skipped_count == 0

    queue = steward.review_queue_list(project=project)
    assert queue["count"] == 3
    candidate_ids = sorted(item["memory_id"] for item in queue["items"])
    assert len(set(candidate_ids)) == 3

    # 2. approve all three; embedding jobs are enqueued per card
    for index, candidate_id in enumerate(candidate_ids):
        steward.candidate_approve(
            candidate_memory_id=candidate_id,
            approved_by="chain-e2e-operator",
            decision_id=f"chain-multi-approve-{index}",
        )
    assert steward.review_queue_list(project=project)["count"] == 0

    # 3. one worker pass consumes all three embeddings
    worker = OutboxWorker(
        store=store, worker_id="chain-multi-worker", embedding_fn=generate_deterministic_embedding
    )
    assert worker.run_once() == 3
    for candidate_id in candidate_ids:
        ready = store.get_card(candidate_id)
        assert ready.embedding_state == "ready"
        assert ready.embedding is not None

    # 4. a fresh session recalls every approved memory (paged reads collected)
    fresh = GraphFirstResolver(PgVectorStore(dsn=store.dsn), None, generate_deterministic_embedding)
    listed = _collect_authority_items(fresh, project=project, expected=3)
    assert sorted(item["id"] for item in listed) == candidate_ids

    recalled = _collect_authority_items(
        fresh, project=project, mode="query", query="PostgreSQL authority store", expected=3
    )
    assert sorted(item["id"] for item in recalled) == candidate_ids

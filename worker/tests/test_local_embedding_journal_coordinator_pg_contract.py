"""Regression tests for PR #349 Codex review findings:
1. GraphOperationJournalCoordinator contract with real PgVectorStore
   (begin_operation, run_step execution/noop/quarantine, record_graph_operation_step).
2. backfill_neo4j_local_embeddings across multiple batches with journal_coordinator
   does not trigger self-conflict quarantine.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

neo4j = pytest.importorskip("neo4j")

from agent_knowledge.llm_brain_core.local_graph_safety import (
    GraphOperationConflictQuarantinedError,
    GraphOperationJournalCoordinator,
    ensure_neo4j_safety_schema,
    get_group_fence_state,
)
from agent_knowledge.local_embedding import (
    CanonicalLocalEmbeddingClient,
    LocalEmbeddingServiceApplication,
    LocalModel2VecEngine,
    backfill_neo4j_local_embeddings,
    create_local_embedding_http_server,
)
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

NEO4J_URI = os.environ.get("LBRAIN_TEST_NEO4J_URI", "")
PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")

pytestmark = [
    pytest.mark.skipif(not NEO4J_URI, reason="disposable Neo4j required"),
    pytest.mark.skipif(not PG_DSN, reason="LBRAIN_TEST_PG_DSN required"),
]

_MODEL_DIR = Path(
    os.environ.get("NEURON_LOCAL_EMBEDDING_MODEL_DIR")
    or (
        Path.home()
        / ".hermes/profiles/atlas/cache/scratch/model2vec-mac-test-hf/hub/models--minishlab--potion-multilingual-128M/snapshots/73908c3438cf03b6a01bcb9611d62b23d0726f08"
    )
)


@pytest.fixture()
def sync_session():
    assert NEO4J_URI.startswith("bolt://127.0.0.1:")
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=None)
    try:
        with driver.session() as session:
            ensure_neo4j_safety_schema(session)
            yield session
    finally:
        driver.close()


@pytest.fixture()
def group_id(sync_session):
    gid = "m2jc-" + uuid.uuid4().hex[:12]
    yield gid
    sync_session.run(
        "MATCH (n) WHERE n.group_id = $g DETACH DELETE n", g=gid
    ).consume()
    sync_session.run(
        "MATCH (f:EmbeddingGroupFence {group_id: $g}) DETACH DELETE f", g=gid
    ).consume()


def test_journal_coordinator_pg_contract_lifecycle(sync_session, group_id, isolated_pg_store):
    coordinator = GraphOperationJournalCoordinator(pg_store=isolated_pg_store)
    op = "op-pg-" + uuid.uuid4().hex[:8]

    # 1. begin_operation must succeed and create both Neo4j lease and PG journal row
    lease = coordinator.begin_operation(
        sync_session,
        operation_id=op,
        group_id=group_id,
        operation_type="test_op",
        lease_owner="worker-1",
        lease_seconds=60,
    )
    assert lease.group_id == group_id

    journal = isolated_pg_store.get_graph_operation_journal(op)
    assert journal is not None
    assert journal["operation_id"] == op
    assert journal["group_id"] == group_id
    assert journal["state"] in ("claimed", "neo4j_fenced")

    # 2. run_step execution
    calls: list[int] = []
    outcome, _ = coordinator.run_step(
        sync_session,
        operation_id=op,
        group_id=group_id,
        step_id="entities_upserted",
        effect_payload={"uuids": ["u1", "u2"]},
        action=lambda: calls.append(1),
    )
    assert outcome == "executed"
    assert calls == [1]

    journal_after = isolated_pg_store.get_graph_operation_journal(op)
    assert journal_after is not None
    assert "entities_upserted" in journal_after["step_receipts"]

    # 3. run_step replay with same effect is noop
    outcome_replay, _ = coordinator.run_step(
        sync_session,
        operation_id=op,
        group_id=group_id,
        step_id="entities_upserted",
        effect_payload={"uuids": ["u1", "u2"]},
        action=lambda: calls.append(2),
    )
    assert outcome_replay == "noop"
    assert calls == [1]

    # 4. run_step with conflicting effect must quarantine both PG and Neo4j
    with pytest.raises(GraphOperationConflictQuarantinedError):
        coordinator.run_step(
            sync_session,
            operation_id=op,
            group_id=group_id,
            step_id="entities_upserted",
            effect_payload={"uuids": ["u1", "u3_conflict"]},
            action=lambda: calls.append(3),
        )
    assert calls == [1]

    fence = get_group_fence_state(sync_session, group_id)
    assert fence["status"] == "quarantined"

    journal_quarantined = isolated_pg_store.get_graph_operation_journal(op)
    assert journal_quarantined["state"] == "quarantine"
    assert "mismatch" in (journal_quarantined["last_error"] or "")

    coordinator.complete_operation(sync_session, lease=lease)


def test_backfill_multi_batch_with_journal_coordinator_no_self_conflict(
    sync_session, group_id, isolated_pg_store
):
    import threading

    if not _MODEL_DIR.is_dir():
        pytest.skip("Pinned Model2Vec snapshot required")

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
        # Create 3 entities in the group without local embeddings
        u1, u2, u3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        sync_session.run(
            """
            CREATE (:Entity {uuid: $u1, name: 'Apple Inc', group_id: $g})
            CREATE (:Entity {uuid: $u2, name: 'Banana Corp', group_id: $g})
            CREATE (:Entity {uuid: $u3, name: 'Cherry LLC', group_id: $g})
            """,
            u1=u1,
            u2=u2,
            u3=u3,
            g=group_id,
        ).consume()

        coordinator = GraphOperationJournalCoordinator(pg_store=isolated_pg_store)

        # batch_size=1, max_batches_per_group=5 => requires 3 separate batches
        # Previously, batch 2 failed with effect_hash conflict and quarantined the group.
        progress = backfill_neo4j_local_embeddings(
            sync_session,
            client,
            group_ids=[group_id],
            batch_size=1,
            max_batches_per_group=5,
            journal_coordinator=coordinator,
        )

        assert progress.entities_embedded == 3
        assert progress.groups_quarantined == 0

        fence = get_group_fence_state(sync_session, group_id)
        assert fence["status"] == "idle"

        # Verify embeddings were actually written to Neo4j
        res = sync_session.run(
            """
            MATCH (n:Entity {group_id: $g})
            RETURN count(n.name_embedding_model2vec_v1) AS cnt
            """,
            g=group_id,
        ).single()
        assert res["cnt"] == 3

    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

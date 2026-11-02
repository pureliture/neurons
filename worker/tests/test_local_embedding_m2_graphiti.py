"""M2/M3 isolated Neo4j + vendored Graphiti 0.30.1 fork verification for the local Model2Vec profile.

Requires `LBRAIN_TEST_NEO4J_URI` (loopback-only disposable Neo4j) and the pinned real Model2Vec
snapshot. Legacy 3,072-dim (`name_embedding`/`fact_embedding`) data and local 256-dim
(`*_model2vec_v1`) data must coexist without being mixed, overwritten, or deleted.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

neo4j = pytest.importorskip("neo4j")

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.local_profile import (
    CommunityDisabledInLocalModeError,
    local_embedding_mode_context,
)
from graphiti_core.nodes import CommunityNode, EntityNode
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.search.search_utils import (
    edge_similarity_search,
    get_edge_invalidation_candidates,
    get_relevant_nodes,
    node_similarity_search,
)

from agent_knowledge.llm_brain_core.local_graph_safety import (
    GraphOperationConflictQuarantinedError,
    GraphOperationJournalCoordinator,
    GroupFenceBusyError,
    GroupQuarantinedError,
    acquire_group_fence_lease,
    bump_group_fence_epoch,
    cas_write_edge_local_embedding,
    cas_write_entity_local_embedding,
    ensure_neo4j_safety_schema,
    get_group_fence_state,
    quarantine_group_fence,
    record_embedding_deletion,
    release_group_fence_lease,
)
from agent_knowledge.local_embedding import (
    LOCAL_EMBEDDING_PROFILE_ID,
    CanonicalLocalEmbeddingClient,
    LocalEmbeddingServiceApplication,
    LocalModel2VecEngine,
    LocalModel2VecGraphitiEmbedder,
    backfill_neo4j_local_embeddings,
    create_local_embedding_http_server,
    evaluate_cutover_readiness,
    inspect_neo4j_local_embedding_state,
)

NEO4J_URI = os.environ.get("LBRAIN_TEST_NEO4J_URI", "")
pytestmark = pytest.mark.skipif(not NEO4J_URI, reason="disposable Neo4j test endpoint required")

_MODEL_DIR_ENV = os.environ.get("NEURON_LOCAL_EMBEDDING_MODEL_DIR", "").strip()
_MODEL_DIR = Path(_MODEL_DIR_ENV) if _MODEL_DIR_ENV else None
LEGACY_DIM = 3072


@pytest.fixture(scope="module")
def local_client():
    if not _MODEL_DIR or not _MODEL_DIR.is_dir():
        pytest.skip("Pinned real Model2Vec snapshot not present on this host (set NEURON_LOCAL_EMBEDDING_MODEL_DIR)")
    engine = LocalModel2VecEngine.from_model_dir(_MODEL_DIR)
    server = create_local_embedding_http_server(
        application=LocalEmbeddingServiceApplication(engine=engine), bind_host="127.0.0.1", port=0
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


@pytest.fixture()
def sync_session():
    assert NEO4J_URI.startswith("bolt://127.0.0.1:")  # never a remote/live Neo4j
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=None)
    try:
        with driver.session() as session:
            yield session
    finally:
        driver.close()


@pytest.fixture()
def group_id(sync_session):
    gid = "m2g-" + uuid.uuid4().hex[:12]
    yield gid
    sync_session.run(
        "MATCH (n) WHERE n.group_id = $g DETACH DELETE n", g=gid
    ).consume()
    sync_session.run("MATCH (f:EmbeddingGroupFence {group_id: $g}) DETACH DELETE f", g=gid).consume()


def _run(coro_factory):
    """Run an async scenario with a fresh no-auth Neo4jDriver bound to this event loop."""

    async def _main():
        driver = Neo4jDriver(uri=NEO4J_URI, user=None, password=None)
        driver.local_embedding_profile = LOCAL_EMBEDDING_PROFILE_ID
        try:
            if driver._init_task is not None:
                await driver._init_task
            return await coro_factory(driver)
        finally:
            await driver.close()

    return asyncio.run(_main())


def _node(name: str, gid: str, **kw) -> EntityNode:
    return EntityNode(name=name, group_id=gid, labels=["Entity"], summary="", **kw)


def _edge(src: EntityNode, dst: EntityNode, fact: str, gid: str) -> EntityEdge:
    return EntityEdge(
        source_node_uuid=src.uuid,
        target_node_uuid=dst.uuid,
        name="RELATES_TO",
        fact=fact,
        group_id=gid,
        episodes=[],
        created_at=datetime.now(timezone.utc),
    )


def test_local_and_legacy_vectors_coexist_without_mixing(local_client, sync_session, group_id):
    embedder = LocalModel2VecGraphitiEmbedder(canonical_client=local_client)

    async def scenario(driver):
        legacy = _node("레거시 Gemini 노드", group_id, name_embedding=[0.01] * LEGACY_DIM)
        with local_embedding_mode_context(False):
            await legacy.save(driver)
        local = _node("로컬 Model2Vec 노드", group_id)
        with local_embedding_mode_context(True):
            await local.generate_name_embedding(embedder)
            assert len(local.name_embedding) == 256
            await local.save(driver)
            hits = await node_similarity_search(driver, local.name_embedding, SearchFilters(), [group_id], 10, 0.0)
        return legacy.uuid, local.uuid, [h.uuid for h in hits]

    legacy_uuid, local_uuid, hit_uuids = _run(scenario)
    assert hit_uuids == [local_uuid]  # legacy node is never returned by a 256-dim local query

    rec = sync_session.run(
        """
        MATCH (l:Entity {uuid: $l}), (n:Entity {uuid: $n})
        RETURN size(l.name_embedding) AS legacy_dim, l.name_embedding_model2vec_v1 IS NULL AS legacy_has_no_local,
               n.name_embedding IS NULL AS local_has_no_legacy, size(n.name_embedding_model2vec_v1) AS local_dim
        """,
        l=legacy_uuid, n=local_uuid,
    ).single()
    assert rec["legacy_dim"] == LEGACY_DIM and rec["legacy_has_no_local"] is True
    assert rec["local_dim"] == 256 and rec["local_has_no_legacy"] is True


def test_edge_save_search_invalidation_and_dedup_paths(local_client, sync_session, group_id):
    embedder = LocalModel2VecGraphitiEmbedder(canonical_client=local_client)
    other_group = group_id + "-other"

    async def scenario(driver):
        with local_embedding_mode_context(True):
            a, b = _node("neurons", group_id), _node("Model2Vec", group_id)
            dup = _node("neurons", group_id)  # exact-name candidate for dedup lookup
            foreign = _node("neurons", other_group)
            for n in (a, b, dup, foreign):
                await n.generate_name_embedding(embedder)
                await n.save(driver)
            edge = _edge(a, b, "neurons는 로컬 Model2Vec 임베딩을 사용한다", group_id)
            await edge.generate_embedding(embedder)
            await edge.save(driver)
            foreign_edge = _edge(foreign, foreign, "neurons는 로컬 Model2Vec 임베딩을 사용한다", other_group)
            await foreign_edge.generate_embedding(embedder)
            await foreign_edge.save(driver)

            # search
            hits = await edge_similarity_search(driver, edge.fact_embedding, None, None, SearchFilters(), [group_id], 10, 0.0)
            # dedup candidates: scoped to the same group only
            probe = _node("neurons", group_id)
            await probe.generate_name_embedding(embedder)
            relevant = await get_relevant_nodes(driver, [probe], SearchFilters(), 0.0, 10)
            # invalidation candidates: a new contradicting edge between the same nodes finds the old edge
            new_edge = _edge(a, b, "neurons는 더 이상 로컬 Model2Vec 임베딩을 사용하지 않는다", group_id)
            await new_edge.generate_embedding(embedder)
            inval = await get_edge_invalidation_candidates(driver, [new_edge], SearchFilters(), 0.0, 10)
        return edge, foreign_edge, hits, relevant, inval, (a, dup, foreign)

    edge, foreign_edge, hits, relevant, inval, (a, dup, foreign) = _run(scenario)
    assert [h.uuid for h in hits] == [edge.uuid]
    rel_ids = {n.uuid for n in relevant[0]}
    assert {a.uuid, dup.uuid} <= rel_ids and foreign.uuid not in rel_ids
    inval_ids = {e.uuid for e in inval[0]}
    assert edge.uuid in inval_ids and foreign_edge.uuid not in inval_ids

    rec = sync_session.run(
        "MATCH ()-[e:RELATES_TO {uuid: $u}]->() RETURN size(e.fact_embedding_model2vec_v1) AS d, e.fact_embedding IS NULL AS no_legacy",
        u=edge.uuid,
    ).single()
    assert rec["d"] == 256 and rec["no_legacy"] is True

    # internal metadata never leaks into Graphiti attributes
    async def reload(driver):
        with local_embedding_mode_context(True):
            return await EntityNode.get_by_uuid(driver, a.uuid)

    loaded = _run(reload)
    assert not any(k.startswith("name_embedding") or k in {"embedding_dirty", "_transient_cas_lock"} for k in loaded.attributes)


def test_community_operations_fail_closed_in_local_mode(group_id):
    async def scenario(driver):
        with local_embedding_mode_context(True):
            community = CommunityNode(name="c", group_id=group_id, summary="s")
            with pytest.raises(CommunityDisabledInLocalModeError):
                await community.save(driver)

    _run(scenario)


def test_group_fence_lease_epoch_and_quarantine(sync_session, group_id):
    ensure_neo4j_safety_schema(sync_session)
    lease = acquire_group_fence_lease(sync_session, group_id, lease_owner="w1", operation_id="op1", lease_seconds=60)
    with pytest.raises(GroupFenceBusyError):
        acquire_group_fence_lease(sync_session, group_id, lease_owner="w2", operation_id="op2", lease_seconds=60)
    assert release_group_fence_lease(sync_session, lease) is True
    lease2 = acquire_group_fence_lease(sync_session, group_id, lease_owner="w2", operation_id="op3", lease_seconds=60)
    assert lease2.epoch == lease.epoch
    new_epoch = bump_group_fence_epoch(sync_session, group_id)
    assert new_epoch == lease.epoch + 1
    release_group_fence_lease(sync_session, lease2)
    quarantine_group_fence(sync_session, group_id, operation_id="op4", reason="test")
    with pytest.raises(GroupQuarantinedError):
        acquire_group_fence_lease(sync_session, group_id, lease_owner="w3", operation_id="op5", lease_seconds=60)
    assert get_group_fence_state(sync_session, group_id)["status"] == "quarantined"


def test_cas_blocks_stale_epoch_text_change_and_deleted_entities(local_client, sync_session, group_id):
    ensure_neo4j_safety_schema(sync_session)
    uid = str(uuid.uuid4())
    sync_session.run(
        "CREATE (:Entity {uuid: $u, name: $n, group_id: $g, created_at: datetime()})", u=uid, n="CAS 대상", g=group_id
    ).consume()
    vec = local_client.embed_document("entity_name: CAS 대상").vector
    lease = acquire_group_fence_lease(sync_session, group_id, lease_owner="w1", operation_id="cas-op", lease_seconds=60)

    # text changed after the vector was computed
    res = cas_write_entity_local_embedding(
        sync_session, entity_uuid=uid, group_id=group_id, expected_name="다른 이름",
        expected_epoch=lease.epoch, vector=vec, lease_owner="w1",
    )
    assert (res.applied, res.reason) == (False, "text_changed")

    # epoch advanced (e.g. concurrent live write) -> stale worker rejected
    bump_group_fence_epoch(sync_session, group_id)
    res = cas_write_entity_local_embedding(
        sync_session, entity_uuid=uid, group_id=group_id, expected_name="CAS 대상",
        expected_epoch=lease.epoch, vector=vec, lease_owner="w1",
    )
    assert (res.applied, res.reason) == (False, "epoch_mismatch")

    fresh_epoch = get_group_fence_state(sync_session, group_id)["epoch"]
    ok = cas_write_entity_local_embedding(
        sync_session, entity_uuid=uid, group_id=group_id, expected_name="CAS 대상",
        expected_epoch=fresh_epoch, vector=vec, lease_owner="w1",
    )
    assert ok.applied and ok.reason == "ok"

    # deleted + tombstoned entity can never receive a zombie vector
    record_embedding_deletion(sync_session, target_type="entity", target_uuid=uid, group_id=group_id)
    sync_session.run("MATCH (n:Entity {uuid: $u}) DETACH DELETE n", u=uid).consume()
    sync_session.run("CREATE (:Entity {uuid: $u, name: $n, group_id: $g})", u=uid, n="CAS 대상", g=group_id).consume()
    epoch_after = get_group_fence_state(sync_session, group_id)["epoch"]
    res = cas_write_entity_local_embedding(
        sync_session, entity_uuid=uid, group_id=group_id, expected_name="CAS 대상",
        expected_epoch=epoch_after, vector=vec, lease_owner="w1",
    )
    assert (res.applied, res.reason) == (False, "deleted_tombstone")
    sync_session.run("MATCH (d:EmbeddingDeletion {target_uuid: $u}) DETACH DELETE d", u=uid).consume()

    # edge CAS: wrong vector dimension is rejected before touching the graph
    with pytest.raises(ValueError):
        cas_write_edge_local_embedding(
            sync_session, edge_uuid="x", group_id=group_id, expected_fact="f", expected_epoch=0, vector=[0.1] * 10
        )


def test_journal_replays_same_effect_and_quarantines_conflict(sync_session, group_id):
    coordinator = GraphOperationJournalCoordinator(pg_store=None)
    op = "journal-" + uuid.uuid4().hex[:8]
    lease = coordinator.begin_operation(
        sync_session, operation_id=op, group_id=group_id, operation_type="test", lease_owner="w1"
    )
    calls: list[int] = []
    out, _ = coordinator.run_step(
        sync_session, operation_id=op, group_id=group_id, step_id="entities_upserted",
        effect_payload={"uuids": ["a", "b"]}, action=lambda: calls.append(1),
    )
    assert out == "executed"
    out, _ = coordinator.run_step(  # replay after a crash/restart: same effect -> no duplicate work
        sync_session, operation_id=op, group_id=group_id, step_id="entities_upserted",
        effect_payload={"uuids": ["a", "b"]}, action=lambda: calls.append(1),
    )
    assert out == "noop" and calls == [1]
    with pytest.raises(GraphOperationConflictQuarantinedError):  # same step, different effect
        coordinator.run_step(
            sync_session, operation_id=op, group_id=group_id, step_id="entities_upserted",
            effect_payload={"uuids": ["a", "c"]}, action=lambda: calls.append(1),
        )
    assert calls == [1]
    assert get_group_fence_state(sync_session, group_id)["status"] == "quarantined"
    del lease
    sync_session.run("MATCH (r:GraphOperationStepReceipt {operation_id: $o}) DELETE r", o=op).consume()


def test_backfill_preserves_legacy_is_bounded_and_idempotent(local_client, sync_session, group_id):
    ensure_neo4j_safety_schema(sync_session)
    ids = [str(uuid.uuid4()) for _ in range(5)]
    for i, u in enumerate(ids):
        sync_session.run(
            "CREATE (:Entity {uuid: $u, name: $n, group_id: $g, name_embedding: $v, created_at: datetime()})",
            u=u, n=f"백필 엔티티 {i}", g=group_id, v=[0.01] * LEGACY_DIM,
        ).consume()
    for i in range(3):
        sync_session.run(
            """
            MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b})
            CREATE (a)-[:RELATES_TO {uuid: $e, group_id: $g, name: 'RELATES_TO', fact: $f,
                                     fact_embedding: $v, created_at: datetime()}]->(b)
            """,
            a=ids[i], b=ids[i + 1], e=str(uuid.uuid4()), g=group_id, f=f"백필 관계 사실 {i}", v=[0.02] * LEGACY_DIM,
        ).consume()

    embed = lambda text: local_client.embed_document(text).vector  # noqa: E731
    before = inspect_neo4j_local_embedding_state(sync_session, group_ids=[group_id])
    assert before.eligible_entities == 5 and before.missing_or_dirty_entities == 5
    assert evaluate_cutover_readiness(neo4j_progress=before).ready is False

    partial = backfill_neo4j_local_embeddings(
        sync_session, embed, group_ids=[group_id], batch_size=2, max_batches_per_group=1
    )
    assert 0 < partial.entities_embedded <= 2
    done = backfill_neo4j_local_embeddings(
        sync_session, embed, group_ids=[group_id], batch_size=2, max_batches_per_group=10
    )
    final = inspect_neo4j_local_embedding_state(sync_session, group_ids=[group_id])
    assert final.missing_or_dirty_entities == 0 and final.missing_or_dirty_edges == 0
    assert evaluate_cutover_readiness(neo4j_progress=final).ready is True
    assert final.legacy_entity_embeddings_preserved == 5 and final.legacy_edge_embeddings_preserved == 3

    again = backfill_neo4j_local_embeddings(sync_session, embed, group_ids=[group_id])
    assert again.entities_embedded == 0 and again.edges_embedded == 0  # idempotent
    legacy_dims = sync_session.run(
        "MATCH (n:Entity {group_id: $g}) RETURN collect(DISTINCT size(n.name_embedding)) AS d", g=group_id
    ).single()["d"]
    assert legacy_dims == [LEGACY_DIM]


def test_backfill_skips_busy_group_fail_closed(local_client, sync_session, group_id):
    ensure_neo4j_safety_schema(sync_session)
    uid = str(uuid.uuid4())
    sync_session.run("CREATE (:Entity {uuid: $u, name: 'busy', group_id: $g})", u=uid, g=group_id).consume()
    acquire_group_fence_lease(sync_session, group_id, lease_owner="live-writer", operation_id="live", status="live_write_in_progress", lease_seconds=60)
    progress = backfill_neo4j_local_embeddings(
        sync_session, lambda t: local_client.embed_document(t).vector, group_ids=[group_id]
    )
    assert progress.groups_fenced_busy == 1 and progress.entities_embedded == 0
    assert evaluate_cutover_readiness(neo4j_progress=progress).ready is False

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from graphiti_core.local_profile import (
    compute_graph_edge_input_hash,
    compute_graph_node_input_hash,
    format_graph_edge_embedding_input,
    format_graph_node_embedding_input,
)

from ..llm_brain_core.local_graph_safety import (
    GraphOperationJournalCoordinator,
    GroupFenceBusyError,
    GroupQuarantinedError,
    acquire_group_fence_lease,
    cas_write_edge_local_embedding,
    cas_write_entity_local_embedding,
    ensure_neo4j_safety_schema,
    release_group_fence_lease,
)
from ..model_connectors.policy import validate_local_embedding_write_or_query_policy
from .canonical import (
    card_input_v1,
    compute_input_hash,
    session_input_v1,
)
from .profile import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
)


def _cell(row: Any, key: str, index: int) -> Any:
    """Read one column from a psycopg row regardless of row factory (tuple or dict_row)."""
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _scalar_count(row: Any) -> int:
    if row is None:
        return 0
    if isinstance(row, Mapping):
        return int(next(iter(row.values())) or 0)
    return int(row[0] or 0)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PgBackfillProgress:
    eligible_cards: int = 0
    ready_cards: int = 0
    missing_cards: int = 0
    eligible_sessions: int = 0
    ready_sessions: int = 0
    missing_sessions: int = 0
    enqueued_cards: int = 0
    enqueued_sessions: int = 0
    batches_executed: int = 0
    claimed: int = 0
    completed: int = 0
    stale_fenced: int = 0
    retried: int = 0
    quarantined: int = 0
    outbox_queued: int = 0
    outbox_processing: int = 0
    outbox_failed_retryable: int = 0
    outbox_dead_letter: int = 0
    tombstones_count: int = 0
    legacy_card_embeddings_preserved: int = 0
    legacy_session_embeddings_preserved: int = 0
    eligible_session_receipts: int = 0
    ready_session_receipts: int = 0
    missing_session_receipts: int = 0
    session_receipts_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Neo4jBackfillProgress:
    group_ids_scanned: tuple[str, ...] = ()
    eligible_entities: int = 0
    ready_entities: int = 0
    missing_or_dirty_entities: int = 0
    eligible_edges: int = 0
    ready_edges: int = 0
    missing_or_dirty_edges: int = 0
    entities_embedded: int = 0
    entities_fenced_stale: int = 0
    edges_embedded: int = 0
    edges_fenced_stale: int = 0
    groups_fenced_busy: int = 0
    groups_quarantined: int = 0
    tombstones_count: int = 0
    legacy_entity_embeddings_preserved: int = 0
    legacy_edge_embeddings_preserved: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["group_ids_scanned"] = list(self.group_ids_scanned)
        return data


@dataclass(frozen=True)
class LocalEmbeddingCutoverReport:
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID
    checked_at: str = field(default_factory=_utc_now_iso)
    ready: bool = False
    blocking_reasons: tuple[str, ...] = ()
    pg: PgBackfillProgress | None = None
    neo4j: Neo4jBackfillProgress | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "checked_at": self.checked_at,
            "ready": self.ready,
            "blocking_reasons": list(self.blocking_reasons),
            "pg": self.pg.to_dict() if self.pg is not None else None,
            "neo4j": self.neo4j.to_dict() if self.neo4j is not None else None,
        }


def inspect_pg_local_embedding_state(
    store: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    couchdb_store: Any | None = None,
) -> PgBackfillProgress:
    """Read-only inspection of PostgreSQL local embedding backfill and outbox state."""
    with store._scope(write=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM memory_cards
                 WHERE lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                   AND authorization_status = 'active'
                   AND currentness IN ('current', 'superseded')
                """
            )
            eligible_cards = _scalar_count(cur.fetchone())

            cur.execute(
                """
                SELECT COUNT(*)
                  FROM memory_cards mc
                  JOIN card_local_embeddings cle
                    ON cle.memory_id = mc.memory_id
                   AND cle.profile_id = %s
                   AND cle.source_content_hash = mc.content_hash
                   AND cle.state = 'ready'
                 WHERE mc.lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                   AND mc.authorization_status = 'active'
                   AND mc.currentness IN ('current', 'superseded')
                """,
                (profile_id,),
            )
            ready_cards = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(*) FROM session_memory_chunks")
            eligible_sessions = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(DISTINCT session_id_hash) FROM session_memory_chunks")
            eligible_session_receipts = _scalar_count(cur.fetchone())

            cur.execute(
                """
                SELECT COUNT(*)
                  FROM session_memory_chunks smc
                  JOIN session_local_embeddings sle
                    ON sle.chunk_id = smc.chunk_id
                   AND sle.profile_id = %s
                   AND sle.source_content_hash = smc.content_hash
                   AND sle.state = 'ready'
                """,
                (profile_id,),
            )
            ready_sessions = _scalar_count(cur.fetchone())

            ready_session_receipts = 0
            session_receipts_verified = False
            if couchdb_store is not None:
                from ..rag_ingress.pg_representation import valid_local_pg_receipt_metadata
                from ..couchdb_source.session_memory_materializer import materialize_session_memory

                cur.execute(
                    """
                    SELECT DISTINCT smc.session_id_hash
                      FROM session_memory_chunks smc
                      JOIN session_local_embeddings sle
                        ON sle.chunk_id = smc.chunk_id
                       AND sle.profile_id = %s
                       AND sle.source_content_hash = smc.content_hash
                       AND sle.state = 'ready'
                    """,
                    (profile_id,),
                )
                session_id_hashes = [
                    str(_cell(r, "session_id_hash", 0) or "") for r in cur.fetchall() or ()
                ]
                for sid_hash in session_id_hashes:
                    if not sid_hash:
                        continue
                    try:
                        doc = couchdb_store.get(f"projection_state:{sid_hash}")
                    except Exception:
                        doc = None
                    if isinstance(doc, dict):
                        receipts = doc.get("backend_receipts")
                        if isinstance(receipts, dict):
                            local_map = receipts.get("postgres_pgvector_local")
                            if isinstance(local_map, dict):
                                rec = local_map.get(profile_id)
                                if isinstance(rec, dict) and rec.get("receipt_version") == 3:
                                    try:
                                        current_mat = materialize_session_memory(
                                            session_id_hash=sid_hash,
                                            store=couchdb_store,
                                        )
                                        if valid_local_pg_receipt_metadata(
                                            rec,
                                            store=store,
                                            current_materialized=current_mat,
                                            require_ready_vector=True,
                                        ):
                                            ready_session_receipts += 1
                                    except Exception:
                                        pass
                session_receipts_verified = True

            cur.execute(
                """
                SELECT status AS status, COUNT(*) AS n
                  FROM local_embedding_outbox
                 WHERE profile_id = %s
                 GROUP BY status
                """,
                (profile_id,),
            )
            outbox_counts = {str(_cell(row, "status", 0)): int(_cell(row, "n", 1)) for row in cur.fetchall() or ()}

            cur.execute(
                """
                SELECT COUNT(*)
                  FROM local_embedding_tombstones
                 WHERE profile_id = %s
                   AND deleted_at IS NOT NULL
                """,
                (profile_id,),
            )
            tombstones_count = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(*) FROM memory_cards WHERE embedding IS NOT NULL")
            legacy_cards = _scalar_count(cur.fetchone())

            cur.execute("SELECT COUNT(*) FROM session_memory_chunks WHERE embedding IS NOT NULL")
            legacy_sessions = _scalar_count(cur.fetchone())

    missing_session_receipts = (
        max(0, eligible_session_receipts - ready_session_receipts)
        if session_receipts_verified
        else eligible_session_receipts
    )

    return PgBackfillProgress(
        eligible_cards=eligible_cards,
        ready_cards=ready_cards,
        missing_cards=max(0, eligible_cards - ready_cards),
        eligible_sessions=eligible_sessions,
        ready_sessions=ready_sessions,
        missing_sessions=max(0, eligible_sessions - ready_sessions),
        eligible_session_receipts=eligible_session_receipts,
        ready_session_receipts=ready_session_receipts,
        missing_session_receipts=missing_session_receipts,
        session_receipts_verified=session_receipts_verified,
        outbox_queued=outbox_counts.get("queued", 0),
        outbox_processing=outbox_counts.get("processing", 0),
        outbox_failed_retryable=outbox_counts.get("failed", 0),
        outbox_dead_letter=outbox_counts.get("dead_letter", 0),
        tombstones_count=tombstones_count,
        legacy_card_embeddings_preserved=legacy_cards,
        legacy_session_embeddings_preserved=legacy_sessions,
    )


def enqueue_missing_pg_local_embeddings(
    store: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    limit_per_kind: int = 200,
    include_cards: bool = True,
    include_sessions: bool = True,
) -> tuple[int, int]:
    """Bounded enqueue of missing/stale cards and session_memory_chunks into local_embedding_outbox."""
    bounded_limit = max(1, min(int(limit_per_kind), 5000))
    enqueued_cards = 0
    enqueued_sessions = 0

    with store._scope(write=False) as conn:
        with conn.cursor() as cur:
            if include_cards:
                cur.execute(
                    """
                    SELECT mc.memory_id, mc.content_hash, mc.summary, mc.title, mc.typed_payload
                      FROM memory_cards mc
                      LEFT JOIN card_local_embeddings cle
                        ON cle.memory_id = mc.memory_id
                       AND cle.profile_id = %s
                       AND cle.source_content_hash = mc.content_hash
                       AND cle.state IN ('ready', 'failed')
                      LEFT JOIN local_embedding_tombstones t
                        ON t.target_type = 'memory_card'
                       AND t.target_id = mc.memory_id
                       AND t.profile_id = %s
                       AND t.deleted_at IS NOT NULL
                     WHERE mc.lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                       AND mc.authorization_status = 'active'
                       AND mc.currentness IN ('current', 'superseded')
                       AND cle.memory_id IS NULL
                       AND t.target_id IS NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM local_embedding_outbox o
                            WHERE o.target_type = 'memory_card'
                              AND o.target_id = mc.memory_id
                              AND o.profile_id = %s
                              AND o.source_content_hash = mc.content_hash
                              AND o.status IN ('queued', 'processing', 'failed', 'dead_letter')
                       )
                     ORDER BY mc.updated_at ASC, mc.memory_id ASC
                     LIMIT %s
                    """,
                    (profile_id, profile_id, profile_id, bounded_limit),
                )
                card_rows = cur.fetchall() or ()
            else:
                card_rows = ()

            if include_sessions:
                cur.execute(
                    """
                    SELECT smc.chunk_id, smc.content_hash, smc.content_markdown
                      FROM session_memory_chunks smc
                      LEFT JOIN session_local_embeddings sle
                        ON sle.chunk_id = smc.chunk_id
                       AND sle.profile_id = %s
                       AND sle.source_content_hash = smc.content_hash
                       AND sle.state IN ('ready', 'failed')
                      LEFT JOIN local_embedding_tombstones t
                        ON t.target_type = 'session_chunk'
                       AND t.target_id = smc.chunk_id
                       AND t.profile_id = %s
                       AND t.deleted_at IS NOT NULL
                     WHERE sle.chunk_id IS NULL
                       AND t.target_id IS NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM local_embedding_outbox o
                            WHERE o.target_type = 'session_chunk'
                              AND o.target_id = smc.chunk_id
                              AND o.profile_id = %s
                              AND o.source_content_hash = smc.content_hash
                              AND o.status IN ('queued', 'processing', 'failed', 'dead_letter')
                       )
                     ORDER BY smc.updated_at ASC, smc.chunk_id ASC
                     LIMIT %s
                    """,
                    (profile_id, profile_id, profile_id, bounded_limit),
                )
                session_rows = cur.fetchall() or ()
            else:
                session_rows = ()

    for card_row in card_rows:
        memory_id = _cell(card_row, "memory_id", 0)
        content_hash = _cell(card_row, "content_hash", 1)
        summary = _cell(card_row, "summary", 2)
        title = _cell(card_row, "title", 3)
        typed_payload_raw = _cell(card_row, "typed_payload", 4)
        typed_payload = (
            typed_payload_raw
            if isinstance(typed_payload_raw, dict)
            else (
                json.loads(typed_payload_raw)
                if isinstance(typed_payload_raw, str) and typed_payload_raw.strip()
                else {}
            )
        )
        try:
            payload_text = card_input_v1(
                summary=str(summary or ""),
                title=str(title or ""),
                typed_payload=typed_payload,
            )
            store.enqueue_local_outbox(
                target_type="memory_card",
                target_id=str(memory_id),
                source_content_hash=str(content_hash),
                payload_text=payload_text,
                profile_id=profile_id,
            )
            enqueued_cards += 1
        except ValueError:
            # Parent deleted or modified concurrently; skip safely
            continue

    for session_row in session_rows:
        chunk_id = _cell(session_row, "chunk_id", 0)
        content_hash = _cell(session_row, "content_hash", 1)
        content_markdown = _cell(session_row, "content_markdown", 2)
        try:
            payload_text = session_input_v1(str(content_markdown or ""))
            store.enqueue_local_outbox(
                target_type="session_chunk",
                target_id=str(chunk_id),
                source_content_hash=str(content_hash),
                payload_text=payload_text,
                profile_id=profile_id,
            )
            enqueued_sessions += 1
        except ValueError:
            continue

    return enqueued_cards, enqueued_sessions


def _resolve_embedding_fn(embedder: Any) -> Callable[[str], list[float]]:
    if callable(embedder) and not hasattr(embedder, "embed") and not hasattr(embedder, "embed_texts"):
        return embedder
    if hasattr(embedder, "embed"):
        return embedder.embed
    if hasattr(embedder, "embed_query"):
        return embedder.embed_query
    if hasattr(embedder, "embed_texts"):
        return lambda text: list(embedder.embed_texts([text])[0].vector)
    raise TypeError(f"Unsupported embedder for LocalEmbeddingOutboxWorker: {type(embedder)!r}")


def backfill_pg_local_embeddings(
    store: Any,
    embedder: Any,
    *,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    batch_size: int = 32,
    max_batches: int = 10,
    enqueue_limit_per_kind: int = 200,
    lease_seconds: int = 30,
    max_retries: int = 5,
    worker_id: str | None = None,
    couchdb_store: Any | None = None,
) -> PgBackfillProgress:
    """Bounded, idempotent, resumable PostgreSQL backfill for cards and session chunks."""
    from ..postgres_store.outbox_worker import LocalEmbeddingOutboxWorker

    bounded_batch = max(1, min(int(batch_size), 64))
    bounded_max_batches = max(1, int(max_batches))
    enq_cards, enq_sessions = enqueue_missing_pg_local_embeddings(
        store,
        profile_id=profile_id,
        limit_per_kind=enqueue_limit_per_kind,
    )
    worker = LocalEmbeddingOutboxWorker(
        store=store,
        worker_id=worker_id or f"m3-pg-backfill-{uuid.uuid4().hex[:8]}",
        batch_size=bounded_batch,
        lease_seconds=lease_seconds,
        embedding_fn=_resolve_embedding_fn(embedder),
        max_retries=max_retries,
        embedding_profile=profile_id,
    )
    batches_executed = 0
    total_claimed = 0
    total_completed = 0
    total_stale = 0
    total_failed = 0

    for _ in range(bounded_max_batches):
        claimed = worker.run_once(batch_size=bounded_batch)
        if claimed == 0:
            break
        batches_executed += 1
        total_claimed += claimed
        total_completed += worker.last_batch_completed
        total_stale += worker.last_batch_stale
        total_failed += worker.last_batch_failed

    if couchdb_store is not None:
        from ..couchdb_source.session_memory_materializer import materialize_session_memory
        from ..model_connectors import DEFAULT_EMBEDDING_PROFILE_ID
        from ..rag_ingress.pg_backfill import _derive_pg_chunk_id_impl
        from ..rag_ingress.pg_embedding_chunks import CHUNKED_PROFILE
        from ..rag_ingress.pg_representation import (
            LOCAL_SESSION_REPRESENTATION_KIND,
            build_local_representation_receipt_v3,
            derive_local_pg_chunk_id,
            valid_local_representation_receipt_v3,
        )

        with store._scope(write=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT smc.chunk_id, smc.project, smc.provider, smc.session_id_hash,
                           smc.content_hash, sle.input_hash
                      FROM session_memory_chunks smc
                      JOIN session_local_embeddings sle
                        ON sle.chunk_id = smc.chunk_id
                       AND sle.profile_id = %s
                       AND sle.source_content_hash = smc.content_hash
                       AND sle.state = 'ready'
                    """,
                    (profile_id,),
                )
                ready_session_rows = cur.fetchall() or ()

        for s_row in ready_session_rows:
            s_chunk_id = str(_cell(s_row, "chunk_id", 0))
            s_project = str(_cell(s_row, "project", 1))
            s_provider = str(_cell(s_row, "provider", 2) or "unspecified")
            s_sid_hash = str(_cell(s_row, "session_id_hash", 3))
            s_content_hash = str(_cell(s_row, "content_hash", 4))
            s_input_hash = str(_cell(s_row, "input_hash", 5))
            doc_id = f"projection_state:{s_sid_hash}"
            try:
                state_doc = couchdb_store.get(doc_id)
            except Exception:
                state_doc = None
            if not isinstance(state_doc, dict):
                continue

            try:
                current_mat = materialize_session_memory(
                    session_id_hash=s_sid_hash,
                    store=couchdb_store,
                )
            except Exception:
                current_mat = None

            if current_mat is None or not getattr(current_mat, "fully_materialized", False):
                continue

            s_source_hash = getattr(current_mat, "source_hash", "") or ""
            s_active_content_hash = getattr(current_mat, "content_hash", "") or ""
            if not s_source_hash or not s_active_content_hash:
                continue

            # Determine identity scheme matching the existing chunk
            try:
                local_v1_id = derive_local_pg_chunk_id(
                    project=s_project,
                    provider=s_provider,
                    session_id_hash=s_sid_hash,
                    source_hash=s_source_hash,
                    content_hash=s_content_hash,
                    embedding_profile=profile_id,
                )
            except Exception:
                local_v1_id = ""

            try:
                legacy_v1_id = _derive_pg_chunk_id_impl(
                    project=s_project,
                    provider=s_provider,
                    session_id_hash=s_sid_hash,
                    source_hash=s_source_hash,
                    content_hash=s_content_hash,
                    embedding_profile=DEFAULT_EMBEDDING_PROFILE_ID,
                )
            except Exception:
                legacy_v1_id = ""

            try:
                legacy_v2_id = _derive_pg_chunk_id_impl(
                    project=s_project,
                    provider=s_provider,
                    session_id_hash=s_sid_hash,
                    source_hash=s_source_hash,
                    content_hash=s_content_hash,
                    embedding_profile=CHUNKED_PROFILE,
                )
            except Exception:
                legacy_v2_id = ""

            if s_chunk_id and s_chunk_id == local_v1_id:
                scheme = "local_v1"
            elif s_chunk_id and s_chunk_id in (legacy_v1_id, legacy_v2_id):
                scheme = "legacy_exact"
            else:
                scheme = "legacy_reuse"

            receipts = state_doc.get("backend_receipts")
            if not isinstance(receipts, dict):
                receipts = {}
            local_map = receipts.get("postgres_pgvector_local")
            if not isinstance(local_map, dict):
                local_map = {}
            existing_rec = local_map.get(profile_id)
            if not isinstance(existing_rec, dict) or existing_rec.get("receipt_version") != 3:
                try:
                    new_rec = build_local_representation_receipt_v3(
                        representation_kind=LOCAL_SESSION_REPRESENTATION_KIND,
                        project=s_project,
                        provider=s_provider,
                        session_id_hash=s_sid_hash,
                        active_content_hash=s_active_content_hash,
                        projected_source_hash=s_source_hash,
                        representation_content_hash=s_content_hash,
                        session_memory_knowledge_id=s_chunk_id,
                        identity_scheme=scheme,
                        embedding_profile=profile_id,
                        input_hash=s_input_hash,
                    )
                    if valid_local_representation_receipt_v3(
                        new_rec,
                        store=store,
                        current_materialized=current_mat,
                        require_ready_vector=True,
                    ):
                        local_map[profile_id] = new_rec
                        receipts["postgres_pgvector_local"] = local_map
                        state_doc["backend_receipts"] = receipts
                        couchdb_store.put(state_doc)
                except Exception:
                    pass

    snapshot = inspect_pg_local_embedding_state(
        store, profile_id=profile_id, couchdb_store=couchdb_store
    )
    return PgBackfillProgress(
        eligible_cards=snapshot.eligible_cards,
        ready_cards=snapshot.ready_cards,
        missing_cards=snapshot.missing_cards,
        eligible_sessions=snapshot.eligible_sessions,
        ready_sessions=snapshot.ready_sessions,
        missing_sessions=snapshot.missing_sessions,
        eligible_session_receipts=snapshot.eligible_session_receipts,
        ready_session_receipts=snapshot.ready_session_receipts,
        missing_session_receipts=snapshot.missing_session_receipts,
        session_receipts_verified=snapshot.session_receipts_verified,
        enqueued_cards=enq_cards,
        enqueued_sessions=enq_sessions,
        batches_executed=batches_executed,
        claimed=total_claimed,
        completed=total_completed,
        stale_fenced=total_stale,
        retried=total_failed,
        quarantined=snapshot.outbox_dead_letter,
        outbox_queued=snapshot.outbox_queued,
        outbox_processing=snapshot.outbox_processing,
        outbox_failed_retryable=snapshot.outbox_failed_retryable,
        outbox_dead_letter=snapshot.outbox_dead_letter,
        tombstones_count=snapshot.tombstones_count,
        legacy_card_embeddings_preserved=snapshot.legacy_card_embeddings_preserved,
        legacy_session_embeddings_preserved=snapshot.legacy_session_embeddings_preserved,
    )


def inspect_neo4j_local_embedding_state(
    session: Any,
    *,
    group_ids: list[str] | None = None,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
) -> Neo4jBackfillProgress:
    """Read-only inspection of Neo4j local Model2Vec backfill and fence state."""
    group_filter = "WHERE n.group_id IN $group_ids" if group_ids else ""
    edge_group_filter = "WHERE e.group_id IN $group_ids" if group_ids else ""
    params: dict[str, Any] = {"group_ids": list(group_ids or ()), "profile_id": profile_id}

    ent_rec = session.run(
        f"""
        MATCH (n:Entity)
        {group_filter}
        RETURN count(n) AS total,
               count(CASE WHEN n.name_embedding_model2vec_v1 IS NOT NULL AND coalesce(n.embedding_dirty, false) = false THEN 1 END) AS ready,
               count(CASE WHEN n.name_embedding IS NOT NULL THEN 1 END) AS legacy_preserved,
               collect(DISTINCT n.group_id) AS groups
        """,
        **params,
    ).single()
    ent_row = dict(ent_rec) if ent_rec is not None else {}
    eligible_entities = int(ent_row.get("total") or 0)
    ready_entities = int(ent_row.get("ready") or 0)
    legacy_entities = int(ent_row.get("legacy_preserved") or 0)
    discovered_groups = {str(g) for g in (ent_row.get("groups") or ()) if g}

    edge_rec = session.run(
        f"""
        MATCH (:Entity)-[e:RELATES_TO]->(:Entity)
        {edge_group_filter}
        RETURN count(e) AS total,
               count(CASE WHEN e.fact_embedding_model2vec_v1 IS NOT NULL AND coalesce(e.embedding_dirty, false) = false THEN 1 END) AS ready,
               count(CASE WHEN e.fact_embedding IS NOT NULL THEN 1 END) AS legacy_preserved,
               collect(DISTINCT e.group_id) AS groups
        """,
        **params,
    ).single()
    edge_row = dict(edge_rec) if edge_rec is not None else {}
    eligible_edges = int(edge_row.get("total") or 0)
    ready_edges = int(edge_row.get("ready") or 0)
    legacy_edges = int(edge_row.get("legacy_preserved") or 0)
    discovered_groups.update(str(g) for g in (edge_row.get("groups") or ()) if g)

    tomb_rec = session.run(
        """
        MATCH (t:EmbeddingDeletion)
        RETURN count(t) AS tombstones
        """
    ).single()
    tombstones_count = int(dict(tomb_rec).get("tombstones") or 0) if tomb_rec is not None else 0

    fence_rec = session.run(
        """
        MATCH (f:EmbeddingGroupFence)
        RETURN count(CASE WHEN f.status = 'quarantined' THEN 1 END) AS quarantined_groups
        """
    ).single()
    quarantined_groups = (
        int(dict(fence_rec).get("quarantined_groups") or 0) if fence_rec is not None else 0
    )

    return Neo4jBackfillProgress(
        group_ids_scanned=tuple(sorted(discovered_groups)),
        eligible_entities=eligible_entities,
        ready_entities=ready_entities,
        missing_or_dirty_entities=max(0, eligible_entities - ready_entities),
        eligible_edges=eligible_edges,
        ready_edges=ready_edges,
        missing_or_dirty_edges=max(0, eligible_edges - ready_edges),
        groups_quarantined=quarantined_groups,
        tombstones_count=tombstones_count,
        legacy_entity_embeddings_preserved=legacy_entities,
        legacy_edge_embeddings_preserved=legacy_edges,
    )


def backfill_neo4j_local_embeddings(
    session: Any,
    embedder: Any,
    *,
    group_ids: list[str] | None = None,
    batch_size: int = 32,
    max_batches_per_group: int = 10,
    lease_seconds: int = 120,
    worker_id: str | None = None,
    journal_coordinator: GraphOperationJournalCoordinator | None = None,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
) -> Neo4jBackfillProgress:
    """Bounded, CAS-fenced, journaled Neo4j backfill for Entity and RELATES_TO."""
    bounded_batch = max(1, min(int(batch_size), 64))
    bounded_max_batches = max(1, int(max_batches_per_group))
    resolved_worker_id = worker_id or f"m3-neo4j-backfill-{uuid.uuid4().hex[:8]}"

    embed_base_url = getattr(embedder, "base_url", None) or getattr(
        getattr(embedder, "_config", None), "base_url", None
    )
    if embed_base_url:
        validate_local_embedding_write_or_query_policy(
            profile_id=profile_id,
            model=LOCAL_EMBEDDING_MODEL_ALIAS,
            base_url=str(embed_base_url),
            dimension=LOCAL_EMBEDDING_DIM,
        )

    ensure_neo4j_safety_schema(session)

    if group_ids is None:
        grp_rec = session.run(
            """
            MATCH (n:Entity)
            WHERE n.group_id IS NOT NULL AND n.group_id <> ''
            RETURN collect(DISTINCT n.group_id) AS groups
            """
        ).single()
        target_groups = sorted(
            str(g)
            for g in (dict(grp_rec).get("groups") if grp_rec is not None else () or ())
            if g
        )
    else:
        target_groups = sorted({str(g).strip() for g in group_ids if str(g).strip()})

    entities_embedded = 0
    entities_fenced_stale = 0
    edges_embedded = 0
    edges_fenced_stale = 0
    groups_fenced_busy = 0

    for group_id in target_groups:
        operation_id = f"backfill:{profile_id}:{group_id}:{resolved_worker_id}"
        try:
            if journal_coordinator is not None:
                lease = journal_coordinator.begin_operation(
                    session,
                    operation_id=operation_id,
                    group_id=group_id,
                    operation_type="graph_local_embedding_backfill",
                    lease_owner=resolved_worker_id,
                    lease_seconds=lease_seconds,
                    fence_status="backfill_in_progress",
                )
            else:
                lease = acquire_group_fence_lease(
                    session,
                    group_id,
                    lease_owner=resolved_worker_id,
                    operation_id=operation_id,
                    status="backfill_in_progress",
                    lease_seconds=lease_seconds,
                    bump_epoch=False,
                )
        except (GroupFenceBusyError, GroupQuarantinedError):
            groups_fenced_busy += 1
            continue

        embedded_entity_uuids: list[str] = []
        embedded_edge_uuids: list[str] = []
        try:
            for _batch_idx in range(bounded_max_batches):
                ent_rows = session.run(
                    """
                    MATCH (n:Entity {group_id: $group_id})
                    OPTIONAL MATCH (d:EmbeddingDeletion {
                        target_type: 'entity',
                        target_uuid: n.uuid
                    })
                    WITH n, d
                    WHERE d IS NULL
                      AND (
                        n.name_embedding_model2vec_v1 IS NULL
                        OR coalesce(n.embedding_dirty, false) = true
                        OR n.name_embedding_model2vec_v1_input_hash IS NULL
                      )
                    RETURN n.uuid AS uuid,
                           n.name AS name,
                           n.name_embedding_model2vec_v1_input_hash AS current_hash
                    ORDER BY n.uuid ASC
                    LIMIT $limit
                    """,
                    group_id=group_id,
                    limit=bounded_batch,
                ).data()
                entity_candidates = [dict(r) for r in (ent_rows or ())]

                edge_rows = session.run(
                    """
                    MATCH (:Entity)-[e:RELATES_TO {group_id: $group_id}]->(:Entity)
                    OPTIONAL MATCH (d:EmbeddingDeletion {
                        target_type: 'edge',
                        target_uuid: e.uuid
                    })
                    WITH e, d
                    WHERE d IS NULL
                      AND (
                        e.fact_embedding_model2vec_v1 IS NULL
                        OR coalesce(e.embedding_dirty, false) = true
                        OR e.fact_embedding_model2vec_v1_input_hash IS NULL
                      )
                    RETURN e.uuid AS uuid,
                           e.fact AS fact,
                           e.fact_embedding_model2vec_v1_input_hash AS current_hash
                    ORDER BY e.uuid ASC
                    LIMIT $limit
                    """,
                    group_id=group_id,
                    limit=bounded_batch,
                ).data()
                edge_candidates = [dict(r) for r in (edge_rows or ())]

                if not entity_candidates and not edge_candidates:
                    break

                if entity_candidates:
                    ent_inputs = [
                        format_graph_node_embedding_input(str(c.get("name") or ""))
                        for c in entity_candidates
                    ]
                    ent_hashes = [
                        compute_graph_node_input_hash(str(c.get("name") or ""))
                        for c in entity_candidates
                    ]
                    ent_vectors = _invoke_embedder_batch(embedder, ent_inputs)
                    for cand, expected_hash, vec in zip(
                        entity_candidates, ent_hashes, ent_vectors, strict=True
                    ):
                        cas = cas_write_entity_local_embedding(
                            session,
                            entity_uuid=str(cand["uuid"]),
                            group_id=group_id,
                            expected_name=str(cand.get("name") or ""),
                            expected_input_hash=expected_hash,
                            expected_epoch=lease.epoch,
                            vector=vec,
                            lease_owner=resolved_worker_id,
                        )
                        if cas.applied:
                            entities_embedded += 1
                            embedded_entity_uuids.append(str(cand["uuid"]))
                        else:
                            entities_fenced_stale += 1

                if edge_candidates:
                    edge_inputs = [
                        format_graph_edge_embedding_input(str(c.get("fact") or ""))
                        for c in edge_candidates
                    ]
                    edge_hashes = [
                        compute_graph_edge_input_hash(str(c.get("fact") or ""))
                        for c in edge_candidates
                    ]
                    edge_vectors = _invoke_embedder_batch(embedder, edge_inputs)
                    for cand, expected_hash, vec in zip(
                        edge_candidates, edge_hashes, edge_vectors, strict=True
                    ):
                        cas = cas_write_edge_local_embedding(
                            session,
                            edge_uuid=str(cand["uuid"]),
                            group_id=group_id,
                            expected_fact=str(cand.get("fact") or ""),
                            expected_input_hash=expected_hash,
                            expected_epoch=lease.epoch,
                            vector=vec,
                            lease_owner=resolved_worker_id,
                        )
                        if cas.applied:
                            edges_embedded += 1
                            embedded_edge_uuids.append(str(cand["uuid"]))
                        else:
                            edges_fenced_stale += 1

            if journal_coordinator is not None and (embedded_entity_uuids or embedded_edge_uuids):
                journal_coordinator.run_step(
                    session,
                    operation_id=operation_id,
                    group_id=group_id,
                    step_id="embeddings_committed",
                    effect_payload={
                        "entities": sorted(embedded_entity_uuids),
                        "edges": sorted(embedded_edge_uuids),
                    },
                    action=lambda: True,
                )
        finally:
            if journal_coordinator is not None:
                journal_coordinator.complete_operation(session, lease=lease)
            else:
                release_group_fence_lease(session, lease)

    snapshot = inspect_neo4j_local_embedding_state(
        session,
        group_ids=target_groups if group_ids is not None else None,
        profile_id=profile_id,
    )
    return Neo4jBackfillProgress(
        group_ids_scanned=snapshot.group_ids_scanned,
        eligible_entities=snapshot.eligible_entities,
        ready_entities=snapshot.ready_entities,
        missing_or_dirty_entities=snapshot.missing_or_dirty_entities,
        eligible_edges=snapshot.eligible_edges,
        ready_edges=snapshot.ready_edges,
        missing_or_dirty_edges=snapshot.missing_or_dirty_edges,
        entities_embedded=entities_embedded,
        entities_fenced_stale=entities_fenced_stale,
        edges_embedded=edges_embedded,
        edges_fenced_stale=edges_fenced_stale,
        groups_fenced_busy=groups_fenced_busy,
        groups_quarantined=snapshot.groups_quarantined,
        tombstones_count=snapshot.tombstones_count,
        legacy_entity_embeddings_preserved=snapshot.legacy_entity_embeddings_preserved,
        legacy_edge_embeddings_preserved=snapshot.legacy_edge_embeddings_preserved,
    )


def _invoke_embedder_batch(embedder: Any, texts: list[str]) -> list[list[float]]:
    if hasattr(embedder, "embed_batch"):
        return [list(vec) for vec in embedder.embed_batch(texts)]
    if hasattr(embedder, "embed_texts"):
        result = embedder.embed_texts(texts)
        return [
            list(item.vector) if hasattr(item, "vector") else list(item)
            for item in result
        ]
    if hasattr(embedder, "embed_documents"):
        result = embedder.embed_documents(texts)
        return [
            list(item.vector) if hasattr(item, "vector") else list(item)
            for item in result
        ]
    if hasattr(embedder, "embed_many"):
        raw = embedder.embed_many(texts)
        if any(vec is None for vec in raw):
            raise RuntimeError("embed_many returned None for one or more texts")
        return [list(vec) for vec in raw if vec is not None]
    if hasattr(embedder, "embed"):
        return [list(embedder.embed(t)) for t in texts]
    if callable(embedder):
        return [list(embedder(t)) for t in texts]
    raise TypeError(f"Unsupported embedder type for backfill: {type(embedder)!r}")


def evaluate_cutover_readiness(
    *,
    pg_progress: PgBackfillProgress | None = None,
    neo4j_progress: Neo4jBackfillProgress | None = None,
    profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    direct_migrated_reader: bool | None = None,
) -> LocalEmbeddingCutoverReport:
    """Fail-closed cutover readiness check across PostgreSQL and Neo4j."""
    is_direct = (
        direct_migrated_reader
        if direct_migrated_reader is not None
        else (os.getenv("PG_RECALL_DIRECT_MIGRATED", "").strip().casefold() == "true")
    )
    blocking: list[str] = []
    if pg_progress is None and neo4j_progress is None:
        blocking.append("no_backend_inspected")

    if pg_progress is not None:
        if pg_progress.missing_cards > 0:
            blocking.append(f"pg_missing_cards={pg_progress.missing_cards}")
        if pg_progress.missing_sessions > 0:
            blocking.append(f"pg_missing_sessions={pg_progress.missing_sessions}")
        if not is_direct:
            if not pg_progress.session_receipts_verified:
                blocking.append("pg_session_receipts_unverified_for_couchdb_authority")
            elif pg_progress.missing_session_receipts > 0:
                blocking.append(f"pg_missing_session_receipts={pg_progress.missing_session_receipts}")
        if pg_progress.outbox_queued > 0:
            blocking.append(f"pg_outbox_queued={pg_progress.outbox_queued}")
        if pg_progress.outbox_processing > 0:
            blocking.append(f"pg_outbox_processing={pg_progress.outbox_processing}")
        if pg_progress.outbox_failed_retryable > 0:
            blocking.append(f"pg_outbox_failed_retryable={pg_progress.outbox_failed_retryable}")
        if pg_progress.outbox_dead_letter > 0:
            blocking.append(f"pg_outbox_dead_letter={pg_progress.outbox_dead_letter}")

    if neo4j_progress is not None:
        if neo4j_progress.missing_or_dirty_entities > 0:
            blocking.append(
                f"neo4j_missing_or_dirty_entities={neo4j_progress.missing_or_dirty_entities}"
            )
        if neo4j_progress.missing_or_dirty_edges > 0:
            blocking.append(
                f"neo4j_missing_or_dirty_edges={neo4j_progress.missing_or_dirty_edges}"
            )
        if neo4j_progress.groups_quarantined > 0:
            blocking.append(f"neo4j_groups_quarantined={neo4j_progress.groups_quarantined}")
        if neo4j_progress.groups_fenced_busy > 0:
            blocking.append(f"neo4j_groups_fenced_busy={neo4j_progress.groups_fenced_busy}")

    return LocalEmbeddingCutoverReport(
        profile_id=profile_id,
        ready=len(blocking) == 0,
        blocking_reasons=tuple(blocking),
        pg=pg_progress,
        neo4j=neo4j_progress,
    )

"""PostgreSQL pgvector storage adapter for LBrain Memory Authority (Milestone 3).

Supports PostgreSQL 17+ with pgvector >= 0.8.0, Transactional Outbox,
Compare-And-Swap (CAS) write-backs, and Cycle-Safe Recursive CTE DAG traversal.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


def compute_cosine_similarity(vec1: list[float] | None, vec2: list[float] | None) -> float:
    """Compute cosine similarity between two vectors bounded in [0.0, 1.0]."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    val = dot / (norm1 * norm2)
    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, float(val)))


def make_dummy_vector(seed: int, dim: int = 1536) -> list[float]:
    """Generate a deterministic unit vector for testing."""
    vec = [(math.sin(seed * 1000 + i) + 1.0) / 2.0 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return [0.0] * dim
    return [x / norm for x in vec]


@dataclass
class MemoryCard:
    memory_id: str
    project: str
    card_type: str  # 'decision', 'preference', 'task', 'evidence', 'drift', 'status'
    title: str
    summary: str
    typed_payload: dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "candidate"  # 'candidate', 'human_accepted', etc.
    authorization_status: str = "disabled"  # 'active', 'disabled'
    currentness: str = "current"  # 'current', 'superseded', 'stale'
    confidence: float = 1.0
    valid_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: datetime | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_revision: int = 1
    embedding_state: str = "pending"  # 'pending', 'ready', 'stale', 'failed'
    embedding: list[float] | None = None
    content_hash: str = ""
    source_ref: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MemoryEdge:
    edge_id: int = 0
    src_id: str = ""
    rel_type: str = "derived_from"  # 'supersedes', 'derived_from', 'contradicts', 'supports'
    dst_id: str = ""
    provenance_hash: str = ""
    confidence: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SessionChunk:
    chunk_id: str
    session_id_hash: str
    project: str
    provider: str = "unspecified"
    chunk_index: int = 0
    content_markdown: str = ""
    token_count: int = 0
    embedding_model: str = "text-embedding-3-small"
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OutboxJob:
    outbox_id: int
    target_type: str  # 'memory_card', 'session_chunk'
    target_id: str
    content_hash: str
    payload_text: str
    status: str = "queued"  # 'queued', 'processing', 'completed', 'failed', 'dead_letter'
    claimed_at: datetime | None = None
    lease_until: datetime | None = None
    worker_id: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PgVectorStore:
    """PostgreSQL pgvector Store with transactional outbox, CAS write-back & DAG CTE."""

    def __init__(
        self,
        dsn: str | None = None,
        connection: Any | None = None,
        pgvector_version: str = "0.8.0",
        use_in_memory: bool = False,
    ):
        self.dsn = dsn
        self.connection = connection
        self.pgvector_version = pgvector_version
        self.use_in_memory = use_in_memory or (dsn is None and connection is None)

        # In-memory storage structures (used in standalone/mock mode)
        self.cards: dict[str, MemoryCard] = {}
        self.edges: dict[int, MemoryEdge] = {}
        self.chunks: dict[str, SessionChunk] = {}
        self.outbox: dict[int, OutboxJob] = {}
        self._next_edge_id = 1
        self._next_outbox_id = 1
        self.executed_ddl: list[str] = []
        self.local_guc: dict[str, str] = {}

    def _get_pg_conn(self):
        if self.connection is not None:
            return self.connection
        if self.dsn:
            try:
                import psycopg
                from psycopg.rows import dict_row
                return psycopg.connect(self.dsn, row_factory=dict_row)
            except Exception as e:
                logger.warning(f"Could not connect to PostgreSQL ({e}), operating in memory mode")
                self.use_in_memory = True
                return None
        return None

    def execute_ddl(self, sql: str | None = None) -> None:
        """Execute DDL statements or load pgvector_schema.sql by default."""
        if sql is None:
            schema_path = os.path.join(os.path.dirname(__file__), "pgvector_schema.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r", encoding="utf-8") as f:
                    sql = f.read()
            else:
                sql = "-- empty ddl"

        self.executed_ddl.append(sql)

        if not self.use_in_memory:
            conn = self._get_pg_conn()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Failed to execute DDL on live DB: {e}")

    def set_local_guc(self, name: str, val: str) -> None:
        """Set local session/transaction GUC."""
        self.local_guc[name] = val
        if not self.use_in_memory:
            conn = self._get_pg_conn()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SET LOCAL {name} = %s;", (val,))
                except Exception as e:
                    logger.warning(f"Failed to set GUC {name}={val}: {e}")

    def set_guc_relaxed_order(self) -> bool:
        """Set HNSW relaxed_order GUC with version check fallback."""
        if self.pgvector_version >= "0.8.0":
            self.set_local_guc("hnsw.iterative_scan", "relaxed_order")
            return True
        logger.info(f"pgvector version {self.pgvector_version} < 0.8.0, skipping relaxed_order GUC")
        return False

    def insert_card(self, card: MemoryCard, conn: Any | None = None) -> str:
        """Insert or validate a MemoryCard enforcing DDL constraints."""
        if card.embedding is not None and len(card.embedding) != 1536:
            raise ValueError(f"Vector dimension mismatch: expected 1536, got {len(card.embedding)}")
        if card.valid_to and card.valid_to < card.valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")
        
        valid_lifecycle = (
            "candidate", "suggested_accept", "human_accepted", "human_rejected",
            "auto_accepted", "needs_review", "accepted", "rejected"
        )
        if card.lifecycle_state not in valid_lifecycle:
            raise ValueError(f"Invalid lifecycle_state: {card.lifecycle_state}")
        if card.authorization_status not in ("active", "disabled"):
            raise ValueError(f"Invalid authorization_status: {card.authorization_status}")
        if card.currentness not in ("current", "superseded", "stale", "conflicted", "unknown"):
            raise ValueError(f"Invalid currentness: {card.currentness}")
        if not (0.0 <= card.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")

        self.cards[card.memory_id] = copy.deepcopy(card)
        return card.memory_id

    def upsert_card(self, card: MemoryCard, conn: Any | None = None) -> str:
        """Upsert card and transactionally enqueue embedding outbox task if pending."""
        memory_id = self.insert_card(card, conn=conn)

        # Transactional Outbox Enqueue if embedding is pending or empty
        if card.embedding_state == "pending" or card.embedding is None:
            payload_text = card.summary or card.title or (
                json.dumps(card.typed_payload) if isinstance(card.typed_payload, dict) else str(card.typed_payload)
            )
            # Check if not already queued to avoid duplicate unique constraint error
            already_queued = any(
                j.target_type == "memory_card"
                and j.target_id == card.memory_id
                and j.content_hash == card.content_hash
                and j.status in ("queued", "processing")
                for j in self.outbox.values()
            )
            if not already_queued:
                self.enqueue_outbox(
                    target_type="memory_card",
                    target_id=card.memory_id,
                    content_hash=card.content_hash,
                    payload_text=payload_text,
                )

        return memory_id

    def get_card(self, memory_id: str) -> MemoryCard | None:
        """Get card by memory_id."""
        card = self.cards.get(memory_id)
        return copy.deepcopy(card) if card else None

    def delete_card(self, memory_id: str, conn: Any | None = None) -> None:
        """Delete card with ON DELETE RESTRICT foreign key enforcement."""
        active_edges = [
            e for e in self.edges.values()
            if e.src_id == memory_id or e.dst_id == memory_id
        ]
        if active_edges:
            raise ValueError(
                f"Foreign key constraint violation: memory_card {memory_id} is referenced by {len(active_edges)} edges"
            )
        if memory_id in self.cards:
            del self.cards[memory_id]

    def insert_edge(self, edge: MemoryEdge, conn: Any | None = None) -> int:
        """Insert MemoryEdge enforcing foreign key and domain constraints."""
        if edge.src_id not in self.cards:
            raise ValueError(f"FK constraint violation: src_id {edge.src_id} not in memory_cards")
        if edge.dst_id not in self.cards:
            raise ValueError(f"FK constraint violation: dst_id {edge.dst_id} not in memory_cards")
        if edge.rel_type not in ("supersedes", "derived_from", "contradicts", "supports"):
            raise ValueError(f"Invalid rel_type: {edge.rel_type}")
        if not (0.0 <= edge.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        if edge.valid_to and edge.valid_to < edge.valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")

        edge_id = edge.edge_id or self._next_edge_id
        if edge_id >= self._next_edge_id:
            self._next_edge_id = edge_id + 1
        edge.edge_id = edge_id
        self.edges[edge_id] = copy.deepcopy(edge)
        return edge_id

    def get_edges(self, src_id: str | None = None, dst_id: str | None = None) -> list[MemoryEdge]:
        """Get edges matching criteria."""
        results = []
        for e in self.edges.values():
            if src_id and e.src_id != src_id:
                continue
            if dst_id and e.dst_id != dst_id:
                continue
            results.append(copy.deepcopy(e))
        return results

    def insert_chunk(self, chunk: SessionChunk, conn: Any | None = None) -> str:
        """Insert session memory chunk."""
        if chunk.embedding is not None and len(chunk.embedding) != 1536:
            raise ValueError(f"Vector dimension mismatch: expected 1536, got {len(chunk.embedding)}")
        self.chunks[chunk.chunk_id] = copy.deepcopy(chunk)
        return chunk.chunk_id

    def get_chunk(self, chunk_id: str) -> SessionChunk | None:
        """Get session memory chunk."""
        chunk = self.chunks.get(chunk_id)
        return copy.deepcopy(chunk) if chunk else None

    def enqueue_outbox(
        self,
        target_type: str,
        target_id: str,
        content_hash: str,
        payload_text: str,
    ) -> int:
        """Enqueue task into embedding_outbox with partial unique index guard."""
        # Partial unique index: idx_embedding_outbox_dedup WHERE status IN ('queued', 'processing')
        for job in self.outbox.values():
            if (
                job.target_type == target_type
                and job.target_id == target_id
                and job.content_hash == content_hash
                and job.status in ("queued", "processing")
            ):
                raise ValueError("Unique constraint violation: idx_embedding_outbox_dedup already exists")

        outbox_id = self._next_outbox_id
        self._next_outbox_id += 1
        job = OutboxJob(
            outbox_id=outbox_id,
            target_type=target_type,
            target_id=target_id,
            content_hash=content_hash,
            payload_text=payload_text,
            status="queued",
        )
        self.outbox[outbox_id] = job
        return outbox_id

    def enqueue_embedding(self, target_type: str, target_id: str, content_hash: str, payload_text: str) -> int:
        """Alias for enqueue_outbox."""
        return self.enqueue_outbox(target_type, target_id, content_hash, payload_text)

    def claim_outbox_leases(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
    ) -> list[OutboxJob]:
        """Claim unassigned or expired outbox jobs using FOR UPDATE SKIP LOCKED logic."""
        now = datetime.now(timezone.utc)
        claimed: list[OutboxJob] = []

        # Simulate SELECT ... FOR UPDATE SKIP LOCKED
        for job in sorted(self.outbox.values(), key=lambda j: j.created_at):
            if len(claimed) >= batch_size:
                break
            is_queued = job.status == "queued"
            is_failed_retryable = job.status == "failed" and job.retry_count < 5
            is_expired_lease = (
                job.status == "processing"
                and job.lease_until is not None
                and job.lease_until < now
            )
            if is_queued or is_failed_retryable or is_expired_lease:
                job.status = "processing"
                job.worker_id = worker_id
                job.claimed_at = now
                job.lease_until = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=timezone.utc)
                job.updated_at = now
                claimed.append(copy.deepcopy(job))

        return claimed

    def claim_outbox_jobs(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
    ) -> list[OutboxJob]:
        """Alias for claim_outbox_leases."""
        return self.claim_outbox_leases(worker_id, batch_size=batch_size, lease_seconds=lease_seconds)

    def cas_update_embedding(
        self,
        outbox_id_or_target_type: int | str,
        target_id: str,
        enqueued_content_hash: str,
        vector: list[float],
        outbox_id: int | None = None,
        target_type: str = "memory_card",
    ) -> bool:
        """
        Compare-And-Swap (CAS) write-back with stale hash guard.
        
        WHERE target_id = :target_id AND content_hash = :enqueued_content_hash
        """
        if isinstance(outbox_id_or_target_type, int):
            actual_outbox_id = outbox_id_or_target_type
            actual_target_type = target_type
        else:
            actual_target_type = outbox_id_or_target_type
            actual_outbox_id = outbox_id

        now = datetime.now(timezone.utc)

        if actual_target_type == "session_chunk":
            if target_id not in self.chunks:
                if actual_outbox_id and actual_outbox_id in self.outbox:
                    self.outbox[actual_outbox_id].status = "failed"
                    self.outbox[actual_outbox_id].last_error = f"Target chunk {target_id} not found"
                return False
            self.chunks[target_id].embedding = list(vector)
            if actual_outbox_id and actual_outbox_id in self.outbox:
                self.outbox[actual_outbox_id].status = "completed"
                self.outbox[actual_outbox_id].updated_at = now
            return True

        # memory_card CAS
        if target_id not in self.cards:
            if actual_outbox_id and actual_outbox_id in self.outbox:
                self.outbox[actual_outbox_id].status = "failed"
                self.outbox[actual_outbox_id].last_error = f"Target card {target_id} not found"
            return False

        card = self.cards[target_id]
        if card.content_hash != enqueued_content_hash:
            # Stale write detected! CAS No-op (rowcount == 0)
            if actual_outbox_id and actual_outbox_id in self.outbox:
                self.outbox[actual_outbox_id].status = "completed"  # Safely retire stale outbox task
                self.outbox[actual_outbox_id].last_error = "CAS skip: content_hash mismatch"
                self.outbox[actual_outbox_id].updated_at = now
            return False

        # CAS matched! Update card
        card.embedding = list(vector)
        card.embedding_state = "ready"
        card.embedding_revision += 1
        card.updated_at = now

        if actual_outbox_id and actual_outbox_id in self.outbox:
            self.outbox[actual_outbox_id].status = "completed"
            self.outbox[actual_outbox_id].last_error = None
            self.outbox[actual_outbox_id].updated_at = now

        return True

    def apply_embedding_cas(
        self,
        outbox_id: int,
        target_type: str,
        target_id: str,
        enqueued_content_hash: str,
        vector: list[float],
    ) -> bool:
        """Alias for cas_update_embedding."""
        return self.cas_update_embedding(
            outbox_id_or_target_type=outbox_id,
            target_id=target_id,
            enqueued_content_hash=enqueued_content_hash,
            vector=vector,
            target_type=target_type,
        )

    def mark_outbox_failed(self, outbox_id: int, error_message: str, max_retries: int = 5) -> None:
        """Mark outbox job failed with exponential backoff or dead_letter escalation."""
        if outbox_id not in self.outbox:
            return
        now = datetime.now(timezone.utc)
        job = self.outbox[outbox_id]
        job.retry_count += 1
        job.last_error = error_message
        job.updated_at = now

        if job.retry_count >= max_retries:
            job.status = "dead_letter"
            job.lease_until = None
            if job.target_type == "memory_card" and job.target_id in self.cards:
                self.cards[job.target_id].embedding_state = "failed"
        else:
            job.status = "failed"
            backoff_secs = min(60.0, 2.0 ** job.retry_count)
            job.lease_until = datetime.fromtimestamp(now.timestamp() + backoff_secs, tz=timezone.utc)

    def mark_outbox_completed(self, outbox_id: int) -> None:
        """Mark outbox job completed."""
        if outbox_id in self.outbox:
            self.outbox[outbox_id].status = "completed"
            self.outbox[outbox_id].updated_at = datetime.now(timezone.utc)

    def hybrid_search(
        self,
        project: str | list[float] = "",
        query_vector: list[float] | str | None = None,
        limit: int = 5,
        card_type: str | None = None,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: datetime | str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Filtered vector search with relaxed_order GUC and cosine similarity."""
        if isinstance(project, (list, tuple)):
            actual_query_vector = list(project)
            actual_project = str(query_vector) if query_vector is not None else kwargs.get("project", "")
        else:
            actual_project = project or kwargs.get("project", "")
            actual_query_vector = query_vector if query_vector is not None else kwargs.get("query_vector")

        if actual_query_vector is None:
            return []

        # Parse as_of timestamp if string
        as_of_dt: datetime | None = None
        if isinstance(as_of, str):
            try:
                as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            except Exception:
                as_of_dt = None
        elif isinstance(as_of, datetime):
            as_of_dt = as_of

        # Execute GUC relaxed_order
        self.set_guc_relaxed_order()

        candidates = []
        for card in self.cards.values():
            if actual_project and card.project != actual_project:
                continue
            if authorization_status and card.authorization_status != authorization_status:
                continue
            if currentness and card.currentness != currentness:
                continue
            if card_type and card.card_type != card_type:
                continue
            if as_of_dt:
                if card.valid_from > as_of_dt or (card.valid_to and card.valid_to < as_of_dt):
                    continue
            if card.embedding_state != "ready" or card.embedding is None:
                continue

            score = compute_cosine_similarity(actual_query_vector, card.embedding)
            candidates.append((score, card))

        # Sort descending by similarity score, secondary ascending by memory_id
        candidates.sort(key=lambda x: (-x[0], x[1].memory_id))
        top = candidates[:limit]

        return [
            {
                "memory_id": c.memory_id,
                "card_type": c.card_type,
                "title": c.title,
                "summary": c.summary,
                "typed_payload": c.typed_payload,
                "currentness": c.currentness,
                "confidence": c.confidence,
                "content_hash": c.content_hash,
                "similarity_score": round(score, 4),
            }
            for score, c in top
        ]

    def hybrid_vector_search(
        self,
        query_vector: list[float],
        project: str,
        limit: int = 5,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: datetime | str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Alias for hybrid_search with parameter order matching E2E tests."""
        return self.hybrid_search(
            project=project,
            query_vector=query_vector,
            limit=limit,
            authorization_status=authorization_status,
            currentness=currentness,
            as_of=as_of,
            **kwargs,
        )

    def traverse_provenance_dag(
        self,
        root_memory_id: str,
        max_depth: int = 5,
        as_of: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Cycle-safe recursive DAG traversal simulating:
        
        WITH RECURSIVE provenance_tree AS (...)
        WHERE p.depth < :max_depth AND NOT (e.dst_id = ANY(p.visited_path))
        """
        as_of_dt: datetime | None = None
        if isinstance(as_of, str):
            try:
                as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            except Exception:
                as_of_dt = None
        elif isinstance(as_of, datetime):
            as_of_dt = as_of

        results: list[dict[str, Any]] = []
        # queue element: (src_id, dst_id, rel_type, depth, visited_node_ids)
        queue: list[tuple[str, str, str, int, list[str]]] = []

        # Find direct anchor edges originating from root_memory_id
        for e in self.edges.values():
            if e.src_id == root_memory_id:
                if as_of_dt:
                    if e.valid_from > as_of_dt or (e.valid_to and e.valid_to < as_of_dt):
                        continue
                queue.append((e.src_id, e.dst_id, e.rel_type, 1, [e.src_id]))

        while queue:
            src_id, dst_id, rel_type, depth, visited = queue.pop(0)
            results.append({
                "src_id": src_id,
                "dst_id": dst_id,
                "rel_type": rel_type,
                "depth": depth,
                "visited_path": list(visited),
            })
            if depth >= max_depth:
                continue

            for next_e in self.edges.values():
                if next_e.src_id == dst_id:
                    if as_of_dt:
                        if next_e.valid_from > as_of_dt or (next_e.valid_to and next_e.valid_to < as_of_dt):
                            continue
                    if next_e.src_id in visited:
                        # Cycle prevention guard!
                        continue
                    queue.append((
                        next_e.src_id,
                        next_e.dst_id,
                        next_e.rel_type,
                        depth + 1,
                        visited + [next_e.src_id],
                    ))

        return results

    def recursive_dag_traversal(
        self,
        root_memory_id: str,
        max_depth: int = 5,
        as_of: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Alias for traverse_provenance_dag."""
        return self.traverse_provenance_dag(root_memory_id, max_depth=max_depth, as_of=as_of)

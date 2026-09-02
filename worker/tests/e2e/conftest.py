from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest


def sha256_str(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm1 * norm2)))


def make_dummy_vector(seed: int, dim: int = 1536) -> list[float]:
    # Deterministic unit vector
    vec = [(math.sin(seed * 1000 + i) + 1.0) / 2.0 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return [0.0] * dim
    return [x / norm for x in vec]


@dataclass
class MemoryCard:
    memory_id: str
    project: str
    card_type: str  # 'decision', 'preference', 'task', 'evidence'
    title: str
    summary: str
    typed_payload: dict[str, Any]
    lifecycle_state: str = "candidate"  # 'candidate', 'human_accepted'
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
    edge_id: int
    src_id: str
    rel_type: str  # 'supersedes', 'derived_from', 'contradicts', 'supports'
    dst_id: str
    provenance_hash: str
    confidence: float = 1.0
    valid_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SessionChunk:
    chunk_id: str
    session_id_hash: str
    project: str
    provider: str
    chunk_index: int
    content_markdown: str
    embedding_model: str
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


class InMemoryPostgresStore:
    def __init__(self):
        self.cards: dict[str, MemoryCard] = {}
        self.edges: dict[int, MemoryEdge] = {}
        self.chunks: dict[str, SessionChunk] = {}
        self.outbox: dict[int, OutboxJob] = {}
        self._next_edge_id = 1
        self._next_outbox_id = 1
        self.executed_ddl: list[str] = []
        self.local_guc: dict[str, str] = {}

    def execute_ddl(self, sql: str) -> None:
        self.executed_ddl.append(sql)

    def set_local_guc(self, name: str, val: str) -> None:
        self.local_guc[name] = val

    def insert_card(self, card: MemoryCard) -> str:
        if card.embedding is not None and len(card.embedding) != 1536:
            raise ValueError(f"Vector dimension mismatch: expected 1536, got {len(card.embedding)}")
        if card.valid_to and card.valid_to < card.valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")
        if card.lifecycle_state not in ("candidate", "human_accepted"):
            raise ValueError(f"Invalid lifecycle_state: {card.lifecycle_state}")
        if card.authorization_status not in ("active", "disabled"):
            raise ValueError(f"Invalid authorization_status: {card.authorization_status}")
        self.cards[card.memory_id] = copy.deepcopy(card)
        return card.memory_id

    def insert_edge(self, edge: MemoryEdge) -> int:
        if edge.src_id not in self.cards:
            raise ValueError(f"FK constraint violation: src_id {edge.src_id} not in memory_cards")
        if edge.dst_id not in self.cards:
            raise ValueError(f"FK constraint violation: dst_id {edge.dst_id} not in memory_cards")
        if edge.rel_type not in ("supersedes", "derived_from", "contradicts", "supports"):
            raise ValueError(f"Invalid rel_type: {edge.rel_type}")
        edge.edge_id = self._next_edge_id
        self._next_edge_id += 1
        self.edges[edge.edge_id] = copy.deepcopy(edge)
        return edge.edge_id

    def delete_card(self, memory_id: str) -> None:
        # ON DELETE RESTRICT
        active_edges = [e for e in self.edges.values() if e.src_id == memory_id or e.dst_id == memory_id]
        if active_edges:
            raise ValueError(f"Foreign key constraint violation: memory_card {memory_id} is referenced by {len(active_edges)} edges")
        if memory_id in self.cards:
            del self.cards[memory_id]

    def insert_chunk(self, chunk: SessionChunk) -> str:
        if chunk.embedding is not None and len(chunk.embedding) != 1536:
            raise ValueError(f"Vector dimension mismatch: expected 1536, got {len(chunk.embedding)}")
        self.chunks[chunk.chunk_id] = copy.deepcopy(chunk)
        return chunk.chunk_id

    def enqueue_outbox(self, target_type: str, target_id: str, content_hash: str, payload_text: str) -> int:
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

    def claim_outbox_jobs(self, worker_id: str, batch_size: int = 10, lease_seconds: int = 30) -> list[OutboxJob]:
        now = datetime.now(timezone.utc)
        claimed: list[OutboxJob] = []
        # Simulate FOR UPDATE SKIP LOCKED
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

    def cas_update_embedding(
        self,
        outbox_id: int,
        target_id: str,
        enqueued_content_hash: str,
        vector: list[float],
    ) -> bool:
        """
        Compare-And-Swap (CAS) write-back.
        Only updates memory_cards if content_hash matches enqueued_content_hash.
        """
        if target_id not in self.cards:
            if outbox_id in self.outbox:
                self.outbox[outbox_id].status = "failed"
                self.outbox[outbox_id].last_error = f"Target {target_id} not found"
            return False

        card = self.cards[target_id]
        if card.content_hash != enqueued_content_hash:
            # Stale write detected! CAS No-op
            if outbox_id in self.outbox:
                self.outbox[outbox_id].status = "completed"  # Stale job is retired safely
                self.outbox[outbox_id].last_error = "CAS skip: content_hash mismatch"
                self.outbox[outbox_id].updated_at = datetime.now(timezone.utc)
            return False

        # CAS matched!
        card.embedding = list(vector)
        card.embedding_state = "ready"
        card.embedding_revision += 1
        card.updated_at = datetime.now(timezone.utc)

        if outbox_id in self.outbox:
            self.outbox[outbox_id].status = "completed"
            self.outbox[outbox_id].updated_at = datetime.now(timezone.utc)
        return True

    def recursive_dag_traversal(
        self,
        root_memory_id: str,
        max_depth: int = 5,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Simulates:
        WITH RECURSIVE provenance_tree AS (...)
        WHERE p.depth < 5 AND NOT (e.src_id = ANY(p.visited_path))
        """
        results: list[dict[str, Any]] = []
        queue: list[tuple[str, str, str, int, list[str]]] = []

        # Find direct root edges
        for e in self.edges.values():
            if e.src_id == root_memory_id:
                if as_of:
                    if e.valid_from > as_of or (e.valid_to and e.valid_to < as_of):
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
                    if as_of:
                        if next_e.valid_from > as_of or (next_e.valid_to and next_e.valid_to < as_of):
                            continue
                    if next_e.src_id in visited:
                        # Cycle prevention!
                        continue
                    queue.append((
                        next_e.src_id,
                        next_e.dst_id,
                        next_e.rel_type,
                        depth + 1,
                        visited + [next_e.src_id],
                    ))

        return results

    def hybrid_vector_search(
        self,
        query_vector: list[float],
        project: str,
        limit: int = 5,
        authorization_status: str = "active",
        currentness: str = "current",
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        candidates = []
        for card in self.cards.values():
            if card.project != project:
                continue
            if authorization_status and card.authorization_status != authorization_status:
                continue
            if currentness and card.currentness != currentness:
                continue
            if as_of:
                if card.valid_from > as_of or (card.valid_to and card.valid_to < as_of):
                    continue
            if card.embedding_state != "ready" or card.embedding is None:
                continue

            score = compute_cosine_similarity(query_vector, card.embedding)
            candidates.append((score, card))

        # Sort by similarity descending, secondary by memory_id deterministic
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


class InMemoryQdrantStore:
    def __init__(self):
        self.vectors: dict[str, tuple[list[float], dict[str, Any]]] = {}

    def upsert(self, point_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        self.vectors[point_id] = (list(vector), copy.deepcopy(payload))

    def search(self, query_vector: list[float], limit: int = 5, project_filter: str | None = None) -> list[dict[str, Any]]:
        scored = []
        for pid, (vec, payload) in self.vectors.items():
            if project_filter and payload.get("project") != project_filter:
                continue
            sim = compute_cosine_similarity(query_vector, vec)
            scored.append((sim, pid, payload))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [
            {"id": pid, "score": round(sim, 4), "payload": payload}
            for sim, pid, payload in scored[:limit]
        ]


class SlimSerializer:
    @staticmethod
    def serialize_slim(
        project: str,
        decisions: list[dict[str, Any]],
        preferences: list[dict[str, Any]],
        guardrails: list[str],
        recent_context: str = "",
        gaps: list[str] | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        start_idx = 0
        if cursor:
            try:
                decoded = base64.b64decode(cursor).decode("utf-8")
                start_idx = int(decoded.split(":")[-1])
            except Exception:
                start_idx = 0

        paged_decisions = decisions[start_idx : start_idx + limit]
        has_more = (start_idx + limit) < len(decisions)
        next_cursor = (
            base64.b64encode(f"offset:{start_idx + limit}".encode("utf-8")).decode("utf-8")
            if has_more
            else None
        )

        slim_decisions = []
        for d in paged_decisions:
            slim_decisions.append({
                "id": d.get("memory_id") or d.get("id"),
                "title": d.get("title", ""),
                "decision": d.get("typed_payload", {}).get("decision") or d.get("summary", ""),
                "rationale": d.get("typed_payload", {}).get("rationale", ""),
                "currentness": d.get("currentness", "current"),
                "content_hash": d.get("content_hash", ""),
            })

        slim_preferences = [
            {
                "rule": p.get("summary") or p.get("rule", ""),
                "scope": p.get("typed_payload", {}).get("scope") or p.get("scope", "general"),
            }
            for p in preferences
        ]

        payload = {
            "schema_version": "lbrain_slim_context.v1",
            "project": project,
            "recent_context": recent_context or f"Session context for {project}",
            "decisions": slim_decisions,
            "preferences": slim_preferences,
            "active_guardrails": guardrails,
            "gaps": gaps or [],
            "has_more": has_more,
            "next_cursor": next_cursor,
        }
        return payload

    @staticmethod
    def serialize_with_evidence(
        project: str,
        decisions: list[dict[str, Any]],
        preferences: list[dict[str, Any]],
        guardrails: list[str],
        edges: list[dict[str, Any]],
        evidence_hashes: list[str],
        source_refs: list[dict[str, Any]],
        recent_context: str = "",
        gaps: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        base = SlimSerializer.serialize_slim(
            project=project,
            decisions=decisions,
            preferences=preferences,
            guardrails=guardrails,
            recent_context=recent_context,
            gaps=gaps,
            limit=limit,
        )
        base["schema_version"] = "lbrain_evidence_context.v1"
        base["evidence_hashes"] = evidence_hashes
        base["edges"] = edges
        base["source_refs"] = source_refs
        return base


class MockMCPServer:
    PUBLIC_TOOLS = ("brain.resolve", "memory_candidate_create")
    ADMIN_TOOLS = (
        "memory_candidate_approve",
        "memory_candidate_reject",
        "memory_candidate_auto_accept",
        "memory_supersede_commit",
        "memory_stale_commit",
        "brain_permission_sensitive_audit_probe",
        "brain_corpus_ingest_plan",
        "brain_object_decision_commit",
        "brain_approval_board_decide",
    )

    def __init__(self, store: InMemoryPostgresStore):
        self.store = store
        self._cand_counter = 0

    def list_public_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "brain.resolve",
                "description": "Unified read tool with modes context, query, list and tiered serializers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "mode": {"type": "string", "enum": ["context", "query", "list"], "default": "context"},
                        "project": {"type": "string"},
                        "response_mode": {"type": "string", "enum": ["slim", "with_evidence"], "default": "slim"},
                        "limit": {"type": "integer", "default": 5},
                        "as_of": {"type": "string"},
                    },
                    "required": ["project"],
                },
            },
            {
                "name": "memory_candidate_create",
                "description": "Proposal-only write tool strictly enforcing candidate/disabled state.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "card_type": {"type": "string", "enum": ["decision", "preference", "task", "evidence"]},
                        "project": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "typed_payload": {"type": "object"},
                        "content_hash": {"type": "string", "pattern": "^sha256:"},
                        "source_ref": {"type": "object"},
                        "span_ref": {"type": "object"},
                        "proposer": {"type": "string", "default": "unspecified"},
                    },
                    "required": ["card_type", "project", "title", "summary", "typed_payload", "content_hash"],
                },
            },
        ]

    def handle_public_request(self, message: dict[str, Any]) -> dict[str, Any]:
        msg_id = message.get("id", 1)
        method = message.get("method")
        params = message.get("params", {})

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": self.list_public_tools()}}

        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": f"Invalid Request method: {method}"}}

        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name not in self.PUBLIC_TOOLS:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Tool '{tool_name}' not found or unauthorized"}}

        if tool_name == "brain.resolve":
            return self._handle_brain_resolve(msg_id, args)
        elif tool_name == "memory_candidate_create":
            return self._handle_candidate_create(msg_id, args)

        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Unknown tool"}}

    def handle_admin_request(self, message: dict[str, Any], auth_token: str | None) -> dict[str, Any]:
        msg_id = message.get("id", 1)
        if auth_token != "lbrain_admin":
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": "Unauthorized: requires lbrain_admin identity"}}

        method = message.get("method")
        params = message.get("params", {})

        if method == "tools/list":
            tools = self.list_public_tools() + [{"name": name, "description": "Admin tool"} for name in self.ADMIN_TOOLS]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}

        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": "Invalid method"}}

        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "memory_candidate_approve":
            memory_id = args.get("memory_id")
            if not memory_id or memory_id not in self.store.cards:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Card {memory_id} not found"}}
            card = self.store.cards[memory_id]
            card.lifecycle_state = "human_accepted"
            card.authorization_status = "active"
            card.updated_at = datetime.now(timezone.utc)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"status": "approved", "memory_id": memory_id}}

        if tool_name == "memory_candidate_reject":
            memory_id = args.get("memory_id")
            if not memory_id or memory_id not in self.store.cards:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Card {memory_id} not found"}}
            card = self.store.cards[memory_id]
            card.lifecycle_state = "rejected"
            card.authorization_status = "disabled"
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"status": "rejected", "memory_id": memory_id}}

        if tool_name == "memory_supersede_commit":
            target_id = args.get("target_id")
            superseded_id = args.get("superseded_id")
            if target_id not in self.store.cards or superseded_id not in self.store.cards:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Invalid card ids"}}
            self.store.cards[superseded_id].currentness = "superseded"
            self.store.insert_edge(
                MemoryEdge(
                    edge_id=0,
                    src_id=target_id,
                    rel_type="supersedes",
                    dst_id=superseded_id,
                    provenance_hash=sha256_str(f"supersede:{target_id}->{superseded_id}"),
                )
            )
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"status": "committed", "target_id": target_id, "superseded_id": superseded_id}}

        return {"jsonrpc": "2.0", "id": msg_id, "result": {"status": "admin_tool_executed", "tool": tool_name}}

    def _handle_brain_resolve(self, msg_id: Any, args: dict[str, Any]) -> dict[str, Any]:
        project = args.get("project")
        if not project:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Missing required field: project"}}

        mode = args.get("mode", "context")
        if mode not in ("context", "query", "list"):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Invalid mode: {mode}"}}

        response_mode = args.get("response_mode", "slim")
        if response_mode not in ("slim", "with_evidence"):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Invalid response_mode: {response_mode}"}}

        limit = args.get("limit", 5)
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "limit must be integer between 1 and 100"}}

        as_of_str = args.get("as_of")
        as_of_dt = None
        if as_of_str:
            try:
                as_of_dt = datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
            except Exception:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Invalid ISO format for as_of"}}

        # Retrieve active cards for project
        cards = [
            c for c in self.store.cards.values()
            if c.project == project and c.authorization_status == "active" and c.currentness == "current"
        ]
        if as_of_dt:
            cards = [c for c in cards if c.valid_from <= as_of_dt and (c.valid_to is None or c.valid_to >= as_of_dt)]

        decisions = [
            {
                "memory_id": c.memory_id,
                "title": c.title,
                "summary": c.summary,
                "typed_payload": c.typed_payload,
                "currentness": c.currentness,
                "content_hash": c.content_hash,
            }
            for c in cards if c.card_type == "decision"
        ]
        preferences = [
            {
                "rule": c.summary,
                "scope": c.typed_payload.get("scope", "general"),
            }
            for c in cards if c.card_type == "preference"
        ]
        guardrails = ["agents_use_brain_resolve", "ledger_is_single_authority"]

        if response_mode == "with_evidence":
            edges = [
                {"src_id": e.src_id, "rel_type": e.rel_type, "dst_id": e.dst_id, "provenance_hash": e.provenance_hash}
                for e in self.store.edges.values()
            ]
            evidence_hashes = [c.content_hash for c in cards if c.content_hash]
            source_refs = [c.source_ref for c in cards if c.source_ref]
            payload = SlimSerializer.serialize_with_evidence(
                project=project,
                decisions=decisions,
                preferences=preferences,
                guardrails=guardrails,
                edges=edges,
                evidence_hashes=evidence_hashes,
                source_refs=source_refs,
                limit=limit,
            )
        else:
            payload = SlimSerializer.serialize_slim(
                project=project,
                decisions=decisions,
                preferences=preferences,
                guardrails=guardrails,
                limit=limit,
                cursor=args.get("cursor"),
            )

        return {"jsonrpc": "2.0", "id": msg_id, "result": payload}

    def _handle_candidate_create(self, msg_id: Any, args: dict[str, Any]) -> dict[str, Any]:
        required = ["card_type", "project", "title", "summary", "typed_payload", "content_hash"]
        for req in required:
            if req not in args or args[req] is None or (isinstance(args[req], str) and not args[req].strip()):
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Missing required field: {req}"}}

        card_type = args["card_type"]
        if card_type not in ("decision", "preference", "task", "evidence"):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Invalid card_type: {card_type}"}}

        content_hash = args["content_hash"]
        if not content_hash.startswith("sha256:"):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "content_hash must start with sha256:"}}

        self._cand_counter += 1
        memory_id = f"mem_cand_{int(time.time() * 1000)}_{self._cand_counter}"
        card = MemoryCard(
            memory_id=memory_id,
            project=args["project"],
            card_type=card_type,
            title=args["title"],
            summary=args["summary"],
            typed_payload=args["typed_payload"],
            lifecycle_state="candidate",  # Strictly enforced
            authorization_status="disabled",  # Strictly enforced
            content_hash=content_hash,
            source_ref=[args["source_ref"]] if args.get("source_ref") else [],
        )
        self.store.insert_card(card)

        # Enqueue for embedding generation
        self.store.enqueue_outbox(
            target_type="memory_card",
            target_id=memory_id,
            content_hash=content_hash,
            payload_text=f"{card.title}\n{card.summary}",
        )

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "memory_id": memory_id,
                "lifecycle_state": "candidate",
                "authorization_status": "disabled",
                "proposal_write_performed": True,
            },
        }


@pytest.fixture
def pg_store() -> InMemoryPostgresStore:
    return InMemoryPostgresStore()


@pytest.fixture
def qdrant_store() -> InMemoryQdrantStore:
    return InMemoryQdrantStore()


@pytest.fixture
def mcp_server(pg_store: InMemoryPostgresStore) -> MockMCPServer:
    return MockMCPServer(pg_store)

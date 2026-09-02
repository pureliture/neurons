"""One-shot migration & backfill script from Qdrant to PostgreSQL (Milestone 4).

Migrates session_memory_chunks and memory_cards with dry-run support,
JSON checkpointing, payload validation/quarantine, and outbox enqueueing
for unvectorized records.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Callable

from .pgvector_store import (
    PgVectorStore,
    MemoryCard,
    SessionChunk,
)

logger = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    collection_name: str
    total_scanned: int = 0
    total_migrated: int = 0
    total_skipped: int = 0
    total_quarantined: int = 0
    outbox_enqueued: int = 0
    elapsed_seconds: float = 0.0
    dry_run: bool = False
    quarantined_records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullMigrationSummary:
    started_at: str
    completed_at: str
    dry_run: bool
    session_chunks_result: MigrationResult
    memory_cards_result: MigrationResult
    total_migrated: int = 0
    total_quarantined: int = 0
    total_outbox_enqueued: int = 0
    total_elapsed_seconds: float = 0.0
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "dry_run": self.dry_run,
            "total_migrated": self.total_migrated,
            "total_quarantined": self.total_quarantined,
            "total_outbox_enqueued": self.total_outbox_enqueued,
            "total_elapsed_seconds": self.total_elapsed_seconds,
            "success": self.success,
            "session_chunks": self.session_chunks_result.to_dict(),
            "memory_cards": self.memory_cards_result.to_dict(),
        }


class QdrantToPostgresMigrator:
    """Migrates records and vectors from Qdrant source into PostgreSQL PgVectorStore."""

    def __init__(
        self,
        qdrant_client: Any,
        target_store: PgVectorStore,
        batch_size: int = 100,
        dry_run: bool = False,
        checkpoint_file: str | None = None,
    ):
        self.qdrant = qdrant_client
        self.target_store = target_store
        self.batch_size = batch_size
        self.dry_run = dry_run
        self.checkpoint_file = checkpoint_file
        self.checkpoints: dict[str, Any] = self._load_checkpoints()

    def _load_checkpoints(self) -> dict[str, Any]:
        if self.checkpoint_file and os.path.exists(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load checkpoint file {self.checkpoint_file}: {e}")
        return {}

    def _save_checkpoints(self) -> None:
        if self.checkpoint_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.checkpoint_file)), exist_ok=True)
                with open(self.checkpoint_file, "w", encoding="utf-8") as f:
                    json.dump(self.checkpoints, f, indent=2)
            except Exception as e:
                logger.warning(f"Could not save checkpoint file {self.checkpoint_file}: {e}")

    def _fetch_qdrant_points(self, collection_name: str, offset: Any = None) -> tuple[list[Any], Any]:
        """Fetch a page of points from Qdrant client or in-memory dict."""
        # Check collections dict first (collection-scoped in-memory store)
        if hasattr(self.qdrant, "collections") and isinstance(self.qdrant.collections, dict):
            coll = self.qdrant.collections.get(collection_name, {})
            items = list(coll.items())
            start_idx = int(offset) if offset is not None else 0
            end_idx = start_idx + self.batch_size
            page = items[start_idx:end_idx]
            next_offset = end_idx if end_idx < len(items) else None
            return page, next_offset

        # Check if in-memory test store has vectors dict
        if hasattr(self.qdrant, "vectors") and isinstance(self.qdrant.vectors, dict):
            # Dict mapping point_id -> (vector, payload)
            items = list(self.qdrant.vectors.items())
            start_idx = int(offset) if offset is not None else 0
            end_idx = start_idx + self.batch_size
            page = items[start_idx:end_idx]
            next_offset = end_idx if end_idx < len(items) else None
            return page, next_offset

        # Check official QdrantClient (scroll API)
        if hasattr(self.qdrant, "scroll"):
            records, next_offset = self.qdrant.scroll(
                collection_name=collection_name,
                limit=self.batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            return records, next_offset

        return [], None

    def migrate_session_chunks(
        self,
        collection_name: str = "session_chunks",
        project: str | None = None,
    ) -> MigrationResult:
        """Migrate conversation session chunks from Qdrant into PostgreSQL."""
        start_time = time.time()
        result = MigrationResult(collection_name=collection_name, dry_run=self.dry_run)
        current_offset = self.checkpoints.get(f"{collection_name}_offset", None)

        while True:
            points, next_offset = self._fetch_qdrant_points(collection_name, offset=current_offset)
            if not points:
                break

            for item in points:
                result.total_scanned += 1
                try:
                    # Unpack point
                    if isinstance(item, tuple) and len(item) == 2:
                        point_id, point_data = item
                        if isinstance(point_data, tuple):
                            vector, payload = point_data
                        elif isinstance(point_data, dict):
                            vector = point_data.get("vector")
                            payload = point_data.get("payload", point_data)
                        else:
                            vector = None
                            payload = {}
                    elif hasattr(item, "id") and hasattr(item, "payload"):
                        point_id = str(item.id)
                        vector = item.vector
                        payload = item.payload or {}
                    else:
                        point_id = str(item)
                        vector = None
                        payload = {}

                    # Project filter
                    item_project = payload.get("project", "default")
                    if project and item_project != project:
                        result.total_skipped += 1
                        continue

                    # Validate vector dimensions
                    if vector is not None and len(vector) != 1536:
                        result.total_quarantined += 1
                        result.quarantined_records.append({
                            "id": str(point_id),
                            "reason": f"Invalid vector dimension: {len(vector)} != 1536",
                            "payload": payload,
                        })
                        continue

                    chunk = SessionChunk(
                        chunk_id=str(payload.get("chunk_id", point_id)),
                        session_id_hash=str(payload.get("session_id_hash", f"sha256:{point_id}")),
                        project=item_project,
                        provider=str(payload.get("provider", "unspecified")),
                        chunk_index=int(payload.get("chunk_index", 0)),
                        content_markdown=str(payload.get("content_markdown", payload.get("text", ""))),
                        token_count=int(payload.get("token_count", 0)),
                        embedding_model=str(payload.get("embedding_model", "text-embedding-3-small")),
                        embedding=list(vector) if vector else None,
                    )

                    if not self.dry_run:
                        self.target_store.insert_chunk(chunk)
                    result.total_migrated += 1

                except Exception as e:
                    logger.error(f"Error migrating session chunk: {e}", exc_info=True)
                    result.total_quarantined += 1
                    result.quarantined_records.append({
                        "id": str(item),
                        "reason": str(e),
                    })

            current_offset = next_offset
            self.checkpoints[f"{collection_name}_offset"] = current_offset
            self._save_checkpoints()
            if next_offset is None:
                break

        result.elapsed_seconds = round(time.time() - start_time, 4)
        return result

    def migrate_memory_cards(
        self,
        collection_name: str = "memory_cards",
        project: str | None = None,
    ) -> MigrationResult:
        """Migrate authoritative memory cards from Qdrant into PostgreSQL."""
        start_time = time.time()
        result = MigrationResult(collection_name=collection_name, dry_run=self.dry_run)
        current_offset = self.checkpoints.get(f"{collection_name}_offset", None)

        while True:
            points, next_offset = self._fetch_qdrant_points(collection_name, offset=current_offset)
            if not points:
                break

            for item in points:
                result.total_scanned += 1
                try:
                    # Unpack point
                    if isinstance(item, tuple) and len(item) == 2:
                        point_id, point_data = item
                        if isinstance(point_data, tuple):
                            vector, payload = point_data
                        elif isinstance(point_data, dict):
                            vector = point_data.get("vector")
                            payload = point_data.get("payload", point_data)
                        else:
                            vector = None
                            payload = {}
                    elif hasattr(item, "id") and hasattr(item, "payload"):
                        point_id = str(item.id)
                        vector = item.vector
                        payload = item.payload or {}
                    else:
                        point_id = str(item)
                        vector = None
                        payload = {}

                    # Project filter
                    item_project = payload.get("project", "default")
                    if project and item_project != project:
                        result.total_skipped += 1
                        continue

                    # Validate vector dimensions
                    if vector is not None and len(vector) != 1536:
                        result.total_quarantined += 1
                        result.quarantined_records.append({
                            "id": str(point_id),
                            "reason": f"Invalid vector dimension: {len(vector)} != 1536",
                            "payload": payload,
                        })
                        continue

                    memory_id = str(payload.get("memory_id", point_id))
                    content_hash = str(payload.get("content_hash", f"sha256:{point_id}"))
                    if not content_hash.startswith("sha256:"):
                        content_hash = f"sha256:{content_hash}"

                    card = MemoryCard(
                        memory_id=memory_id,
                        project=item_project,
                        card_type=str(payload.get("card_type", "decision")),
                        title=str(payload.get("title", f"Card {memory_id}")),
                        summary=str(payload.get("summary", payload.get("text", ""))),
                        typed_payload=payload.get("typed_payload", {}),
                        lifecycle_state=str(payload.get("lifecycle_state", "candidate")),
                        authorization_status=str(payload.get("authorization_status", "disabled")),
                        currentness=str(payload.get("currentness", "current")),
                        confidence=float(payload.get("confidence", 1.0)),
                        embedding_model=str(payload.get("embedding_model", "text-embedding-3-small")),
                        embedding=list(vector) if vector else None,
                        embedding_state="ready" if vector is not None else "pending",
                        content_hash=content_hash,
                        source_ref=payload.get("source_ref", []),
                    )

                    if not self.dry_run:
                        self.target_store.insert_card(card)
                        # If card lacks vector, enqueue outbox job
                        if card.embedding is None:
                            payload_text = card.summary or card.title
                            self.target_store.enqueue_outbox(
                                target_type="memory_card",
                                target_id=card.memory_id,
                                content_hash=card.content_hash,
                                payload_text=payload_text,
                            )
                            result.outbox_enqueued += 1

                    result.total_migrated += 1

                except Exception as e:
                    logger.error(f"Error migrating memory card: {e}", exc_info=True)
                    result.total_quarantined += 1
                    result.quarantined_records.append({
                        "id": str(item),
                        "reason": str(e),
                    })

            current_offset = next_offset
            self.checkpoints[f"{collection_name}_offset"] = current_offset
            self._save_checkpoints()
            if next_offset is None:
                break

        result.elapsed_seconds = round(time.time() - start_time, 4)
        return result

    def run_full_migration(self, project: str | None = None) -> FullMigrationSummary:
        """Execute complete migration sequence for chunks and cards."""
        started_at = datetime.now(timezone.utc).isoformat()
        start_time = time.time()

        chunks_res = self.migrate_session_chunks(collection_name="session_chunks", project=project)
        cards_res = self.migrate_memory_cards(collection_name="memory_cards", project=project)

        completed_at = datetime.now(timezone.utc).isoformat()
        total_elapsed = round(time.time() - start_time, 4)

        return FullMigrationSummary(
            started_at=started_at,
            completed_at=completed_at,
            dry_run=self.dry_run,
            session_chunks_result=chunks_res,
            memory_cards_result=cards_res,
            total_migrated=chunks_res.total_migrated + cards_res.total_migrated,
            total_quarantined=chunks_res.total_quarantined + cards_res.total_quarantined,
            total_outbox_enqueued=cards_res.outbox_enqueued,
            total_elapsed_seconds=total_elapsed,
            success=(len(chunks_res.errors) == 0 and len(cards_res.errors) == 0),
        )

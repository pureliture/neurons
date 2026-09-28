"""Transactional Outbox Worker for LBrain PostgreSQL Store (Milestone 3).

Polls embedding_outbox using FOR UPDATE SKIP LOCKED leases, generates
vector embeddings, and applies Compare-And-Swap (CAS) write-backs.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from typing import Callable, Any

from ..model_connectors import DEFAULT_EMBEDDING_DIM
from .pgvector_store import PgVectorStore, OutboxJob, make_dummy_vector

logger = logging.getLogger(__name__)


def generate_deterministic_embedding(text: str, dim: int = DEFAULT_EMBEDDING_DIM) -> list[float]:
    """Generate deterministic normalized vector from text content (safe for empty text)."""
    if not text:
        # Default unit vector for empty text
        return make_dummy_vector(0, dim=dim)
    
    # Hash seed from SHA256 of text
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], byteorder="big") % 100000
    return make_dummy_vector(seed, dim=dim)


class OutboxWorker:
    """Outbox Worker with atomic lease claiming, CAS write-backs & backoff retry."""

    def __init__(
        self,
        store: PgVectorStore,
        worker_id: str | None = None,
        batch_size: int = 10,
        lease_seconds: int = 30,
        poll_interval_seconds: float = 0.5,
        embedding_fn: Callable[[str], list[float]] | None = None,
        max_retries: int = 5,
    ):
        self.store = store
        self.worker_id = worker_id or f"worker_{int(time.time() * 1000)}"
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval_seconds
        if embedding_fn is None:
            raise ValueError(
                "embedding_fn is required; inject the configured Embedding Provider "
                "(generate_deterministic_embedding is test-only)"
            )
        self.embedding_fn = embedding_fn
        self.max_retries = max_retries
        self._stop_event = threading.Event()

    def run_once(self) -> int:
        """Claim and process a single batch of outbox jobs."""
        jobs = self.store.claim_outbox_leases(
            worker_id=self.worker_id,
            batch_size=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        if not jobs:
            return 0

        processed_count = 0
        for job in jobs:
            try:
                self.process_job(job)
                processed_count += 1
            except Exception as e:
                logger.error(
                    f"[{self.worker_id}] Error processing outbox job {job.outbox_id}: {e}",
                    exc_info=True,
                )
                try:
                    self.store.mark_outbox_failed(
                        job.outbox_id,
                        error_message=str(e),
                        max_retries=self.max_retries,
                        worker_id=self.worker_id,
                    )
                except ValueError as lease_error:
                    # Another worker may have reclaimed an expired lease. Do
                    # not overwrite its retry/terminal state.
                    logger.warning(
                        "[%s] outbox job %s was not owned while recording failure: %s",
                        self.worker_id,
                        job.outbox_id,
                        lease_error,
                    )
                processed_count += 1

        return processed_count

    def process_job(self, job: OutboxJob) -> bool:
        """
        Process an outbox job:
        1. Compute vector embedding outside DB transaction.
        2. Apply CAS write-back with enqueued_content_hash guard.
        3. Gracefully retire outbox job on CAS mismatch (stale write).
        """
        # 1. Compute embedding vector
        vector = self.embedding_fn(job.payload_text)
        if not vector or len(vector) != DEFAULT_EMBEDDING_DIM:
            raise ValueError(f"Embedding function returned invalid vector length {len(vector) if vector else 0}")

        # 2. CAS write-back
        success = self.store.cas_update_embedding(
            outbox_id_or_target_type=job.target_type,
            target_id=job.target_id,
            enqueued_content_hash=job.content_hash,
            vector=vector,
            outbox_id=job.outbox_id,
            worker_id=self.worker_id,
        )
        return success

    def renew_lease(self, outbox_id: int, lease_seconds: int = 30) -> bool:
        """Renew active lease for a long-running job."""
        return self.store.renew_outbox_lease(
            outbox_id=outbox_id,
            worker_id=self.worker_id,
            lease_seconds=lease_seconds,
        )

    def run_loop(
        self,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Run continuous polling loop until stopped or max_iterations reached."""
        stop = stop_event or self._stop_event
        iterations = 0

        while not stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break

            processed = self.run_once()
            iterations += 1

            if processed == 0:
                stop.wait(self.poll_interval)

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._stop_event.set()


def _card_mapping_from_outbox_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bridge the outbox payload's field names to the card mapper's contract.

    `PgVectorStore.upsert_card` enqueues the card's identity under
    ``authority_memory_id`` (plus ``source_id``), while
    `episode_from_memory_card` reads ``memory_id`` and fails closed without it.
    The two conventions exist in different layers, so the rename belongs here at
    the seam rather than in either layer. The already-correct canonical key
    ``content_hash`` is passed through untouched -- the PG authority join
    compares it for equality against `memory_cards.content_hash`.
    """

    card = dict(payload)
    memory_id = str(
        card.get("memory_id")
        or card.get("authority_memory_id")
        or card.get("source_id")
        or ""
    )
    if memory_id:
        card["memory_id"] = memory_id
    return card


class GraphProjectionWorker:
    """Leased worker for graph_projection_outbox projecting episodes to Graphiti / Neo4j."""

    def __init__(
        self,
        store: PgVectorStore,
        graph_adapter: Any,
        worker_id: str | None = None,
        batch_size: int = 10,
        lease_seconds: int = 60,
        poll_interval_seconds: float = 0.5,
        max_retries: int = 5,
    ):
        self.store = store
        if graph_adapter is None:
            raise ValueError("graph_adapter is required")
        self.graph_adapter = graph_adapter
        self.worker_id = worker_id or f"graph_worker_{int(time.time() * 1000)}"
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval_seconds
        self.max_retries = max_retries
        self._stop_event = threading.Event()
        self.last_batch_failed = 0

    def process_job(self, job: Any) -> None:
        """Project one episode payload through the Graphiti adapter seam."""
        from ..llm_brain_core.ontology import episode_from_memory_card
        from .graph_replay import call_adapter_seam

        payload = job.episode_payload
        if not isinstance(payload, dict):
            raise TypeError("graph_projection_payload_must_be_mapping")
        # The leased row and the projected payload must refer to the same
        # authority revision. Do not mark a mismatched job completed.
        if any(
            str(payload.get(key) or "") != str(getattr(job, key, ""))
            for key in ("source_type", "source_id", "source_revision", "content_hash")
        ) or str(payload.get("authority_memory_id") or "") != str(job.source_id):
            raise ValueError("graph_projection_authority_key_mismatch")
        # The adapter contract is `upsert_episode(OntologyEpisode)`, NOT a raw
        # mapping. Passing the dict straight through raised AttributeError on
        # the first attribute access inside the real adapter, so every claimed
        # job failed, retried OUTBOX_RETRY_LIMIT times, and landed in
        # dead_letter. Map the stored payload into the episode the adapter
        # actually expects.
        episode = episode_from_memory_card(
            _card_mapping_from_outbox_payload(payload),
            project=str(payload.get("project") or ""),
        )
        outcome = call_adapter_seam(self.graph_adapter, episode, require_explicit_result=True)
        # A disabled/unavailable graph did not persist an episode. Do not retire
        # the leased job as completed: it must remain retryable or dead-letter.
        if outcome not in {"inserted", "duplicate"}:
            raise RuntimeError(f"graph_projection_not_persisted:{outcome}")

    def run_once(self) -> int:
        """Claim and process a single batch of graph projection outbox jobs."""
        self.last_batch_failed = 0
        jobs = self.store.claim_graph_projection_leases(
            worker_id=self.worker_id,
            batch_size=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        if not jobs:
            return 0

        processed_count = 0
        for job in jobs:
            try:
                self.process_job(job)
                self.store.mark_graph_projection_completed(
                    projection_id=job.projection_id,
                    worker_id=self.worker_id,
                )
            except Exception as e:
                self.last_batch_failed += 1
                logger.error(
                    "[%s] graph projection job %s failed (%s)",
                    self.worker_id,
                    job.projection_id,
                    type(e).__name__,
                )
                try:
                    self.store.mark_graph_projection_failed(
                        projection_id=job.projection_id,
                        error_message=type(e).__name__,
                        max_retries=self.max_retries,
                        worker_id=self.worker_id,
                    )
                except ValueError as lease_error:
                    logger.warning(
                        "[%s] graph projection job %s was not owned while recording failure: %s",
                        self.worker_id,
                        job.projection_id,
                        lease_error,
                    )
            processed_count += 1

        return processed_count

    def run_loop(
        self,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Run continuous polling loop until stopped or max_iterations reached."""
        stop = stop_event or self._stop_event
        iterations = 0

        while not stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break

            processed = self.run_once()
            iterations += 1

            if processed == 0:
                stop.wait(self.poll_interval)

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._stop_event.set()

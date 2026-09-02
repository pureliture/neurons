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

from .pgvector_store import PgVectorStore, OutboxJob, make_dummy_vector

logger = logging.getLogger(__name__)


def generate_deterministic_embedding(text: str, dim: int = 1536) -> list[float]:
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
        self.embedding_fn = embedding_fn or generate_deterministic_embedding
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
                self.store.mark_outbox_failed(
                    job.outbox_id,
                    error_message=str(e),
                    max_retries=self.max_retries,
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
        if not vector or len(vector) != 1536:
            raise ValueError(f"Embedding function returned invalid vector length {len(vector) if vector else 0}")

        # 2. CAS write-back
        success = self.store.cas_update_embedding(
            outbox_id_or_target_type=job.target_type,
            target_id=job.target_id,
            enqueued_content_hash=job.content_hash,
            vector=vector,
            outbox_id=job.outbox_id,
        )
        return success

    def renew_lease(self, outbox_id: int, lease_seconds: int = 30) -> bool:
        """Renew active lease for a long-running job."""
        if outbox_id in self.store.outbox:
            job = self.store.outbox[outbox_id]
            if job.worker_id == self.worker_id and job.status == "processing":
                from datetime import datetime, timezone, timedelta
                now = datetime.now(timezone.utc)
                job.lease_until = now + timedelta(seconds=lease_seconds)
                job.updated_at = now
                return True
        return False

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

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
from typing import Callable, Any, Sequence

from ..model_connectors import (
    DEFAULT_EMBEDDING_DIM,
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_PROFILE_ID,
)
from .pgvector_store import PgVectorStore, OutboxJob, LocalOutboxJob, make_dummy_vector

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
        self.last_batch_failed = 0
        self.last_batch_completed = 0
        self.last_batch_stale = 0
        self.last_batch_attempted = 0

    def run_once(self) -> int:
        """Claim a batch; returned count is attempts, not successful embeddings."""
        self.last_batch_failed = 0
        self.last_batch_completed = 0
        self.last_batch_stale = 0
        self.last_batch_attempted = 0
        jobs = self.store.claim_outbox_leases(
            worker_id=self.worker_id,
            batch_size=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        if not jobs:
            return 0

        processed_count = 0
        for job in jobs:
            self.last_batch_attempted += 1
            try:
                completed = self.process_job(job)
                if completed:
                    self.last_batch_completed += 1
                else:
                    self.last_batch_stale += 1
                processed_count += 1
            except Exception as e:
                self.last_batch_failed += 1
                # Provider/DB exceptions can echo input or connection credentials.
                # Persist a source-free class code, never their message/traceback.
                error_code = "embedding_failed:" + type(e).__name__
                logger.error("Embedding outbox processing failed (%s)", error_code)
                try:
                    self.store.mark_outbox_failed(
                        job.outbox_id,
                        error_message=error_code,
                        max_retries=self.max_retries,
                        worker_id=self.worker_id,
                    )
                except ValueError:
                    # Another worker may have reclaimed an expired lease. Do
                    # not overwrite its retry/terminal state or echo DB errors.
                    logger.warning("Embedding failure not recorded: lease_not_owned")
                processed_count += 1

        return processed_count

    def process_job(self, job: OutboxJob) -> bool:
        """
        Process an outbox job:
        1. Retire stale work before spending on an embedding.
        2. Compute vector embedding outside DB transaction.
        3. Apply CAS write-back with enqueued_content_hash guard.
        """
        stale_check = getattr(self.store, "skip_stale_outbox_embedding", None)
        if stale_check is not None and stale_check(
            outbox_id=job.outbox_id,
            target_type=job.target_type,
            target_id=job.target_id,
            content_hash=job.content_hash,
            worker_id=self.worker_id,
        ):
            return False

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


class LocalEmbeddingOutboxWorker:
    """Leased worker for local_embedding_outbox (D2 lock-order, fence epoch & incarnation CAS)."""

    def __init__(
        self,
        store: PgVectorStore,
        worker_id: str | None = None,
        batch_size: int = 10,
        lease_seconds: int = 30,
        poll_interval_seconds: float = 0.5,
        embedding_fn: Callable[[str], list[float]] | None = None,
        max_retries: int = 5,
        embedding_profile: str = LOCAL_EMBEDDING_PROFILE_ID,
        target_types: Sequence[str] | None = None,
    ):
        self.store = store
        self.worker_id = worker_id or f"local_worker_{int(time.time() * 1000)}"
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval_seconds
        if embedding_fn is None:
            raise ValueError(
                "embedding_fn is required; inject CanonicalLocalEmbeddingClient.embed "
                "(generate_deterministic_embedding is test-only)"
            )
        self.embedding_fn = embedding_fn
        self.max_retries = max_retries
        self.embedding_profile = embedding_profile
        if target_types is None:
            self.target_types = None
        else:
            normalized_types = tuple(sorted({str(value).strip() for value in target_types}))
            if not normalized_types or not set(normalized_types).issubset(
                {"memory_card", "session_chunk"}
            ):
                raise ValueError("invalid local embedding target types")
            self.target_types = normalized_types
        self._stop_event = threading.Event()
        self.last_batch_failed = 0
        self.last_batch_completed = 0
        self.last_batch_stale = 0
        self.last_batch_attempted = 0

    def run_once(self, *, batch_size: int | None = None) -> int:
        """Claim a batch of local_embedding_outbox jobs and process each outside DB transactions."""
        self.last_batch_failed = 0
        self.last_batch_completed = 0
        self.last_batch_stale = 0
        self.last_batch_attempted = 0
        jobs = self.store.claim_local_outbox_leases(
            worker_id=self.worker_id,
            batch_size=int(batch_size) if batch_size is not None else self.batch_size,
            lease_seconds=self.lease_seconds,
            profile_id=self.embedding_profile,
            max_retries=self.max_retries,
            target_types=self.target_types,
        )
        if not jobs:
            return 0

        processed_count = 0
        for job in jobs:
            self.last_batch_attempted += 1
            try:
                completed = self.process_job(job)
                if completed:
                    self.last_batch_completed += 1
                else:
                    self.last_batch_stale += 1
                processed_count += 1
            except Exception as exc:
                self.last_batch_failed += 1
                retryable = getattr(exc, "retryable", None)
                if retryable is None:
                    retryable = not isinstance(exc, (ValueError, TypeError))
                reason_code = getattr(exc, "code", None) or type(exc).__name__
                error_code = ("retryable_error:" if retryable else "terminal_error:") + str(reason_code)
                logger.error("Local embedding outbox processing failed (%s)", error_code)
                try:
                    self.store.mark_local_outbox_failed(
                        outbox_id=job.outbox_id,
                        worker_id=self.worker_id,
                        fencing_token=job.fencing_token,
                        error_code=error_code,
                        retryable=bool(retryable),
                        max_retries=self.max_retries,
                        profile_id=self.embedding_profile,
                    )
                except ValueError:
                    logger.warning("Local embedding failure not recorded: lease_not_owned")
                processed_count += 1

        return processed_count

    def process_job(self, job: LocalOutboxJob) -> bool:
        """Compute 256-dim vector outside DB transaction, then complete via lock-ordered CAS."""
        vector = self.embedding_fn(job.payload_text)
        if not vector or len(vector) != LOCAL_EMBEDDING_DIM:
            raise ValueError(
                f"Local embedding function returned invalid vector length {len(vector) if vector else 0}"
            )
        return self.store.complete_local_outbox_cas(
            outbox_id=job.outbox_id,
            worker_id=self.worker_id,
            fencing_token=job.fencing_token,
            vector=vector,
            profile_id=self.embedding_profile,
        )

    def renew_lease(
        self,
        outbox_id: int,
        lease_seconds: int = 30,
        fencing_token: int = 1,
    ) -> bool:
        """Renew active lease for a long-running local embedding job."""
        return self.store.renew_local_outbox_lease(
            outbox_id=outbox_id,
            worker_id=self.worker_id,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        )

    def run_loop(
        self,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
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
        self._stop_event.set()

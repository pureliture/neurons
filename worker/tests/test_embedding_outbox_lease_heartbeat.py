"""Lease heartbeat and stale-fence behavior for the local outbox worker.

Synthetic jobs/fakes only. No DB, network, or production data.
"""

from __future__ import annotations

import threading
import time

from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker
from agent_knowledge.postgres_store.pgvector_store import LocalOutboxJob


def _job(outbox_id: int, fencing_token: int) -> LocalOutboxJob:
    return LocalOutboxJob(
        outbox_id=outbox_id,
        target_type="session_chunk",
        target_id="seg_" + str(outbox_id) * 60,
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        source_content_hash="sha256:" + "a" * 64,
        input_hash="sha256:" + "b" * 64,
        payload_text="payload-" + str(outbox_id),
        fencing_token=fencing_token,
    )


class _LeaseStore:
    def __init__(self, jobs, *, renew=True, renew_raises=False):
        self._jobs = list(jobs)
        self.renew = renew
        self.renew_raises = renew_raises
        self.renewals: list[dict] = []
        self.completions: list[dict] = []
        self.failures: list[dict] = []
        self._lock = threading.Lock()

    def claim_local_outbox_leases(self, **_kwargs):
        jobs, self._jobs = self._jobs, []
        return jobs

    def renew_local_outbox_lease(self, **kwargs):
        with self._lock:
            self.renewals.append(dict(kwargs))
        if self.renew_raises:
            raise RuntimeError("synthetic db down")
        return self.renew

    def complete_local_outbox_cas(self, **kwargs):
        with self._lock:
            self.completions.append(dict(kwargs))
        return True

    def mark_local_outbox_failed(self, **kwargs):
        with self._lock:
            self.failures.append(dict(kwargs))


def _worker(store, embed_fn, lease_seconds=1):
    return LocalEmbeddingOutboxWorker(
        store=store,
        embedding_fn=embed_fn,
        batch_size=1,
        lease_seconds=lease_seconds,
        poll_interval_seconds=0.01,
    )


def _slow_embed(seconds: float):
    def embed(_text):
        time.sleep(seconds)
        return [0.1] * 256

    return embed


def test_heartbeat_renews_lease_during_long_embedding():
    job = _job(1, 7)
    store = _LeaseStore([job])
    worker = _worker(store, _slow_embed(0.8), lease_seconds=1)

    worker.run_once()

    assert worker.last_batch_completed == 1
    assert store.completions and store.completions[0]["fencing_token"] == 7
    assert store.renewals, "long embedding must renew its lease"
    assert all(r["outbox_id"] == 1 and r["fencing_token"] == 7 for r in store.renewals)
    assert all(r["worker_id"] == worker.worker_id for r in store.renewals)


def test_heartbeat_renews_waiting_jobs_in_the_same_claimed_batch():
    jobs = [_job(1, 7), _job(2, 8)]
    store = _LeaseStore(jobs)
    worker = LocalEmbeddingOutboxWorker(
        store=store,
        embedding_fn=_slow_embed(0.7),
        batch_size=2,
        lease_seconds=0.6,
        poll_interval_seconds=0.01,
    )

    worker.run_once()

    renewed_ids = {r["outbox_id"] for r in store.renewals}
    assert renewed_ids == {1, 2}
    assert worker.last_batch_completed == 2


def test_lost_lease_skips_completion_cas():
    job = _job(1, 7)
    store = _LeaseStore([job], renew=False)
    worker = _worker(store, _slow_embed(0.6), lease_seconds=0.6)

    worker.run_once()

    assert store.renewals
    assert store.completions == []
    assert store.failures == []
    assert worker.last_batch_stale == 1
    assert worker.last_batch_failed == 0


def test_transient_renew_error_keeps_processing():
    job = _job(1, 7)
    store = _LeaseStore([job], renew_raises=True)
    worker = _worker(store, _slow_embed(0.4), lease_seconds=0.6)

    worker.run_once()

    assert store.completions
    assert worker.last_batch_completed == 1
    assert worker.last_batch_failed == 0


def test_heartbeat_stops_after_run_once():
    job = _job(1, 7)
    store = _LeaseStore([job])
    worker = _worker(store, _slow_embed(0.25), lease_seconds=0.6)

    worker.run_once()
    count = len(store.renewals)
    time.sleep(0.9)

    assert len(store.renewals) == count


class _RaceStore:
    """Stateful lease/fencing model used to exercise two-worker races."""

    def __init__(self, jobs, *, deny_renew_for: str | None = None):
        self._lock = threading.Lock()
        self._rows = {
            job.outbox_id: {
                "job": job,
                "status": "queued",
                "token": 0,
                "worker": None,
                "lease_until": 0.0,
            }
            for job in jobs
        }
        self._deny_renew_for = deny_renew_for
        self.completions: list[dict] = []

    def claim_local_outbox_leases(self, *, worker_id, batch_size, lease_seconds, **_kwargs):
        now = time.monotonic()
        claimed = []
        with self._lock:
            for row in self._rows.values():
                if len(claimed) >= batch_size:
                    break
                claimable = row["status"] == "queued" or (
                    row["status"] == "processing" and row["lease_until"] < now
                )
                if not claimable:
                    continue
                row["status"] = "processing"
                row["token"] += 1
                row["worker"] = worker_id
                row["lease_until"] = now + lease_seconds
                job = row["job"]
                claimed.append(
                    LocalOutboxJob(
                        **{
                            **job.__dict__,
                            "fencing_token": row["token"],
                            "status": "processing",
                        }
                    )
                )
        return claimed

    def renew_local_outbox_lease(self, *, outbox_id, worker_id, fencing_token, lease_seconds):
        if worker_id == self._deny_renew_for:
            return False
        with self._lock:
            row = self._rows[outbox_id]
            if (
                row["status"] == "processing"
                and row["worker"] == worker_id
                and row["token"] == fencing_token
                and row["lease_until"] > time.monotonic()
            ):
                row["lease_until"] = time.monotonic() + lease_seconds
                return True
            return False

    def complete_local_outbox_cas(self, *, outbox_id, worker_id, fencing_token, **_kwargs):
        with self._lock:
            row = self._rows[outbox_id]
            if (
                row["status"] == "processing"
                and row["worker"] == worker_id
                and row["token"] == fencing_token
                and row["lease_until"] > time.monotonic()
            ):
                row["status"] = "completed"
                self.completions.append({"outbox_id": outbox_id, "worker": worker_id})
                return True
            return False

    def mark_local_outbox_failed(self, **_kwargs):
        return None

    def status(self, outbox_id):
        with self._lock:
            return self._rows[outbox_id]["status"]


def test_active_lease_blocks_second_worker_claim():
    job = _job(1, 1)
    store = _RaceStore([job])
    released = threading.Event()

    def slow_embed(_text):
        released.set()
        time.sleep(1.2)
        return [0.1] * 256

    worker_a = _worker(store, slow_embed, lease_seconds=1)
    worker_b = _worker(store, lambda _text: [0.2] * 256, lease_seconds=1)
    thread = threading.Thread(target=worker_a.run_once)
    thread.start()
    assert released.wait(1.0)
    time.sleep(0.5)  # heartbeat has renewed A's lease at ~0.33s

    attempts_b = worker_b.run_once()

    thread.join(timeout=3)
    assert attempts_b == 0
    assert worker_a.last_batch_completed == 1
    assert store.status(1) == "completed"
    assert store.completions == [{"outbox_id": 1, "worker": worker_a.worker_id}]


def test_expired_lease_is_reclaimed_and_stale_worker_skips_cas():
    job = _job(1, 1)
    store = _RaceStore([job])
    worker_a = _worker(store, _slow_embed(1.6), lease_seconds=1)
    store._deny_renew_for = worker_a.worker_id
    worker_b = _worker(store, lambda _text: [0.2] * 256, lease_seconds=1)
    thread = threading.Thread(target=worker_a.run_once)
    thread.start()
    time.sleep(1.15)  # A's lease (1s) expired, no renewal

    worker_b.run_once()
    thread.join(timeout=3)

    assert store.status(1) == "completed"
    assert store.completions == [{"outbox_id": 1, "worker": worker_b.worker_id}]
    assert worker_a.last_batch_stale == 1
    assert worker_a.last_batch_completed == 0

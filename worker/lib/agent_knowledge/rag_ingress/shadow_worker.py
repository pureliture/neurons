"""G1 shadow worker — server-side consume→deliver→state, reusing agent-knowledge.

This is the Phase G1 (R2-full + C) artifact: a Python worker that reuses the
existing agent-knowledge ingest pipeline (``document_from_ingress_payload`` +
``RAGFlowIndexBackendAdapter``) and owns a server-volume IngestStateStore SQLite,
driven by a NATS JetStream consumer.

SAFETY (critical): the live stream ``RAG_INGRESS_QUEUE`` uses ``WorkQueue``
retention, which forbids a second overlapping consumer (a parallel consumer would
steal/delete the live Java worker's messages). Therefore the shadow worker NEVER
attaches to ``RAG_INGRESS_QUEUE``; it consumes an isolated shadow stream
(``RAG_INGRESS_SHADOW`` / subject ``rag.shadow.>``) fed by synthetic events. The
live ingress-api, ``rag_target_delivery_worker``, stream, and RAGFlow production
delivery are untouched.

Modes:
  - ``process`` : process one in-memory payload (used by the local dry-run / smoke).
  - ``consume`` : async NATS JetStream loop over the shadow stream (Ubuntu shadow).

Delivery is gated by ``deliver``: in shadow/parallel observation it is False
(record state only); the isolated smoke sets it True and cleans up the doc.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .index_backend import RAGFlowIndexBackendAdapter
from .server_runtime import (
    apply_server_redaction,
    document_from_ingress_payload,
    normalize_ingest_job_payload,
    public_ingress_leak_violations,
)
from ..ragflow_client import RagflowHttpClient

SHADOW_LOG_DDL = """
CREATE TABLE IF NOT EXISTS shadow_ingest_log (
    idempotency_key TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    document_kind   TEXT NOT NULL,
    target_profile  TEXT NOT NULL,
    status          TEXT NOT NULL,
    dataset_ref     TEXT DEFAULT '',
    document_ref    TEXT DEFAULT '',
    delivered       INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


@dataclass
class ShadowResult:
    status: str
    idempotency_key: str
    content_hash_present: bool
    delivered: bool
    dataset_ref: str = ""
    document_ref: str = ""


class IngestStateStore:
    """Server-volume SQLite owned by the worker (G1 = minimal lifecycle log).

    Full ``delivery_jobs`` contract wiring (commands/domain_records) is a G1/G2
    follow-on; G1 proves the worker owns + writes a server-volume store.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute(SHADOW_LOG_DDL)
        return conn

    def record(self, *, idempotency_key: str, content_hash: str, document_kind: str,
               target_profile: str, status: str, dataset_ref: str = "",
               document_ref: str = "", delivered: bool = False, now_iso: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO shadow_ingest_log
                  (idempotency_key, content_hash, document_kind, target_profile,
                   status, dataset_ref, document_ref, delivered, recorded_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                  status=excluded.status, dataset_ref=excluded.dataset_ref,
                  document_ref=excluded.document_ref, delivered=excluded.delivered,
                  updated_at=excluded.updated_at
                """,
                (idempotency_key, content_hash, document_kind, target_profile, status,
                 dataset_ref, document_ref, 1 if delivered else 0, now_iso, now_iso),
            )
            conn.commit()
        finally:
            conn.close()

    def counts(self) -> dict:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM shadow_ingest_log GROUP BY status"
            ).fetchall()
            return {str(s): int(n) for s, n in rows}
        finally:
            conn.close()

    def get_delivered(self, idempotency_key: str) -> tuple[str, str] | None:
        """Return ``(dataset_ref, document_ref)`` for an already-delivered row,
        or ``None``. Lets the worker dedup a NATS at-least-once redelivery
        against its own durable log (restart-safe, no backend round-trip)
        before re-uploading."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT dataset_ref, document_ref FROM shadow_ingest_log "
                "WHERE idempotency_key = ? AND delivered = 1 AND document_ref != ''",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            return str(row[0]), str(row[1])
        finally:
            conn.close()


def build_backend(*, ragflow_base_url: str, ragflow_api_key: str,
                  dataset_id: str | None = None,
                  resolve_dataset_id: Callable[[str], str] | None = None,
                  broad_scan_pages: int = 0) -> RAGFlowIndexBackendAdapter:
    client = RagflowHttpClient(base_url=ragflow_base_url, bearer_token=ragflow_api_key)
    if resolve_dataset_id is None:
        # smoke/dry-run: resolve every profile to one configured dataset.
        resolve_dataset_id = lambda _profile: dataset_id
    return RAGFlowIndexBackendAdapter(
        client=client,
        resolve_dataset_id=resolve_dataset_id,
        broad_scan_pages=broad_scan_pages,
    )


def env_profile_dataset_resolver(getenv: Callable[[str], str | None]) -> Callable[[str], str]:
    """Live routing: map a target profile to its dataset via env, mirroring the
    Java worker's per-profile datasets. ``ragflow-transcript-memory`` ->
    ``RAGFLOW_TRANSCRIPT_MEMORY_DATASET_ID`` etc. Raises for an unconfigured
    profile so a mis-routed delivery fails (and quarantines) rather than landing
    in the wrong dataset."""
    def resolve(profile: str) -> str:
        key = str(profile).upper().replace("-", "_")
        if not key.endswith("_DATASET_ID"):
            key = key + "_DATASET_ID"
        dataset = getenv(key)
        if not dataset:
            raise ValueError(f"no dataset configured for target profile {profile!r} (env {key})")
        return dataset
    return resolve


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def process_payload(payload: dict, *, store: IngestStateStore,
                    backend: RAGFlowIndexBackendAdapter | None, deliver: bool) -> ShadowResult:
    """Build the RagReadyDocument from a rag_ingress_enqueue.v1 payload, record
    state, and (if deliver) submit to RAGFlow. No live stream is touched.

    G2 scoped: applies the server-side full public redaction to conservatively-
    redacted payloads (no-op for already-full ones), then fail-closes — if any
    real leak survives redaction the document is quarantined, never delivered."""
    payload = normalize_ingest_job_payload(payload)
    payload = apply_server_redaction(payload)
    document = document_from_ingress_payload(payload)
    ik = document.idempotency_key
    # Dedup a NATS at-least-once redelivery BEFORE recording "received" (a plain
    # "received" upsert would overwrite a prior delivered row's refs/flag). The
    # worker holds no lease, so JetStream may redeliver a message whose previous
    # attempt already delivered the document (delivered but ack lost, or a
    # transient failure after upload). Two layers mirror the retired Java worker
    # (RecentDeliveryCache + findByContentHash):
    #   1) the worker's own durable log — restart-safe, no backend round-trip;
    #   2) a backend natural-key probe — covers a first attempt that uploaded but
    #      crashed before recording, and a lost/fresh local volume.
    if deliver and backend is not None:
        existing = store.get_delivered(ik)
        if existing is None:
            handle = backend.find_by_natural_key(
                target_profile=document.target_profile,
                idempotency_key=ik,
                payload_hash=document.content_hash,
            )
            if handle is not None:
                existing = (handle.dataset_ref, handle.document_ref)
        if existing is not None:
            dataset_ref, document_ref = existing
            # Persist as "delivered" (it IS delivered — the document exists in
            # RAGFlow) so reconcile/counts treat a deduped redelivery exactly like
            # an original delivery. The in-memory ShadowResult.status is
            # "deduplicated" only so run_consume can log/observe that this pass
            # skipped a re-upload; the durable vocab stays delivered on purpose.
            store.record(
                idempotency_key=ik, content_hash=document.content_hash,
                document_kind=document.document_kind, target_profile=document.target_profile,
                status="delivered", dataset_ref=dataset_ref,
                document_ref=document_ref, delivered=True, now_iso=_now_iso(),
            )
            return ShadowResult(status="deduplicated", idempotency_key=ik,
                                content_hash_present=bool(document.content_hash), delivered=True,
                                dataset_ref=dataset_ref, document_ref=document_ref)
    store.record(
        idempotency_key=ik, content_hash=document.content_hash,
        document_kind=document.document_kind, target_profile=document.target_profile,
        status="received", now_iso=_now_iso(),
    )
    leaks = public_ingress_leak_violations(document.body)
    if leaks:
        store.record(
            idempotency_key=ik, content_hash=document.content_hash,
            document_kind=document.document_kind, target_profile=document.target_profile,
            status="quarantined_leak", delivered=False, now_iso=_now_iso(),
        )
        return ShadowResult(status="quarantined_leak", idempotency_key=ik,
                            content_hash_present=bool(document.content_hash), delivered=False)
    if not deliver or backend is None:
        store.record(
            idempotency_key=ik, content_hash=document.content_hash,
            document_kind=document.document_kind, target_profile=document.target_profile,
            status="observed_no_deliver", delivered=False, now_iso=_now_iso(),
        )
        return ShadowResult(status="observed_no_deliver", idempotency_key=ik,
                            content_hash_present=bool(document.content_hash), delivered=False)
    submit = backend.submit_document(document)
    store.record(
        idempotency_key=ik, content_hash=document.content_hash,
        document_kind=document.document_kind, target_profile=document.target_profile,
        status="delivered", dataset_ref=submit.dataset_ref,
        document_ref=submit.document_ref, delivered=True, now_iso=_now_iso(),
    )
    return ShadowResult(status="delivered", idempotency_key=ik,
                        content_hash_present=bool(document.content_hash), delivered=True,
                        dataset_ref=submit.dataset_ref, document_ref=submit.document_ref)


async def run_consume(*, nats_url: str, stream: str, subject: str, durable: str,
                      store: IngestStateStore, backend: RAGFlowIndexBackendAdapter | None,
                      deliver: bool, max_messages: int | None, idle_timeout: float = 5.0,
                      allow_live: bool = False, max_deliver: int = 5,
                      fetch_batch: int = 1, concurrency: int = 1,
                      pressure_open: Callable[[], bool] | None = None,
                      log: Callable[[str], None] = print) -> dict:
    """Async JetStream pull-consume loop.

    Defaults to the isolated shadow stream; consuming the live RAG_INGRESS_QUEUE
    requires the explicit ``allow_live`` opt-in (G2 cutover). Faithful to the Java
    ``IngestWorker`` contract: success->ack, transient failure->nak, but once a
    message has been delivered ``max_deliver`` times it is ack-dropped (quarantine)
    instead of nak'd so a poison message cannot block the WorkQueue head. When
    ``pressure_open`` is given, fetching pauses while the target is not OPEN."""
    if stream == "RAG_INGRESS_QUEUE" and not allow_live:
        raise ValueError("shadow worker must not consume the live RAG_INGRESS_QUEUE stream")
    import asyncio
    import nats  # nats-py

    nc = await nats.connect(nats_url)
    js = nc.jetstream()
    # ensure the (isolated) stream exists; NEVER create/modify the live stream.
    if stream != "RAG_INGRESS_QUEUE":
        try:
            await js.stream_info(stream)
        except Exception:
            await js.add_stream(name=stream, subjects=[subject])
    if allow_live:
        # Live takeover from the retired Java worker: delete its durable so we
        # create a fresh pull consumer with our own config (avoids a bind config
        # mismatch). WorkQueue retains all un-acked messages across the delete,
        # so no queued work is lost; rollback re-provisions the durable when the
        # Java worker restarts.
        try:
            await js.delete_consumer(stream, durable)
            log(f"deleted existing durable {durable} for fresh live takeover")
        except Exception:
            pass
    sub = await js.pull_subscribe(subject, durable=durable, stream=stream)
    processed = 0
    results: list[str] = []
    fetch_batch = max(int(fetch_batch), 1)
    concurrency = max(int(concurrency), 1)
    semaphore = asyncio.Semaphore(concurrency)

    async def handle_msg(msg) -> str:
        async with semaphore:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
                res = await asyncio.to_thread(
                    process_payload, payload, store=store, backend=backend, deliver=deliver
                )
                await msg.ack()
                log(f"worker processed status={res.status} delivered={res.delivered}")
                return res.status
            except Exception as exc:  # noqa: BLE001
                attempts = _num_delivered(msg)
                if attempts >= max_deliver:
                    await msg.ack()  # drop: matches Java quarantineCandidate("max deliver exceeded")
                    _record_poison(store, msg, attempts)
                    log(f"worker quarantine(max_deliver={attempts}) error={type(exc).__name__}")
                    return "quarantined_max_deliver"
                await msg.nak()
                log(f"worker nak(attempt={attempts}) error={type(exc).__name__}")
                return "nak"

    while max_messages is None or processed < max_messages:
        if pressure_open is not None and not pressure_open():
            log("worker paused: target pressure not OPEN")
            await asyncio.sleep(idle_timeout)
            continue
        try:
            remaining = fetch_batch if max_messages is None else min(fetch_batch, max_messages - processed)
            msgs = await sub.fetch(max(remaining, 1), timeout=idle_timeout)
        except Exception:
            if max_messages is not None:
                break
            continue  # idle fetch timeout on a long-running live loop: keep polling
        statuses = await asyncio.gather(*(handle_msg(msg) for msg in msgs))
        results.extend(statuses)
        processed += sum(1 for status in statuses if status != "nak")
    await nc.drain()
    return {"processed": processed, "statuses": results, "store_counts": store.counts()}


def _num_delivered(msg) -> int:
    try:
        return int(msg.metadata.num_delivered)
    except Exception:
        return 1


def _record_poison(store: IngestStateStore, msg, attempts: int) -> None:
    try:
        seq = int(msg.metadata.sequence.stream)
    except Exception:
        seq = 0
    try:
        store.record(
            idempotency_key=f"poison:{seq}", content_hash="", document_kind="unknown",
            target_profile="unknown", status="quarantined_max_deliver", delivered=False,
            now_iso=_now_iso(),
        )
    except Exception:
        pass


def build_synthetic_event(*, tag: str) -> dict:
    """A synthetic, benign rag_ingress_enqueue.v1 event for the isolated smoke."""
    import hashlib
    body = (
        "---\n"
        "schema_version: agent_knowledge_document.v2\n"
        "result_type: conversation_chunk\n"
        "provider: claude\n"
        f"project: {tag}\n"
        "privacy_level: private\n"
        "---\n\n"
        f"# G1 shadow NATS smoke ({tag})\n\n"
        "Synthetic content for the isolated RAG_INGRESS_SHADOW smoke. No real "
        "transcript content. Deleted after run=DONE.\n"
    )
    content_hash = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "kind": "conversation_chunk",
        "contentHash": content_hash,
        "idempotencyKey": f"{tag}",
        "targetProfile": "ragflow-transcript-memory",
        "source": {"provider": "claude", "project": tag, "host": "shadow", "producer": "g1-smoke"},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "contentType": "text/markdown",
                "filename": f"{tag}.md",
                "body": body,
                "metadata": {
                    "schema_version": "agent_knowledge_document.v2",
                    "result_type": "conversation_chunk",
                    "provider": "claude", "project": tag, "privacy_level": "private",
                },
            },
        },
    }


async def run_smoke(*, nats_url: str, stream: str, subject: str, durable: str,
                    store: IngestStateStore, backend: RAGFlowIndexBackendAdapter | None,
                    deliver: bool, tag: str, log: Callable[[str], None] = print) -> dict:
    """Isolated end-to-end NATS smoke: ensure shadow stream → publish 1 synthetic
    event → consume 1 → process. Never touches RAG_INGRESS_QUEUE."""
    if stream == "RAG_INGRESS_QUEUE":
        raise ValueError("smoke must not use the live RAG_INGRESS_QUEUE stream")
    import nats

    publish_subject = subject.replace(".>", ".transcript") if subject.endswith(".>") else subject
    nc = await nats.connect(nats_url)
    js = nc.jetstream()
    try:
        await js.stream_info(stream)
    except Exception:
        await js.add_stream(name=stream, subjects=[subject])
    event = build_synthetic_event(tag=tag)
    await js.publish(publish_subject, json.dumps(event).encode("utf-8"))
    log(f"smoke published to {publish_subject}")
    await nc.drain()
    consume = await run_consume(
        nats_url=nats_url, stream=stream, subject=subject, durable=durable,
        store=store, backend=backend, deliver=deliver, max_messages=1, idle_timeout=10.0, log=log,
    )
    return {"tag": tag, "content_hash": event["contentHash"], **consume}


def main() -> int:
    """Env-driven shadow-consume entrypoint (Ubuntu shadow container).

    Required env: RAG_INGRESS_NATS_URL, SHADOW_STREAM(!=RAG_INGRESS_QUEUE),
    SHADOW_SUBJECT, SHADOW_DURABLE, INGEST_STATE_DB_PATH. Delivery is OFF by default
    (SHADOW_DELIVER=0); when on, RAGFLOW_BASE_URL/RAGFLOW_API_KEY/RAGFLOW_DATASET_ID
    are required."""
    import argparse
    import asyncio
    import os

    parser = argparse.ArgumentParser(prog="rag-ingress-shadow-worker")
    parser.add_argument("--mode", choices=["consume", "smoke"], default="consume")
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--idle-timeout", type=float, default=5.0)
    parser.add_argument("--tag", default="g1-shadow-smoke")
    args = parser.parse_args()

    stream = os.environ.get("SHADOW_STREAM", "RAG_INGRESS_SHADOW")
    allow_live = os.environ.get("ALLOW_LIVE_QUEUE", "0") == "1"
    if stream == "RAG_INGRESS_QUEUE" and not allow_live:
        raise SystemExit("refusing: set ALLOW_LIVE_QUEUE=1 to consume the live RAG_INGRESS_QUEUE")
    store = IngestStateStore(os.environ["INGEST_STATE_DB_PATH"])
    deliver = os.environ.get("SHADOW_DELIVER", "0") == "1"
    broad_scan_pages = int(os.environ.get("NATURAL_KEY_BROAD_SCAN_PAGES", "0"))
    backend = None
    if deliver:
        _delivery_backend = os.environ.get("INGRESS_DELIVERY_BACKEND", "ragflow").strip().lower()
        if _delivery_backend == "couchdb":
            # CouchDB sink: construct CouchDBIndexBackendAdapter.
            # ragflow_client is NOT imported for this path.
            from .couchdb_index_backend import build_couchdb_index_backend
            backend = build_couchdb_index_backend(
                couchdb_url=os.environ["COUCHDB_URL"],
                couchdb_user=os.environ["COUCHDB_USER"],
                couchdb_password=os.environ["COUCHDB_PASSWORD"],
                couchdb_db=os.environ["COUCHDB_DB"],
            )
        else:
            # Default RAGFlow sink (ragflow or any unrecognised value).
            single = os.environ.get("RAGFLOW_DATASET_ID", "")
            # live (no single dataset): route per target profile like the Java worker.
            resolver = None if single else env_profile_dataset_resolver(os.environ.get)
            backend = build_backend(
                ragflow_base_url=os.environ["RAGFLOW_BASE_URL"],
                ragflow_api_key=os.environ["RAGFLOW_API_KEY"],
                dataset_id=single or None,
                resolve_dataset_id=resolver,
                broad_scan_pages=broad_scan_pages,
            )
        # M6 dual-write shadow (OFF unless MIRROR_DUAL_WRITE=1 + QDRANT_URL set).
        # Best-effort Qdrant mirror alongside the authoritative primary; a mirror
        # failure never breaks RAGFlow/CouchDB delivery. Default-off keeps the live
        # worker byte-identical.
        from .qdrant_dual_write import maybe_wrap_dual_write
        backend = maybe_wrap_dual_write(backend, environ=os.environ)
    nats_url = os.environ.get("RAG_INGRESS_NATS_URL", "nats://127.0.0.1:4222")
    subject = os.environ.get("SHADOW_SUBJECT", "rag.shadow.>")
    durable = os.environ.get("SHADOW_DURABLE", "shadow_python_worker")
    max_deliver = int(os.environ.get("MAX_DELIVER", "5"))
    fetch_batch = int(os.environ.get("WORKER_FETCH_BATCH", "1"))
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "1"))
    pressure_url = os.environ.get("RAG_INGRESS_PRESSURE_URL", "")
    pressure_open = build_pressure_check(pressure_url) if pressure_url else None
    if args.mode == "smoke":
        result = asyncio.run(run_smoke(
            nats_url=nats_url, stream=stream, subject=subject, durable=durable,
            store=store, backend=backend, deliver=deliver, tag=args.tag,
        ))
    else:
        result = asyncio.run(run_consume(
            nats_url=nats_url, stream=stream, subject=subject, durable=durable,
            store=store, backend=backend, deliver=deliver,
            max_messages=args.max_messages, idle_timeout=args.idle_timeout,
            allow_live=allow_live, max_deliver=max_deliver,
            fetch_batch=fetch_batch, concurrency=concurrency,
            pressure_open=pressure_open,
        ))
    print(json.dumps(result, sort_keys=True))
    return 0


def build_pressure_check(status_url: str, *, ttl: float = 10.0) -> Callable[[], bool]:
    """Cached pressure gate reusing the ingress-api's own verdict (single source,
    no policy re-implementation). Polls ``GET <status_url>`` at most once per
    ``ttl`` seconds; returns True (OPEN) on read failure (fail-open) so a flaky
    /status read never stalls delivery — divergence from Java fail-closed is
    deliberate because serial fetch(1)+maxDeliver already bound overload risk."""
    import urllib.request
    state = {"ts": -1e9, "open": True}

    def check() -> bool:
        now = time.time()
        if now - state["ts"] < ttl:
            return state["open"]
        try:
            with urllib.request.urlopen(status_url, timeout=5) as resp:
                data = json.load(resp)
            node = data.get("target") or data.get("queue") or data
            pressure = str((node or {}).get("pressure") or "OPEN").upper()
            state["open"] = pressure == "OPEN"
        except Exception:
            state["open"] = True
        state["ts"] = now
        return state["open"]

    return check


if __name__ == "__main__":
    raise SystemExit(main())

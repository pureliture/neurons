import asyncio
import copy
import hashlib
import os
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent_knowledge.couchdb_source.document_model import conversation_chunk_doc_id
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.couchdb_delivery_backend import CouchDBDeliveryBackend
from agent_knowledge.rag_ingress.shadow_worker import (
    IngestStateStore,
    build_synthetic_event,
    env_profile_dataset_resolver,
    process_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHADOW_SESSION_ID_HASH = "sha256:" + hashlib.sha256(b"shadow-worker-session").hexdigest()


def test_env_profile_dataset_resolver_uses_retired_index_bridge_session_dataset_env_key():
    env = {"RETIRED_INDEX_BRIDGE_SESSION_MEMORY_DATASET_ID": "ds_session"}

    resolve = env_profile_dataset_resolver(env.get)
    assert resolve("index-session-memory") == "ds_session"


def test_env_profile_dataset_resolver_covers_application_profiles_and_compose_env():
    profiles = _target_profile_contract()
    expected_keys = {
        profile: entry["retiredIndexBridgeDatasetEnv"]
        for profile, entry in profiles.items()
    }
    env = {
        key: f"ds_{profile.removeprefix('index-').replace('-', '_')}"
        for profile, key in expected_keys.items()
    }

    resolve = env_profile_dataset_resolver(env.get)

    assert list(profiles) == [
        "index-transcript-memory",
        "index-session-memory",
        "index-session-summary",
        "index-project-memory",
        "index-task-summary",
        "index-approved-memory-card",
        "index-procedural-memory",
    ]
    for profile, key in expected_keys.items():
        assert resolve(profile) == env[key]

    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    application_profiles = _application_target_profiles()
    for key in expected_keys.values():
        assert key in compose
        assert key in env_example
    for profile, entry in profiles.items():
        assert application_profiles[profile]["dataset-role"] == entry["datasetRole"]
        assert application_profiles[profile]["adapter"] == "retired_index_bridge"


def _target_profile_contract() -> dict[str, object]:
    contract = yaml.safe_load(
        (REPO_ROOT / "docs/contracts/target-profiles.yaml").read_text(encoding="utf-8")
    )
    assert contract["schemaVersion"] == "neurons.target_profiles.v1"
    return contract["profiles"]


def _application_target_profiles() -> dict[str, object]:
    application = yaml.safe_load(
        (REPO_ROOT / "src/main/resources/application.yml").read_text(encoding="utf-8")
    )
    return application["rag-ingress"]["target-profiles"]


def _payload_with_observed_bounds(*, tag: str) -> dict:
    payload = build_synthetic_event(tag=tag)
    payload["payload"]["document"]["metadata"].update(
        {
            "observed_at_start": "2026-07-01T00:00:00Z",
            "observed_at_end": "2026-07-01T00:05:00Z",
        }
    )
    return payload


def _couchdb_payload(*, tag: str, body: str = "safe shadow worker body") -> dict:
    payload = _payload_with_observed_bounds(tag=tag)
    payload["payload"]["document"]["body"] = body
    payload["payload"]["document"]["metadata"].update(
        {
            "session_id_hash": SHADOW_SESSION_ID_HASH,
            "chunk_id": f"chunk-{tag}",
            "turn_start_index": 0,
            "turn_end_index": 1,
            "part_index": 1,
            "part_count": 1,
            "char_start": 0,
            "char_end": len(body),
        }
    )
    payload["contentHash"] = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return payload


def _couchdb_backend(store: IngestStateStore) -> tuple[CouchDBDeliveryBackend, InMemoryCouchDBSourceStore]:
    couchdb_store = InMemoryCouchDBSourceStore()
    return CouchDBDeliveryBackend(state_db=store.state_db, store=couchdb_store), couchdb_store


def test_process_payload_adds_canonical_tables_to_legacy_state_db_without_replacing_legacy_rows(tmp_path):
    db_path = tmp_path / "ingress.sqlite"
    store = IngestStateStore(db_path)
    store.record(
        idempotency_key="legacy-observer-row",
        content_hash="sha256:legacy",
        document_kind="conversation_chunk",
        target_profile="index-transcript-memory",
        status="observed_no_deliver",
        now_iso="2026-07-01T00:00:00Z",
    )

    result = process_payload(
        _payload_with_observed_bounds(tag="canonical-table-coexistence"),
        store=store,
        backend=None,
        deliver=False,
    )

    assert result.status == "observed_no_deliver"
    with sqlite3.connect(db_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        legacy_row = connection.execute(
            "SELECT status, content_hash FROM shadow_ingest_log WHERE idempotency_key = ?",
            ("legacy-observer-row",),
        ).fetchone()

    assert {"shadow_ingest_log", "delivery_payloads", "delivery_jobs"} <= table_names
    assert legacy_row == ("observed_no_deliver", "sha256:legacy")


def test_process_payload_records_temporal_payload_and_success_proof_before_legacy_delivered_observer(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    payload = _couchdb_payload(tag="canonical-delivery-success")
    backend, couchdb_store = _couchdb_backend(store)
    original_record = store.record

    def record_after_canonical_success(**kwargs):
        if kwargs["status"] == "delivered":
            job = store.state_db.get_row(
                "delivery_jobs", "idempotency_key", kwargs["idempotency_key"]
            )
            assert job["status"] == "succeeded"
            assert job["index_document_id"]
        return original_record(**kwargs)

    store.record = record_after_canonical_success

    result = process_payload(payload, store=store, backend=backend, deliver=True)

    assert result.status == "delivered"
    stored_payload = store.state_db.get_delivery_payload(payload["idempotencyKey"])
    job = store.state_db.get_row("delivery_jobs", "idempotency_key", payload["idempotencyKey"])
    assert stored_payload["contentHash"] == payload["contentHash"]
    assert stored_payload["payload"]["document"]["metadata"]["observed_at_start"] == "2026-07-01T00:00:00Z"
    assert stored_payload["payload"]["document"]["metadata"]["observed_at_end"] == "2026-07-01T00:05:00Z"
    assert job["status"] == "succeeded"
    assert job["index_document_id"]
    assert job["last_reconciled_at"]
    chunk = couchdb_store.get(conversation_chunk_doc_id(SHADOW_SESSION_ID_HASH, "chunk-canonical-delivery-success"))
    assert chunk["observed_at_start"] == "2026-07-01T00:00:00Z"
    assert chunk["observed_at_end"] == "2026-07-01T00:05:00Z"


def test_couchdb_delivery_persists_wire_identity_before_deliver_time_redaction(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    body = "safe body Bearer testtoken-123456789"
    payload = _couchdb_payload(tag="canonical-redaction-order", body=body)
    backend, couchdb_store = _couchdb_backend(store)

    result = process_payload(payload, store=store, backend=backend, deliver=True)

    assert result.status == "delivered"
    stored_payload = store.state_db.get_delivery_payload(payload["idempotencyKey"])
    assert stored_payload["contentHash"] == payload["contentHash"]
    assert stored_payload["payload"]["document"]["body"] == body
    chunk = couchdb_store.get(conversation_chunk_doc_id(SHADOW_SESSION_ID_HASH, "chunk-canonical-redaction-order"))
    assert chunk is not None
    assert chunk["body"] != body
    assert "credential_scheme" in chunk["body"]


def test_process_payload_exact_duplicate_submits_once_and_reuses_canonical_success_proof(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    payload = _couchdb_payload(tag="canonical-exact-duplicate")
    backend, _ = _couchdb_backend(store)
    original_submit = backend.submit
    submit_calls = 0

    def counted_submit(job):
        nonlocal submit_calls
        submit_calls += 1
        return original_submit(job)

    backend.submit = counted_submit

    first = process_payload(payload, store=store, backend=backend, deliver=True)
    second = process_payload(copy.deepcopy(payload), store=store, backend=backend, deliver=True)

    assert (first.status, second.status) == ("delivered", "deduplicated")
    assert submit_calls == 1
    assert store.state_db.scalar("SELECT COUNT(*) FROM delivery_jobs") == 1
    assert store.state_db.get_delivery_payload(payload["idempotencyKey"]) == payload
    assert store.get_delivered(payload["idempotencyKey"]) is not None


def test_process_payload_conflict_preserves_canonical_and_legacy_delivery_observers(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    original = _couchdb_payload(tag="canonical-content-conflict")
    conflict = _couchdb_payload(
        tag="canonical-content-conflict-replacement",
        body="replacement body for the same idempotency key",
    )
    conflict["idempotencyKey"] = original["idempotencyKey"]
    backend, _ = _couchdb_backend(store)
    original_submit = backend.submit
    submit_calls = 0

    def counted_submit(job):
        nonlocal submit_calls
        submit_calls += 1
        return original_submit(job)

    backend.submit = counted_submit

    process_payload(original, store=store, backend=backend, deliver=True)
    legacy_before = store.get_delivered(original["idempotencyKey"])

    with pytest.raises(RuntimeError, match="state db accept rejected: conflict"):
        process_payload(conflict, store=store, backend=backend, deliver=True)

    assert submit_calls == 1
    assert store.state_db.get_delivery_payload(original["idempotencyKey"]) == original
    assert store.state_db.get_row(
        "delivery_jobs", "idempotency_key", original["idempotencyKey"]
    )["status"] == "succeeded"
    assert store.get_delivered(original["idempotencyKey"]) == legacy_before


def test_run_consume_naks_uncertain_delivery_and_keeps_canonical_job_replayable(tmp_path, monkeypatch):
    from agent_knowledge.rag_ingress.shadow_worker import run_consume

    store = IngestStateStore(tmp_path / "ingress.sqlite")
    payload = _couchdb_payload(tag="canonical-uncertain-delivery")

    class FailingCouchDBStore(InMemoryCouchDBSourceStore):
        def put(self, document):
            raise RuntimeError("simulated CouchDB mid-flight failure")

    backend = CouchDBDeliveryBackend(state_db=store.state_db, store=FailingCouchDBStore())

    class FakeMessage:
        def __init__(self):
            self.data = __import__("json").dumps(payload).encode("utf-8")
            self.metadata = SimpleNamespace(
                num_delivered=1,
                sequence=SimpleNamespace(stream=1),
            )
            self.ack_calls = 0
            self.nak_calls = 0

        async def ack(self):
            self.ack_calls += 1

        async def nak(self):
            self.nak_calls += 1

    message = FakeMessage()

    class FakeSubscription:
        def __init__(self):
            self.fetches = 0

        async def fetch(self, _count, timeout):
            self.fetches += 1
            if self.fetches == 1:
                return [message]
            raise TimeoutError("idle")

    class FakeJetStream:
        async def stream_info(self, _stream):
            return object()

        async def pull_subscribe(self, _subject, *, durable, stream):
            return FakeSubscription()

    class FakeNatsConnection:
        def jetstream(self):
            return FakeJetStream()

        async def drain(self):
            return None

    async def connect(_url):
        return FakeNatsConnection()

    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))

    result = asyncio.run(
        run_consume(
            nats_url="nats://fake",
            stream="RAG_INGRESS_SHADOW",
            subject="rag.shadow.>",
            durable="shadow-test",
            store=store,
            backend=backend,
            deliver=True,
            max_messages=1,
            idle_timeout=0.01,
            log=lambda _line: None,
        )
    )

    job = store.state_db.get_row("delivery_jobs", "idempotency_key", payload["idempotencyKey"])
    assert result["statuses"] == ["nak"]
    assert message.ack_calls == 0
    assert message.nak_calls == 1
    assert job["status"] == "replayable"
    assert store.get_delivered(payload["idempotencyKey"]) is None


def test_distinct_lease_owners_submit_exact_duplicate_once(tmp_path, monkeypatch):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    payload = _couchdb_payload(tag="canonical-concurrent-duplicate")
    backend, _ = _couchdb_backend(store)
    original_submit = backend.submit
    first_submit_entered = threading.Event()
    release_first_submit = threading.Event()
    submit_calls = 0
    results = []
    errors = []

    def blocked_submit(job):
        nonlocal submit_calls
        submit_calls += 1
        first_submit_entered.set()
        assert release_first_submit.wait(timeout=5)
        return original_submit(job)

    backend.submit = blocked_submit

    def deliver(owner: str) -> None:
        try:
            results.append(
                process_payload(
                    copy.deepcopy(payload), store=store, backend=backend,
                    deliver=True, lease_owner=owner,
                )
            )
        except Exception as exc:  # noqa: BLE001 - competing lease must fail closed
            errors.append(exc)

    first = threading.Thread(target=deliver, args=("shadow-worker:one",))
    second = threading.Thread(target=deliver, args=("shadow-worker:two",))
    first.start()
    assert first_submit_entered.wait(timeout=5)
    second.start()
    second.join(timeout=5)
    release_first_submit.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert submit_calls == 1
    assert [result.status for result in results] == ["delivered"]
    assert len(errors) == 1
    assert store.state_db.get_row("delivery_jobs", "idempotency_key", payload["idempotencyKey"])["status"] == "succeeded"


def test_ingest_state_store_creates_new_parent_private(tmp_path):
    db_path = tmp_path / "new-private-parent" / "ingress.sqlite"

    IngestStateStore(db_path)

    assert os.stat(db_path.parent).st_mode & 0o777 == 0o700


def test_ingest_state_store_rejects_existing_non_private_parent_without_chmod(tmp_path):
    parent = tmp_path / "existing-non-private-parent"
    parent.mkdir()
    os.chmod(parent, 0o755)

    with pytest.raises(ValueError, match="state db parent must be private"):
        IngestStateStore(parent / "ingress.sqlite")

    assert os.stat(parent).st_mode & 0o777 == 0o755


def test_main_injects_couchdb_backend_directly_with_one_process_lease(tmp_path, monkeypatch):
    import agent_knowledge.rag_ingress.couchdb_delivery_backend as couchdb_delivery_backend
    import agent_knowledge.rag_ingress.shadow_worker as shadow_worker

    private_parent = tmp_path / "private"
    private_parent.mkdir()
    os.chmod(private_parent, 0o700)
    captured = {}

    def build_backend(**kwargs):
        backend = CouchDBDeliveryBackend(
            state_db=kwargs["state_db"],
            store=InMemoryCouchDBSourceStore(),
        )
        captured["built"] = backend
        return backend

    async def run_consume(**kwargs):
        captured["backend"] = kwargs["backend"]
        captured["lease_owner"] = kwargs["lease_owner"]
        return {"processed": 0, "statuses": [], "store_counts": {}}

    monkeypatch.setattr(couchdb_delivery_backend, "build_couchdb_delivery_backend", build_backend)
    monkeypatch.setattr(shadow_worker, "run_consume", run_consume)
    monkeypatch.setattr(shadow_worker, "_new_lease_owner", lambda: "shadow-worker:pod-process-unique")
    monkeypatch.setattr(
        sys,
        "argv",
        ["rag-ingress-shadow-worker", "--mode", "consume", "--max-messages", "0"],
    )
    monkeypatch.setenv("INGEST_STATE_DB_PATH", str(private_parent / "ingress.sqlite"))
    monkeypatch.setenv("SHADOW_DELIVER", "1")
    monkeypatch.setenv("INGRESS_DELIVERY_BACKEND", "couchdb")
    monkeypatch.setenv("COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setenv("COUCHDB_USER", "test-user")
    monkeypatch.setenv("COUCHDB_PASSWORD", "test-password")
    monkeypatch.setenv("COUCHDB_DB", "test-db")

    assert shadow_worker.main() == 0
    assert captured["backend"] is captured["built"]
    assert isinstance(captured["backend"], CouchDBDeliveryBackend)
    assert captured["lease_owner"] == "shadow-worker:pod-process-unique"

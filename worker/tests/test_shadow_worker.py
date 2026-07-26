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
from agent_knowledge.redaction import redact_text_v2
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


def test_retired_default_state_store_keeps_legacy_sqlite_without_canonical_tables(tmp_path):
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
        _payload_with_observed_bounds(tag="retired-legacy-table-contract"),
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

    assert "shadow_ingest_log" in table_names
    assert "delivery_payloads" not in table_names
    assert "delivery_jobs" not in table_names
    assert legacy_row == ("observed_no_deliver", "sha256:legacy")


def test_process_payload_records_temporal_payload_and_success_proof_before_legacy_delivered_observer(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
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
    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
    body = "safe body dataset_id reference"
    payload = _couchdb_payload(tag="canonical-redaction-order", body=body)
    backend, couchdb_store = _couchdb_backend(store)

    result = process_payload(payload, store=store, backend=backend, deliver=True)

    assert result.status == "delivered"
    assert redact_text_v2(body) == body
    stored_payload = store.state_db.get_delivery_payload(payload["idempotencyKey"])
    assert stored_payload["contentHash"] == payload["contentHash"]
    assert stored_payload["payload"]["document"]["body"] == body
    chunk = couchdb_store.get(conversation_chunk_doc_id(SHADOW_SESSION_ID_HASH, "chunk-canonical-redaction-order"))
    assert chunk is not None
    assert chunk["body"] != body
    assert "dataset_ref" in chunk["body"]


def test_couchdb_rejects_wire_payload_that_conservative_redaction_would_change(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
    body = "public fixture Bearer exampletoken-123456789"
    payload = _couchdb_payload(tag="canonical-wire-redaction-reject", body=body)
    backend, couchdb_store = _couchdb_backend(store)

    result = process_payload(payload, store=store, backend=backend, deliver=True)

    assert redact_text_v2(body) != body
    assert result.status == "quarantined_wire_redaction"
    assert not result.delivered
    assert store.state_db.get_delivery_payload(payload["idempotencyKey"]) is None
    assert store.state_db.get_row("delivery_jobs", "idempotency_key", payload["idempotencyKey"]) is None
    assert couchdb_store.get(conversation_chunk_doc_id(SHADOW_SESSION_ID_HASH, "chunk-canonical-wire-redaction-reject")) is None


def test_process_payload_exact_duplicate_submits_once_and_reuses_canonical_success_proof(tmp_path):
    class RecordingMirror:
        def __init__(self) -> None:
            self.documents = []

        def submit_document(self, document):
            self.documents.append(document)
            return SimpleNamespace(document_ref=f"mirror-{len(self.documents)}")

    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
    payload = _couchdb_payload(tag="canonical-exact-duplicate")
    mirror = RecordingMirror()
    mirror_outcomes = []
    backend = CouchDBDeliveryBackend(
        state_db=store.state_db,
        store=InMemoryCouchDBSourceStore(),
        mirror=mirror,
        on_mirror_outcome=mirror_outcomes.append,
    )
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
    assert len(mirror.documents) == 1
    assert [outcome.status for outcome in mirror_outcomes] == ["mirrored"]
    assert store.state_db.scalar("SELECT COUNT(*) FROM delivery_jobs") == 1
    assert store.state_db.get_delivery_payload(payload["idempotencyKey"]) == payload
    assert store.get_delivered(payload["idempotencyKey"]) is not None


def test_process_payload_conflict_preserves_canonical_and_legacy_delivery_observers(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
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

    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
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
    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
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


def test_run_consume_assigns_unique_handler_leases_for_concurrent_exact_duplicates(tmp_path, monkeypatch):
    from agent_knowledge.rag_ingress.shadow_worker import run_consume

    store = IngestStateStore(tmp_path / "ingress.sqlite", canonical_state=True)
    payload = _couchdb_payload(tag="run-consume-concurrent-duplicate")
    backend, _ = _couchdb_backend(store)
    original_submit = backend.submit
    first_submit_entered = threading.Event()
    release_first_submit = threading.Event()
    submit_calls = 0
    assigned_owners: list[str] = []

    def blocked_submit(job):
        nonlocal submit_calls
        submit_calls += 1
        first_submit_entered.set()
        assert release_first_submit.wait(timeout=5)
        return original_submit(job)

    backend.submit = blocked_submit

    class FakeMessage:
        def __init__(self, *, delivered: int):
            self.data = __import__("json").dumps(payload).encode("utf-8")
            self.metadata = SimpleNamespace(
                num_delivered=delivered,
                sequence=SimpleNamespace(stream=delivered),
            )
            self.ack_calls = 0
            self.nak_calls = 0

        async def ack(self):
            self.ack_calls += 1

        async def nak(self):
            self.nak_calls += 1

    first, competing, redelivery = FakeMessage(delivered=1), FakeMessage(delivered=1), FakeMessage(delivered=2)

    class FakeSubscription:
        def __init__(self):
            self.batches = [[first, competing], [redelivery]]

        async def fetch(self, _count, timeout):
            del timeout
            if self.batches:
                return self.batches.pop(0)
            raise TimeoutError("idle")

    class FakeJetStream:
        async def stream_info(self, _stream):
            return object()

        async def pull_subscribe(self, _subject, *, durable, stream):
            del durable, stream
            return FakeSubscription()

    class FakeNatsConnection:
        def jetstream(self):
            return FakeJetStream()

        async def drain(self):
            return None

    async def connect(_url):
        return FakeNatsConnection()

    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))

    def owner_factory(base: str, ordinal: int) -> str:
        owner = f"{base}:test-{ordinal}"
        assigned_owners.append(owner)
        return owner

    async def consume() -> dict:
        task = asyncio.create_task(
            run_consume(
                nats_url="nats://fake",
                stream="RAG_INGRESS_SHADOW",
                subject="rag.shadow.>",
                durable="shadow-test",
                store=store,
                backend=backend,
                deliver=True,
                max_messages=2,
                idle_timeout=0.01,
                fetch_batch=2,
                concurrency=2,
                lease_owner="shadow-worker:test-run",
                lease_owner_factory=owner_factory,
                log=lambda _line: None,
            )
        )
        assert await asyncio.to_thread(first_submit_entered.wait, 5)
        release_first_submit.set()
        return await task

    result = asyncio.run(consume())

    assert submit_calls == 1
    assert assigned_owners in (
        ["shadow-worker:test-run:test-1", "shadow-worker:test-run:test-2"],
        [
            "shadow-worker:test-run:test-1",
            "shadow-worker:test-run:test-2",
            "shadow-worker:test-run:test-3",
        ],
    )
    assert len(assigned_owners) == len(set(assigned_owners))
    # The two first-batch handlers race for the lease. The competing handler
    # either observes the live lease and NAKs before a deduplicated redelivery,
    # or runs just after authoritative completion and deduplicates immediately.
    if len(assigned_owners) == 3:
        assert sorted(result["statuses"]) == ["deduplicated", "delivered", "nak"]
        assert first.ack_calls + competing.ack_calls == 1
        assert first.nak_calls + competing.nak_calls == 1
        assert (redelivery.ack_calls, redelivery.nak_calls) == (1, 0)
    else:
        assert sorted(result["statuses"]) == ["deduplicated", "delivered"]
        assert first.ack_calls + competing.ack_calls == 2
        assert first.nak_calls + competing.nak_calls == 0
        assert (redelivery.ack_calls, redelivery.nak_calls) == (0, 0)
    assert store.state_db.get_row(
        "delivery_jobs", "idempotency_key", payload["idempotencyKey"]
    )["status"] == "succeeded"


def test_ingest_state_store_creates_new_parent_private(tmp_path):
    db_path = tmp_path / "new-private-parent" / "ingress.sqlite"

    store = IngestStateStore(db_path, canonical_state=True)

    assert not db_path.parent.exists()
    assert store.state_db is not None

    assert os.stat(db_path.parent).st_mode & 0o777 == 0o700


def test_ingest_state_store_rejects_existing_non_private_parent_without_chmod(tmp_path):
    parent = tmp_path / "existing-non-private-parent"
    parent.mkdir()
    os.chmod(parent, 0o755)

    store = IngestStateStore(parent / "ingress.sqlite", canonical_state=True)
    with pytest.raises(ValueError, match="state db parent must be private"):
        _ = store.state_db

    assert os.stat(parent).st_mode & 0o777 == 0o755


def test_worker_image_precreates_private_canonical_state_parent_without_runtime_chmod():
    dockerfile = (REPO_ROOT / "worker" / "Dockerfile").read_text(encoding="utf-8")
    state_db = (REPO_ROOT / "worker" / "lib" / "agent_knowledge" / "rag_ingress" / "state_db.py").read_text(
        encoding="utf-8"
    )

    assert "install -d -m 0700 -o appuser -g appuser /var/lib/agent-knowledge/ingest-state" in dockerfile
    assert "os.chmod(" not in state_db


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
        captured["environ"] = kwargs["environ"]
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
    assert captured["environ"] is os.environ


@pytest.mark.parametrize("backend_name", ("retired_index_brdige", ""))
def test_main_rejects_unknown_delivery_backend_before_state_or_backend_startup(
    tmp_path,
    monkeypatch,
    backend_name,
):
    import agent_knowledge.rag_ingress.shadow_worker as shadow_worker

    state_path = tmp_path / "private" / "ingress.sqlite"

    def fail_backend_build(**_kwargs):
        raise AssertionError("unknown backend must not construct a delivery adapter")

    monkeypatch.setattr(shadow_worker, "build_backend", fail_backend_build)
    monkeypatch.setattr(
        sys,
        "argv",
        ["rag-ingress-shadow-worker", "--mode", "consume", "--max-messages", "0"],
    )
    monkeypatch.setenv("INGEST_STATE_DB_PATH", str(state_path))
    monkeypatch.setenv("SHADOW_DELIVER", "1")
    monkeypatch.setenv("INGRESS_DELIVERY_BACKEND", backend_name)

    with pytest.raises(SystemExit, match="must be one of"):
        shadow_worker.main()

    assert state_path.exists() is False


@pytest.mark.parametrize(
    "missing_name",
    ("COUCHDB_URL", "COUCHDB_USER", "COUCHDB_PASSWORD", "COUCHDB_DB"),
)
def test_main_rejects_blank_couchdb_config_before_state_or_backend_startup(
    tmp_path,
    monkeypatch,
    missing_name,
):
    import agent_knowledge.rag_ingress.couchdb_delivery_backend as couchdb_delivery_backend
    import agent_knowledge.rag_ingress.shadow_worker as shadow_worker

    private_parent = tmp_path / "private"
    private_parent.mkdir()
    os.chmod(private_parent, 0o700)
    state_path = private_parent / "ingress.sqlite"

    def fail_backend_build(**_kwargs):
        raise AssertionError("backend construction must not start with blank CouchDB config")

    monkeypatch.setattr(
        couchdb_delivery_backend,
        "build_couchdb_delivery_backend",
        fail_backend_build,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["rag-ingress-shadow-worker", "--mode", "consume", "--max-messages", "0"],
    )
    monkeypatch.setenv("INGEST_STATE_DB_PATH", str(state_path))
    monkeypatch.setenv("SHADOW_DELIVER", "1")
    monkeypatch.setenv("INGRESS_DELIVERY_BACKEND", "couchdb")
    configured = {
        "COUCHDB_URL": "http://couchdb.test",
        "COUCHDB_USER": "test-user",
        "COUCHDB_PASSWORD": "test-password",
        "COUCHDB_DB": "test-db",
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(missing_name, "   ")

    with pytest.raises(SystemExit, match=missing_name):
        shadow_worker.main()

    assert state_path.exists() is False

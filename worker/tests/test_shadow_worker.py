import asyncio
import copy
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent_knowledge.rag_ingress.retired_index_bridge import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    IndexStatus,
)
from agent_knowledge.rag_ingress.shadow_worker import (
    IngestStateStore,
    build_synthetic_event,
    env_profile_dataset_resolver,
    process_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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


class _RecordingDeliveryAdapter:
    def __init__(self, *, submit_status: str = IndexStatus.INDEXED, raise_after_submit: bool = False):
        self.submit_calls = 0
        self.submit_status = submit_status
        self.raise_after_submit = raise_after_submit
        self._handles: dict[tuple[str, str], BackendDocumentHandle] = {}

    def submit_document(self, document):
        self.submit_calls += 1
        handle = BackendDocumentHandle(
            dataset_ref="dataset-observer",
            document_ref=f"document-{self.submit_calls}",
        )
        self._handles[(document.idempotency_key, document.content_hash)] = handle
        if self.raise_after_submit:
            raise TimeoutError("delivery outcome is uncertain")
        return BackendSubmitResult(
            dataset_ref=handle.dataset_ref,
            document_ref=handle.document_ref,
            status=self.submit_status,
        )

    def find_by_natural_key(self, *, target_profile, idempotency_key, payload_hash):
        return self._handles.get((idempotency_key, payload_hash))

    def document_status_detail(self, handle):
        return BackendStatusDetail(
            status=self.submit_status,
            backend_raw_status="DONE" if self.submit_status == IndexStatus.INDEXED else "FAIL",
        )


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
    payload = _payload_with_observed_bounds(tag="canonical-delivery-success")
    backend = _RecordingDeliveryAdapter()
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
    assert backend.submit_calls == 1
    stored_payload = store.state_db.get_delivery_payload(payload["idempotencyKey"])
    job = store.state_db.get_row("delivery_jobs", "idempotency_key", payload["idempotencyKey"])
    assert stored_payload["contentHash"] == payload["contentHash"]
    assert stored_payload["payload"]["document"]["metadata"]["observed_at_start"] == "2026-07-01T00:00:00Z"
    assert stored_payload["payload"]["document"]["metadata"]["observed_at_end"] == "2026-07-01T00:05:00Z"
    assert job["status"] == "succeeded"
    assert job["index_document_id"]


def test_process_payload_exact_duplicate_submits_once_and_reuses_canonical_success_proof(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    payload = _payload_with_observed_bounds(tag="canonical-exact-duplicate")
    backend = _RecordingDeliveryAdapter()

    first = process_payload(payload, store=store, backend=backend, deliver=True)
    second = process_payload(copy.deepcopy(payload), store=store, backend=backend, deliver=True)

    assert (first.status, second.status) == ("delivered", "deduplicated")
    assert backend.submit_calls == 1
    assert store.state_db.scalar("SELECT COUNT(*) FROM delivery_jobs") == 1
    assert store.state_db.get_delivery_payload(payload["idempotencyKey"]) == payload
    assert store.get_delivered(payload["idempotencyKey"]) is not None


def test_process_payload_conflict_preserves_canonical_and_legacy_delivery_observers(tmp_path):
    store = IngestStateStore(tmp_path / "ingress.sqlite")
    original = _payload_with_observed_bounds(tag="canonical-content-conflict")
    conflict = _payload_with_observed_bounds(tag="canonical-content-conflict-replacement")
    conflict["idempotencyKey"] = original["idempotencyKey"]
    backend = _RecordingDeliveryAdapter()

    process_payload(original, store=store, backend=backend, deliver=True)
    legacy_before = store.get_delivered(original["idempotencyKey"])

    with pytest.raises(RuntimeError, match="state db accept rejected: conflict"):
        process_payload(conflict, store=store, backend=backend, deliver=True)

    assert backend.submit_calls == 1
    assert store.state_db.get_delivery_payload(original["idempotencyKey"]) == original
    assert store.state_db.get_row(
        "delivery_jobs", "idempotency_key", original["idempotencyKey"]
    )["status"] == "succeeded"
    assert store.get_delivered(original["idempotencyKey"]) == legacy_before


def test_run_consume_naks_uncertain_delivery_and_keeps_canonical_job_replayable(tmp_path, monkeypatch):
    from agent_knowledge.rag_ingress.shadow_worker import run_consume

    store = IngestStateStore(tmp_path / "ingress.sqlite")
    payload = _payload_with_observed_bounds(tag="canonical-uncertain-delivery")
    backend = _RecordingDeliveryAdapter(raise_after_submit=True)

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
    assert backend.submit_calls == 1
    assert job["status"] == "replayable"
    assert store.get_delivered(payload["idempotencyKey"]) is None

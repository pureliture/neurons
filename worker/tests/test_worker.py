"""Tests for the co-located Python delivery worker.

Focus: the boundary this co-locate is responsible for —
  - the vendored package imports the live delivery path WITHOUT pulling in the
    client/Ledger/outbox wiring;
  - the redelivery dedup (under-dedup gap fix) works via both layers;
  - submit_document persists the natural key so the RAGFlow-side probe can match.
"""
import importlib

import pytest

from agent_knowledge.rag_ingress.shadow_worker import (
    IngestStateStore,
    build_synthetic_event,
    process_payload,
)
from agent_knowledge.rag_ingress.index_backend import (
    BackendDocumentHandle,
    BackendSubmitResult,
    RAGFlowIndexBackendAdapter,
)
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


# --- fakes -----------------------------------------------------------------

class FakeBackend:
    """Counts uploads and lets a test inject a natural-key hit."""

    def __init__(self, natural_key_hit: BackendDocumentHandle | None = None):
        self.submit_calls = 0
        self.natural_key_hit = natural_key_hit
        self.nk_calls = 0

    def submit_document(self, document, *, on_step_complete=None):
        self.submit_calls += 1
        return BackendSubmitResult(
            dataset_ref="ds-1",
            document_ref=f"doc-{self.submit_calls}",
            status="UNSTART",
        )

    def find_by_natural_key(self, *, target_profile, idempotency_key, payload_hash):
        self.nk_calls += 1
        return self.natural_key_hit


class FakeClient:
    """Captures the metadata dict passed to update_metadata."""

    def __init__(self):
        self.metadata = None

    def upload_document(self, dataset_id, body, *, filename):
        return {"document_id": "d1", "run": "UNSTART"}

    def update_metadata(self, dataset_id, document_id, metadata):
        self.metadata = dict(metadata)

    def request_parse(self, dataset_id, document_ids):
        return None


# --- vendoring boundary ----------------------------------------------------

def test_server_state_primitives_are_vendored_without_client_or_ledger_wiring():
    # The live path imports cleanly (and importing the package does not pull in
    # Ledger via the trimmed __init__).
    import agent_knowledge.rag_ingress  # noqa: F401
    import agent_knowledge.rag_ingress.shadow_worker  # noqa: F401

    for included in (
        "agent_knowledge.rag_ingress.state_db",
        "agent_knowledge.rag_ingress.idempotency",
        "agent_knowledge.rag_ingress.domain_state",
        "agent_knowledge.rag_ingress.ingress_journal",
        "agent_knowledge.rag_ingress.delivery_executor",
        "agent_knowledge.rag_ingress.delivery_reconcile",
        "agent_knowledge.rag_ingress.delivery_backend",
        "agent_knowledge.rag_ingress.delivery_drain",
        "agent_knowledge.rag_ingress.backfill",
        "agent_knowledge.rag_ingress.backfill_apply",
        "agent_knowledge.rag_ingress.product_surface_switch_plan",
        "agent_knowledge.rag_ingress.replay_delivery",
        "agent_knowledge.rag_ingress.state_shadow_readiness",
        "agent_knowledge.rag_ingress.retirement_readiness",
        "agent_knowledge.rag_ingress.state_sink",
        "agent_knowledge.backfill",
        "agent_knowledge.document_envelope",
        "agent_knowledge.memory_card",
        "agent_knowledge.memory_regeneration",
        "agent_knowledge.curation",
        "agent_knowledge.memory_miner",
        "agent_knowledge.query_planner",
        "agent_knowledge.tool_evidence_sync",
        "agent_knowledge.transcript_chunking",
        "agent_knowledge.transcript_ingest",
        "agent_knowledge.transcript_packer",
        "agent_knowledge.transcript_parsers",
        "agent_knowledge.transcript_model",
        "agent_knowledge.session_memory.brain_query",
        "agent_knowledge.session_memory.brain_read_model",
        "agent_knowledge.session_memory.backfill",
        "agent_knowledge.session_memory.gc_backup",
        "agent_knowledge.session_memory.llm_brain_service",
        "agent_knowledge.session_memory.memory_card",
        "agent_knowledge.session_memory.curation",
        "agent_knowledge.session_memory.memory_evaluation",
        "agent_knowledge.session_memory.memory_regeneration",
        "agent_knowledge.session_memory.memory_miner",
        "agent_knowledge.session_memory.memory_promotion",
        "agent_knowledge.session_memory.native_memory_governance",
        "agent_knowledge.session_memory.native_memory_mirror",
        "agent_knowledge.session_memory.native_memory_recall",
        "agent_knowledge.session_memory.native_memory_reconcile",
        "agent_knowledge.session_memory.native_memory_sync_approval",
        "agent_knowledge.session_memory.native_memory_writer",
        "agent_knowledge.session_memory.native_memory_write_runner",
        "agent_knowledge.session_memory.query_planner",
        "agent_knowledge.session_memory.ragflow_projection",
        "agent_knowledge.session_memory.session_memory_gc",
        "agent_knowledge.session_memory.transcript_model",
        "agent_knowledge.session_memory.terminal_skipped_quarantine",
        "agent_knowledge.session_memory.tool_evidence_sync",
        "agent_knowledge.session_memory.transcript_chunking",
        "agent_knowledge.session_memory.transcript_ingest",
        "agent_knowledge.session_memory.transcript_packer",
        "agent_knowledge.session_memory.transcript_parsers",
        "agent_knowledge.session_memory.transcript_quality",
        "agent_knowledge.session_memory.transcript_memory_gc",
        "agent_knowledge.session_memory.transcript_volume_gc",
        "agent_knowledge.session_memory.zombie_snapshot_repair",
    ):
        importlib.import_module(included)

    gc_backup = importlib.import_module("agent_knowledge.session_memory.gc_backup")
    # restore_gc_backup is vendored with the GC executors so a mistaken hard delete
    # is recoverable (re-upload + re-embed) where GC runs; it takes the ragflow
    # client as an argument, so the slice still has no wired client.
    assert hasattr(gc_backup, "restore_gc_backup")

    for excluded in (
        "agent_knowledge.rag_ingress.state_store",
        "agent_knowledge.rag_ingress.outbox_client",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(excluded)

    transcript_ingest = importlib.import_module("agent_knowledge.session_memory.transcript_ingest")
    assert not hasattr(transcript_ingest, "IngressQueueClient")
    assert not hasattr(transcript_ingest, "StateDBIngressSink")

    memory_regeneration = importlib.import_module("agent_knowledge.session_memory.memory_regeneration")
    assert not hasattr(memory_regeneration, "IngressQueueClient")


# --- redelivery dedup ------------------------------------------------------

def test_first_delivery_uploads_once(tmp_path):
    store = IngestStateStore(tmp_path / "s.sqlite")
    backend = FakeBackend()
    res = process_payload(build_synthetic_event(tag="first"), store=store, backend=backend, deliver=True)
    assert res.status == "delivered"
    assert res.delivered is True
    assert backend.submit_calls == 1


def test_redelivery_dedups_via_local_log(tmp_path):
    store = IngestStateStore(tmp_path / "s.sqlite")
    backend = FakeBackend()
    payload = build_synthetic_event(tag="local-dedup")

    r1 = process_payload(payload, store=store, backend=backend, deliver=True)
    assert r1.status == "delivered"
    assert backend.submit_calls == 1

    # NATS redelivers the same message: no second upload, reuse the prior ref.
    r2 = process_payload(payload, store=store, backend=backend, deliver=True)
    assert r2.status == "deduplicated"
    assert r2.delivered is True
    assert backend.submit_calls == 1          # NOT re-uploaded
    assert r2.document_ref == r1.document_ref  # same RAGFlow document


def test_redelivery_dedups_via_natural_key_when_local_log_lost(tmp_path):
    # Fresh local volume (no prior row) but the document already exists in RAGFlow
    # from a first attempt that uploaded then crashed before recording.
    store = IngestStateStore(tmp_path / "s.sqlite")
    backend = FakeBackend(natural_key_hit=BackendDocumentHandle(dataset_ref="ds-9", document_ref="pre-existing"))
    res = process_payload(build_synthetic_event(tag="nk-dedup"), store=store, backend=backend, deliver=True)
    assert res.status == "deduplicated"
    assert backend.submit_calls == 0          # found in RAGFlow, no upload
    assert res.document_ref == "pre-existing"
    assert backend.nk_calls == 1


def test_local_dedup_survives_restart(tmp_path):
    db = tmp_path / "s.sqlite"
    payload = build_synthetic_event(tag="restart")
    b1 = FakeBackend()
    process_payload(payload, store=IngestStateStore(db), backend=b1, deliver=True)
    assert b1.submit_calls == 1
    # New worker process == new store object over the SAME sqlite file.
    b2 = FakeBackend()
    res = process_payload(payload, store=IngestStateStore(db), backend=b2, deliver=True)
    assert res.status == "deduplicated"
    assert b2.submit_calls == 0


# --- natural-key persistence ----------------------------------------------

def test_submit_document_persists_natural_key_metadata():
    client = FakeClient()
    adapter = RAGFlowIndexBackendAdapter(client=client, resolve_dataset_id=lambda _profile: "ds-1")
    document = build_rag_ready_document(
        target_profile="ragflow-transcript-memory",
        document_kind="conversation_chunk",
        source_namespace="claude",
        source_alias="x.md",
        privacy_class="private",
        body="# body\n",
        filename="x.md",
        metadata={"provider": "claude"},
    )
    adapter.submit_document(document)
    assert client.metadata["content_hash"] == document.content_hash
    assert client.metadata["idempotency_key"] == document.idempotency_key
    assert client.metadata["provider"] == "claude"  # producer metadata preserved


# --- transport closure -----------------------------------------------------

def test_default_transport_closure_resolves():
    # The default RagflowHttpClient transport (_urllib_transport) lazily imports
    # transport_contract.ProxyResponse at call time; it must be vendored or live
    # delivery fails at runtime with ModuleNotFoundError.
    import agent_knowledge.ragflow_client as rc
    from agent_knowledge.transport_contract import ProxyResponse  # noqa: F401

    client = rc.RagflowHttpClient(base_url="http://example", bearer_token="t")
    assert client.transport is rc._urllib_transport


# --- natural-key match fail-safe -------------------------------------------

def test_natural_key_match_requires_idempotency_key():
    from agent_knowledge.rag_ingress.index_backend import _document_matches_natural_key

    ph = "sha256:abc"
    ik = "claude:conversation_chunk:" + ph
    # both present and equal -> match
    assert _document_matches_natural_key(
        {"meta_fields": {"content_hash": ph, "idempotency_key": ik}},
        idempotency_key=ik, payload_hash=ph) is True
    # content_hash matches but idempotency_key missing -> NO match (fail-safe:
    # body-equal docs with different namespaces share content_hash; re-upload).
    assert _document_matches_natural_key(
        {"meta_fields": {"content_hash": ph}},
        idempotency_key=ik, payload_hash=ph) is False
    # idempotency_key present but different -> no match
    assert _document_matches_natural_key(
        {"meta_fields": {"content_hash": ph, "idempotency_key": "other"}},
        idempotency_key=ik, payload_hash=ph) is False
    # content_hash mismatch -> no match
    assert _document_matches_natural_key(
        {"meta_fields": {"content_hash": "sha256:zzz", "idempotency_key": ik}},
        idempotency_key=ik, payload_hash=ph) is False


def test_find_by_natural_key_empty_inputs_fail_closed():
    from agent_knowledge.rag_ingress.index_backend import RAGFlowIndexBackendAdapter

    class BoomClient:
        def list_documents(self, *a, **k):
            raise AssertionError("must not scan when the natural key is empty")

    adapter = RAGFlowIndexBackendAdapter(client=BoomClient(), resolve_dataset_id=lambda _p: "ds")
    assert adapter.find_by_natural_key(target_profile="p", idempotency_key="", payload_hash="sha256:x") is None
    assert adapter.find_by_natural_key(target_profile="p", idempotency_key="k", payload_hash="") is None


def test_find_by_natural_key_does_not_broad_scan_by_default():
    from agent_knowledge.rag_ingress.index_backend import RAGFlowIndexBackendAdapter

    payload_hash = "sha256:" + "abcdef123456" + ("0" * 52)

    class RecordingClient:
        def __init__(self):
            self.calls = []

        def list_documents(self, dataset_id, *, page=1, page_size=100, keywords=""):
            self.calls.append({"page": page, "page_size": page_size, "keywords": keywords})
            return []

    client = RecordingClient()
    adapter = RAGFlowIndexBackendAdapter(client=client, resolve_dataset_id=lambda _p: "ds")

    assert adapter.find_by_natural_key(
        target_profile="p", idempotency_key="ik", payload_hash=payload_hash
    ) is None
    assert [call["keywords"] for call in client.calls] == [payload_hash, "abcdef123456"]


def test_find_by_natural_key_checks_content_hash_fragment_keyword():
    from agent_knowledge.rag_ingress.index_backend import RAGFlowIndexBackendAdapter

    payload_hash = "sha256:" + "abcdef123456" + ("0" * 52)
    idempotency_key = "ik"

    class FragmentClient:
        def __init__(self):
            self.calls = []

        def list_documents(self, dataset_id, *, page=1, page_size=100, keywords=""):
            self.calls.append(keywords)
            if keywords == "abcdef123456":
                return [
                    {
                        "id": "doc_fragment_match",
                        "meta_fields": {
                            "content_hash": payload_hash,
                            "idempotency_key": idempotency_key,
                        },
                    }
                ]
            return []

    client = FragmentClient()
    adapter = RAGFlowIndexBackendAdapter(client=client, resolve_dataset_id=lambda _p: "ds")

    handle = adapter.find_by_natural_key(
        target_profile="p", idempotency_key=idempotency_key, payload_hash=payload_hash
    )

    assert handle == BackendDocumentHandle(dataset_ref="ds", document_ref="doc_fragment_match")
    assert client.calls == [payload_hash, "abcdef123456"]

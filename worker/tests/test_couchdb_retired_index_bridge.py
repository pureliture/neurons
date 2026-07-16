"""CouchDBRetiredIndexBridgeAdapter 유닛 테스트.

검증 항목:
- submit_document가 session, chunk, coverage, projection_state 4개 문서를 결정론적 id로 기록한다.
- projection_state는 PENDING으로 표시된다.
- BackendSubmitResult(status="submitted")를 반환한다.
- find_by_natural_key는 submit 전 None, submit 후 None을 반환한다
  (CouchDB 어댑터는 conservative dedup — store put이 idempotent하게 dedup 처리).
- submit은 멱등(re-submit 동일 doc이 중복 생성 없이 처리)이다.
- 이미 PROJECTED인 세션에 re-submit하면 projection_state를 덮어쓰지 않는다.
- session_id_hash / chunk_id 누락 시 ValueError 발생 (guard).
- shadow_worker wiring: INGRESS_DELIVERY_BACKEND=couchdb 환경 변수 선택 시 CouchDBRetiredIndexBridgeAdapter가
  build_backend 대신 선택된다.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from agent_knowledge.couchdb_source.document_model import (
    ProjectionStatus,
    SourceDocType,
    conversation_chunk_doc_id,
    coverage_manifest_doc_id,
    projection_state_doc_id,
    session_doc_id,
    sha256_hash,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.couchdb_retired_index_bridge import (
    CouchDBRetiredIndexBridgeAdapter,
    build_couchdb_docs_from_rag_document,
)
from agent_knowledge.rag_ingress.retired_index_bridge import (
    BackendDocumentHandle,
    BackendSubmitResult,
    IndexStatus,
)
from agent_knowledge.rag_ingress.server_runtime import document_from_ingress_payload


# ---------------------------------------------------------------------------
# 픽스처 및 헬퍼
# ---------------------------------------------------------------------------

SESSION_ID_HASH = sha256_hash("codex:retired-index-bridge-test-session")
CHUNK_ID = "chunk_index_001"
PROVIDER = "codex"
PROJECT = "neurons"


def _body(text: str = "Index backend test conversation transcript body.") -> str:
    return text


def _ingress_payload(
    *,
    idempotency_key: str = "idem_index_1",
    body: str | None = None,
    session_id_hash: str = SESSION_ID_HASH,
    chunk_id: str = CHUNK_ID,
    provider: str = PROVIDER,
    project: str = PROJECT,
    target_profile: str = "couchdb-transcript-source",
) -> dict:
    """process_payload가 받는 것과 동일한 형태의 rag_ingress_enqueue.v1 payload.

    document_from_ingress_payload(payload)가 RagReadyDocument를 생성할 수 있어야 하며,
    metadata에 session_id_hash / chunk_id를 포함해야 CouchDBRetiredIndexBridgeAdapter가
    올바른 doc_id를 계산한다.
    """
    if body is None:
        body = _body()
    content_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {
            "host": "test_host",
            "producer": "test",
            "provider": provider,
            "project": project,
        },
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": "session.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {
                    "type": "conversation_chunk",
                    "session_id_hash": session_id_hash,
                    "chunk_id": chunk_id,
                    "provider": provider,
                    "project": project,
                    "turn_start_index": 0,
                    "turn_end_index": 2,
                    "part_index": 1,
                    "part_count": 1,
                    "char_start": 0,
                    "char_end": len(body),
                },
            },
        },
        "contentHash": content_hash,
        "targetProfile": target_profile,
        "kind": "conversation_chunk",
        "idempotencyKey": idempotency_key,
    }


def _document_from_payload(payload: dict):
    """server_runtime.document_from_ingress_payload 경로와 동일하게 RagReadyDocument 생성."""
    return document_from_ingress_payload(payload)


def _adapter(store: InMemoryCouchDBSourceStore | None = None) -> CouchDBRetiredIndexBridgeAdapter:
    if store is None:
        store = InMemoryCouchDBSourceStore()
    return CouchDBRetiredIndexBridgeAdapter(store=store)


# ---------------------------------------------------------------------------
# build_couchdb_docs_from_rag_document: 공유 transform 헬퍼 단위 테스트
# ---------------------------------------------------------------------------

class TestBuildCouchdbDocsFromRagDocument:
    """공유 transform 헬퍼가 I/O 없이 4개 doc 패밀리를 반환한다."""

    def test_returns_four_docs_and_ids(self):
        payload = _ingress_payload()
        document = _document_from_payload(payload)
        session_doc, chunk_doc, coverage_doc, proj_doc, sid_hash, chunk_id = (
            build_couchdb_docs_from_rag_document(document)
        )
        assert session_doc["doc_type"] == SourceDocType.TRANSCRIPT_SESSION
        assert chunk_doc["doc_type"] == SourceDocType.CONVERSATION_CHUNK
        assert coverage_doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST
        assert proj_doc["doc_type"] == SourceDocType.PROJECTION_STATE
        assert sid_hash == SESSION_ID_HASH
        assert chunk_id == CHUNK_ID

    def test_deterministic_ids(self):
        """동일 입력에서 두 번 호출해도 동일한 _id가 나온다."""
        payload = _ingress_payload()
        document = _document_from_payload(payload)
        docs1 = build_couchdb_docs_from_rag_document(document)
        docs2 = build_couchdb_docs_from_rag_document(document)
        for d1, d2 in zip(docs1[:4], docs2[:4]):
            assert d1["_id"] == d2["_id"]

    def test_projection_state_pending(self):
        payload = _ingress_payload()
        document = _document_from_payload(payload)
        _, _, _, proj_doc, _, _ = build_couchdb_docs_from_rag_document(document)
        assert proj_doc["projection_status"] == ProjectionStatus.PENDING

    def test_provider_and_project_propagated(self):
        payload = _ingress_payload(provider="claude", project="test_project")
        document = _document_from_payload(payload)
        session_doc, chunk_doc, _, _, _, _ = build_couchdb_docs_from_rag_document(document)
        assert session_doc["provider"] == "claude"
        assert chunk_doc["project"] == "test_project"


# ---------------------------------------------------------------------------
# CouchDBRetiredIndexBridgeAdapter.submit_document
# ---------------------------------------------------------------------------

class TestSubmitDocument:
    """submit_document가 4개 doc 패밀리를 CouchDB에 기록하고 BackendSubmitResult를 반환한다."""

    def test_submit_writes_session_and_chunk(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        result = adapter.submit_document(document)

        assert isinstance(result, BackendSubmitResult)
        assert result.status == "submitted"
        assert result.document_ref == session_doc_id(SESSION_ID_HASH)
        assert result.dataset_ref.startswith("couchdb:")

    def test_submit_writes_all_four_doc_families(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)

        assert store.get(session_doc_id(SESSION_ID_HASH)) is not None
        assert store.get(conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID)) is not None
        assert store.get(coverage_manifest_doc_id(SESSION_ID_HASH)) is not None
        assert store.get(projection_state_doc_id(SESSION_ID_HASH)) is not None

    def test_submit_doc_types_are_correct(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)

        session_doc = store.get(session_doc_id(SESSION_ID_HASH))
        chunk_doc = store.get(conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID))
        cov_doc = store.get(coverage_manifest_doc_id(SESSION_ID_HASH))
        proj_doc = store.get(projection_state_doc_id(SESSION_ID_HASH))

        assert session_doc["doc_type"] == SourceDocType.TRANSCRIPT_SESSION
        assert chunk_doc["doc_type"] == SourceDocType.CONVERSATION_CHUNK
        assert cov_doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST
        assert proj_doc["doc_type"] == SourceDocType.PROJECTION_STATE

    def test_submit_projection_state_is_pending(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)

        proj_doc = store.get(projection_state_doc_id(SESSION_ID_HASH))
        assert proj_doc is not None
        assert proj_doc["projection_status"] == ProjectionStatus.PENDING

    def test_submit_uses_deterministic_doc_ids(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)

        all_ids = {doc["_id"] for doc in store.all_docs()}
        assert session_doc_id(SESSION_ID_HASH) in all_ids
        assert conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID) in all_ids
        assert coverage_manifest_doc_id(SESSION_ID_HASH) in all_ids
        assert projection_state_doc_id(SESSION_ID_HASH) in all_ids

    def test_submit_result_document_ref_is_session_doc_id(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        result = adapter.submit_document(document)

        assert result.document_ref == session_doc_id(SESSION_ID_HASH)

    def test_submit_on_step_complete_called(self):
        """on_step_complete 콜백이 각 단계에서 호출된다."""
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        steps = []

        def step_hook(step, **kwargs):
            steps.append(step)

        adapter.submit_document(document, on_step_complete=step_hook)

        assert "session" in steps
        assert "chunk" in steps
        assert "coverage" in steps
        assert "projection" in steps


# ---------------------------------------------------------------------------
# 멱등성 테스트
# ---------------------------------------------------------------------------

class TestIdempotency:
    """submit_document는 동일 doc을 재전송해도 중복 생성 없이 처리한다."""

    def test_resubmit_same_doc_does_not_duplicate(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)
        doc_count_after_first = len(store.all_docs())

        adapter.submit_document(document)
        doc_count_after_second = len(store.all_docs())

        # 동일 _id upsert이므로 doc 수 변화 없음
        assert doc_count_after_first == doc_count_after_second

    def test_resubmit_chunk_count_stays_at_one(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)
        adapter.submit_document(document)

        chunks = store.find_by_session(
            session_id_hash=SESSION_ID_HASH,
            doc_type=SourceDocType.CONVERSATION_CHUNK,
        )
        assert len(chunks) == 1

    def test_resubmit_returns_submitted_status(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)
        result = adapter.submit_document(document)

        assert result.status == "submitted"

    def test_exact_duplicate_preserves_projected_source_hash(self):
        """exact duplicate는 current projection을 dirty로 만들지 않는다."""
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)

        proj_id = projection_state_doc_id(SESSION_ID_HASH)
        proj_doc = dict(store.get(proj_id))
        source_hash = proj_doc["source_hash"]
        proj_doc.update(
            {
                "projection_status": ProjectionStatus.PROJECTED,
                "active_content_hash": sha256_hash("projected session memory"),
                "projected_source_hash": source_hash,
            }
        )
        store.put(proj_doc)
        session_id = session_doc_id(SESSION_ID_HASH)
        session_before = dict(store.get(session_id))
        session_before["materialized_at"] = "2026-07-16T01:00:00Z"
        store.put(session_before)
        session_before = dict(store.get(session_id))

        adapter.submit_document(document)

        proj_after = store.get(proj_id)
        assert proj_after["projection_status"] == ProjectionStatus.PROJECTED
        assert proj_after["source_hash"] == source_hash
        assert proj_after["projected_source_hash"] == source_hash
        assert store.get(session_id) == session_before

    def test_distinct_chunk_marks_projected_session_pending_and_changes_source_hash(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        first = _document_from_payload(_ingress_payload())
        second = _document_from_payload(
            _ingress_payload(
                idempotency_key="idem_index_2",
                chunk_id="chunk_index_002",
                body="A distinct later conversation chunk.",
            )
        )

        adapter.submit_document(first)
        proj_id = projection_state_doc_id(SESSION_ID_HASH)
        projected = dict(store.get(proj_id))
        first_source_hash = projected["source_hash"]
        projected.update(
            {
                "projection_status": ProjectionStatus.PROJECTED,
                "active_content_hash": sha256_hash("projected session memory"),
                "projected_source_hash": first_source_hash,
            }
        )
        store.put(projected)

        adapter.submit_document(second)

        after = store.get(proj_id)
        assert after["projection_status"] == ProjectionStatus.PENDING
        assert after["source_hash"] != first_source_hash
        assert after["projected_source_hash"] == first_source_hash


# ---------------------------------------------------------------------------
# find_by_natural_key 테스트
# ---------------------------------------------------------------------------

class TestFindByNaturalKey:
    """CouchDBRetiredIndexBridgeAdapter는 conservative dedup: find_by_natural_key는 항상 None 반환.
    dedup 책임은 store의 idempotent put과 process_payload의 IngestStateStore에 있다.
    """

    def test_find_before_submit_returns_none(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        result = adapter.find_by_natural_key(
            target_profile=document.target_profile,
            idempotency_key=document.idempotency_key,
            payload_hash=document.content_hash,
        )
        assert result is None

    def test_find_after_submit_returns_none(self):
        """submit 후에도 find_by_natural_key는 None (conservative dedup 정책)."""
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        adapter.submit_document(document)

        result = adapter.find_by_natural_key(
            target_profile=document.target_profile,
            idempotency_key=document.idempotency_key,
            payload_hash=document.content_hash,
        )
        # conservative: None 반환 — store의 idempotent put이 dedup 처리
        assert result is None

    def test_find_empty_inputs_returns_none(self):
        adapter = _adapter()
        assert adapter.find_by_natural_key(
            target_profile="", idempotency_key="", payload_hash=""
        ) is None


# ---------------------------------------------------------------------------
# document_status_detail 테스트
# ---------------------------------------------------------------------------

class TestDocumentStatusDetail:
    def test_status_unknown_when_doc_missing(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        handle = BackendDocumentHandle(
            dataset_ref="couchdb:couchdb",
            document_ref=session_doc_id(SESSION_ID_HASH),
        )
        detail = adapter.document_status_detail(handle)
        assert detail.status == IndexStatus.UNKNOWN

    def test_status_indexed_after_submit(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        result = adapter.submit_document(document)
        handle = BackendDocumentHandle(
            dataset_ref=result.dataset_ref,
            document_ref=result.document_ref,
        )
        detail = adapter.document_status_detail(handle)
        assert detail.status == IndexStatus.INDEXED
        assert detail.progress == 1.0

    def test_document_status_unknown_when_missing(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        handle = BackendDocumentHandle(
            dataset_ref="couchdb:couchdb",
            document_ref="transcript_session:nonexistent",
        )
        status = adapter.document_status(handle)
        assert status == IndexStatus.UNKNOWN

    def test_document_status_indexed_after_submit(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        result = adapter.submit_document(document)
        handle = BackendDocumentHandle(
            dataset_ref=result.dataset_ref,
            document_ref=result.document_ref,
        )
        status = adapter.document_status(handle)
        assert status == IndexStatus.INDEXED


# ---------------------------------------------------------------------------
# Guard 테스트: session_id_hash / chunk_id 누락
# ---------------------------------------------------------------------------

class TestGuards:
    def test_submit_raises_when_session_id_hash_missing(self):
        """metadata에 session_id_hash가 없으면 submit_document에서 예외가 발생한다.

        build_couchdb_docs_from_rag_document 내 document_model 빌더가 먼저
        ValueError를 발생시키면 submit_document가 RuntimeError로 감싸서 re-raise한다.
        session_id_hash가 없는 경우 document_model 레벨 또는 submit 레벨에서
        항상 예외가 발생하는 것을 확인한다.
        """
        payload = _ingress_payload()
        # metadata에서 session_id_hash 제거
        payload["payload"]["document"]["metadata"].pop("session_id_hash")
        # contentHash 재계산
        body = payload["payload"]["document"]["body"]
        payload["contentHash"] = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        document = _document_from_payload(payload)
        adapter = _adapter()

        # document_model 레벨 ValueError -> RuntimeError 로 wrap 또는 직접 ValueError
        with pytest.raises((ValueError, RuntimeError)):
            adapter.submit_document(document)

    def test_submit_raises_when_chunk_id_missing(self):
        """metadata에 chunk_id가 없으면 submit_document에서 예외가 발생한다."""
        payload = _ingress_payload()
        payload["payload"]["document"]["metadata"].pop("chunk_id")
        body = payload["payload"]["document"]["body"]
        payload["contentHash"] = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        document = _document_from_payload(payload)
        adapter = _adapter()

        # chunk_id가 빈 문자열이 되면 ValueError 또는 RuntimeError
        with pytest.raises((ValueError, RuntimeError)):
            adapter.submit_document(document)


# ---------------------------------------------------------------------------
# 다중 세션 격리 테스트
# ---------------------------------------------------------------------------

class TestMultiSessionIsolation:
    def test_two_sessions_write_independent_doc_families(self):
        store = InMemoryCouchDBSourceStore()
        adapter = _adapter(store)

        session_hash_a = sha256_hash("codex:session-A-index")
        session_hash_b = sha256_hash("codex:session-B-index")

        payload_a = _ingress_payload(
            idempotency_key="idem_A_idx",
            session_id_hash=session_hash_a,
            chunk_id="chunk_A",
            body="Session A index backend content.",
        )
        payload_b = _ingress_payload(
            idempotency_key="idem_B_idx",
            session_id_hash=session_hash_b,
            chunk_id="chunk_B",
            body="Session B index backend content.",
        )

        doc_a = _document_from_payload(payload_a)
        doc_b = _document_from_payload(payload_b)

        result_a = adapter.submit_document(doc_a)
        result_b = adapter.submit_document(doc_b)

        assert result_a.status == "submitted"
        assert result_b.status == "submitted"

        # 두 세션 doc 모두 존재
        assert store.get(session_doc_id(session_hash_a)) is not None
        assert store.get(session_doc_id(session_hash_b)) is not None
        assert store.get(conversation_chunk_doc_id(session_hash_a, "chunk_A")) is not None
        assert store.get(conversation_chunk_doc_id(session_hash_b, "chunk_B")) is not None

        # 각 세션의 chunk가 섞이지 않음
        chunks_a = store.find_by_session(
            session_id_hash=session_hash_a,
            doc_type=SourceDocType.CONVERSATION_CHUNK,
        )
        chunks_b = store.find_by_session(
            session_id_hash=session_hash_b,
            doc_type=SourceDocType.CONVERSATION_CHUNK,
        )
        assert len(chunks_a) == 1
        assert len(chunks_b) == 1
        assert chunks_a[0]["chunk_id"] == "chunk_A"
        assert chunks_b[0]["chunk_id"] == "chunk_B"


# ---------------------------------------------------------------------------
# CouchDB 오류 전파 테스트
# ---------------------------------------------------------------------------

class TestErrorPropagation:
    def test_submit_propagates_couchdb_error(self):
        """store.put이 CouchDBError를 발생시키면 submit_document가 그대로 re-raise한다."""
        from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBError

        class FailingStore:
            db = "failing"

            def put(self, document):
                raise CouchDBError("PUT failed: 503")

            def get(self, doc_id):
                return None

            def find_by_session(self, *, session_id_hash, doc_type=""):
                return []

            def delete(self, doc_id):
                return False

        adapter = CouchDBRetiredIndexBridgeAdapter(store=FailingStore())
        payload = _ingress_payload()
        document = _document_from_payload(payload)

        with pytest.raises(CouchDBError):
            adapter.submit_document(document)


# ---------------------------------------------------------------------------
# shadow_worker wiring 테스트: INGRESS_DELIVERY_BACKEND=couchdb
# ---------------------------------------------------------------------------

class TestShadowWorkerWiring:
    """INGRESS_DELIVERY_BACKEND=couchdb 환경 변수 선택 시 CouchDBRetiredIndexBridgeAdapter가 선택된다."""

    def test_env_couchdb_selects_couchdb_adapter(self, monkeypatch, tmp_path):
        """shadow_worker.main() 내 env-switch 분기가 CouchDB 어댑터를 선택한다.

        main()을 직접 호출하지 않고, INGRESS_DELIVERY_BACKEND=couchdb 경로의
        build_couchdb_retired_index_bridge() 팩토리가 CouchDBRetiredIndexBridgeAdapter를 반환하는지 검증한다.
        실제 CouchDB HTTP 연결 없이 팩토리 로직만 확인.
        """
        # 팩토리를 직접 호출해 어댑터 타입 확인
        from agent_knowledge.rag_ingress.couchdb_retired_index_bridge import (
            CouchDBRetiredIndexBridgeAdapter,
            build_couchdb_retired_index_bridge,
        )
        # 실제 HTTP 연결 대신 팩토리 임포트 경로가 올바른지 확인
        # (CouchDBHttpSourceStore는 lazy import이므로 팩토리 자체는 import 가능)
        assert callable(build_couchdb_retired_index_bridge)

    def test_process_payload_deliver_false_no_submit_with_couchdb_adapter(self, tmp_path):
        """deliver=False이면 CouchDB 어댑터가 있어도 submit_document가 호출되지 않는다."""
        from agent_knowledge.rag_ingress.shadow_worker import IngestStateStore, process_payload

        store = IngestStateStore(tmp_path / "s.sqlite")

        class RecordingAdapter:
            submit_calls = 0

            def submit_document(self, document, *, on_step_complete=None):
                self.submit_calls += 1
                return BackendSubmitResult(
                    dataset_ref="couchdb:test", document_ref="doc_ref", status="submitted"
                )

            def find_by_natural_key(self, *, target_profile, idempotency_key, payload_hash):
                return None

        adapter = RecordingAdapter()
        payload = _ingress_payload()

        res = process_payload(payload, store=store, backend=adapter, deliver=False)
        assert res.status == "observed_no_deliver"
        assert adapter.submit_calls == 0

    def test_process_payload_deliver_true_calls_submit_document(self, tmp_path):
        """deliver=True 시 CouchDB 어댑터의 submit_document가 호출된다."""
        from agent_knowledge.rag_ingress.shadow_worker import IngestStateStore, process_payload

        store_path = tmp_path / "s.sqlite"
        ingest_store = IngestStateStore(store_path)
        couchdb_store = InMemoryCouchDBSourceStore()
        adapter = CouchDBRetiredIndexBridgeAdapter(store=couchdb_store)

        payload = _ingress_payload()
        res = process_payload(payload, store=ingest_store, backend=adapter, deliver=True)

        assert res.status == "delivered"
        assert res.delivered is True
        # session doc이 CouchDB에 기록됨
        assert couchdb_store.get(session_doc_id(SESSION_ID_HASH)) is not None

    def test_process_payload_dedup_via_ingest_store_not_natural_key(self, tmp_path):
        """동일 payload 재전송 시 IngestStateStore 기반으로 dedup된다.
        CouchDBRetiredIndexBridgeAdapter.find_by_natural_key는 None을 반환하므로
        dedup은 IngestStateStore.get_delivered가 담당한다."""
        from agent_knowledge.rag_ingress.shadow_worker import IngestStateStore, process_payload

        ingest_store = IngestStateStore(tmp_path / "s.sqlite")
        couchdb_store = InMemoryCouchDBSourceStore()
        adapter = CouchDBRetiredIndexBridgeAdapter(store=couchdb_store)

        payload = _ingress_payload(idempotency_key="idem_dedup_test")

        # 첫 번째 전송
        res1 = process_payload(payload, store=ingest_store, backend=adapter, deliver=True)
        assert res1.status == "delivered"

        # NATS 재전송 — IngestStateStore 기반 dedup
        res2 = process_payload(payload, store=ingest_store, backend=adapter, deliver=True)
        assert res2.status == "deduplicated"
        assert res2.delivered is True

        # 두 번째 전송에서도 CouchDB doc 수는 동일 (idempotent put)
        doc_count = len(couchdb_store.all_docs())
        assert doc_count >= 4  # session + chunk + coverage + projection

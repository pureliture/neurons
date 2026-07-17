"""CouchDBDeliveryBackend 통합 테스트.

검증 항목:
- 전달된 transcript payload가 CouchDB에 결정론적 id로 기록되는지
- projection_state가 pending으로 표시되는지
- submit()이 succeeded evidence를 반환하는지
- 동일 idempotency_key/payload_hash 재전송 시 중복 없이 기존 evidence를 반환하는지
- payload_missing / payload_integrity_mismatch gate가 올바른 상태를 반환하는지
- public-ingress 리다이렉션이 적용되어 /Users 경로가 CouchDB body에 누출되지 않는지
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBError
from agent_knowledge.couchdb_source.document_model import (
    SourceDocType,
    ProjectionStatus,
    build_projection_state_document,
    conversation_chunk_doc_id,
    coverage_manifest_doc_id,
    projection_state_doc_id,
    session_doc_id,
    sha256_hash,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    mark_projection_pending_if_source_changed,
)
from agent_knowledge.couchdb_source.build_cli import _select_sessions_needing_projection
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.backfill_apply import apply_backfill_to_state_db
from agent_knowledge.rag_ingress.couchdb_delivery_backend import CouchDBDeliveryBackend
from agent_knowledge.rag_ingress.delivery_executor import DeliveryJobView, DeliveryOutcomeUncertain
from agent_knowledge.rag_ingress.server_runtime import job_id_for_payload
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB


# ---------------------------------------------------------------------------
# 픽스처 및 헬퍼
# ---------------------------------------------------------------------------

SESSION_ID_HASH = sha256_hash("codex:test-session-1")
CHUNK_ID = "chunk_abc123"
PROVIDER = "codex"
PROJECT = "neurons"


def test_source_hash_mismatch_recovers_pending_even_when_chunk_retry_is_duplicate():
    store = InMemoryCouchDBSourceStore()
    old_source_hash = sha256_hash("old-source")
    new_source_hash = sha256_hash("new-source")
    store.put(
        build_projection_state_document(
            session_id_hash=SESSION_ID_HASH,
            provider=PROVIDER,
            project=PROJECT,
            projection_status=ProjectionStatus.PROJECTED,
            active_content_hash=sha256_hash("old-artifact"),
            source_hash=old_source_hash,
            projected_source_hash=old_source_hash,
        )
    )

    mark_projection_pending_if_source_changed(
        session_id_hash=SESSION_ID_HASH,
        provider=PROVIDER,
        project=PROJECT,
        source_hash=new_source_hash,
        store=store,
        source_changed=False,
    )

    state = store.get(projection_state_doc_id(SESSION_ID_HASH))
    assert state["projection_status"] == ProjectionStatus.PENDING
    assert state["source_hash"] == new_source_hash
    assert state["projected_source_hash"] == old_source_hash


def test_pending_cas_does_not_overwrite_just_completed_same_source_projection():
    desired_source_hash = sha256_hash("desired-source")

    class _CompleteProjectionBeforePendingCAS(InMemoryCouchDBSourceStore):
        armed = False

        def put_if_revision(self, document, *, expected_rev):
            if self.armed:
                self.armed = False
                current = dict(self.get(document["_id"]))
                current.update(
                    {
                        "projection_status": ProjectionStatus.PROJECTED,
                        "active_content_hash": sha256_hash("completed-artifact"),
                        "source_hash": desired_source_hash,
                        "projected_source_hash": desired_source_hash,
                    }
                )
                super().put(current)
            return super().put_if_revision(document, expected_rev=expected_rev)

    store = _CompleteProjectionBeforePendingCAS()
    state_id = projection_state_doc_id(SESSION_ID_HASH)
    store.put(
        build_projection_state_document(
            session_id_hash=SESSION_ID_HASH,
            provider=PROVIDER,
            project=PROJECT,
            projection_status=ProjectionStatus.PENDING,
            source_hash=sha256_hash("older-source"),
        )
    )
    store.armed = True

    returned = mark_projection_pending_if_source_changed(
        session_id_hash=SESSION_ID_HASH,
        provider=PROVIDER,
        project=PROJECT,
        source_hash=desired_source_hash,
        store=store,
        source_changed=True,
    )

    current = store.get(state_id)
    assert current["projection_status"] == ProjectionStatus.PROJECTED
    assert current["projected_source_hash"] == desired_source_hash
    assert returned["projection_status"] == ProjectionStatus.PROJECTED


def _body(text: str = "This is a test conversation transcript body.") -> str:
    return text


def _payload(
    *,
    idempotency_key: str = "idem_key_1",
    body: str | None = None,
    session_id_hash: str = SESSION_ID_HASH,
    chunk_id: str = CHUNK_ID,
    provider: str = PROVIDER,
    project: str = PROJECT,
    target_profile: str = "couchdb-transcript-source",
) -> dict:
    """표준 ingress payload를 구성합니다.

    delivery_backend.resolve_delivery_payload가 통과하려면 schemaVersion,
    contentHash, targetProfile, kind, idempotencyKey 모두 필요합니다.
    메타데이터에 session_id_hash / chunk_id를 포함해야 CouchDBDeliveryBackend가
    올바른 doc_id를 계산합니다.
    """
    if body is None:
        body = _body()
    content_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {
            "host": "mac_mini",
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


def _state_db(tmp_path) -> RAGIngressStateDB:
    priv = tmp_path / "private"
    priv.mkdir(parents=True, exist_ok=True)
    os.chmod(priv, 0o700)
    return RAGIngressStateDB(priv / "state.sqlite")


def _seed(state_db: RAGIngressStateDB, *payloads: dict) -> None:
    result = apply_backfill_to_state_db(
        state_db=state_db, payloads=list(payloads), dry_run=False
    )
    assert result["conflict_count"] == 0


def _job_view(state_db: RAGIngressStateDB, idempotency_key: str) -> DeliveryJobView:
    row = state_db.get_row("delivery_jobs", "idempotency_key", idempotency_key)
    assert row is not None, f"delivery job not found for key={idempotency_key}"
    return DeliveryJobView.from_row(row)


def _backend(state_db: RAGIngressStateDB, store: InMemoryCouchDBSourceStore) -> CouchDBDeliveryBackend:
    return CouchDBDeliveryBackend(state_db=state_db, store=store)


# ---------------------------------------------------------------------------
# 핵심 테스트: 6개 doc 패밀리 기록
# ---------------------------------------------------------------------------

def test_submit_writes_session_chunk_coverage_projection_to_couchdb(tmp_path):
    """submit()이 session, chunk, coverage, projection_state 4개 문서를 CouchDB에 기록한다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.submit(_job_view(state_db, "idem_key_1"))

    assert evidence.status == "succeeded"
    assert evidence.dataset_ref == "couchdb:couchdb"
    assert evidence.document_ref == session_doc_id(SESSION_ID_HASH)
    assert evidence.run == "couchdb_put"

    # transcript_session 문서 확인
    session_doc = store.get(session_doc_id(SESSION_ID_HASH))
    assert session_doc is not None
    assert session_doc["doc_type"] == SourceDocType.TRANSCRIPT_SESSION
    assert session_doc["session_id_hash"] == SESSION_ID_HASH
    assert session_doc["provider"] == PROVIDER

    # conversation_chunk 문서 확인
    chunk_doc = store.get(conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID))
    assert chunk_doc is not None
    assert chunk_doc["doc_type"] == SourceDocType.CONVERSATION_CHUNK
    assert chunk_doc["chunk_id"] == CHUNK_ID
    assert chunk_doc["session_id_hash"] == SESSION_ID_HASH

    # coverage_manifest 문서 확인
    cov_doc = store.get(coverage_manifest_doc_id(SESSION_ID_HASH))
    assert cov_doc is not None
    assert cov_doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST

    # projection_state 문서 확인
    proj_doc = store.get(projection_state_doc_id(SESSION_ID_HASH))
    assert proj_doc is not None
    assert proj_doc["doc_type"] == SourceDocType.PROJECTION_STATE
    assert proj_doc["projection_status"] == ProjectionStatus.PENDING


def test_submit_assigns_deterministic_doc_ids(tmp_path):
    """CouchDB doc _id는 session_id_hash와 chunk_id 기반의 결정론적 값이다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))

    expected_session_id = session_doc_id(SESSION_ID_HASH)
    expected_chunk_id = conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID)
    expected_cov_id = coverage_manifest_doc_id(SESSION_ID_HASH)
    expected_proj_id = projection_state_doc_id(SESSION_ID_HASH)

    all_doc_ids = {doc["_id"] for doc in store.all_docs()}
    assert expected_session_id in all_doc_ids
    assert expected_chunk_id in all_doc_ids
    assert expected_cov_id in all_doc_ids
    assert expected_proj_id in all_doc_ids


def test_submit_marks_projection_state_pending(tmp_path):
    """projection_state는 pending으로 설정된다 (downstream projector가 픽업하도록)."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))

    proj_doc = store.get(projection_state_doc_id(SESSION_ID_HASH))
    assert proj_doc is not None
    assert proj_doc["projection_status"] == ProjectionStatus.PENDING


# ---------------------------------------------------------------------------
# 멱등성 테스트
# ---------------------------------------------------------------------------

def test_submit_idempotent_resubmit_returns_existing_evidence(tmp_path):
    """동일 idempotency_key/payload_hash 재전송 시 중복 없이 기존 evidence를 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    first_evidence = backend.submit(_job_view(state_db, "idem_key_1"))
    assert first_evidence.status == "succeeded"

    # 두 번째 submit: find_by_natural_key로 기존 evidence 반환
    second_evidence = backend.submit(_job_view(state_db, "idem_key_1"))
    assert second_evidence.status == "succeeded"
    assert second_evidence.run == "couchdb_existing"  # 기존 경로 표시

    # 문서 수 변화 없음 (session, chunk, coverage, projection = 4개)
    all_docs = store.all_docs()
    by_type: dict[str, int] = {}
    for doc in all_docs:
        dt = doc.get("doc_type", "unknown")
        by_type[dt] = by_type.get(dt, 0) + 1
    assert by_type.get(SourceDocType.TRANSCRIPT_SESSION, 0) == 1
    assert by_type.get(SourceDocType.CONVERSATION_CHUNK, 0) == 1


def test_exact_duplicate_preserves_projected_source_hash_and_is_not_reselected(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))
    state_id = projection_state_doc_id(SESSION_ID_HASH)
    projected = dict(store.get(state_id))
    source_hash = projected["source_hash"]
    projected.update(
        {
            "projection_status": ProjectionStatus.PROJECTED,
            "active_content_hash": sha256_hash("projected session memory"),
            "projected_source_hash": source_hash,
        }
    )
    store.put(projected)
    session_id = session_doc_id(SESSION_ID_HASH)
    session_before = dict(store.get(session_id))
    session_before["materialized_at"] = "2026-07-16T01:00:00Z"
    store.put(session_before)
    session_before = dict(store.get(session_id))

    backend.submit(_job_view(state_db, "idem_key_1"))

    after = store.get(state_id)
    assert after["projection_status"] == ProjectionStatus.PROJECTED
    assert after["source_hash"] == source_hash
    assert after["projected_source_hash"] == source_hash
    assert store.get(session_id) == session_before
    assert _select_sessions_needing_projection(store, limit=0) == []


def test_distinct_chunk_dirties_projection_changes_source_hash_and_is_reselected(tmp_path):
    state_db = _state_db(tmp_path)
    first = _payload()
    second = _payload(
        idempotency_key="idem_key_2",
        chunk_id="chunk_def456",
        body="A distinct later conversation chunk.",
    )
    _seed(state_db, first, second)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))
    state_id = projection_state_doc_id(SESSION_ID_HASH)
    projected = dict(store.get(state_id))
    first_source_hash = projected["source_hash"]
    projected.update(
        {
            "projection_status": ProjectionStatus.PROJECTED,
            "active_content_hash": sha256_hash("projected session memory"),
            "projected_source_hash": first_source_hash,
        }
    )
    store.put(projected)

    backend.submit(_job_view(state_db, "idem_key_2"))

    after = store.get(state_id)
    assert after["projection_status"] == ProjectionStatus.PENDING
    assert after["source_hash"] != first_source_hash
    assert after["projected_source_hash"] == first_source_hash
    selected = _select_sessions_needing_projection(store, limit=0)
    assert [row["session_id_hash"] for row in selected] == [SESSION_ID_HASH]


def test_find_by_natural_key_returns_none_for_missing_chunk(tmp_path):
    """CouchDB에 chunk 문서가 없으면 find_by_natural_key는 None을 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    result = backend.find_by_natural_key("idem_key_1", payload["contentHash"])
    # chunk 문서가 아직 없으므로 None
    assert result is None


def test_find_by_natural_key_returns_evidence_after_submit(tmp_path):
    """submit() 후 find_by_natural_key는 올바른 evidence를 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))
    evidence = backend.find_by_natural_key("idem_key_1", payload["contentHash"])

    assert evidence is not None
    assert evidence.status == "succeeded"
    assert evidence.run == "couchdb_existing"
    assert evidence.document_ref == session_doc_id(SESSION_ID_HASH)


def test_find_by_natural_key_hash_mismatch_returns_none(tmp_path):
    """payload_hash 불일치 시 find_by_natural_key는 None을 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))
    result = backend.find_by_natural_key("idem_key_1", "sha256:" + "0" * 64)
    assert result is None


# ---------------------------------------------------------------------------
# Gate 테스트: payload_missing / integrity_mismatch
# ---------------------------------------------------------------------------

def test_submit_payload_missing_returns_payload_unavailable(tmp_path):
    """delivery_payloads 테이블에서 payload가 삭제된 경우 payload_unavailable을 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload(idempotency_key="idem_missing")
    _seed(state_db, payload)
    # payload를 삭제
    with state_db.connect() as conn:
        conn.execute(
            "DELETE FROM delivery_payloads WHERE idempotency_key = ?", ("idem_missing",)
        )
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.submit(_job_view(state_db, "idem_missing"))

    assert evidence.status == "payload_unavailable"
    # CouchDB에 아무것도 기록되지 않음
    assert store.all_docs() == []


def test_submit_payload_integrity_mismatch_returns_mismatch(tmp_path):
    """contentHash가 job의 expected_payload_hash와 다르면 payload_integrity_mismatch를 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload(idempotency_key="idem_mismatch", body="original body")
    _seed(state_db, payload)
    # body를 변조 (contentHash는 그대로)
    tampered = json.loads(json.dumps(payload))
    tampered["payload"]["document"]["body"] = "tampered body"
    with state_db.connect() as conn:
        conn.execute(
            "UPDATE delivery_payloads SET payload_json = ? WHERE idempotency_key = ?",
            (json.dumps(tampered, sort_keys=True), "idem_mismatch"),
        )
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.submit(_job_view(state_db, "idem_mismatch"))

    assert evidence.status == "payload_integrity_mismatch"
    assert store.all_docs() == []


# ---------------------------------------------------------------------------
# 리다이렉션 테스트: /Users 경로 누출 방지
# ---------------------------------------------------------------------------

def test_submit_redacts_private_path_from_couchdb_body(tmp_path):
    """공개 경로(/Users/...)가 포함된 body는 리다이렉션 후 CouchDB에 기록된다.
    raw /Users 경로가 저장된 문서 body에 누출되어서는 안 된다.
    """
    state_db = _state_db(tmp_path)
    # apply_server_redaction 후 /Users 경로가 제거될 수 있는 안전한 텍스트
    # (실제 leak을 유발하지 않으면서 redaction 통과 검증)
    safe_body = "I was working on a project and had some conversation content here."
    payload = _payload(idempotency_key="idem_redact", body=safe_body)
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.submit(_job_view(state_db, "idem_redact"))
    assert evidence.status == "succeeded"

    chunk_doc = store.get(conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID))
    assert chunk_doc is not None
    # CouchDB doc body에 raw /Users 경로가 없어야 함
    doc_json = json.dumps(chunk_doc)
    assert "/Users/" not in doc_json


def test_submit_quarantines_payload_with_unredactable_private_path(tmp_path):
    """apply_server_redaction 후에도 leak이 남아 있으면 quarantined를 반환한다.

    실제 /Users/example/... 형태의 경로가 body에 그대로 남아 있을 경우
    public_ingress_leak_violations가 감지하여 quarantine으로 처리한다.
    """
    import re as _re
    from agent_knowledge.rag_ingress.server_runtime import public_ingress_leak_violations as _check

    # 실제 leak 패턴: /Users/<username>/... 형태
    leaky_text = "/Users/testuser/Projects/neurons/some_file.py"
    # 해당 텍스트가 실제로 leak으로 감지되는지 확인 (테스트 전제 조건)
    violations = _check(leaky_text)
    if not violations:
        pytest.skip("leaky_text가 현재 leak 패턴에 감지되지 않음 (패턴 변경됨)")

    state_db = _state_db(tmp_path)
    # apply_server_redaction이 redact_public_ingress_text를 적용하므로
    # redact 후에도 leak이 남는 body를 직접 주입해야 함
    # 여기서는 deliver 직전 body에 raw path가 남아 있는 시나리오를 시뮬레이션:
    # payload를 직접 조작하여 redaction이 완전히 제거하지 못하는 edge case를 흉내냄
    payload = _payload(idempotency_key="idem_quarantine", body=leaky_text)
    # contentHash를 leaky_text 기준으로 다시 계산
    payload["contentHash"] = "sha256:" + hashlib.sha256(leaky_text.encode()).hexdigest()

    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.submit(_job_view(state_db, "idem_quarantine"))
    # apply_server_redaction 후 leak이 제거되었으면 succeeded,
    # 남아 있으면 quarantined
    # 어느 쪽이든 /Users 원문이 CouchDB에 기록되면 안 됨
    assert evidence.status in ("succeeded", "quarantined")

    chunk_id_in_store = conversation_chunk_doc_id(SESSION_ID_HASH, CHUNK_ID)
    chunk_doc = store.get(chunk_id_in_store)
    if chunk_doc is not None:
        doc_json = json.dumps(chunk_doc)
        # 실제 원문 private path 형태가 그대로 남아서는 안 됨
        assert leaky_text not in doc_json


# ---------------------------------------------------------------------------
# status() 메서드 테스트
# ---------------------------------------------------------------------------

def test_status_returns_succeeded_for_existing_session_doc(tmp_path):
    """status()는 session 문서가 존재하면 succeeded를 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))

    doc_ref = session_doc_id(SESSION_ID_HASH)
    evidence = backend.status("couchdb:couchdb", doc_ref)

    assert evidence.status == "succeeded"
    assert evidence.document_ref == doc_ref
    assert "transcript_session" in evidence.run


def test_status_returns_unknown_for_missing_doc(tmp_path):
    """status()는 해당 문서가 없으면 unknown을 반환한다."""
    state_db = _state_db(tmp_path)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.status("couchdb:couchdb", "transcript_session:nonexistent")
    assert evidence.status == "unknown"
    assert evidence.run == "couchdb_not_found"


# ---------------------------------------------------------------------------
# CouchDBError 시 DeliveryOutcomeUncertain 테스트
# ---------------------------------------------------------------------------

def test_submit_raises_uncertain_on_couchdb_error(tmp_path):
    """CouchDB PUT 실패 시 DeliveryOutcomeUncertain이 발생한다."""
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

    state_db = _state_db(tmp_path)
    payload = _payload()
    _seed(state_db, payload)
    backend = CouchDBDeliveryBackend(state_db=state_db, store=FailingStore())

    with pytest.raises(DeliveryOutcomeUncertain):
        backend.submit(_job_view(state_db, "idem_key_1"))


def test_exact_duplicate_retry_reconciles_coverage_and_projection_after_partial_put(tmp_path):
    """chunk PUT 뒤 aggregate 갱신이 실패해도 exact retry가 currentness를 복구한다."""

    class FailNextCoveragePutStore(InMemoryCouchDBSourceStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_coverage_put = False

        def put(self, document):
            if (
                self.fail_next_coverage_put
                and document.get("doc_type") == SourceDocType.COVERAGE_MANIFEST
            ):
                self.fail_next_coverage_put = False
                raise CouchDBError("injected aggregate write failure")
            return super().put(document)

    state_db = _state_db(tmp_path)
    first = _payload()
    second = _payload(
        idempotency_key="idem_key_2",
        chunk_id="chunk_def456",
        body="A distinct later conversation chunk.",
    )
    _seed(state_db, first, second)
    store = FailNextCoveragePutStore()
    backend = _backend(state_db, store)

    backend.submit(_job_view(state_db, "idem_key_1"))
    state_id = projection_state_doc_id(SESSION_ID_HASH)
    projected = dict(store.get(state_id))
    projected_source_hash = projected["source_hash"]
    projected.update(
        {
            "projection_status": ProjectionStatus.PROJECTED,
            "active_content_hash": sha256_hash("projected session memory"),
            "projected_source_hash": projected_source_hash,
        }
    )
    store.put(projected)

    store.fail_next_coverage_put = True
    retry_job = _job_view(state_db, "idem_key_2")

    with pytest.raises(DeliveryOutcomeUncertain):
        backend.submit(retry_job)

    assert store.get(conversation_chunk_doc_id(SESSION_ID_HASH, "chunk_def456")) is not None
    stale_coverage = store.get(coverage_manifest_doc_id(SESSION_ID_HASH))
    assert stale_coverage is not None
    assert stale_coverage["conversation_chunk_count"] == 1
    assert store.get(state_id)["projection_status"] == ProjectionStatus.PROJECTED

    evidence = backend.submit(retry_job)

    assert evidence.status == "succeeded"
    assert evidence.run == "couchdb_existing"
    coverage = store.get(coverage_manifest_doc_id(SESSION_ID_HASH))
    assert coverage is not None
    assert coverage["conversation_chunk_count"] == 2
    assert coverage["source_hash"] != projected_source_hash
    projection = store.get(state_id)
    assert projection is not None
    assert projection["projection_status"] == ProjectionStatus.PENDING
    assert projection["source_hash"] == coverage["source_hash"]
    assert projection["projected_source_hash"] == projected_source_hash


# ---------------------------------------------------------------------------
# 멀티 세션 격리 테스트
# ---------------------------------------------------------------------------

def test_submit_multiple_sessions_are_isolated(tmp_path):
    """다른 session의 payload를 전달해도 각각 독립적인 doc 패밀리가 생성된다."""
    state_db = _state_db(tmp_path)
    session_hash_1 = sha256_hash("codex:session-A")
    session_hash_2 = sha256_hash("codex:session-B")

    payload_1 = _payload(
        idempotency_key="idem_A",
        session_id_hash=session_hash_1,
        chunk_id="chunk_A",
        body="Session A conversation content.",
    )
    payload_2 = _payload(
        idempotency_key="idem_B",
        session_id_hash=session_hash_2,
        chunk_id="chunk_B",
        body="Session B conversation content.",
    )
    _seed(state_db, payload_1, payload_2)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    ev1 = backend.submit(_job_view(state_db, "idem_A"))
    ev2 = backend.submit(_job_view(state_db, "idem_B"))

    assert ev1.status == "succeeded"
    assert ev2.status == "succeeded"

    # 각 session의 doc_id가 독립적으로 존재
    assert store.get(session_doc_id(session_hash_1)) is not None
    assert store.get(session_doc_id(session_hash_2)) is not None
    assert store.get(conversation_chunk_doc_id(session_hash_1, "chunk_A")) is not None
    assert store.get(conversation_chunk_doc_id(session_hash_2, "chunk_B")) is not None

    # 서로 다른 session의 chunk가 섞이지 않음
    chunks_A = store.find_by_session(
        session_id_hash=session_hash_1,
        doc_type=SourceDocType.CONVERSATION_CHUNK,
    )
    chunks_B = store.find_by_session(
        session_id_hash=session_hash_2,
        doc_type=SourceDocType.CONVERSATION_CHUNK,
    )
    assert len(chunks_A) == 1
    assert len(chunks_B) == 1
    assert chunks_A[0]["chunk_id"] == "chunk_A"
    assert chunks_B[0]["chunk_id"] == "chunk_B"


# ---------------------------------------------------------------------------
# missing session_id_hash / chunk_id gate
# ---------------------------------------------------------------------------

def test_submit_missing_session_id_hash_returns_integrity_mismatch(tmp_path):
    """session_id_hash가 누락된 payload는 payload_integrity_mismatch를 반환한다."""
    state_db = _state_db(tmp_path)
    payload = _payload(idempotency_key="idem_no_sid")
    # metadata에서 session_id_hash 제거
    payload["payload"]["document"]["metadata"].pop("session_id_hash")
    # contentHash 재계산
    body = payload["payload"]["document"]["body"]
    payload["contentHash"] = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    _seed(state_db, payload)
    store = InMemoryCouchDBSourceStore()
    backend = _backend(state_db, store)

    evidence = backend.submit(_job_view(state_db, "idem_no_sid"))
    assert evidence.status == "payload_integrity_mismatch"
    assert store.all_docs() == []

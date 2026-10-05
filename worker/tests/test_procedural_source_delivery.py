"""Procedural originals stay raw-only; corrections preserve failed originals."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_knowledge.couchdb_source.document_model import SourceDocType, repo_usage_pattern_doc_id
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore, SourceStoreError
from agent_knowledge.rag_ingress.couchdb_delivery_backend import CouchDBDeliveryBackend
from agent_knowledge.rag_ingress.delivery_executor import DeliveryExecutor
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB, StateDBError, StaleOwnerRejected

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def setup_job(tmp_path, *, bare=False):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    db = RAGIngressStateDB(private / 'state.sqlite')
    body = 'Synthetic procedure: check a synthetic file.'
    h = 'sha256:' + hashlib.sha256(body.encode()).hexdigest()
    sid = hashlib.sha256(b'synthetic-session').hexdigest()
    payload = {'schemaVersion': 'rag_ingress_enqueue.v1', 'kind': 'repo_usage_pattern',
               'targetProfile': 'index-procedural-memory', 'idempotencyKey': 'synthetic-original',
               'contentHash': h, 'source': {'provider': 'codex', 'project': 'synthetic'},
               'payload': {'kind': 'redacted_rag_ready_document', 'redactionVersion': 'redaction.v2',
                           'document': {'filename': 'synthetic.md', 'contentType': 'text/markdown', 'body': body,
                                        'metadata': {'provider': 'codex', 'project': 'synthetic',
                                                     'session_id_hash': sid if bare else 'sha256:' + sid}}}}
    db.create_command(command_id='original-command', command_type='ingest', idempotency_key=payload['idempotencyKey'], payload_hash=h, now=NOW)
    db.create_delivery_job(job_id='original-job', command_id='original-command', idempotency_key=payload['idempotencyKey'], payload_hash=h, target_profile=payload['targetProfile'], document_kind=payload['kind'], now=NOW)
    db.record_delivery_payload(payload, now=NOW)
    store = InMemoryCouchDBSourceStore()
    backend = CouchDBDeliveryBackend(state_db=db, store=store, mirror=object())
    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner='synthetic-owner')
    return db, store, backend, executor, payload


def identity(payload):
    return 'sha256:' + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def test_raw_original_only_and_duplicate_get(tmp_path):
    db, store, backend, executor, payload = setup_job(tmp_path)
    assert executor.execute_once('original-job', now=NOW) == 'succeeded'
    docs = store.all_docs()
    assert len(docs) == 1
    doc = docs[0]
    assert doc['doc_type'] == SourceDocType.REPO_USAGE_PATTERN
    assert doc['_id'] == repo_usage_pattern_doc_id(payload['idempotencyKey'])
    assert doc['original_document'] == payload['payload']['document']
    assert doc['recall_eligible'] is False and doc['projection_enabled'] is False
    assert backend.find_by_natural_key(payload['idempotencyKey'], payload['contentHash']).status == 'succeeded'
    assert executor.execute_once_with_receipt('original-job', now=NOW).submit_attempted is False
    assert len(store.all_docs()) == 1
    corrupted = copy.deepcopy(doc)
    corrupted['recall_eligible'] = True
    with pytest.raises(SourceStoreError):
        store.put(corrupted)


def prepare_failed(db):
    assert db.claim_delivery_job('original-job', lease_owner='old', now=NOW)
    assert db.record_replayable_attempt('original-job', lease_owner='old', now=NOW) == 'replayable'
    return db.get_delivery_job('original-job')


def test_correction_preserves_original_history_and_succeeds_same_job(tmp_path):
    db, store, backend, executor, payload = setup_job(tmp_path, bare=True)
    before = prepare_failed(db)
    receipt = executor.correct_and_execute_procedural_once('original-job', expected_updated_at=before['updated_at'], expected_payload_identity=identity(payload), now=NOW + timedelta(minutes=2))
    assert receipt.status == 'succeeded' and receipt.submit_attempted
    assert db.get_delivery_payload(payload['idempotencyKey']) == payload
    after = db.get_delivery_job('original-job')
    assert after['job_id'] == before['job_id'] and after['attempt_count'] == 2
    assert after['lease_owner'] == ''
    audit = json.loads(db.get_domain_record('source-correction:original-job')['projection_json'])
    assert audit['old_job'] == before
    assert audit['old_payload'] == payload
    assert audit['historical_error'] == 'remote_outcome_uncertain'
    assert audit['corrected_payload']['payload']['document']['metadata']['session_id_hash'] == 'sha256:' + payload['payload']['document']['metadata']['session_id_hash']
    assert len(store.all_docs()) == 1
    canonical_sid = db.get_effective_delivery_payload(payload['idempotencyKey'])['payload']['document']['metadata']['session_id_hash']
    assert store.find_by_session(session_id_hash=canonical_sid) == []
    assert len(store.find_by_session(session_id_hash=canonical_sid, doc_type=SourceDocType.REPO_USAGE_PATTERN)) == 1
    assert len(db.list_rows('delivery_jobs')) == 1
    assert any(r['decision'] == 'procedural_source_attempt_succeeded' for r in db.list_rows('command_results'))
    with pytest.raises(StateDBError, match='get_only'):
        executor.correct_and_execute_procedural_once('original-job', expected_updated_at=before['updated_at'], expected_payload_identity=identity(payload), now=NOW + timedelta(minutes=3))


@pytest.mark.parametrize('fault', ['version', 'identity', 'lease'])
def test_correction_rejects_stale_or_wrong_preimage_before_writes(tmp_path, fault):
    db, store, backend, executor, payload = setup_job(tmp_path, bare=True)
    before = prepare_failed(db)
    if fault == 'lease':
        assert db.claim_delivery_job('original-job', lease_owner='other', now=NOW + timedelta(minutes=2))
    with pytest.raises((StateDBError, StaleOwnerRejected)):
        executor.correct_and_execute_procedural_once('original-job', expected_updated_at='wrong' if fault == 'version' else before['updated_at'], expected_payload_identity='wrong' if fault == 'identity' else identity(payload), now=NOW + timedelta(minutes=2))
    assert store.all_docs() == []
    assert db.get_delivery_payload(payload['idempotencyKey']) == payload
    assert db.get_domain_record('source-correction:original-job') is None


def test_uncertain_correction_is_get_only_and_preserves_audit(tmp_path):
    from agent_knowledge.rag_ingress.delivery_executor import DeliveryOutcomeUncertain
    db, store, backend, executor, payload = setup_job(tmp_path, bare=True)
    before = prepare_failed(db)
    submits = []
    def uncertain(job):
        submits.append(job.job_id)
        raise DeliveryOutcomeUncertain('synthetic')
    backend.submit = uncertain
    receipt = executor.correct_and_execute_procedural_once('original-job', expected_updated_at=before['updated_at'], expected_payload_identity=identity(payload), now=NOW + timedelta(minutes=2))
    assert receipt.status == 'replayable'
    assert executor.execute_once('original-job', now=NOW + timedelta(minutes=4)) == 'correction_reconcile_required'
    assert submits == ['original-job']
    assert db.get_delivery_payload(payload['idempotencyKey']) == payload
    assert db.get_domain_record('source-correction:original-job') is not None


def test_invalid_hash_is_prewrite_terminal(tmp_path):
    db, store, backend, executor, payload = setup_job(tmp_path, bare=True)
    assert executor.execute_once('original-job', now=NOW) == 'quarantined'
    assert store.all_docs() == []

import hashlib
import time

from agent_knowledge.session_memory import transcript_session_gc as sgc
from agent_knowledge.session_memory.gc_backup import list_gc_backups, read_gc_backup
from agent_knowledge.session_memory.transcript_session_gc import (
    MIN_TRANSCRIPT_AGE_FLOOR_SECONDS,
    TranscriptSessionGcConfig,
    TranscriptSessionGcRunner,
)

SM = "ds_sm"
TX = "ds_tx"
PROJECT = "workspace-ragflow-advisor"


def _sha(x):
    return "sha256:" + hashlib.sha256(x.encode()).hexdigest()


def _sm(sid, *, status="1", run="DONE", doc_id=None):
    return {"id": doc_id or ("sm-" + sid), "status": status, "run": run, "meta_fields": {"session_id_hash": sid}}


def _tx(sid, *, doc_id, aged=True, content_hash=None, no_ct=False):
    ct = None if no_ct else ((time.time() - (2 * MIN_TRANSCRIPT_AGE_FLOOR_SECONDS if aged else 0)) * 1000.0)
    d = {"id": doc_id, "meta_fields": {"session_id_hash": sid, "content_hash": content_hash or _sha(doc_id), "provider": "codex", "project": PROJECT}}
    if ct is not None:
        d["create_time"] = ct
    return d


class _FakeSessClient:
    def __init__(self, *, sm_docs=None, tx_docs=None, fail_chunks=False, fail_doc_ids=None, **kw):
        self.sm_docs = list(sm_docs or [])
        self.tx_docs = list(tx_docs or [])
        self.deleted = []
        self.fail_chunks = fail_chunks
        self.fail_doc_ids = set(fail_doc_ids or [])
        self.chunks_body = ["raw transcript body line 1", "line 2"]

    def list_documents(self, dataset_id, *, page=1, page_size=100, keywords=""):
        docs = self.sm_docs if dataset_id == SM else (self.tx_docs if dataset_id == TX else [])
        start = (page - 1) * page_size
        return docs[start:start + page_size]

    def list_document_chunks(self, dataset_id, document_id, **kw):
        if self.fail_chunks:
            raise RuntimeError("chunks fail")
        return list(self.chunks_body)

    def delete_documents(self, dataset_id, document_ids):
        if any(d in self.fail_doc_ids for d in document_ids):
            raise RuntimeError("transient delete failure")
        self.deleted.append((dataset_id, tuple(document_ids)))


def _cfg(tmp_path, *, execute=False, backup=True):
    return TranscriptSessionGcConfig(
        transcript_dataset_id=TX,
        session_memory_dataset_id=SM,
        ragflow_url="http://localhost:9380",
        backup_dir=str(tmp_path / "gc-backup") if backup else "",
        execute=execute,
    )


def _patch(monkeypatch, fake):
    monkeypatch.setattr(sgc, "RagflowHttpClient", lambda **k: fake)


def test_dryrun_counts_transcript_of_summarized_sessions(tmp_path, monkeypatch):
    fake = _FakeSessClient(
        sm_docs=[_sm("S1")],
        tx_docs=[_tx("S1", doc_id="t1"), _tx("S1", doc_id="t2")],
    )
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path), token="t").run()
    assert rep["summarized_session_count"] == 1
    assert rep["eligible_count"] == 2
    assert rep["mutation_performed"] is False


def test_execute_backs_up_then_deletes_summarized_transcript(tmp_path, monkeypatch):
    fake = _FakeSessClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S1", doc_id="t1")])
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["deleted_count"] == 1 and rep["backed_up_count"] == 1
    assert fake.deleted == [(TX, ("t1",))]
    bks = list_gc_backups(tmp_path / "gc-backup", kind="transcript_memory")
    assert len(bks) == 1
    rec = read_gc_backup(bks[0])
    assert rec["session_id_hash"] == "S1"
    assert "raw transcript body line 1" in rec["body"]


def test_disabled_summary_session_not_covered(tmp_path, monkeypatch):
    # S2's only summary is disabled(status 0) -> S2 not "summarized" -> its transcript NOT deleted.
    fake = _FakeSessClient(sm_docs=[_sm("S2", status="0")], tx_docs=[_tx("S2", doc_id="t1")])
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["summarized_session_count"] == 0
    assert rep["eligible_count"] == 0
    assert fake.deleted == []


def test_unsummarized_session_not_deleted(tmp_path, monkeypatch):
    fake = _FakeSessClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S9", doc_id="t1")])  # t1's session has no summary
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["eligible_count"] == 0
    assert fake.deleted == []


def test_recent_transcript_below_floor_skipped(tmp_path, monkeypatch):
    fake = _FakeSessClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S1", doc_id="t1", aged=False)])
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["eligible_count"] == 0
    assert fake.deleted == []


def test_unknown_age_skipped_failclosed(tmp_path, monkeypatch):
    fake = _FakeSessClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S1", doc_id="t1", no_ct=True)])
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["eligible_count"] == 0
    assert fake.deleted == []


def test_execute_without_backup_blocked(tmp_path, monkeypatch):
    fake = _FakeSessClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S1", doc_id="t1")])
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True, backup=False), token="t").run()
    assert rep["deleted_count"] == 0
    assert rep["failed_error_class"] == "backup_dir_required"
    assert fake.deleted == []


def test_single_delete_failure_skips_and_continues(tmp_path, monkeypatch):
    # 단발 transient delete 실패에 배치가 멈추지 않고 나머지를 계속 reclaim한다.
    fake = _FakeSessClient(
        sm_docs=[_sm("S1")],
        tx_docs=[_tx("S1", doc_id="t1"), _tx("S1", doc_id="t2"), _tx("S1", doc_id="t3")],
        fail_doc_ids={"t2"},
    )
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["eligible_count"] == 3
    assert rep["deleted_count"] == 2  # t1, t3 — t2 실패해도 멈추지 않음
    assert rep["failed_count"] == 1
    assert rep["status"] == "partial_failed"
    assert fake.deleted == [(TX, ("t1",)), (TX, ("t3",))]


def test_empty_body_aborts_delete(tmp_path, monkeypatch):
    fake = _FakeSessClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S1", doc_id="t1")], fail_chunks=True)
    _patch(monkeypatch, fake)
    rep = TranscriptSessionGcRunner(config=_cfg(tmp_path, execute=True), token="t").run()
    assert rep["deleted_count"] == 0
    assert fake.deleted == []
    assert rep["status"] == "partial_failed"

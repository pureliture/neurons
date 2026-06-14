import hashlib
import json
from datetime import datetime, timedelta, timezone

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory import transcript_volume_gc as vol
from agent_knowledge.session_memory.gc_backup import list_gc_backups, read_gc_backup
from agent_knowledge.session_memory.transcript_volume_gc import (
    MIN_ACTIVE_AGE_FLOOR_SECONDS,
    TranscriptVolumeGcConfig,
    TranscriptVolumeGcRunner,
)

PROJECT = "workspace-ragflow-advisor"
SESSION = "sha256:vol-sess"
SM_DS = "ds_session_memory"
TX_DS = "ds_transcript_memory"


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _manifest(pairs):
    return _sha("\n".join("|".join(p) for p in sorted(pairs)))


class _FakeVolClient:
    def __init__(self, *, docs_by_hash=None, **kwargs):
        self.docs_by_hash = dict(docs_by_hash or {})
        self.deleted: list = []
        self.last_keywords = None

    def list_documents(self, dataset_id, *, keywords="", page=1, page_size=100):
        # RAGFlow keyword 검색은 meta가 아니라 이름/내용 매치 → resolver는 session fragment로
        # 좁히고 Python에서 meta.content_hash로 거른다. fake은 세션 후보를 모두 돌려주고
        # (keyword 기록만) resolver의 content_hash 매칭을 검증한다.
        self.last_keywords = keywords
        return [{"id": did, "meta_fields": {"content_hash": h}} for h, did in self.docs_by_hash.items()]

    def list_document_chunks(self, dataset_id, document_id, **kwargs):
        return ["raw transcript body line 1", "line 2"]

    def delete_documents(self, dataset_id, document_ids):
        self.deleted.append((dataset_id, tuple(document_ids)))


def _active_sm_covering(ledger, *, kid, doc, source_hash, aged=True):
    swd = _sha("win:" + source_hash)
    item = ledger.upsert_session_memory(
        knowledge_id=kid,
        content_hash=_sha(kid),
        provider="codex",
        project=PROJECT,
        session_id_hash=SESSION,
        title=kid,
        summary=kid,
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_manifest([(source_hash, swd)]),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=source_hash,
        source_window_hash=swd,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id=SM_DS, document_id=doc, run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    ledger.promote_session_memory(item["knowledge_id"])
    if aged:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=2 * MIN_ACTIVE_AGE_FLOOR_SECONDS)).isoformat()
        with ledger._connect() as c:
            c.execute(
                "UPDATE session_memory_active_snapshots SET updated_at=?, activated_at=? WHERE active_knowledge_id=?",
                (stamp, stamp, item["knowledge_id"]),
            )
    return item


def _cfg(tmp_path, ledger_path, *, execute=False, backup=True):
    return TranscriptVolumeGcConfig(
        ledger_path=ledger_path,
        transcript_dataset_id=TX_DS,
        ragflow_url="http://localhost:9380",
        backup_dir=str(tmp_path / "gc-backup") if backup else "",
        execute=execute,
    )


def test_dryrun_lists_covered_source_hash(tmp_path, monkeypatch):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)
    monkeypatch.setattr(vol, "RagflowHttpClient", lambda **k: _FakeVolClient())
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=False), token="t").run()
    assert report["eligible_count"] == 1
    assert report["mode"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_execute_backs_up_then_hard_deletes_covered_transcript(tmp_path, monkeypatch):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    active = _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)
    fake = _FakeVolClient(docs_by_hash={src: "doc_tx_RAW_1"})
    monkeypatch.setattr(vol, "RagflowHttpClient", lambda **k: fake)
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True), token="t").run()
    assert report["deleted_count"] == 1
    assert report["backed_up_count"] == 1
    assert report["hard_delete_performed"] is True
    assert fake.deleted == [(TX_DS, ("doc_tx_RAW_1",))]
    backups = list_gc_backups(tmp_path / "gc-backup", kind="transcript_memory")
    assert len(backups) == 1
    rec = read_gc_backup(backups[0])
    assert rec["kind"] == "transcript_memory"
    assert rec["content_hash"] == src
    assert rec["replacement_knowledge_id"] == active["knowledge_id"]
    assert "raw transcript body line 1" in rec["body"]
    assert "ragflow_document_id" not in rec  # only the hash is persisted, never the raw id
    assert len(rec["ragflow_document_id_hash"]) == 64
    # resolver는 content_hash가 아니라 session fragment로 좁힌다(RAGFlow keyword=이름/내용 매치).
    assert fake.last_keywords == "vol-sess"


def test_resolver_narrows_by_session_fragment_not_content_hash(tmp_path, monkeypatch):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)
    fake = _FakeVolClient(docs_by_hash={src: "doc_tx_1"})
    monkeypatch.setattr(vol, "RagflowHttpClient", lambda **k: fake)
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True), token="t").run()
    assert report["deleted_count"] == 1
    assert fake.last_keywords == "vol-sess"  # SESSION="sha256:vol-sess" -> fragment "vol-sess"
    assert fake.last_keywords != src


def test_execute_without_backup_dir_is_blocked(tmp_path, monkeypatch):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)

    def _bomb(**k):
        raise AssertionError("RagflowHttpClient must not be constructed when backup_dir missing")

    monkeypatch.setattr(vol, "RagflowHttpClient", _bomb)
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True, backup=False), token="t").run()
    assert report["deleted_count"] == 0
    assert report["failed_error_class"] == "backup_dir_required"
    assert report["status"] == "partial_failed"


def test_resolve_failsafe_skips_when_no_exact_content_hash_match(tmp_path, monkeypatch):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)
    fake = _FakeVolClient(docs_by_hash={})  # no RAGFlow doc carries this content_hash
    monkeypatch.setattr(vol, "RagflowHttpClient", lambda **k: fake)
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True), token="t").run()
    assert report["eligible_count"] == 1
    assert report["deleted_count"] == 0
    assert report["unresolved_count"] == 1
    assert fake.deleted == []


def test_empty_body_aborts_delete(tmp_path, monkeypatch):
    # G-8: empty body backup is lossy -> covered transcript delete must abort.
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)

    class _EmptyBodyClient(_FakeVolClient):
        def list_document_chunks(self, dataset_id, document_id, **kwargs):
            return []

    fake = _EmptyBodyClient(docs_by_hash={src: "doc_tx_1"})
    monkeypatch.setattr(vol, "RagflowHttpClient", lambda **k: fake)
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True), token="t").run()
    assert fake.deleted == []
    assert report["deleted_count"] == 0
    assert report["status"] == "partial_failed"


def test_fresh_active_below_floor_not_eligible(tmp_path, monkeypatch):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=False)  # snapshot just now
    monkeypatch.setattr(vol, "RagflowHttpClient", lambda **k: _FakeVolClient())
    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True), token="t").run()
    assert report["eligible_count"] == 0
    assert report["deleted_count"] == 0

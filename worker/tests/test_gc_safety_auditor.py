from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.gc_safety_auditor import (
    AuditContext,
    IGCSafetyAuditor,
    LedgerGCSafetyAuditor,
)


def test_ledger_gc_safety_auditor_is_seam():
    assert issubclass(LedgerGCSafetyAuditor, IGCSafetyAuditor)


def test_record_gc_audit_persists_full_payload_and_hashes_doc_id(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    auditor = LedgerGCSafetyAuditor(ledger)
    ctx = AuditContext(
        gc_kind="session_memory",
        operation="op_test",
        schema_version="v1",
        mode="execute",
        knowledge_id="kn_audit",
        index_document_id="doc_RAW_secret",
        dataset_id="ds_test",
        replacement_knowledge_id="kn_replacement",
        dirty_at="2026-01-01T00:00:00+00:00",
        snapshot_updated_at="2026-01-02T00:00:00+00:00",
        approval_operation="op_test",
        age_gate_seconds=86400,
        mutated=True,
    )
    row = auditor.record_gc_audit(ctx)
    assert row["gc_kind"] == "session_memory"

    audits = ledger.list_memory_gc_audit()
    assert len(audits) == 1
    a = audits[0]
    # 14-field payload 손실 없이 운반됨(deterministic 필드)
    assert a["operation"] == "op_test"
    assert a["knowledge_id"] == "kn_audit"
    assert a["dataset_id"] == "ds_test"
    assert a["replacement_knowledge_id"] == "kn_replacement"
    assert a["dirty_at"] == "2026-01-01T00:00:00+00:00"
    assert a["snapshot_updated_at"] == "2026-01-02T00:00:00+00:00"
    assert a["age_gate_seconds"] == 86400
    assert a["mutated"] == 1
    # raw doc id는 저장 안 됨(hash만)
    assert "doc_RAW_secret" not in a["index_document_id_hash"]
    assert a["index_document_id_hash"]


def test_mark_session_memory_deleted_missing_row_is_noop(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    # 존재하지 않는 knowledge_id -> 예외 없이 no-op
    LedgerGCSafetyAuditor(ledger).mark_session_memory_deleted(
        "nonexistent", now_iso="2026-06-16T00:00:00+00:00", operation="op_test"
    )

"""SQLite ↔ PostgreSQL parity (Phase C).

같은 Ledger 연산 시퀀스(GC-critical 경로: upsert/coverage/disable/promote/dirty/audit)를
SQLite와 PostgreSQL 양쪽에 돌려 관측 결과가 동일함을 단언한다. ``LEDGER_PG_DSN`` 환경변수가
없으면 skip(일반 스위트엔 영향 0). live Postgres가 있을 때만 dialect-correctness를 증명한다.
"""

import hashlib
import os

import pytest

# psycopg가 없는 환경(일반 스위트)에서는 이 모듈 전체를 skip해 collection 실패를 막는다.
psycopg = pytest.importorskip("psycopg")

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.postgres_db_adapter import PostgresLedgerDbAdapter

PG_DSN = os.environ.get("LEDGER_PG_DSN", "")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="LEDGER_PG_DSN 미설정 (live Postgres parity)")

SID = "sha256:parity-session"


def _sha(x: str) -> str:
    return "sha256:" + hashlib.sha256(x.encode()).hexdigest()


def _reset_pg(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.commit()


def _sm(ledger, kid, doc):
    src, win = _sha(kid + ":src"), _sha(kid + ":win")
    item = ledger.upsert_session_memory(
        knowledge_id=kid,
        content_hash=_sha(kid),
        provider="codex",
        project="p",
        session_id_hash=SID,
        title=kid,
        summary=kid,
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_sha("|".join(sorted([src, win]))),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=src,
        source_window_hash=win,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id="ds", document_id=doc, run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    return item


def _run_sequence(ledger) -> dict:
    old = _sm(ledger, "kn_old", "doc_old")
    active = _sm(ledger, "kn_active", "doc_active")
    ledger.mark_disabled(old["knowledge_id"])
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(session_id_hash=SID, provider="codex", project="p", reason="parity")
    ledger.mark_dirty_session_memory_promoted(session_id_hash=SID, summary_knowledge_id=active["knowledge_id"])
    ledger.record_memory_gc_audit(
        gc_kind="session_memory",
        operation="parity_op",
        schema_version="v1",
        mode="execute",
        knowledge_id="kn_old",
        ragflow_document_id="doc_old",
        dataset_id="ds",
        replacement_knowledge_id="kn_active",
        age_gate_seconds=86400,
        mutated=True,
    )
    snap = ledger.get_session_memory_active_snapshot(SID) or {}
    audits = ledger.list_memory_gc_audit()
    return {
        "old_status": ledger.get_by_knowledge_id("kn_old")["status"],
        "old_type": ledger.get_by_knowledge_id("kn_old")["type"],
        "active_present": ledger.get_by_knowledge_id("kn_active") is not None,
        "snap_active": snap.get("active_knowledge_id"),
        "audit_count": len(audits),
        "audit_kind": audits[0]["gc_kind"],
        "audit_operation": audits[0]["operation"],
        "audit_replacement": audits[0]["replacement_knowledge_id"],
        "audit_age_gate": audits[0]["age_gate_seconds"],
        "audit_mutated": int(audits[0]["mutated"]),
        "audit_doc_hash": audits[0]["ragflow_document_id_hash"],
    }


def test_sqlite_postgres_parity(tmp_path):
    _reset_pg(PG_DSN)
    sqlite_result = _run_sequence(Ledger(tmp_path / "l.sqlite"))
    pg_result = _run_sequence(Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN)))
    assert sqlite_result == pg_result, f"\nsqlite={sqlite_result}\npg={pg_result}"

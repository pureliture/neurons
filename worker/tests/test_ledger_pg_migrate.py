"""SQLite → PostgreSQL 데이터 이관 검증 (Phase C, cutover 직전)."""

import hashlib
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.ledger_pg_migrate import migrate_sqlite_to_postgres
from agent_knowledge.postgres_db_adapter import PostgresLedgerDbAdapter

PG_DSN = os.environ.get("LEDGER_PG_DSN", "")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="LEDGER_PG_DSN 미설정 (live Postgres)")

SID = "sha256:migrate-session"


def _sha(x: str) -> str:
    return "sha256:" + hashlib.sha256(x.encode()).hexdigest()


def _reset_pg(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.commit()


def _sm(ledger, kid, doc):
    src, win = _sha(kid + ":s"), _sha(kid + ":w")
    item = ledger.upsert_session_memory(
        knowledge_id=kid, content_hash=_sha(kid), provider="codex", project="p",
        session_id_hash=SID, title=kid, summary=kid,
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS, coverage_status="complete",
        source_manifest_hash=_sha(f"{src}|{win}"), source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"], source_content_hash=src, source_window_hash=win,
        derived_content_hash=item["content_hash"], redaction_version="redaction.v2",
        turn_start_index=1, turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id="ds", document_id=doc, run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    return item


def _populate(ledger):
    old = _sm(ledger, "kn_old", "doc_old")
    active = _sm(ledger, "kn_active", "doc_active")
    ledger.mark_disabled(old["knowledge_id"])
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(session_id_hash=SID, provider="codex", project="p", reason="m")
    ledger.mark_dirty_session_memory_promoted(session_id_hash=SID, summary_knowledge_id=active["knowledge_id"])
    ledger.record_memory_gc_audit(
        gc_kind="session_memory", operation="op", schema_version="v1", mode="execute",
        knowledge_id="kn_old", ragflow_document_id="doc_old", dataset_id="ds",
        replacement_knowledge_id="kn_active", age_gate_seconds=86400, mutated=True,
    )


def test_migrate_copies_rows_and_verifies(tmp_path):
    _reset_pg(PG_DSN)
    sqlite_path = tmp_path / "src.sqlite"
    src = Ledger(sqlite_path)
    _populate(src)

    result = migrate_sqlite_to_postgres(sqlite_path, PG_DSN)

    assert result["ok"], result["count_mismatches"]
    assert result["count_mismatches"] == []
    assert result["rows_copied"]["knowledge_items"] >= 2
    assert result["rows_copied"]["memory_gc_audit"] >= 1
    assert result["rows_copied"]["session_memory_coverage_edges"] >= 2

    # 이관 후 PostgreSQL ledger 읽기가 원본 SQLite와 동일
    pg = Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN))
    assert pg.get_by_knowledge_id("kn_old")["status"] == src.get_by_knowledge_id("kn_old")["status"]
    assert pg.get_by_knowledge_id("kn_active") is not None
    assert len(pg.list_memory_gc_audit()) == len(src.list_memory_gc_audit())
    assert (pg.get_session_memory_active_snapshot(SID) or {}).get("active_knowledge_id") == "kn_active"


def test_migrate_does_not_mutate_source(tmp_path):
    # rollback 안전: 이관은 원본 SQLite를 절대 변경하지 않는다.
    _reset_pg(PG_DSN)
    sqlite_path = tmp_path / "src.sqlite"
    src = Ledger(sqlite_path)
    _populate(src)
    before = src.get_by_knowledge_id("kn_old")["status"]
    before_audit = len(src.list_memory_gc_audit())
    migrate_sqlite_to_postgres(sqlite_path, PG_DSN)
    assert src.get_by_knowledge_id("kn_old")["status"] == before
    assert len(src.list_memory_gc_audit()) == before_audit

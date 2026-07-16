"""SQLite ↔ PostgreSQL parity (Phase C).

같은 Ledger 연산 시퀀스(GC-critical 경로: upsert/coverage/disable/promote/dirty/audit)를
SQLite와 PostgreSQL 양쪽에 돌려 관측 결과가 동일함을 단언한다. ``LEDGER_PG_DSN`` 환경변수가
없으면 skip(일반 스위트엔 영향 0). live Postgres가 있을 때만 dialect-correctness를 증명한다.
"""

import hashlib
import json
import os

import pytest

# psycopg가 없는 환경(일반 스위트)에서는 이 모듈 전체를 skip해 collection 실패를 막는다.
psycopg = pytest.importorskip("psycopg")

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.postgres_db_adapter import PostgresLedgerDbAdapter

PG_DSN = os.environ.get("LEDGER_PG_DSN", "")
if os.environ.get("REQUIRE_LEDGER_PG_DSN") == "1" and not PG_DSN:
    raise RuntimeError("LEDGER_PG_DSN is required by the PostgreSQL CI gate")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="LEDGER_PG_DSN 미설정 (live Postgres parity)")

SID = "sha256:parity-session"


def _sha(x: str) -> str:
    return "sha256:" + hashlib.sha256(x.encode()).hexdigest()


def _reset_pg(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.commit()


def test_permission_audit_marker_is_read_only_and_xid_free():
    from agent_knowledge.permission_audit import _read_postgres_database_marker

    _reset_pg(PG_DSN)
    adapter = PostgresLedgerDbAdapter(PG_DSN, read_only=True)
    with adapter.connect() as connection:
        before = connection.execute(
            """
            SELECT txid_current_if_assigned()::text AS xid,
                   current_setting('transaction_read_only') AS transaction_read_only,
                   to_regprocedure('public.julianday(text)')::text AS compatibility_function
            """
        ).fetchone()
        marker = _read_postgres_database_marker(connection)
        after = connection.execute(
            """
            SELECT txid_current_if_assigned()::text AS xid,
                   current_setting('transaction_read_only') AS transaction_read_only,
                   to_regprocedure('public.julianday(text)')::text AS compatibility_function
            """
        ).fetchone()

    assert marker["count"] == 1
    assert marker["hash"].startswith("sha256:")
    assert before["xid"] is None
    assert after["xid"] is None
    assert before["transaction_read_only"] == "on"
    assert after["transaction_read_only"] == "on"
    assert before["compatibility_function"] is None
    assert after["compatibility_function"] is None


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
        source_manifest_hash=_sha(f"{src}|{win}"),
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
        index_document_id="doc_old",
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
        "audit_doc_hash": audits[0]["index_document_id_hash"],
    }


def test_sqlite_postgres_parity(tmp_path):
    _reset_pg(PG_DSN)
    sqlite_result = _run_sequence(Ledger(tmp_path / "l.sqlite"))
    pg_result = _run_sequence(Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN)))
    assert sqlite_result == pg_result, f"\nsqlite={sqlite_result}\npg={pg_result}"


def test_julianday_shim_matches_sqlite(tmp_path):
    # 1차 cutover를 깨뜨린 julianday: GC/dirty-sync age-gate가 쓰는 정확한 패턴
    # ``julianday(replace(ts,'Z','+00:00'))`` 의 ``>=`` 비교가 양 엔진 동일해야 한다.
    # 고정 리터럴(실시간·해상도 의존 없음) → 결정적.
    _reset_pg(PG_DSN)
    sl = Ledger(tmp_path / "l.sqlite")
    migrate_sqlite_to_postgres = __import__(
        "agent_knowledge.ledger_pg_migrate", fromlist=["migrate_sqlite_to_postgres"]
    ).migrate_sqlite_to_postgres
    migrate_sqlite_to_postgres(tmp_path / "l.sqlite", PG_DSN)
    pg = Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN))

    cases = [
        ("2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"),  # a < b
        ("2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z"),  # a == b (>= True)
        ("2026-06-02T00:00:00Z", "2026-06-01T00:00:00Z"),  # a > b
    ]

    def ge(ledger, a, b):
        q = (
            f"SELECT julianday(replace('{a}','Z','+00:00')) "
            f">= julianday(replace('{b}','Z','+00:00')) AS r"
        )
        with ledger._connect() as connection:
            return bool(connection.execute(q).fetchone()["r"])

    for a, b in cases:
        assert ge(sl, a, b) == ge(pg, a, b), (a, b)
    # a==b 는 양쪽 True 여야(>= 의미 보존)
    assert ge(sl, cases[1][0], cases[1][1]) is True
    assert ge(pg, cases[1][0], cases[1][1]) is True


def test_dirty_actionable_query_executes_on_postgres(tmp_path):
    # builder가 매 사이클 실행하는 actionable 쿼리(julianday 포함)가 PG에서 에러 없이 실행 —
    # 1차 cutover는 여기서 "function julianday does not exist"로 깨졌다. shim 회귀 가드.
    from agent_knowledge.session_memory.dirty_session_memory_sync import (
        DirtySessionMemorySyncRunner,
    )

    _reset_pg(PG_DSN)
    sl = Ledger(tmp_path / "l.sqlite")
    _run_sequence(sl)
    migrate_sqlite_to_postgres = __import__(
        "agent_knowledge.ledger_pg_migrate", fromlist=["migrate_sqlite_to_postgres"]
    ).migrate_sqlite_to_postgres
    migrate_sqlite_to_postgres(tmp_path / "l.sqlite", PG_DSN)
    pg = Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN))

    sql = DirtySessionMemorySyncRunner._actionable_rows_sql()
    with pg._connect() as connection:
        connection.execute(sql).fetchall()  # 예외 없으면 통과(julianday shim 존재)

    # rowid→ingested_at 이식: PG에서 최신 session_memory 조회가 에러 없이 동작.
    assert (pg.get_session_memory_by_session_id_hash(SID) is None) == (
        sl.get_session_memory_by_session_id_hash(SID) is None
    )


def test_pg_row_positional_and_named_access(tmp_path):
    # sqlite3.Row 호환: 위치(row[0])·이름(row['c'])·.get·dict(row). cutover builder가
    # count 쿼리 row[0]에서 KeyError:0 으로 깨졌던 회귀 가드.
    _reset_pg(PG_DSN)
    sqlite_path = tmp_path / "l.sqlite"
    sl = Ledger(sqlite_path)
    _run_sequence(sl)
    migrate_sqlite_to_postgres = __import__(
        "agent_knowledge.ledger_pg_migrate", fromlist=["migrate_sqlite_to_postgres"]
    ).migrate_sqlite_to_postgres
    migrate_sqlite_to_postgres(sqlite_path, PG_DSN)
    pg = Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN))

    with pg._connect() as connection:
        count_row = connection.execute("SELECT count(*) FROM knowledge_items").fetchone()
        assert count_row[0] == 2  # 위치 접근
        row = connection.execute(
            "SELECT knowledge_id, status FROM knowledge_items ORDER BY knowledge_id LIMIT 1"
        ).fetchone()
        assert row[0] == row["knowledge_id"]  # 위치 == 이름
        assert row.get("status") is not None
        assert row.get("missing", "d") == "d"
        assert set(dict(row).keys()) == {"knowledge_id", "status"}


def test_env_switch_routes_ledger_to_postgres(monkeypatch):
    # cutover flip: NEURON_LEDGER_PG_DSN 설정 시 명시 어댑터 없이도 Ledger(path)가 Postgres 사용.
    _reset_pg(PG_DSN)
    monkeypatch.setenv("NEURON_LEDGER_PG_DSN", PG_DSN)
    ledger = Ledger("ignored-when-pg")
    assert isinstance(ledger._db_adapter, PostgresLedgerDbAdapter)
    ledger.record_memory_gc_audit(
        gc_kind="session_memory", operation="env_op", schema_version="v1", mode="execute",
        knowledge_id="k", index_document_id="d", dataset_id="ds",
        replacement_knowledge_id="r", age_gate_seconds=1, mutated=True,
    )
    audits = ledger.list_memory_gc_audit()
    assert len(audits) == 1 and audits[0]["operation"] == "env_op"


def test_memory_card_typed_payload_filters_execute_on_postgres():
    _reset_pg(PG_DSN)
    ledger = Ledger("pg", db_adapter=PostgresLedgerDbAdapter(PG_DSN))
    envelope = {
        "memory_id": "mem_pg_artifact_preference",
        "card_type": "preference",
        "project": "neurons",
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "typed_payload": {
            "source_object_type": "ArtifactPreference",
            "target_object_id": "ko:ArtifactPreference:html-review",
            "applies_to": "html_review_artifact",
        },
    }
    with ledger._connect() as connection:
        connection.execute(
            """
            INSERT INTO llm_brain_memory_cards (
                memory_id, brain_id, card_type, project, provider,
                lifecycle_state, judgment_state, approval_state, currentness,
                status, content_hash, envelope_json, accepted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope["memory_id"],
                "brain_pg_parity",
                envelope["card_type"],
                envelope["project"],
                "codex",
                envelope["lifecycle_state"],
                "accepted",
                envelope["approval_state"],
                envelope["currentness"],
                "accepted",
                _sha("pg-artifact-preference"),
                json.dumps(envelope, sort_keys=True),
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
            ),
        )

    cards = ledger.list_llm_brain_memory_cards(
        project="neurons",
        accepted_only=True,
        current_only=True,
        card_type="preference",
        source_object_type="ArtifactPreference",
        target_object_type="ArtifactPreference",
        applies_to="html_review_artifact",
        limit=10,
    )

    assert [card["memory_id"] for card in cards] == ["mem_pg_artifact_preference"]

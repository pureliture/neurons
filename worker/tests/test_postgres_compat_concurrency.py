"""PostgreSQL julianday 호환성 함수 동시성 안전 계약 테스트.

Synthetic Mock Contract Tests (외부 네트워크/DB 의존성 0건, 상시 실행 단위 테스트)
"""

from psycopg import errors
import pytest

from agent_knowledge import postgres_db_adapter
from agent_knowledge.postgres_db_adapter import (
    _CHECK_JULIANDAY_SQL,
    _JULIANDAY_BODY,
    _JULIANDAY_SHIM,
    _LOCK_COMPAT_SHIM_SQL,
    PostgresLedgerDbAdapter,
    _normalize_plpgsql_body,
)

# ==============================================================================
# Synthetic Contract / Unit Tests (No Real DB Required, Always Runs)
# ==============================================================================


class _MockCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, query, params=None):
        self.conn.executed_statements.append(query)
        if "pg_proc.prosrc" in query:
            self.conn.last_result = (self.conn.current_prosrc,)
            return
        if "pg_advisory_xact_lock" in query:
            self.conn.advisory_lock_count += 1
            return
        if "CREATE OR REPLACE FUNCTION julianday" in query:
            if self.conn.fail_on_ddl:
                self.conn.current_prosrc = _JULIANDAY_BODY
                raise errors.InternalError_("tuple concurrently updated")
            self.conn.current_prosrc = _JULIANDAY_BODY
            return

    def fetchone(self):
        return self.conn.last_result


class _MockPsycopgConnection:
    def __init__(self, *, prosrc=None, fail_on_ddl=False):
        self.read_only = False
        self.current_prosrc = prosrc
        self.fail_on_ddl = fail_on_ddl
        self.executed_statements = []
        self.last_result = None
        self.commit_count = 0
        self.rollback_count = 0
        self.advisory_lock_count = 0
        self.closed = False

    def cursor(self):
        return _MockCursor(self)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def test_synthetic_fast_path_skips_ddl_and_commits_idle_when_identical(monkeypatch):
    """[Synthetic] 동일한 정의의 함수가 이미 존재할 때 DDL 및 자문 락을 0회 수행하고,
    트랜잭션을 반드시 commit()하여 IDLE 상태로 반환함을 검증."""
    mock_conn = _MockPsycopgConnection(prosrc=_JULIANDAY_BODY)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *args, **kwargs: mock_conn)

    adapter = PostgresLedgerDbAdapter("postgresql://fake:5432/test")
    conn = adapter.connect()

    assert any("pg_proc.prosrc" in s for s in mock_conn.executed_statements)
    assert not any("CREATE OR REPLACE FUNCTION" in s for s in mock_conn.executed_statements)
    assert mock_conn.advisory_lock_count == 0  # 락 0회
    assert mock_conn.commit_count == 1  # 트랜잭션 IDLE 확정 commit
    assert mock_conn.rollback_count == 0


def test_synthetic_slow_path_acquires_lock_and_creates_when_missing(monkeypatch):
    """[Synthetic] 함수가 존재하지 않을 때 자문 락을 획득한 후 DDL을 1회 실행함을 검증."""
    mock_conn = _MockPsycopgConnection(prosrc=None)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *args, **kwargs: mock_conn)

    adapter = PostgresLedgerDbAdapter("postgresql://fake:5432/test")
    conn = adapter.connect()

    assert mock_conn.advisory_lock_count == 1
    assert any("CREATE OR REPLACE FUNCTION" in s for s in mock_conn.executed_statements)
    assert mock_conn.commit_count == 1
    assert mock_conn.current_prosrc == _JULIANDAY_BODY


def test_synthetic_updates_definition_when_prosrc_differs(monkeypatch):
    """[Synthetic] 함수 이름은 있으나 정의(prosrc)가 다른 오래된/변조된 버전일 때
    이를 감지하고 최신 버전으로 안전하게 DDL 갱신함을 검증."""
    drifted_prosrc = "BEGIN RETURN 12345.0; END;"
    mock_conn = _MockPsycopgConnection(prosrc=drifted_prosrc)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *args, **kwargs: mock_conn)

    adapter = PostgresLedgerDbAdapter("postgresql://fake:5432/test")
    conn = adapter.connect()

    assert mock_conn.advisory_lock_count == 1
    assert any("CREATE OR REPLACE FUNCTION" in s for s in mock_conn.executed_statements)
    assert mock_conn.commit_count == 1
    assert mock_conn.current_prosrc == _JULIANDAY_BODY


def test_synthetic_recovers_from_concurrent_tuple_update_error(monkeypatch):
    """[Synthetic] DDL 도중 tuple concurrently updated가 발생하더라도 롤백 후
    타 연결에 의해 생성 완료되었음이 확인되면 정상 복원됨을 검증."""
    mock_conn = _MockPsycopgConnection(prosrc=None, fail_on_ddl=True)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *args, **kwargs: mock_conn)

    adapter = PostgresLedgerDbAdapter("postgresql://fake:5432/test")
    conn = adapter.connect()

    assert mock_conn.rollback_count == 1
    assert mock_conn.commit_count == 1  # 복원 후 IDLE 확정 commit


def test_synthetic_fails_closed_on_unrelated_exception(monkeypatch):
    """[Synthetic] 동시성 충돌이 아닌 무관한 에러(문법, 권한, 디스크 풀 등)는 삼키지 않고 raise함을 검증."""
    class _FailingConnection(_MockPsycopgConnection):
        def cursor(self):
            class _FailingCursor(_MockCursor):
                def execute(self, query, params=None):
                    self.conn.executed_statements.append(query)
                    if "pg_advisory_xact_lock" in query:
                        raise errors.InsufficientPrivilege("permission denied for lock")
            return _FailingCursor(self)

    mock_conn = _FailingConnection(prosrc=None)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *args, **kwargs: mock_conn)

    adapter = PostgresLedgerDbAdapter("postgresql://fake:5432/test")
    try:
        adapter.connect()
    except errors.InsufficientPrivilege as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("Unrelated exception must be re-raised")

    assert mock_conn.rollback_count == 1


def test_normalize_plpgsql_body_preserves_internal_spacing():
    """줄 단위 좌우 공백은 정규화하되, 내부 SQL 구문 의미를 보존함을 검증."""
    raw = "  DECLARE ts timestamptz;  \n\n  BEGIN  \n    RETURN NULL;  \n  END;  "
    expected = "DECLARE ts timestamptz;\nBEGIN\nRETURN NULL;\nEND;"
    assert _normalize_plpgsql_body(raw) == expected


def test_check_sql_contains_strict_signature_guards():
    """카탈로그 조회 쿼리가 올바른 인자(text), 리턴형(float8), 스키마 우선순위를 엄격히 대조함을 검증."""
    sql = _CHECK_JULIANDAY_SQL
    assert "pg_proc.proname = 'julianday'" in sql
    assert "pg_proc.proargtypes[0] = 'pg_catalog.text'::regtype" in sql
    assert "ret.typname = 'float8'" in sql
    assert "lang.lanname = 'plpgsql'" in sql
    assert "current_schemas(true)" in sql
    assert "ORDER BY array_position(current_schemas(true), pg_namespace.nspname::text)" in sql


def test_connection_commits_and_remains_idle_after_connect(monkeypatch):
    """연결 완료 후 항상 commit되어 트랜잭션이 IDLE 상태(미커밋 상태 방지)로 유지됨을 검증."""
    mock_conn = _MockPsycopgConnection(prosrc=_JULIANDAY_BODY)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *args, **kwargs: mock_conn)

    adapter = PostgresLedgerDbAdapter("postgresql://fake:5432/test")
    conn = adapter.connect()

    # 트랜잭션이 열린 채로 방치되지 않고 IDLE 상태를 위해 commit이 호출되었는지 검증
    assert mock_conn.commit_count >= 1
    assert mock_conn.rollback_count == 0

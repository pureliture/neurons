"""PostgreSQL engine adapter for Ledger (Phase C).

``ILedgerCoreDbAdapter``의 PostgreSQL 구현. callers(raw DBAPI 패턴: ``connection.execute(
sql, ?params)``, ``executescript``, ``dict(row)``, ``with conn:``)가 sqlite3와 동일한
인터페이스를 쓰도록 psycopg connection을 얇게 wrap한다.

이식성 근거(이미 확보): SQL *의미*는 B2/B3에서 표준 SQL(ON CONFLICT / CURRENT_TIMESTAMP)로
통일됐다. 이 어댑터가 처리하는 차이는 (1) placeholder ``?``→``%s`` (pg_paramstyle), (2)
row dict 접근, (3) PRAGMA/sqlite_master → information_schema(스키마 헬퍼 dialect 분기,
ledger.py), (4) file-backed 아님(Ledger.__init__ 분기) 뿐이다.
"""

from __future__ import annotations

import re

import psycopg
from psycopg.rows import dict_row

from .db_adapter import ILedgerCoreDbAdapter
from .pg_paramstyle import qmark_to_pyformat

# SQLite 연결-튜닝 PRAGMA(``PRAGMA busy_timeout=…`` / ``journal_mode=WAL`` /
# ``synchronous=NORMAL`` / ``foreign_keys=ON`` / ``query_only=ON`` 등 assignment 형태)는
# PostgreSQL에 대응이 없다. caller 모듈들이 ``_connect()`` 로 받은 연결에 직접 이 PRAGMA를
# 발행하므로(SQLite 가정), 단일 chokepoint인 어댑터 execute에서 no-op 처리한다. introspection
# PRAGMA(``PRAGMA table_info(...)`` — ``=`` 없음)는 통과시켜, dialect 미라우팅 시 조용히 빈
# 결과를 주는 대신 시끄럽게 실패하도록 둔다(스키마 헬퍼는 PG에서 information_schema 사용).
_NOOP_PRAGMA = re.compile(r"^\s*PRAGMA\s+\w+\s*=", re.IGNORECASE)


def _is_noop_pragma(sql: str) -> bool:
    """PostgreSQL에서 무시해야 하는 SQLite 연결-튜닝 PRAGMA인가."""
    return bool(_NOOP_PRAGMA.match(sql))


# SQLite ``julianday(t)`` 호환 shim. caller(GC/dirty-sync)가 age-gate 델타 비교에 쓰는
# ``julianday(replace(col,'Z','+00:00'))`` / ``julianday('now')`` 를 SQL 무수정으로 PG에서
# 동작시킨다. Julian Day Number = epoch초/86400 + 2440587.5(Unix epoch=JD 2440587.5). 모든
# 사용처가 델타(a-b, a>=b)라 절대값보다 단조성·일관성이 핵심. 빈/무효 입력은 SQLite처럼 NULL
# 반환(plpgsql 예외처리) — caller가 nullif로 거르지만 방어적으로.
_JULIANDAY_SHIM = """
CREATE OR REPLACE FUNCTION julianday(t text) RETURNS double precision AS $JD$
DECLARE ts timestamptz;
BEGIN
  IF t IS NULL THEN RETURN NULL; END IF;
  IF t = 'now' THEN RETURN extract(epoch FROM now()) / 86400.0 + 2440587.5; END IF;
  BEGIN
    ts := t::timestamptz;
  EXCEPTION WHEN others THEN
    RETURN NULL;
  END;
  RETURN extract(epoch FROM ts) / 86400.0 + 2440587.5;
END;
$JD$ LANGUAGE plpgsql STABLE;
"""


class _PgResult:
    """psycopg cursor를 sqlite3 cursor처럼(fetchall/fetchone) 노출. dict_row라 row는
    ``dict(row)``·``row['col']`` 모두 가능 — sqlite3.Row와 호환."""

    def __init__(self, cursor):
        self._cursor = cursor  # None = no-op(예: PG에서 무시된 PRAGMA)

    def fetchall(self):
        return [] if self._cursor is None else self._cursor.fetchall()

    def fetchone(self):
        return None if self._cursor is None else self._cursor.fetchone()

    @property
    def rowcount(self):
        return -1 if self._cursor is None else self._cursor.rowcount


class _PgConnection:
    """sqlite3.Connection 호환 wrapper. ``with conn:`` 시 성공→commit/예외→rollback 후
    close(ClosingSqliteConnection과 동일 시맨틱)."""

    dialect = "postgres"

    def __init__(self, dsn: str):
        self._conn = psycopg.connect(dsn, row_factory=dict_row)
        self.row_factory = None  # 호환용: callers가 sqlite3.Row를 set해도 무시(dict_row 고정)
        self._ensure_compat_functions()

    def _ensure_compat_functions(self) -> None:
        # SQLite 호환 함수(julianday) 보장 — idempotent CREATE OR REPLACE, 연결당 1회.
        with self._conn.cursor() as cursor:
            cursor.execute(_JULIANDAY_SHIM)
        self._conn.commit()

    def execute(self, sql: str, params=None) -> _PgResult:
        if _is_noop_pragma(sql):
            return _PgResult(None)  # SQLite 연결-튜닝 PRAGMA — PG no-op
        cursor = self._conn.cursor()
        if params:
            cursor.execute(qmark_to_pyformat(sql), tuple(params))
        else:
            # 파라미터 없음 → 원문 그대로(%-보간 없음). ?도 없음(있으면 params 필수).
            cursor.execute(sql)
        return _PgResult(cursor)

    def executescript(self, script: str) -> None:
        # 멀티스테이트먼트 DDL(파라미터 없음). psycopg는 한 execute에 다중 statement 가능.
        with self._conn.cursor() as cursor:
            cursor.execute(script)
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "_PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False


class PostgresLedgerDbAdapter(ILedgerCoreDbAdapter):
    """PostgreSQL 엔진 어댑터. ``dsn`` = psycopg 연결 문자열(예:
    'host=127.0.0.1 port=5432 user=... dbname=...'). connect()마다 새 연결을 연다
    (현행 SQLite 어댑터의 per-call 연결 시맨틱과 동일)."""

    is_file_backed = False

    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self, *, configure_journal: bool = False) -> _PgConnection:
        # configure_journal(WAL)은 SQLite 전용 — Postgres에선 무의미(no-op).
        return _PgConnection(self.dsn)

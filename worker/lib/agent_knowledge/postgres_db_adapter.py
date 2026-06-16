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

import psycopg
from psycopg.rows import dict_row

from .db_adapter import ILedgerCoreDbAdapter
from .pg_paramstyle import qmark_to_pyformat


class _PgResult:
    """psycopg cursor를 sqlite3 cursor처럼(fetchall/fetchone) 노출. dict_row라 row는
    ``dict(row)``·``row['col']`` 모두 가능 — sqlite3.Row와 호환."""

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _PgConnection:
    """sqlite3.Connection 호환 wrapper. ``with conn:`` 시 성공→commit/예외→rollback 후
    close(ClosingSqliteConnection과 동일 시맨틱)."""

    dialect = "postgres"

    def __init__(self, dsn: str):
        self._conn = psycopg.connect(dsn, row_factory=dict_row)
        self.row_factory = None  # 호환용: callers가 sqlite3.Row를 set해도 무시(dict_row 고정)

    def execute(self, sql: str, params=None) -> _PgResult:
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

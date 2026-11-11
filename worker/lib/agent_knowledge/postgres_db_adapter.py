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

import math
import re
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .db_adapter import ILedgerCoreDbAdapter
from .pg_paramstyle import qmark_to_pyformat


class _PgRow:
    """sqlite3.Row 호환 row. caller가 위치(``row[0]``)·이름(``row['c']``)·``row.get``·
    ``keys()``·``dict(row)`` 를 sqlite3.Row와 동일하게 쓰도록 한다. psycopg 기본 dict_row는
    이름 접근만 돼 ``row[0]`` 위치 접근(count 쿼리 등)에서 ``KeyError: 0`` 을 낸다."""

    __slots__ = ("_v", "_c")

    def __init__(self, values, columns):
        self._v = values
        self._c = columns

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._v[key]
        return self._v[self._c.index(key)]

    def get(self, key, default=None):
        try:
            return self._v[self._c.index(key)]
        except (ValueError, IndexError):
            return default

    def keys(self):  # dict(row) 가 mapping 프로토콜로 인식
        return list(self._c)

    def __iter__(self):  # sqlite3.Row 처럼 값 순회(tuple(row))
        return iter(self._v)

    def __len__(self):
        return len(self._v)


def _pg_row_factory(cursor):
    desc = cursor.description
    cols = [c.name for c in desc] if desc else []

    def make_row(values):
        return _PgRow(tuple(values), cols)

    return make_row

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


def _options_with_deadline(dsn: str, *, timeout_ms: int) -> str:
    """Preserve libpq session options while appending a command deadline."""

    configured = str(conninfo_to_dict(dsn).get("options") or "").strip()
    deadline = f"-c statement_timeout={int(timeout_ms)}"
    return " ".join(value for value in (configured, deadline) if value)


# SQLite ``julianday(t)`` 호환 shim 본문. caller(GC/dirty-sync)가 age-gate 델타 비교에 쓰는
# ``julianday(replace(col,'Z','+00:00'))`` / ``julianday('now')`` 를 SQL 무수정으로 PG에서
# 동작시킨다. Julian Day Number = epoch초/86400 + 2440587.5(Unix epoch=JD 2440587.5). 모든
# 사용처가 델타(a-b, a>=b)라 절대값보다 단조성·일관성이 핵심. 빈/무효 입력은 SQLite처럼 NULL
# 반환(plpgsql 예외처리) — caller가 nullif로 거르지만 방어적으로.
_JULIANDAY_BODY = """DECLARE ts timestamptz;
BEGIN
  IF t IS NULL THEN RETURN NULL; END IF;
  IF t = 'now' THEN RETURN extract(epoch FROM now()) / 86400.0 + 2440587.5; END IF;
  BEGIN
    ts := t::timestamptz;
  EXCEPTION WHEN others THEN
    RETURN NULL;
  END;
  RETURN extract(epoch FROM ts) / 86400.0 + 2440587.5;
END;"""

_JULIANDAY_SHIM = f"""
CREATE OR REPLACE FUNCTION julianday(t text) RETURNS double precision AS $JD$
{_JULIANDAY_BODY}
$JD$ LANGUAGE plpgsql STABLE;
"""

_CHECK_JULIANDAY_SQL = """
SELECT
    ret.typname,
    pg_proc.prokind,
    pg_proc.provolatile,
    lang.lanname,
    pg_proc.prosrc
FROM pg_proc
JOIN pg_namespace ON pg_proc.pronamespace = pg_namespace.oid
JOIN pg_type ret ON pg_proc.prorettype = ret.oid
JOIN pg_language lang ON pg_proc.prolang = lang.oid
WHERE pg_proc.proname = 'julianday'
  AND pg_proc.pronargs = 1
  AND pg_proc.proargtypes[0] = 'pg_catalog.text'::regtype
  AND pg_namespace.nspname = ANY(current_schemas(true))
ORDER BY array_position(current_schemas(true), pg_namespace.nspname::text)
LIMIT 1;
"""

_LOCK_COMPAT_SHIM_SQL = "SELECT pg_advisory_xact_lock(hashtext('julianday_compat_shim'))"


def _normalize_plpgsql_body(text: str) -> str:
    """줄 단위 좌우 공백만 정규화하여 SQL 문자열 리터럴 내부 공백을 보존하면서 대조한다."""
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())



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

    def __init__(
        self,
        dsn: str,
        *,
        read_only: bool = False,
        deadline_monotonic: float | None = None,
        on_network_attempt=None,
    ):
        self._deadline_monotonic = deadline_monotonic
        connect_kwargs = {"row_factory": _pg_row_factory}
        timeout_seconds = self._remaining_deadline_seconds()
        if timeout_seconds is not None:
            if timeout_seconds < 1:
                raise TimeoutError("postgres ledger deadline has insufficient connect budget")
            connect_kwargs.update(
                {
                    "connect_timeout": max(1, int(timeout_seconds)),
                    "options": _options_with_deadline(
                        dsn,
                        timeout_ms=max(1, int(timeout_seconds * 1000)),
                    ),
                }
            )
        if callable(on_network_attempt):
            on_network_attempt()
        self._conn = psycopg.connect(dsn, **connect_kwargs)
        self.row_factory = None  # 호환용: callers가 sqlite3.Row를 set해도 무시(_PgRow 고정)
        self._read_only = bool(read_only)
        if self._read_only:
            # TokenReview/audit sentinel 같은 read-only runtime이 connection 생성만으로
            # CREATE OR REPLACE FUNCTION을 실행하지 않도록 DB 자체 read-only transaction
            # mode를 먼저 고정한다.
            self._conn.read_only = True
        else:
            self._ensure_compat_functions()

    def _remaining_deadline_seconds(self) -> float | None:
        if self._deadline_monotonic is None:
            return None
        remaining = self._deadline_monotonic - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("postgres ledger deadline exceeded")
        return remaining

    def _refresh_statement_timeout(self) -> None:
        remaining = self._remaining_deadline_seconds()
        if remaining is None:
            return
        if remaining < 0.001:
            raise TimeoutError("postgres ledger deadline has insufficient statement budget")
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{max(1, int(remaining * 1000))}ms",),
            )

    def _is_julianday_defined_and_current(self) -> bool:
        with self._conn.cursor() as cursor:
            cursor.execute(_CHECK_JULIANDAY_SQL)
            row = cursor.fetchone()
            if not row:
                return False
            ret_typname, prokind, provolatile, lanname, prosrc = row
            if (
                ret_typname != "float8"
                or prokind != "f"
                or provolatile != "s"
                or lanname != "plpgsql"
                or not prosrc
            ):
                return False
            existing = _normalize_plpgsql_body(prosrc)
            expected = _normalize_plpgsql_body(_JULIANDAY_BODY)
            return existing == expected

    def _ensure_compat_functions(self) -> None:
        # SQLite 호환 함수(julianday) 보장 — idempotent, 동시성 안전 보장.
        # 1. Fast Path: 현재 search_path 상에 올바른 정의의 함수가 이미 존재하면 DDL 0건 수행 후 트랜잭션 IDLE 확정.
        try:
            if self._is_julianday_defined_and_current():
                self._conn.commit()
                return
        except psycopg.Error:
            self._conn.rollback()
            raise

        # 2. Slow Path: 함수가 없거나 정의가 다른 경우 트랜잭션 자문 잠금을 획득하여 신규 프로세스 간 동시 DDL 직렬화.
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(_LOCK_COMPAT_SHIM_SQL)
                # 잠금 획득 후 대기하는 동안 다른 연결이 이미 생성했을 수 있으므로 재확인
                if not self._is_julianday_defined_and_current():
                    cursor.execute(_JULIANDAY_SHIM)
            self._conn.commit()  # 자문 잠금 자동 해제 및 트랜잭션 IDLE 확정
        except psycopg.Error as exc:
            self._conn.rollback()
            # 롤링 배포 중 구버전 프로세스(자문 잠금 미사용)와의 경합 등 실제 카탈로그 락 충돌에 한해서만
            # 롤백 후 다른 연결에 의해 올바른 함수가 생성 완료되었는지 확인하여 복원한다.
            err_msg = str(exc).lower()
            sqlstate = getattr(exc, "sqlstate", None)
            is_concurrent_catalog_error = (
                "tuple concurrently updated" in err_msg
                or (sqlstate == "55P03" and "catalog" in err_msg)
            )
            if is_concurrent_catalog_error:
                try:
                    if self._is_julianday_defined_and_current():
                        self._conn.commit()
                        return
                except psycopg.Error:
                    self._conn.rollback()
            # 무관한 에러(권한 부족, 구문 오류, search_path 위반 등)이거나 여전히 함수가 없으면 원래 예외 re-raise
            raise

    def execute(self, sql: str, params=None) -> _PgResult:
        if _is_noop_pragma(sql):
            return _PgResult(None)  # SQLite 연결-튜닝 PRAGMA — PG no-op
        self._refresh_statement_timeout()
        cursor = self._conn.cursor()
        if params:
            cursor.execute(qmark_to_pyformat(sql), tuple(params))
        else:
            # 파라미터 없음 → 원문 그대로(%-보간 없음). ?도 없음(있으면 params 필수).
            cursor.execute(sql)
        return _PgResult(cursor)

    def executescript(self, script: str) -> None:
        # 멀티스테이트먼트 DDL(파라미터 없음). psycopg는 한 execute에 다중 statement 가능.
        self._refresh_statement_timeout()
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

    def __init__(
        self,
        dsn: str,
        *,
        read_only: bool = False,
        deadline_monotonic: float | None = None,
    ):
        self.dsn = dsn
        self.read_only = bool(read_only)
        self._deadline_monotonic = deadline_monotonic
        self.network_attempted = False

    def _mark_network_attempted(self) -> None:
        self.network_attempted = True

    def connect(self, *, configure_journal: bool = False) -> _PgConnection:
        # configure_journal(WAL)은 SQLite 전용 — Postgres에선 무의미(no-op).
        return _PgConnection(
            self.dsn,
            read_only=self.read_only,
            deadline_monotonic=self._deadline_monotonic,
            on_network_attempt=self._mark_network_attempted,
        )

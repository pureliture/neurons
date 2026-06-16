"""SQLite → PostgreSQL 데이터 이관 (Phase C, cutover 직전 단계).

기존 SQLite ledger의 모든 행을 새 PostgreSQL ledger로 복사한다. 대상 스키마는
``Ledger(..., db_adapter=PostgresLedgerDbAdapter(dsn))`` 생성 시 ``_initialize``가 자동
생성하므로(B2/B3 표준 SQL로 양 엔진 공통), 이 도구는 *행 복사 + 행수 검증*만 한다.

cutover 절차(운영, Ubuntu·사용자 go):
  1. 시스템 정지(또는 read-only) → 이 도구로 이관 → 행수/검증 통과 확인.
  2. 시스템을 PostgresLedgerDbAdapter로 전환(엔진 flip).
  3. 문제 시 rollback = SQLite 원본 그대로 유지(이 도구는 원본을 읽기만 함, 변경 없음).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .ledger import Ledger
from .postgres_db_adapter import PostgresLedgerDbAdapter


def _sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _pg_count(target: Ledger, table: str) -> int:
    with target._connect() as connection:
        row = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def migrate_sqlite_to_postgres(sqlite_path: Path | str, pg_dsn: str) -> dict:
    """SQLite 원본(읽기 전용) → 새 PostgreSQL ledger로 모든 행을 이관한다.

    원본 SQLite는 절대 변경하지 않는다(rollback = 원본 그대로). 반환값에 per-table 이관
    행수와 검증 결과(SQLite vs PostgreSQL 행수 불일치 목록)를 담는다.
    """
    source = sqlite3.connect(str(sqlite_path))
    source.row_factory = sqlite3.Row
    try:
        # 대상: fresh 스키마(_initialize) — 데이터 테이블은 비어 있고 schema_migrations만 seed됨.
        target = Ledger("pg-migrate-target", db_adapter=PostgresLedgerDbAdapter(pg_dsn))
        tables = _sqlite_tables(source)
        copied: dict[str, int] = {}
        for table in tables:
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                copied[table] = 0
                continue
            cols = list(rows[0].keys())
            col_list = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)
            # schema_migrations는 _initialize가 이미 seed → 충돌 무시(idempotent).
            suffix = " ON CONFLICT DO NOTHING" if table == "schema_migrations" else ""
            insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}){suffix}"
            with target._connect() as connection:
                for row in rows:
                    connection.execute(insert_sql, tuple(row[c] for c in cols))
            copied[table] = len(rows)

        # 검증: per-table 행수 일치(schema_migrations는 seed 분 동일하므로 일치해야 함).
        mismatches: list[dict] = []
        for table in tables:
            src_n = int(source.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            pg_n = _pg_count(target, table)
            if src_n != pg_n:
                mismatches.append({"table": table, "sqlite": src_n, "postgres": pg_n})
        return {
            "tables_migrated": len(tables),
            "rows_copied": copied,
            "count_mismatches": mismatches,
            "ok": not mismatches,
        }
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger-pg-migrate")
    parser.add_argument("--sqlite", required=True, help="원본 SQLite ledger 경로(읽기 전용)")
    parser.add_argument("--pg-dsn", required=True, help="대상 PostgreSQL DSN")
    args = parser.parse_args(argv)
    result = migrate_sqlite_to_postgres(args.sqlite, args.pg_dsn)
    import json

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

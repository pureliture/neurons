"""PG 어댑터의 SQLite 연결-튜닝 PRAGMA no-op 판정 (Phase C 후속 fix).

caller 모듈이 ``_connect()`` 연결에 직접 발행하는 ``PRAGMA busy_timeout=…`` 등은
PostgreSQL에서 syntax error → 어댑터가 no-op 처리해야 한다. introspection PRAGMA
(``table_info``)는 통과(= 없음)시켜 dialect 미라우팅을 숨기지 않는다.
"""

import pytest

pytest.importorskip("psycopg")

from agent_knowledge.postgres_db_adapter import _is_noop_pragma


def test_connection_tuning_pragmas_are_noop():
    for sql in (
        "PRAGMA busy_timeout=30000",
        "PRAGMA busy_timeout=30000;",
        "  pragma journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA query_only=ON;",
    ):
        assert _is_noop_pragma(sql), sql


def test_introspection_pragma_and_sql_not_noop():
    # table_info(...) 는 = 가 없으므로 통과(PG 미라우팅 시 시끄럽게 실패해야 함).
    assert not _is_noop_pragma("PRAGMA table_info(knowledge_items)")
    assert not _is_noop_pragma("SELECT 1")
    assert not _is_noop_pragma("INSERT INTO knowledge_items VALUES (1)")

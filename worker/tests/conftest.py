"""Opt-in, function-owned PostgreSQL namespaces for global-lease workers."""
from contextlib import contextmanager
import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from agent_knowledge.postgres_store.pgvector_store import PgVectorStore


@pytest.fixture
def isolated_pg_store_factory():
    dsn = os.environ.get("LBRAIN_TEST_PG_DSN", "")
    if not dsn:
        pytest.skip("LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)")
    config = conninfo_to_dict(dsn)
    host = str(config.get("host") or "")
    if config.get("hostaddr") or not (host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")):
        pytest.fail("isolated PostgreSQL fixtures require an explicit local test instance")
    if not config.get("dbname") or not config.get("user") or config.get("service"):
        pytest.fail("isolated PostgreSQL fixtures require explicit database/user, no service")

    @contextmanager
    def create_store():
        schema = "c1_test_" + uuid.uuid4().hex
        # Same explicit instance/database; never use an ambient administrator DSN.
        with psycopg.connect(dsn, autocommit=True) as admin:
            for extension in ("vector", "uuid-ossp"):
                admin.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {} WITH SCHEMA public").format(sql.Identifier(extension)))
                row = admin.execute(
                    "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = %s",
                    (extension,),
                ).fetchone()
                assert row is not None and row[0] == "public", "test extensions must live outside disposable schemas"
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            try:
                scoped_dsn = make_conninfo(
                    dsn, options=f"-csearch_path={schema},public -cstatement_timeout=15000 -clock_timeout=10000",
                    connect_timeout=5,
                )
                store = PgVectorStore(dsn=scoped_dsn)
                store.execute_ddl()
                yield store
            finally:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                row = admin.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()
                assert row is not None and row[0] is None

    return create_store


@pytest.fixture
def isolated_pg_store(isolated_pg_store_factory):
    with isolated_pg_store_factory() as store:
        yield store

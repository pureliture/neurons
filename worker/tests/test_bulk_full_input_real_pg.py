"""Real Postgres seam test against a local disposable instance only.

The managed database from TRIAL_PG_DSN is used solely as the administration
target. Every test creates and drops its own UUID database; no shared database
schema is mutated and no FORCE drop is used.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.ledger_adapter import (
    EXTRACTION_LEVEL_ENTITY,
    LedgerGraphProjectionStateStore,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("TRIAL_PG_DSN"),
    reason="no isolated local Postgres test database provided",
)


@pytest.fixture
def isolated_trial_pg_dsn(monkeypatch):
    """Create a UUID database on a verified local managed test instance."""
    managed_dsn = os.environ["TRIAL_PG_DSN"]
    config = conninfo_to_dict(managed_dsn)
    host = str(config.get("host") or "")
    if config.get("hostaddr") or not (
        host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
    ):
        pytest.fail("isolated PostgreSQL fixtures require an explicit local instance")
    managed_db = str(config.get("dbname") or "")
    if not managed_db or not config.get("user") or config.get("service"):
        pytest.fail("isolated PostgreSQL fixtures require explicit database/user, no service")
    if managed_db in {"postgres", "template0", "template1"}:
        pytest.fail("managed test database must not be an administrative template")

    token = uuid.uuid4().hex
    db_name = "bulkpgtest_" + token
    application_name = "bulkpgtest_" + token
    with psycopg.connect(managed_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
        )

    scoped_dsn = make_conninfo(
        managed_dsn, dbname=db_name, application_name=application_name
    )
    try:
        store = PgVectorStore(dsn=scoped_dsn)
        store.execute_ddl()
        monkeypatch.setenv("NEURON_LEDGER_PG_DSN", scoped_dsn)
        yield scoped_dsn
    finally:
        with psycopg.connect(managed_dsn, autocommit=True) as admin:
            deadline = time.monotonic() + 5.0
            while True:
                row = admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = %s",
                    (db_name,),
                ).fetchone()
                active = int(row[0]) if row else 0
                if active == 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            assert active == 0, "UUID test database still has active sessions"
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name))
            )
            row = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
            ).fetchone()
            assert row is None, "UUID test database cleanup failed"


def test_real_pg_projection_state_roundtrip(tmp_path, isolated_trial_pg_dsn):
    ledger = Ledger(tmp_path / "unused-sqlite-path.db")
    store = LedgerGraphProjectionStateStore(ledger)
    source_hash = "sha256:" + "a" * 64
    episode = OntologyEpisode.from_payload(
        event_id="synthetic-event",
        entity_type="Session",
        natural_id="synthetic-natural",
        payload={
            "project": "synthetic-project",
            "brain_id": "/project/synthetic-project",
            "source_hash": source_hash,
        },
    )
    store.mark_projected(episode, "inserted", extraction_level=EXTRACTION_LEVEL_ENTITY)
    common = dict(
        project="synthetic-project",
        natural_id="synthetic-natural",
        extraction_level=EXTRACTION_LEVEL_ENTITY,
        entity_type="Session",
    )
    assert store.has_projected_source_hash(source_hash=source_hash, **common) is True
    assert store.has_projected_source_hash(source_hash="sha256:" + "b" * 64, **common) is False

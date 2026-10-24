"""Contract and integration tests for pg-freshness-revalidate CLI."""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.postgres_store.freshness_revalidate_cli import (
    classify,
    content_sha256,
    main,
)


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_command_is_registered():
    assert "pg-freshness-revalidate" in COMMAND_HANDLERS


def test_command_metadata_present():
    assert COMMAND_METADATA.get("pg-freshness-revalidate", {}).get("live_mutation_requires_approval") is True


def test_plan_mode_no_dsn(capsys):
    assert main([], environ={}) == 0
    report = _payload(capsys)
    assert report["status"] == "planned"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_check_requires_dsn(capsys):
    assert main(["--check"], environ={}) == 2
    report = _payload(capsys)
    assert report["status"] == "configuration_required"


def test_classify_rules():
    current = "sha256:" + "a" * 64
    assert classify(None, current) == ("verified_current", current)
    assert classify("", current) == ("verified_current", current)
    assert classify(current, current) == ("verified_current", current)
    assert classify("sha256:" + "b" * 64, current) == ("verified_changed", "sha256:" + "b" * 64)


def test_content_sha256():
    text = "hello"
    assert content_sha256(text) == "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert content_sha256(None) == "sha256:" + hashlib.sha256(b"").hexdigest()


PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")


@pytest.fixture
def freshness_pg():
    if not PG_DSN:
        pytest.skip("LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)")
    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    yield PG_DSN
    # cleanup handled by tests using unique ids? Best-effort cleanup of test rows.
    with store.transaction() as conn:
        conn.execute("DELETE FROM memory_cards WHERE memory_id LIKE 'card-%'")
        conn.execute("DELETE FROM session_memory_chunks WHERE chunk_id LIKE 'chunk-%'")


def _insert_chunk(conn, chunk_id, content):
    h = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO session_memory_chunks (chunk_id, session_id_hash, project, content_markdown, content_hash) VALUES (%s,%s,%s,%s,%s)",
        (chunk_id, "sess", "neurons", content, h),
    )
    return h


def _insert_card(conn, memory_id, source_id, source_hash=None):
    conn.execute(
        """INSERT INTO memory_cards
           (memory_id, project, card_type, title, summary, content_hash, source_ref,
            authorization_status, currentness, lifecycle_state)
           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
        (
            memory_id,
            "neurons",
            "decision",
            "t",
            "s",
            "sha256:" + "0" * 64,
            __import__("json").dumps([{"source_id": source_id}]),
            "active",
            "current",
            "accepted",
        ),
    )
    if source_hash is not None:
        conn.execute(
            "UPDATE memory_cards SET source_hash=%s WHERE memory_id=%s",
            (source_hash, memory_id),
        )


@pytest.mark.skipif(not PG_DSN, reason="LBRAIN_TEST_PG_DSN 미설정")
def test_integration_plan_check_execute(freshness_pg, capsys):
    dsn = freshness_pg
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": dsn}
    content = "chunk body"
    chunk_hash = None
    with psycopg.connect(dsn) as conn:
        chunk_hash = _insert_chunk(conn, "chunk-1", content)
        _insert_card(conn, "card-resolvable", "chunk-1", None)
        _insert_card(conn, "card-changed", "chunk-1", "sha256:" + "f" * 64)
        _insert_card(conn, "card-unresolved", "no-chunk", None)
        conn.commit()

    # plan mode (no DB)
    assert main([], environ=env) == 0
    capsys.readouterr()  # plan output is not the report under test
    # check mode
    assert main(["--check"], environ=env) == 0
    report = _payload(capsys)
    assert report["resolvable_cards"] == 2
    assert report["unresolved_active_cards"] == 1
    assert report["counts"].get("verified_current", 0) >= 1
    assert report["counts"].get("verified_changed", 0) >= 1

    # execute
    assert main(["--execute"], environ=env) == 0
    report2 = _payload(capsys)
    assert report2["mutation_performed"] is True
    assert report2["written"] == 2

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT freshness_state, source_hash FROM memory_cards WHERE memory_id='card-resolvable'"
        ).fetchone()
        assert row[0] == "verified_current"
        assert row[1] == chunk_hash
        row2 = conn.execute(
            "SELECT freshness_state, source_hash FROM memory_cards WHERE memory_id='card-changed'"
        ).fetchone()
        assert row2[0] == "verified_changed"
        assert row2[1] == "sha256:" + "f" * 64
        row3 = conn.execute(
            "SELECT freshness_state, freshness_checked_at FROM memory_cards WHERE memory_id='card-unresolved'"
        ).fetchone()
        assert row3[0] == "unchecked"
        assert row3[1] is None


@pytest.mark.skipif(not PG_DSN, reason="LBRAIN_TEST_PG_DSN 미설정")
def test_integration_verified_changed_sample(freshness_pg, capsys):
    dsn = freshness_pg
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": dsn}
    with psycopg.connect(dsn) as conn:
        _insert_chunk(conn, "chunk-2", "body2")
        _insert_card(conn, "card-vc", "chunk-2", "sha256:" + "9" * 64)
        conn.commit()
    assert main(["--check", "--interval-days", "7"], environ=env) == 0
    report = _payload(capsys)
    assert "card-vc" in [x.get("memory_id") for x in report["verified_changed_sample"]]

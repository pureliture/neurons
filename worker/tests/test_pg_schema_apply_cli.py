"""Contract tests for the approval-gated PostgreSQL schema apply command."""
from __future__ import annotations

import json
import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.postgres_store.schema_apply_cli import main


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_command_is_registered_as_a_human_gated_mutation():
    assert "pg-schema-apply" in COMMAND_HANDLERS
    assert COMMAND_METADATA["pg-schema-apply"] == {
        "runtime_category": "human_gated_schema_apply",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    }


def test_plan_mode_lists_review_bundle_objects(capsys):
    assert main([], environ={}) == 0

    report = _payload(capsys)

    assert report["schema_version"] == "lbrain_pg_schema_apply.v1"
    assert report["status"] == "planned"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["schema_sha256"].startswith("sha256:")
    assert "memory_cards" in report["tables"]
    assert "steward_bundle_decisions" in report["tables"]
    assert "idx_steward_bundle_decisions_scope" in report["indexes"]
    assert "steward_card_decisions.result_card" in report["columns"]
    assert "memory_cards.freshness_state" in report["columns"]
    assert "memory_cards.freshness_checked_at" in report["columns"]
    assert "memory_cards.freshness_next_check_after" in report["columns"]
    assert "memory_cards.source_hash" in report["columns"]
    assert "memory_cards.observed_at" in report["columns"]
    assert "idx_memory_cards_freshness" in report["indexes"]


def test_schema_manifest_captures_freshness_ddl():
    """FR-AGE-040~045: Verify schema_manifest regex captures freshness columns & index."""
    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
    from agent_knowledge.postgres_store.schema_apply_cli import schema_manifest

    sql = PgVectorStore.schema_sql()
    manifest = schema_manifest(sql)

    expected_columns = {
        "memory_cards.freshness_state",
        "memory_cards.freshness_checked_at",
        "memory_cards.freshness_next_check_after",
        "memory_cards.source_hash",
        "memory_cards.observed_at",
    }
    assert expected_columns.issubset(set(manifest["columns"]))
    assert "idx_memory_cards_freshness" in manifest["indexes"]


def test_production_execute_requires_approval_before_connecting(capsys):
    assert main(["--target", "production", "--execute"], environ={}) == 1

    report = _payload(capsys)

    assert report["status"] == "denied"
    assert report["reason"] == "production_schema_apply_requires_approval"
    assert {"approved", "approval_ref_sha256", "backup_ref", "rollback_ref"}.issubset(
        report["missing"]
    )
    assert report["network_used"] is False
    assert report["mutation_performed"] is False


def test_production_execute_rejects_approval_for_a_different_schema(capsys):
    assert main([], environ={}) == 0
    expected = _payload(capsys)["schema_sha256"]

    assert (
        main(
            [
                "--target",
                "production",
                "--execute",
                "--approved",
                "--approval-ref",
                "sha256:" + "0" * 64,
                "--backup-ref",
                "backup-20261022",
                "--rollback-ref",
                "rollback-20261022",
            ],
            environ={},
        )
        == 1
    )

    report = _payload(capsys)

    assert report["status"] == "denied"
    assert report["missing"] == ["approval_ref_matches_schema"]
    assert report["schema_sha256"] == expected
    assert report["network_used"] is False


def test_local_execute_rejects_remote_dsn_before_connecting(capsys):
    rc = main(
        ["--execute"],
        environ={
            "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://user:pass@db.example:5432/neurons"
        },
    )

    report = _payload(capsys)

    assert rc == 1
    assert report["status"] == "denied"
    assert report["reason"] == "local_test_target_requires_local_dsn"
    assert report["network_used"] is False


def test_check_requires_an_explicit_dsn(capsys):
    assert main(["--check"], environ={}) == 2

    report = _payload(capsys)

    assert report["status"] == "configuration_required"
    assert report["reason"] == "postgres_dsn_not_configured"
    assert report["network_used"] is False


@pytest.fixture
def empty_pg_schema():
    dsn = os.environ.get("LBRAIN_TEST_PG_DSN", "")
    if not dsn:
        pytest.skip("LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)")
    config = conninfo_to_dict(dsn)
    host = str(config.get("host") or "")
    if config.get("hostaddr") or not (
        host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
    ):
        pytest.fail("isolated PostgreSQL fixtures require an explicit local test instance")
    if not config.get("dbname") or not config.get("user") or config.get("service"):
        pytest.fail("isolated PostgreSQL fixtures require explicit database/user, no service")

    schema = "c1_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        for extension in ("vector", "uuid-ossp"):
            admin.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {} WITH SCHEMA public").format(
                    sql.Identifier(extension)
                )
            )
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_dsn = make_conninfo(
        dsn,
        options=f"-csearch_path={schema},public -cstatement_timeout=15000 -clock_timeout=10000",
        connect_timeout=5,
    )
    try:
        yield scoped_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            row = admin.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()
            assert row is not None and row[0] is None


def test_check_then_execute_applies_the_schema_idempotently(empty_pg_schema, capsys):
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": empty_pg_schema}

    assert main(["--check"], environ=env) == 1
    before = _payload(capsys)
    assert before["status"] == "missing"
    assert before["schema_name"].startswith("c1_test_")
    assert "table:memory_cards" in before["missing"]
    assert before["mutation_performed"] is False

    assert main(["--execute"], environ=env) == 0
    applied = _payload(capsys)
    assert applied["status"] == "applied"
    assert applied["schema_name"] == before["schema_name"]
    assert applied["mutation_performed"] is True
    assert applied["missing"] == []

    assert main(["--check"], environ=env) == 0
    after = _payload(capsys)
    assert after["status"] == "ok"
    assert after["schema_name"] == before["schema_name"]
    assert after["missing"] == []
    assert after["mutation_performed"] is False

    assert main(["--execute"], environ=env) == 0
    reapplied = _payload(capsys)
    assert reapplied["status"] == "applied"
    assert reapplied["missing"] == []

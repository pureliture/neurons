from __future__ import annotations

import sqlite3

from agent_knowledge.ledger import _migrate_backend_neutral_index_schema
from agent_knowledge.rag_ingress.state_db import _migrate_backend_neutral_delivery_schema


def test_ledger_migration_backfills_legacy_ragflow_columns_and_targets():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE knowledge_items (
            knowledge_id TEXT PRIMARY KEY,
            ragflow_dataset_id TEXT DEFAULT '',
            ragflow_document_id TEXT DEFAULT '',
            ragflow_run TEXT DEFAULT '',
            index_dataset_id TEXT DEFAULT '',
            index_run TEXT DEFAULT ''
        );
        INSERT INTO knowledge_items (
            knowledge_id, ragflow_dataset_id, ragflow_document_id, ragflow_run
        ) VALUES ('kn_ragflow', 'ds_ragflow', 'doc_ragflow', 'run_ragflow');
        INSERT INTO knowledge_items (
            knowledge_id, index_dataset_id, index_run
        ) VALUES ('kn_index', 'ds_index', 'run_index');

        CREATE TABLE index_targets (
            logical_name TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            chunk_method TEXT NOT NULL,
            metadata_policy_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            disabled_at TEXT DEFAULT ''
        );
        CREATE TABLE ragflow_datasets (
            logical_name TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            chunk_method TEXT NOT NULL,
            metadata_policy_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            disabled_at TEXT DEFAULT ''
        );
        INSERT INTO ragflow_datasets (
            logical_name, dataset_id, embedding_model, chunk_method,
            metadata_policy_version, contract_version, created_at, enabled, disabled_at
        ) VALUES (
            'transcript-memory', 'legacy_ds', 'bge-m3', 'naive',
            'metadata.v1', 'contract.v1', '2026-06-29T00:00:00Z', 1, ''
        );
        """
    )

    _migrate_backend_neutral_index_schema(connection)

    ragflow_row = connection.execute(
        """
        SELECT index_target_id, index_document_id, index_run_id
        FROM knowledge_items WHERE knowledge_id = 'kn_ragflow'
        """
    ).fetchone()
    index_row = connection.execute(
        """
        SELECT index_target_id, index_document_id, index_run_id
        FROM knowledge_items WHERE knowledge_id = 'kn_index'
        """
    ).fetchone()
    target_row = connection.execute(
        "SELECT dataset_id FROM index_targets WHERE logical_name = 'transcript-memory'"
    ).fetchone()

    assert dict(ragflow_row) == {
        "index_target_id": "ds_ragflow",
        "index_document_id": "doc_ragflow",
        "index_run_id": "run_ragflow",
    }
    assert dict(index_row) == {
        "index_target_id": "ds_index",
        "index_document_id": "",
        "index_run_id": "run_index",
    }
    assert target_row["dataset_id"] == "legacy_ds"


def test_state_db_migration_backfills_legacy_delivery_job_columns():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE delivery_jobs (
            job_id TEXT PRIMARY KEY,
            ragflow_dataset_id TEXT DEFAULT '',
            ragflow_document_id TEXT DEFAULT '',
            ragflow_run TEXT DEFAULT '',
            index_dataset_id TEXT DEFAULT '',
            index_run TEXT DEFAULT ''
        );
        INSERT INTO delivery_jobs (
            job_id, ragflow_dataset_id, ragflow_document_id, ragflow_run
        ) VALUES ('job_ragflow', 'ds_ragflow', 'doc_ragflow', 'run_ragflow');
        INSERT INTO delivery_jobs (
            job_id, index_dataset_id, index_run
        ) VALUES ('job_index', 'ds_index', 'run_index');
        """
    )

    _migrate_backend_neutral_delivery_schema(connection)

    ragflow_row = connection.execute(
        """
        SELECT index_target_id, index_document_id, index_run_id
        FROM delivery_jobs WHERE job_id = 'job_ragflow'
        """
    ).fetchone()
    index_row = connection.execute(
        """
        SELECT index_target_id, index_document_id, index_run_id
        FROM delivery_jobs WHERE job_id = 'job_index'
        """
    ).fetchone()

    assert dict(ragflow_row) == {
        "index_target_id": "ds_ragflow",
        "index_document_id": "doc_ragflow",
        "index_run_id": "run_ragflow",
    }
    assert dict(index_row) == {
        "index_target_id": "ds_index",
        "index_document_id": "",
        "index_run_id": "run_index",
    }

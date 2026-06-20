"""register_all is an all-or-nothing batch: a write failure on a later record
must roll back records already INSERTed earlier in the same transaction, so a
mid-batch DB error never leaves the catalog partially loaded.

This drives the rollback directly at the DB-write level (not just the
parse/validate stage in projection_cli._import_source_refs): the first record's
INSERT really executes, then a later record fails its write, and we assert the
catalog opened fresh has zero rows.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.models import SourceRefRecord


def _h(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _record(source_ref_id: str) -> SourceRefRecord:
    return SourceRefRecord(
        source_ref_id=source_ref_id,
        device_id_hash=_h("device-a"),
        root_id="project-root",
        relative_path_hash=_h(f"{source_ref_id}:path"),
        content_hash=_h(f"{source_ref_id}:content"),
        mtime="2026-06-18T00:00:00Z",
        size=100,
        sync_policy="derived_only",
        permission_scope="project",
        last_seen_at="2026-06-18T00:00:00Z",
    )


def test_register_all_rolls_back_first_record_when_later_write_fails(tmp_path: Path, monkeypatch):
    catalog = LedgerSourceRefCatalog(_ledger(tmp_path))

    first = _record("src_rollback_first")
    second = _record("src_rollback_second")

    original = LedgerSourceRefCatalog._register_on_connection
    calls = {"count": 0}

    def _failing_register(self, connection, record):
        calls["count"] += 1
        if calls["count"] == 1:
            # First record really lands an INSERT inside the open transaction.
            original(self, connection, record)
            return
        # Second record fails its DB write (constraint violation), so the
        # connection context manager rolls back the whole transaction — the
        # first INSERT must not survive.
        connection.execute(
            "INSERT INTO llm_brain_source_refs (source_ref_id) VALUES (NULL)"
        )

    monkeypatch.setattr(LedgerSourceRefCatalog, "_register_on_connection", _failing_register)

    with pytest.raises(sqlite3.IntegrityError):
        catalog.register_all([first, second])

    # Fresh read: the first record's INSERT was rolled back with the failed batch.
    assert catalog.get(first.source_ref_id) is None
    assert catalog.list_all() == []


def test_register_all_commits_whole_batch_on_clean_exit(tmp_path: Path):
    """Control: a clean batch commits every record (catalog is not empty)."""

    catalog = LedgerSourceRefCatalog(_ledger(tmp_path))
    first = _record("src_clean_first")
    second = _record("src_clean_second")

    catalog.register_all([first, second])

    assert catalog.get(first.source_ref_id) is not None
    assert {record.source_ref_id for record in catalog.list_all()} == {
        first.source_ref_id,
        second.source_ref_id,
    }

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


REQUEST_HASH = "sha256:" + "d" * 64
ACTOR_REF_HASH = "sha256:" + "c" * 64
FIRST_ATTEMPT_HASH = "sha256:" + "1" * 64
SECOND_ATTEMPT_HASH = "sha256:" + "2" * 64


def test_permission_audit_store_initializes_schema_before_audit_append(tmp_path):
    from agent_knowledge.permission_audit_store import PermissionAuditStore

    store_path = tmp_path / "permission-audit.sqlite3"
    store = PermissionAuditStore(store_path)

    with sqlite3.connect(store_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM permission_denials").fetchone()[0]

    assert row_count == 0
    assert store.readback(request_hash=REQUEST_HASH)["status"] == "missing"


def test_permission_audit_store_append_denied_once_is_restart_safe(tmp_path):
    from agent_knowledge.permission_audit_store import PermissionAuditStore

    store_path = tmp_path / "permission-audit.sqlite3"
    first = PermissionAuditStore(store_path).append_denied_once(
        request_hash=REQUEST_HASH,
        actor_ref_hash=ACTOR_REF_HASH,
        action="single_bounded_denial.v1",
        append_attempt_hash=FIRST_ATTEMPT_HASH,
    )
    duplicate = PermissionAuditStore(store_path).append_denied_once(
        request_hash=REQUEST_HASH,
        actor_ref_hash=ACTOR_REF_HASH,
        action="single_bounded_denial.v1",
        append_attempt_hash=SECOND_ATTEMPT_HASH,
    )

    assert first == {
        "status": "recorded",
        "append_count": 1,
        "stored_row_count": 1,
        "read_after_write_status": "validated",
        "request_hash": REQUEST_HASH,
        "append_attempt_hash": FIRST_ATTEMPT_HASH,
        "production_mutation_performed": False,
    }
    assert duplicate == {
        **first,
        "append_count": 0,
    }

    with sqlite3.connect(store_path) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        stored = connection.execute(
            "SELECT action, permission, authority_write_performed, production_mutation_performed "
            "FROM permission_denials"
        ).fetchall()
    assert table_names == {"permission_denials"}
    assert stored == [("single_bounded_denial.v1", "denied", 0, 0)]


def test_permission_audit_store_has_exactly_one_winner_under_concurrency(tmp_path):
    from agent_knowledge.permission_audit_store import PermissionAuditStore

    store_path = tmp_path / "permission-audit.sqlite3"

    def append_once(index):
        attempt_hash = (FIRST_ATTEMPT_HASH, SECOND_ATTEMPT_HASH)[index]
        return PermissionAuditStore(store_path).append_denied_once(
            request_hash=REQUEST_HASH,
            actor_ref_hash=ACTOR_REF_HASH,
            action="single_bounded_denial.v1",
            append_attempt_hash=attempt_hash,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append_once, range(2)))

    assert sorted(result["append_count"] for result in results) == [0, 1]
    assert all(result["stored_row_count"] == 1 for result in results)
    assert all(result["read_after_write_status"] == "validated" for result in results)


def test_permission_audit_store_fails_closed_on_preexisting_unsafe_row(tmp_path):
    from agent_knowledge.permission_audit_store import (
        PermissionAuditStore,
        PermissionAuditStoreError,
    )

    store_path = tmp_path / "permission-audit.sqlite3"
    with sqlite3.connect(store_path) as connection:
        connection.execute(
            """
            CREATE TABLE permission_denials (
                request_hash TEXT PRIMARY KEY,
                schema_version TEXT,
                actor_ref_hash TEXT,
                action TEXT,
                permission TEXT,
                authority_write_performed INTEGER,
                production_mutation_performed INTEGER,
                appended_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO permission_denials VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                REQUEST_HASH,
                "permission_audit_store_event.v1",
                ACTOR_REF_HASH,
                "single_bounded_denial.v1",
                "denied",
                2,
                2,
                "fixture",
            ),
        )

    with pytest.raises(PermissionAuditStoreError, match="permission-audit store"):
        PermissionAuditStore(store_path).append_denied_once(
            request_hash=REQUEST_HASH,
            actor_ref_hash=ACTOR_REF_HASH,
            action="single_bounded_denial.v1",
            append_attempt_hash=FIRST_ATTEMPT_HASH,
        )


def test_permission_audit_store_loopback_api_exposes_only_append_and_readback(tmp_path):
    from agent_knowledge.permission_audit_store import build_permission_audit_store_server

    server = build_permission_audit_store_server(
        database=tmp_path / "permission-audit.sqlite3",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, payload):
        request = urllib.request.Request(
            base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read())

    try:
        appended = post(
            "/append-denied-once",
            {
                "request_hash": REQUEST_HASH,
                "actor_ref_hash": ACTOR_REF_HASH,
                "action": "single_bounded_denial.v1",
                "append_attempt_hash": FIRST_ATTEMPT_HASH,
            },
        )
        readback = post("/readback", {"request_hash": REQUEST_HASH})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            post("/delete", {"request_hash": REQUEST_HASH})
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert appended["append_count"] == 1
    assert appended["stored_row_count"] == 1
    assert readback == {
        "schema_version": "permission_audit_store_readback.v2",
        "status": "recorded",
        "stored_row_count": 1,
        "request_hash": REQUEST_HASH,
        "append_attempt_hash": FIRST_ATTEMPT_HASH,
        "actor_ref_hash": ACTOR_REF_HASH,
        "action": "single_bounded_denial.v1",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
    }
def test_permission_audit_store_image_is_dedicated_and_loopback_only():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile.permission-audit-store").read_text(
        encoding="utf-8"
    )

    assert 'org.opencontainers.image.source="https://github.com/pureliture/neurons"' in dockerfile
    validation = "grep -Eq '^[0-9a-f]{40}$'"
    assert validation in dockerfile
    assert dockerfile.index(validation) < dockerfile.index("org.opencontainers.image.revision")
    assert "permission-audit-store" in dockerfile
    assert '"--host", "127.0.0.1"' in dockerfile
    assert "/app/state/ledger" not in dockerfile
    assert "NEURON_LEDGER_PG_DSN" not in dockerfile

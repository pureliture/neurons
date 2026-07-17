from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
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


def _append_result(*, append_count, actor_ref_hash=ACTOR_REF_HASH):
    return {
        "status": "recorded",
        "append_count": append_count,
        "stored_row_count": 1,
        "read_after_write_status": "validated",
        "request_hash": REQUEST_HASH,
        "append_attempt_hash": FIRST_ATTEMPT_HASH,
        "schema_version": "bounded_permission_denial_result.v1",
        "action": "single_bounded_denial.v1",
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "actor_ref_hash": actor_ref_hash,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }


def _valid_production_authority_arguments():
    return {
        "project": "scope-project",
        "target_object_id": "ko:RepoDocument:scope-target",
        "proposal_type": "propose_current",
        "proposed_object": {
            "object_id": "ko:RepoDocument:scope-target",
            "object_type": "RepoDocument",
        },
        "production_gate": {
            "approved": True,
            "approval_ref": "approved-ref",
            "scope": "single_project_single_object",
            "project": "scope-project",
            "max_objects": 1,
            "configured_deployed_mcp_identity_matches_source": True,
            "read_after_write_smoke_plan": True,
            "rollback_or_supersession_plan": True,
            "no_raw_private_evidence": True,
        },
    }


def _evaluate_production_authority(arguments):
    from agent_knowledge.production_authority_permission import (
        evaluate_production_object_authority_permission,
    )

    return evaluate_production_object_authority_permission(
        arguments,
        capability="production_object_authority_write",
        action="brain_object_proposal_create",
        service_write_enabled=True,
        ledger_read_only=False,
    )


def test_permission_audit_store_initializes_schema_before_audit_append(tmp_path):
    from agent_knowledge.permission_audit_store import PermissionAuditStore

    store_path = tmp_path / "permission-audit.sqlite3"
    store = PermissionAuditStore(store_path)

    with sqlite3.connect(store_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM permission_denials").fetchone()[0]

    assert row_count == 0
    assert store.readback(request_hash=REQUEST_HASH)["status"] == "missing"


def test_permission_audit_store_import_is_closed_under_stdlib_only_python():
    worker_root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, 'lib'); "
                "import agent_knowledge.permission_audit_store; "
                "print('permission-audit-import-ok')"
            ),
        ],
        cwd=worker_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "permission-audit-import-ok"


def test_permission_audit_store_uses_the_product_canonical_policy_at_permissive_baseline(
    tmp_path,
):
    from agent_knowledge import mcp_jsonrpc
    from agent_knowledge.llm_brain_core.objects.authority_policy import (
        allowed_object_class_gap,
        is_allowed_object_target as product_allowed_object_target,
    )
    from agent_knowledge.permission_audit_store import PermissionAuditStore
    from agent_knowledge.production_authority_permission import (
        evaluate_production_object_authority_permission,
        is_allowed_object_target as canonical_allowed_object_target,
    )

    store = PermissionAuditStore(tmp_path / "permission-audit.sqlite3")
    decision = evaluate_production_object_authority_permission(
        {},
        capability="production_object_authority_write",
        action="single_bounded_denial.v1",
        service_write_enabled=True,
        ledger_read_only=False,
    )

    assert store._denial_policy_evaluator is evaluate_production_object_authority_permission
    assert (
        mcp_jsonrpc.evaluate_production_object_authority_permission
        is evaluate_production_object_authority_permission
    )
    assert product_allowed_object_target is canonical_allowed_object_target
    assert decision["allowed"] is False
    assert "service_production_object_authority_write_flag" not in decision[
        "missing_gate_evidence"
    ]
    assert "writable_ledger" not in decision["missing_gate_evidence"]
    assert {
        "approved",
        "approval_ref",
        "single_project_single_object_scope",
        "project_scope_match",
        "max_objects_1",
        "configured_deployed_mcp_identity_matches_source",
        "read_after_write_smoke_plan",
        "rollback_or_supersession_plan",
        "no_raw_private_evidence",
        allowed_object_class_gap(),
    }.issubset(decision["missing_gate_evidence"])


def test_canonical_policy_rejects_overlength_project_scope_prefix_collision():
    arguments = _valid_production_authority_arguments()
    arguments["project"] = "p" * 120 + "source-suffix"
    arguments["production_gate"]["project"] = "p" * 120 + "gate-suffix"

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is False
    assert "project_scope_match" in decision["missing_gate_evidence"]


@pytest.mark.parametrize(
    "project",
    [
        "scope  project",
        "/Users/example/private/scope-project",
    ],
)
def test_canonical_policy_rejects_project_identity_normalization_change(project):
    arguments = _valid_production_authority_arguments()
    arguments["project"] = project
    arguments["production_gate"]["project"] = project

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is False
    assert "project_scope_match" in decision["missing_gate_evidence"]


def test_canonical_policy_rejects_project_strip_normalization_change():
    arguments = _valid_production_authority_arguments()
    arguments["project"] = " scope-project "

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is False
    assert "project_scope_match" in decision["missing_gate_evidence"]


def test_canonical_policy_rejects_target_and_proposed_id_prefix_collision():
    arguments = _valid_production_authority_arguments()
    common = "ko:RepoDocument:" + "x" * 180
    arguments["target_object_id"] = common + "source-suffix"
    arguments["proposed_object"]["object_id"] = common + "proposal-suffix"

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is False
    assert "target_object_id_exact" in decision["missing_gate_evidence"]
    assert "proposed_object_id_exact" in decision["missing_gate_evidence"]
    assert "proposed_object_target_match" in decision["missing_gate_evidence"]


def test_canonical_policy_rejects_target_identity_normalization_change():
    arguments = _valid_production_authority_arguments()
    arguments["target_object_id"] = "ko:RepoDocument:scope  target"
    arguments["proposed_object"]["object_id"] = "ko:RepoDocument:scope  target"

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is False
    assert "target_object_id_exact" in decision["missing_gate_evidence"]
    assert "proposed_object_id_exact" in decision["missing_gate_evidence"]


def test_canonical_policy_rejects_overlength_approval_reference():
    arguments = _valid_production_authority_arguments()
    arguments["production_gate"]["approval_ref"] = "a" * 161

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is False
    assert "approval_ref" in decision["missing_gate_evidence"]


def test_canonical_policy_preserves_exact_valid_scope_and_target_identity():
    arguments = _valid_production_authority_arguments()

    decision = _evaluate_production_authority(arguments)

    assert decision["allowed"] is True
    assert decision["missing_gate_evidence"] == []
    assert decision["project"] == arguments["project"]
    assert decision["target_object_id"] == arguments["target_object_id"]


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

    assert first == _append_result(append_count=1)
    assert duplicate == _append_result(append_count=0)

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


def test_permission_audit_store_elects_one_bound_pod_winner_before_policy_evaluation(
    tmp_path,
):
    from agent_knowledge.permission_audit_store import (
        PermissionAuditStore,
        PermissionAuditStoreError,
    )
    from agent_knowledge.production_authority_permission import (
        evaluate_production_object_authority_permission,
    )

    store_path = tmp_path / "permission-audit.sqlite3"
    policy_calls = []
    policy_lock = threading.Lock()
    barrier = threading.Barrier(3)

    def counted_canonical_policy(arguments, **kwargs):
        with policy_lock:
            policy_calls.append((arguments, kwargs))
        return evaluate_production_object_authority_permission(arguments, **kwargs)

    store = PermissionAuditStore(
        store_path,
        denial_policy_evaluator=counted_canonical_policy,
    )

    def append(request_hash, attempt_hash):
        barrier.wait(timeout=5)
        try:
            return store.append_denied_once(
                request_hash=request_hash,
                actor_ref_hash=ACTOR_REF_HASH,
                action="single_bounded_denial.v1",
                append_attempt_hash=attempt_hash,
            )
        except PermissionAuditStoreError:
            return {"status": "rejected"}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(append, REQUEST_HASH, FIRST_ATTEMPT_HASH),
            executor.submit(append, REQUEST_HASH, SECOND_ATTEMPT_HASH),
            executor.submit(
                append,
                "sha256:" + "e" * 64,
                "sha256:" + "3" * 64,
            ),
        ]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result.get("append_count", 0) for result in results) == 1
    assert policy_calls == [
        (
            {},
            {
                "capability": "production_object_authority_write",
                "action": "single_bounded_denial.v1",
                "service_write_enabled": True,
                "ledger_read_only": False,
            },
        )
    ]
    with sqlite3.connect(store_path) as connection:
        assert connection.execute("SELECT count(*) FROM permission_denials").fetchone()[0] == 1

    different_actor = store.append_denied_once(
        request_hash="sha256:" + "f" * 64,
        actor_ref_hash="sha256:" + "b" * 64,
        action="single_bounded_denial.v1",
        append_attempt_hash="sha256:" + "4" * 64,
    )
    assert different_actor["append_count"] == 1
    assert len(policy_calls) == 2
    assert policy_calls[1] == policy_calls[0]


def test_permission_audit_store_rejects_changed_operation_or_actor_before_append(
    tmp_path,
):
    from agent_knowledge.permission_audit_store import (
        PermissionAuditStore,
        PermissionAuditStoreError,
    )
    from agent_knowledge.production_authority_permission import (
        evaluate_production_object_authority_permission,
    )

    policy_calls = []

    def counted_canonical_policy(arguments, **kwargs):
        policy_calls.append((arguments, kwargs))
        return evaluate_production_object_authority_permission(arguments, **kwargs)

    store_path = tmp_path / "permission-audit.sqlite3"
    store = PermissionAuditStore(
        store_path,
        denial_policy_evaluator=counted_canonical_policy,
    )
    store.append_denied_once(
        request_hash=REQUEST_HASH,
        actor_ref_hash=ACTOR_REF_HASH,
        action="single_bounded_denial.v1",
        append_attempt_hash=FIRST_ATTEMPT_HASH,
    )

    with pytest.raises(
        PermissionAuditStoreError,
        match="permission-audit bound actor already consumed",
    ):
        store.append_denied_once(
            request_hash=REQUEST_HASH,
            actor_ref_hash="sha256:" + "b" * 64,
            action="single_bounded_denial.v1",
            append_attempt_hash=SECOND_ATTEMPT_HASH,
        )
    with pytest.raises(
        PermissionAuditStoreError,
        match="permission-audit bound actor already consumed",
    ):
        store.append_denied_once(
            request_hash="sha256:" + "e" * 64,
            actor_ref_hash=ACTOR_REF_HASH,
            action="single_bounded_denial.v1",
            append_attempt_hash=SECOND_ATTEMPT_HASH,
        )

    assert len(policy_calls) == 1
    with sqlite3.connect(store_path) as connection:
        stored = connection.execute(
            "SELECT request_hash, actor_ref_hash FROM permission_denials"
        ).fetchall()
    assert stored == [(REQUEST_HASH, ACTOR_REF_HASH)]


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
    install = "RUN pip install --no-cache-dir --no-deps ."
    entrypoint_smoke = "RUN permission-audit-store --help"
    assert install in dockerfile
    assert entrypoint_smoke in dockerfile
    assert dockerfile.index(install) < dockerfile.index(entrypoint_smoke)
    assert '"--host", "127.0.0.1"' in dockerfile
    assert "/app/state/ledger" not in dockerfile
    assert "NEURON_LEDGER_PG_DSN" not in dockerfile

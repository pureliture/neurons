from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from types import SimpleNamespace

import pytest

from agent_knowledge.local_embedding.profile import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
)


@pytest.mark.parametrize(
    ("scope", "target_types", "include_cards", "include_sessions"),
    [
        ("cards", ("memory_card",), True, False),
        ("sessions", ("session_chunk",), False, True),
        ("all", ("memory_card", "session_chunk"), True, True),
    ],
)
def test_pg_vector_scope_is_bound_to_plan_and_approval(
    scope, target_types, include_cards, include_sessions
):
    from agent_knowledge.local_embedding import operator_runner

    plan = operator_runner.build_backfill_plan(
        lane="pg", phase="vectors", vector_scope=scope
    )
    other_plans = [
        operator_runner.build_backfill_plan(
            lane="pg", phase="vectors", vector_scope=other_scope
        )
        for other_scope in {"cards", "sessions", "all"} - {scope}
    ]

    assert plan.vector_scope == scope
    assert operator_runner._approval_target(plan)["vector_scope"] == scope
    assert all(
        plan.target_fingerprint != other.target_fingerprint
        and plan.selection_fingerprint != other.selection_fingerprint
        for other in other_plans
    )
    assert (target_types, include_cards, include_sessions) == {
        "cards": (("memory_card",), True, False),
        "sessions": (("session_chunk",), False, True),
        "all": (("memory_card", "session_chunk"), True, True),
    }[scope]

    with pytest.raises(ValueError, match="scope"):
        operator_runner.build_backfill_plan(lane="pg", phase="vectors")
    with pytest.raises(ValueError, match="scope"):
        operator_runner.build_backfill_plan(
            lane="pg", phase="vectors", vector_scope="everything"
        )
    with pytest.raises(ValueError, match="scope"):
        operator_runner.build_backfill_plan(
            lane="pg", phase="receipts", vector_scope="cards"
        )


def test_pg_vector_scope_approval_rejects_target_and_argv_mismatch():
    from agent_knowledge.local_embedding import operator_runner

    plan = operator_runner.build_backfill_plan(
        lane="pg", phase="vectors", vector_scope="cards"
    )
    argv = [
        "--lane", "pg", "--phase", "vectors", "--vector-scope", "cards", "--execute"
    ]
    approval = _approval_payload(plan, argv)

    operator_runner.validate_approval_artifact(
        approval,
        plan=plan,
        command_argv=argv,
    )

    wrong_target = _approval_payload(plan, argv)
    wrong_target["target"]["vector_scope"] = "sessions"
    with pytest.raises(operator_runner.ApprovalValidationError) as target_error:
        operator_runner.validate_approval_artifact(
            wrong_target,
            plan=plan,
            command_argv=argv,
        )
    assert target_error.value.reason_code == "approval_target_mismatch"

    changed_argv = [
        "--lane", "pg", "--phase", "vectors", "--vector-scope", "sessions", "--execute"
    ]
    with pytest.raises(operator_runner.ApprovalValidationError) as argv_error:
        operator_runner.validate_approval_artifact(
            approval,
            plan=plan,
            command_argv=changed_argv,
        )
    assert argv_error.value.reason_code == "approval_argv_mismatch"


@pytest.mark.parametrize(
    ("phase", "scope"),
    [("vectors", "cards"), ("vectors", "sessions"), ("vectors", "all"), ("receipts", None)],
)
def test_pg_phase_dry_run_is_network_and_mutation_free(
    phase, scope, tmp_path, monkeypatch, capsys
):
    from agent_knowledge.local_embedding import operator_runner

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must not build clients or connect")

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", forbidden)
    argv = ["--lane", "pg", "--phase", phase]
    if scope is not None:
        argv.extend(["--vector-scope", scope])
    else:
        argv.extend(
            [
                "--receipt-checkpoint", str(tmp_path / "cursor.json"),
                "--receipt-run-id", "receipt-dry-run",
                "--receipt-source-identity", "sha256:" + "a" * 64,
            ]
        )

    assert operator_runner.main(argv, environ={}) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["phase"] == phase
    assert report.get("vector_scope") == scope
    assert report["network_used"] is False
    assert report["mutation_performed"] is False
    assert report["model_calls"] == 0


@pytest.mark.parametrize(
    ("scope", "include_cards", "include_sessions", "target_types"),
    [
        ("cards", True, False, ("memory_card",)),
        ("sessions", False, True, ("session_chunk",)),
        ("all", True, True, ("memory_card", "session_chunk")),
    ],
)
def test_pg_transition_forwards_scope_to_enqueue_and_worker_claim(
    scope, include_cards, include_sessions, target_types, monkeypatch
):
    from agent_knowledge.local_embedding import transition
    from agent_knowledge.postgres_store import outbox_worker

    observed = {}
    monkeypatch.setattr(
        transition,
        "enqueue_missing_pg_local_embeddings",
        lambda *_args, **kwargs: (
            observed.update(enqueue=kwargs.copy()) or (0, 0)
        ),
    )

    class _Worker:
        def __init__(self, **kwargs):
            observed["worker"] = kwargs
            self.last_batch_completed = 0
            self.last_batch_stale = 0
            self.last_batch_failed = 0

        def run_once(self, **_kwargs):
            return 0

    monkeypatch.setattr(outbox_worker, "LocalEmbeddingOutboxWorker", _Worker)
    monkeypatch.setattr(
        transition,
        "inspect_pg_local_embedding_state",
        lambda *_args, **_kwargs: transition.PgBackfillProgress(),
    )

    transition.backfill_pg_local_embeddings(
        object(),
        lambda _text: [0.0] * LOCAL_EMBEDDING_DIM,
        vector_scope=scope,
    )

    assert observed["enqueue"]["include_cards"] is include_cards
    assert observed["enqueue"]["include_sessions"] is include_sessions
    assert observed["worker"]["target_types"] == target_types


@pytest.mark.parametrize(
    ("scope", "expected_type"),
    [("cards", "memory_card"), ("sessions", "session_chunk"), ("all", None)],
)
def test_pg_enqueue_query_only_selects_approved_vector_kinds(scope, expected_type):
    from contextlib import nullcontext

    from agent_knowledge.local_embedding.transition import enqueue_missing_pg_local_embeddings

    class _Cursor:
        def __init__(self):
            self.queries = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params):
            self.queries.append(query)

        def fetchall(self):
            query = self.queries[-1]
            if "FROM memory_cards mc" in query:
                return [("card-1", "sha256:card", "summary", "title", {})]
            if "FROM session_memory_chunks smc" in query:
                return [("chunk-1", "sha256:session", "session text")]
            raise AssertionError("unexpected enqueue query")

    cursor = _Cursor()
    store = SimpleNamespace(
        _scope=lambda **_kwargs: nullcontext(SimpleNamespace(cursor=lambda: cursor)),
        enqueued=[],
    )
    store.enqueue_local_outbox = lambda **kwargs: store.enqueued.append(kwargs)
    include_cards = scope in {"cards", "all"}
    include_sessions = scope in {"sessions", "all"}

    enqueue_missing_pg_local_embeddings(
        store,
        include_cards=include_cards,
        include_sessions=include_sessions,
    )

    enqueued_types = {item["target_type"] for item in store.enqueued}
    assert enqueued_types == (
        {expected_type} if expected_type else {"memory_card", "session_chunk"}
    )
    assert ("FROM memory_cards mc" in " ".join(cursor.queries)) is include_cards
    assert ("FROM session_memory_chunks smc" in " ".join(cursor.queries)) is include_sessions


@pytest.mark.parametrize(
    ("target_types", "expected_types"),
    [
        (("memory_card",), ["memory_card"]),
        (("session_chunk",), ["session_chunk"]),
        (("memory_card", "session_chunk"), ["memory_card", "session_chunk"]),
    ],
)
def test_pg_outbox_claim_query_is_filtered_to_approved_target_types(
    target_types, expected_types
):
    from contextlib import nullcontext

    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

    class _Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            self.calls.append((query, params))

        def fetchall(self):
            return []

    cursor = _Cursor()
    db = SimpleNamespace(cursor=lambda: cursor)
    store = PgVectorStore.__new__(PgVectorStore)
    store._scope = lambda **_kwargs: nullcontext(db)

    PgVectorStore.claim_local_outbox_leases(
        store,
        "scope-test-worker",
        batch_size=2,
        target_types=target_types,
    )

    query, params = cursor.calls[0]
    assert "target_type = ANY(%s)" in query
    assert list(params[1]) == expected_types


def test_default_runner_dry_run_has_no_network_write_or_model_calls(monkeypatch, capsys):
    from agent_knowledge.local_embedding import operator_runner

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must not build clients or connect")

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", forbidden)
    assert operator_runner.main(
        ["--lane", "pg", "--phase", "vectors", "--vector-scope", "all"],
        environ={},
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["network_used"] is False
    assert report["mutation_performed"] is False
    assert report["model_calls"] == 0
    assert report["selection_fingerprint"].startswith("sha256:")
    assert report["target_fingerprint"].startswith("sha256:")


def test_dry_run_does_not_create_approved_receipt_checkpoint_path(tmp_path, capsys):
    from agent_knowledge.local_embedding import operator_runner

    checkpoint = tmp_path / "unused" / "cursor.json"
    argv = [
        "--lane", "pg",
        "--phase", "receipts",
        "--receipt-checkpoint", str(checkpoint),
        "--receipt-run-id", "receipt-dry-run",
        "--receipt-source-identity", "sha256:" + "a" * 64,
    ]

    assert operator_runner.main(argv, environ={}) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert checkpoint.exists() is False
    assert checkpoint.parent.exists() is False


def test_pg_approval_binds_exact_receipt_checkpoint_path(tmp_path):
    from agent_knowledge.local_embedding import operator_runner

    checkpoint = tmp_path / "secure" / "receipt-cursor.json"
    other_checkpoint = tmp_path / "other" / "receipt-cursor.json"
    argv = [
        "--lane", "pg",
        "--phase", "receipts",
        "--receipt-checkpoint", str(checkpoint),
        "--receipt-run-id", "receipt-fixture",
        "--receipt-source-identity", "sha256:" + "a" * 64,
        "--execute",
    ]
    plan = operator_runner.build_backfill_plan(
        lane="pg",
        phase="receipts",
        receipt_checkpoint_path=str(checkpoint),
        receipt_run_id="receipt-fixture",
        receipt_source_identity="sha256:" + "a" * 64,
    )
    other_plan = operator_runner.build_backfill_plan(
        lane="pg",
        phase="receipts",
        receipt_checkpoint_path=str(other_checkpoint),
        receipt_run_id="receipt-fixture",
        receipt_source_identity="sha256:" + "a" * 64,
    )
    approval = _approval_payload(plan, argv)

    operator_runner.validate_approval_artifact(
        approval,
        plan=plan,
        command_argv=argv,
    )
    with pytest.raises(operator_runner.ApprovalValidationError) as exc:
        operator_runner.validate_approval_artifact(
            approval,
            plan=other_plan,
            command_argv=argv,
        )
    assert exc.value.reason_code == "approval_target_mismatch"


def test_receipt_checkpoint_storage_is_durable_private_and_rejects_shared_paths(
    tmp_path,
    monkeypatch,
):
    import os
    import stat

    from agent_knowledge.local_embedding import operator_runner

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(
        operator_runner.tempfile,
        "gettempdir",
        lambda: str(tmp_path / "system-temp"),
    )
    path = home / ".config" / "neurons" / "receipt-cursor.json"
    resolved = operator_runner._prepare_receipt_checkpoint_path(str(path), home=home)

    assert resolved == path.resolve()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.parent.stat().st_uid == os.getuid()

    with pytest.raises(ValueError, match="receipt_checkpoint_storage_invalid"):
        operator_runner._prepare_receipt_checkpoint_path(
            str(tmp_path / "outside" / "cursor.json"),
            home=home,
        )

    shared = home / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(ValueError, match="receipt_checkpoint_storage_not_private"):
        operator_runner._prepare_receipt_checkpoint_path(
            str(shared / "cursor.json"),
            home=home,
        )


def test_receipt_checkpoint_modes_ignore_restrictive_umask(tmp_path, monkeypatch):
    import os
    import stat
    from pathlib import Path

    from agent_knowledge.local_embedding import operator_runner, transition

    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        operator_runner.tempfile,
        "gettempdir",
        lambda: str(tmp_path / "system-temp"),
    )
    path = home / ".config" / "neurons" / "receipt-cursor.json"

    previous_umask = os.umask(0o777)
    try:
        resolved = operator_runner._prepare_receipt_checkpoint_path(path)
        transition._write_pg_receipt_checkpoint(resolved, {"schema_version": "fixture"})
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _approval_payload(plan, argv, *, overrides=None):
    from agent_knowledge.local_embedding import operator_runner

    limits = dict(plan.limits)
    payload = {
        "schema_version": operator_runner.APPROVAL_SCHEMA_VERSION,
        "operation": "local_embedding_backfill",
        "operator_approval": {"approved": True},
        "redaction_required": True,
        "rollback_or_abort_criteria": "stop on any stale, failed, or unverifiable result",
        "time_budget_semantics": "between_batches_and_receipt_pages_only",
        "target": operator_runner._approval_target(plan),
        "limits": limits,
        "command": {"argv": list(argv)},
    }
    for key, value in (overrides or {}).items():
        payload[key] = value
    return payload


def _private_pg_checkpoint(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_knowledge.local_embedding import operator_runner

    home = tmp_path / "operator-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        operator_runner.tempfile,
        "gettempdir",
        lambda: str(tmp_path / "system-temp"),
    )
    return home / ".config" / "neurons" / "receipt-cursor.json"


def _pg_plan_for_empty_environment(operator_runner, checkpoint):
    environment = {}
    return operator_runner.build_backfill_plan(
        lane="pg",
        phase="receipts",
        target_identity=operator_runner._resolve_target_identity(
            "pg", environment, phase="receipts"
        ),
        receipt_checkpoint_path=checkpoint,
        receipt_run_id="receipt-fixture",
        receipt_source_identity="sha256:" + "a" * 64,
    )


def test_approval_target_or_exact_argv_mismatch_fails_before_client_creation(
    tmp_path, monkeypatch, capsys
):
    from agent_knowledge.local_embedding import operator_runner

    checkpoint = _private_pg_checkpoint(tmp_path, monkeypatch)
    argv = [
        "--lane",
        "pg",
        "--phase",
        "receipts",
        "--receipt-checkpoint",
        str(checkpoint),
        "--receipt-run-id",
        "receipt-fixture",
        "--receipt-source-identity",
        "sha256:" + "a" * 64,
        "--execute",
        "--approval",
        str(tmp_path / "approval.json"),
    ]
    plan = _pg_plan_for_empty_environment(operator_runner, checkpoint)
    payload = _approval_payload(plan, argv)
    payload["target"]["profile_id"] = "different-profile"
    (tmp_path / "approval.json").write_text(json.dumps(payload), encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("approval mismatch must be rejected before client construction")

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", forbidden)
    assert operator_runner.main(argv, environ={}) == 2
    report = json.loads(capsys.readouterr().err)
    assert report["status"] == "approval_rejected"
    assert report["reason_code"] == "approval_target_mismatch"


def test_approval_exact_argv_mismatch_fails_before_client_creation(tmp_path, monkeypatch, capsys):
    from agent_knowledge.local_embedding import operator_runner

    checkpoint = _private_pg_checkpoint(tmp_path, monkeypatch)
    approval_path = tmp_path / "approval.json"
    approved_argv = [
        "--lane",
        "pg",
        "--phase",
        "receipts",
        "--receipt-checkpoint",
        str(checkpoint),
        "--receipt-run-id",
        "receipt-fixture",
        "--receipt-source-identity",
        "sha256:" + "a" * 64,
        "--execute",
        "--approval",
        str(approval_path),
    ]
    actual_argv = [*approved_argv, "--max-batches", "9"]
    plan = _pg_plan_for_empty_environment(operator_runner, checkpoint)
    approval_path.write_text(
        json.dumps(_approval_payload(plan, approved_argv)), encoding="utf-8"
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("argv mismatch must be rejected before client construction")

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", forbidden)
    assert operator_runner.main(actual_argv, environ={}) == 2
    report = json.loads(capsys.readouterr().err)
    assert report["status"] == "approval_rejected"
    assert report["reason_code"] == "approval_argv_mismatch"


def test_valid_approval_builds_clients_only_after_validation(tmp_path, monkeypatch, capsys):
    from agent_knowledge.local_embedding import operator_runner
    from agent_knowledge.local_embedding import pg_operator_runner

    checkpoint = _private_pg_checkpoint(tmp_path, monkeypatch)
    approval_path = tmp_path / "approval.json"
    argv = [
        "--lane",
        "pg",
        "--phase",
        "receipts",
        "--receipt-checkpoint",
        str(checkpoint),
        "--receipt-run-id",
        "receipt-fixture",
        "--receipt-source-identity",
        "sha256:" + "a" * 64,
        "--execute",
        "--approval",
        str(approval_path),
    ]
    plan = _pg_plan_for_empty_environment(operator_runner, checkpoint)
    approval_path.write_text(json.dumps(_approval_payload(plan, argv)), encoding="utf-8")
    events = []

    def build_clients(received_plan, environ):
        assert received_plan == plan
        assert environ == {}
        events.append("clients")
        return {
            "store": object(),
            "couchdb_store": object(),
            "checkpoint_path": checkpoint,
        }

    def run_pg(received_plan, **clients):
        assert received_plan == plan
        assert set(clients) == {"store", "couchdb_store", "checkpoint_path"}
        assert clients["checkpoint_path"] == checkpoint
        events.append("runner")
        return {"status": "completed", "success": True}

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", build_clients)
    monkeypatch.setattr(pg_operator_runner, "run_pg_backfill_phase", run_pg)

    assert operator_runner.main(argv, environ={}) == 0
    assert events == ["clients", "runner"]
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert checkpoint.parent.exists()
    assert checkpoint.exists() is False


def test_missing_receipt_checkpoint_fails_before_client_construction(tmp_path, monkeypatch, capsys):
    from agent_knowledge.local_embedding import operator_runner

    argv = [
        "--lane", "pg",
        "--phase", "receipts",
        "--receipt-run-id", "receipt-fixture",
        "--receipt-source-identity", "sha256:" + "a" * 64,
        "--execute",
        "--approval", str(tmp_path / "approval.json"),
    ]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("missing checkpoint must fail before client construction")

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", forbidden)
    assert operator_runner.main(argv, environ={}) == 2
    report = json.loads(capsys.readouterr().err)
    assert report["status"] == "plan_rejected"
    assert report["reason_code"] == "receipt_checkpoint_required"


def test_pg_plan_rejects_unsafe_serial_lease_before_client_construction(
    tmp_path, monkeypatch, capsys
):
    from agent_knowledge.local_embedding import operator_runner

    default_plan = operator_runner.build_backfill_plan(
        lane="pg", phase="vectors", vector_scope="all"
    )
    assert default_plan.batch_size == 1
    assert default_plan.lease_seconds == 120
    assert default_plan.minimum_lease_seconds == 115
    assert default_plan.max_embedding_input_bytes == 24567

    with pytest.raises(ValueError, match="lease"):
        operator_runner.build_backfill_plan(
            lane="pg", phase="vectors", vector_scope="all",
            batch_size=2, lease_seconds=120,
        )
    approved_override = operator_runner.build_backfill_plan(
        lane="pg",
        phase="vectors",
        vector_scope="all",
        batch_size=2,
        lease_seconds=230,
    )
    assert approved_override.minimum_lease_seconds == 230

    checkpoint = _private_pg_checkpoint(tmp_path, monkeypatch)
    argv = [
        "--lane",
        "pg",
        "--phase",
        "vectors",
        "--vector-scope",
        "all",
        "--batch-size",
        "2",
        "--lease-seconds",
        "120",
        "--execute",
        "--approval",
        str(tmp_path / "approval.json"),
    ]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe lease must fail before client construction")

    monkeypatch.setattr(operator_runner, "_build_runtime_clients", forbidden)
    assert operator_runner.main(argv, environ={}) == 2
    report = json.loads(capsys.readouterr().err)
    assert report["status"] == "plan_rejected"
    assert report["network_used"] is False
    assert checkpoint.parent.exists() is False


def test_plan_and_approval_enforce_hard_batch_time_and_lease_caps(tmp_path):
    from agent_knowledge.local_embedding import operator_runner

    with pytest.raises(ValueError, match="batch"):
        operator_runner.build_backfill_plan(lane="neo4j", batch_size=33)
    with pytest.raises(ValueError, match="lease"):
        operator_runner.build_backfill_plan(
            lane="pg", phase="vectors", vector_scope="all", lease_seconds=301
        )
    with pytest.raises(ValueError, match="time"):
        operator_runner.build_backfill_plan(
            lane="pg", phase="vectors", vector_scope="all", max_seconds=601
        )

    plan = operator_runner.build_backfill_plan(
        lane="pg", phase="vectors", vector_scope="all", target_identity={}
    )
    argv = ["--lane", "pg", "--phase", "vectors", "--vector-scope", "all", "--execute"]
    payload = _approval_payload(plan, argv)
    payload["limits"]["batch_size"] = 33
    with pytest.raises(operator_runner.ApprovalValidationError) as exc:
        operator_runner.validate_approval_artifact(payload, plan=plan, command_argv=argv)
    assert exc.value.reason_code == "approval_limit_exceeded"


def test_neo4j_preflight_is_show_only_and_fails_closed_when_schema_is_missing():
    from agent_knowledge.local_embedding.operator_runner import _preflight_neo4j

    class _Session:
        def __init__(self):
            self.queries = []

        def run(self, query, **_params):
            self.queries.append(query)
            if "SHOW CONSTRAINTS" in query:
                return SimpleNamespace(single=lambda: {"names": []})
            if "SHOW INDEXES" in query:
                return SimpleNamespace(single=lambda: {"names": []})
            raise AssertionError("Neo4j preflight must not execute DDL or writes")

    session = _Session()
    with pytest.raises(RuntimeError, match="neo4j_safety_schema_incomplete"):
        _preflight_neo4j(session)

    assert len(session.queries) == 2
    assert all(query.lstrip().startswith("SHOW ") for query in session.queries)


def test_graph_journal_can_skip_schema_creation_after_show_only_preflight(monkeypatch):
    from agent_knowledge.llm_brain_core import local_graph_safety

    class _PgStore:
        def __init__(self):
            self.upserts = []

        def get_graph_operation_journal(self, _operation_id):
            return None

        def upsert_graph_operation_journal(self, **kwargs):
            self.upserts.append(kwargs)

    class _Session:
        def run(self, *_args, **_kwargs):
            raise AssertionError("journal must not issue Neo4j schema DDL")

    lease = SimpleNamespace(group_id="fixture", epoch=1)
    monkeypatch.setattr(
        local_graph_safety,
        "acquire_group_fence_lease",
        lambda *_args, **_kwargs: lease,
    )
    monkeypatch.setattr(
        local_graph_safety,
        "ensure_neo4j_safety_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("schema creation is outside this runner's approval")
        ),
    )
    pg_store = _PgStore()
    coordinator = local_graph_safety.GraphOperationJournalCoordinator(
        pg_store=pg_store,
        ensure_schema=False,
    )

    coordinator.begin_operation(
        _Session(),
        operation_id="operation-fixture",
        group_id="fixture",
        operation_type="graph_local_embedding_backfill",
        lease_owner="runner-fixture",
        payload={"profile_id": LOCAL_EMBEDDING_PROFILE_ID},
    )

    assert len(pg_store.upserts) == 1
    assert pg_store.upserts[0]["state"] == "neo4j_fenced"


def test_neo4j_runner_requires_pg_journal_store_before_preflight(monkeypatch):
    from agent_knowledge.local_embedding import operator_runner

    def forbidden(*_args, **_kwargs):
        raise AssertionError("missing PG journal target must fail before graph access")

    monkeypatch.setattr(operator_runner, "_preflight_neo4j", forbidden)
    plan = operator_runner.build_backfill_plan(lane="neo4j")

    with pytest.raises(RuntimeError, match="graph_operation_journal_store_required"):
        operator_runner.run_neo4j_backfill(
            plan,
            session=object(),
            embedder=object(),
            pg_store=None,
        )


def test_neo4j_runtime_clients_build_pg_store_for_operation_journal(monkeypatch):
    import sys

    from agent_knowledge.local_embedding import operator_runner
    from agent_knowledge.model_connectors import policy
    from agent_knowledge.postgres_store import pgvector_store

    env = {
        "LLM_BRAIN_NEO4J_URI": "neo4j+s://graph.example:7687",
        "LLM_BRAIN_NEO4J_DATABASE": "brain",
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://pg-user:private-pg-value@pg.example:5432/brain",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "http://embed.example/v1",
        "LLM_BRAIN_EMBEDDING_MODEL": LOCAL_EMBEDDING_MODEL_ALIAS,
    }
    plan = operator_runner.build_backfill_plan(
        lane="neo4j",
        target_identity=operator_runner._resolve_target_identity("neo4j", env),
    )
    constructed = {}

    class _PgStore:
        def __init__(self, **kwargs):
            constructed["pg_kwargs"] = kwargs

    class _Session:
        def close(self):
            constructed["session_closed"] = True

    class _Driver:
        def session(self, *, database=None):
            constructed["database"] = database
            return _Session()

        def close(self):
            constructed["driver_closed"] = True

    class _Embedder:
        def close(self):
            constructed["embedder_closed"] = True

    monkeypatch.setattr(pgvector_store, "PgVectorStore", _PgStore)
    monkeypatch.setattr(
        policy,
        "validate_local_embedding_write_or_query_policy",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(operator_runner, "_build_local_embedder", lambda *_args: _Embedder())
    monkeypatch.setitem(
        sys.modules,
        "neo4j",
        SimpleNamespace(GraphDatabase=SimpleNamespace(driver=lambda *_args, **_kwargs: _Driver())),
    )

    clients = operator_runner._build_runtime_clients(plan, environ=env)
    clients["_close"]()

    assert isinstance(clients["pg_store"], _PgStore)
    assert constructed["pg_kwargs"]["dsn"] == env["NEURON_LBRAIN_PGVECTOR_DSN"]
    assert constructed["pg_kwargs"]["embedding_profile"] == LOCAL_EMBEDDING_PROFILE_ID
    assert constructed["database"] == "brain"
    assert constructed["session_closed"] is True
    assert constructed["driver_closed"] is True
    assert constructed["embedder_closed"] is True


def test_target_fingerprint_uses_safe_endpoint_identity_not_secret_values():
    from agent_knowledge.local_embedding import operator_runner

    env = {
        "LLM_BRAIN_NEO4J_URI": "neo4j+s://graph-user:graph-secret@graph.example:7687?token=uri-secret",
        "LLM_BRAIN_NEO4J_DATABASE": "brain",
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://pg-user:pg-secret@pg.example:5432/brain?sslpassword=query-secret",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "https://svc-user:svc-secret@embed.example/v1?api_key=url-secret",
        "LLM_BRAIN_EMBEDDING_MODEL": LOCAL_EMBEDDING_MODEL_ALIAS,
        "LLM_BRAIN_EMBEDDING_API_KEY": "embedding-secret",
        "NEO4J_USER": "neo4j-user",
        "NEO4J_PASSWORD": "neo4j-secret",
    }
    rotated = {
        **env,
        "LLM_BRAIN_NEO4J_URI": "neo4j+s://other-user:rotated-graph-secret@graph.example:7687?token=rotated-uri-secret",
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://other-user:rotated-pg-secret@pg.example:5432/brain?sslpassword=rotated-query-secret",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "https://other-user:rotated-svc-secret@embed.example/v1?api_key=rotated-url-secret",
        "LLM_BRAIN_EMBEDDING_API_KEY": "rotated-embedding-secret",
        "NEO4J_USER": "other-neo4j-user",
        "NEO4J_PASSWORD": "rotated-neo4j-secret",
    }
    identity = operator_runner._resolve_target_identity("neo4j", env)
    rotated_identity = operator_runner._resolve_target_identity("neo4j", rotated)
    first_plan = operator_runner.build_backfill_plan(
        lane="neo4j",
        target_identity=identity,
    )
    rotated_plan = operator_runner.build_backfill_plan(
        lane="neo4j",
        target_identity=rotated_identity,
    )
    other_target = operator_runner.build_backfill_plan(
        lane="neo4j",
        target_identity={**identity, "neo4j_endpoint": "neo4j+s://different.example:7687"},
    )
    other_pg_target = operator_runner.build_backfill_plan(
        lane="neo4j",
        target_identity={**identity, "pg_endpoint": "postgresql://different.example:5432"},
    )

    serialized_identity = json.dumps(identity, sort_keys=True)
    for secret in (
        "graph-secret",
        "uri-secret",
        "pg-secret",
        "query-secret",
        "svc-secret",
        "url-secret",
        "embedding-secret",
        "neo4j-secret",
    ):
        assert secret not in serialized_identity
    assert identity == rotated_identity
    assert first_plan.target_fingerprint == rotated_plan.target_fingerprint
    assert first_plan.target_fingerprint != other_target.target_fingerprint
    assert first_plan.target_fingerprint != other_pg_target.target_fingerprint
    approved_argv = ["--lane", "neo4j", "--execute"]
    approved = _approval_payload(first_plan, approved_argv)
    with pytest.raises(operator_runner.ApprovalValidationError) as exc:
        operator_runner.validate_approval_artifact(
            approved,
            plan=other_pg_target,
            command_argv=approved_argv,
        )
    assert exc.value.reason_code == "approval_target_mismatch"
    assert identity["pg_endpoint"] == "postgresql://pg.example:5432"
    assert identity["pg_database"] == "brain"
    assert identity["neo4j_endpoint"] == "neo4j+s://graph.example:7687"
    assert identity["credentials_present"] is True


def test_pg_runner_never_counts_processing_or_dead_letter_as_success(monkeypatch, tmp_path):
    from agent_knowledge.local_embedding import operator_runner, pg_operator_runner, transition

    plan = operator_runner.build_backfill_plan(
        lane="pg",
        phase="vectors",
        vector_scope="cards",
        batch_size=8,
        max_batches=2,
        max_seconds=1,
        lease_seconds=240,
    )
    captured = {}

    def fake_backfill(store, embedder, **kwargs):
        captured.update(kwargs)
        return transition.PgBackfillProgress(
            eligible_cards=4,
            ready_cards=4,
            eligible_sessions=3,
            ready_sessions=3,
            session_receipts_verified=True,
            outbox_processing=1,
            outbox_dead_letter=2,
            completed=5,
        )

    monkeypatch.setattr(transition, "backfill_pg_local_embeddings", fake_backfill)
    report = pg_operator_runner.run_pg_vectors_backfill(
        plan,
        store=object(),
        embedder=lambda _text: [1.0] * LOCAL_EMBEDDING_DIM,
    )

    assert report["success"] is False
    assert report["outbox"]["processing"] == 1
    assert report["outbox"]["dead_letter"] == 2
    assert captured["vector_scope"] == "cards"
    assert captured["couchdb_store"] is None
    assert captured["batch_size"] == 8
    assert captured["max_batches"] == 2
    assert captured["lease_seconds"] == 240
    assert captured["profile_id"] == LOCAL_EMBEDDING_PROFILE_ID


@pytest.mark.parametrize(
    "blocked_field",
    [
        "receipt_conflict",
        "receipt_failed",
        "receipt_mutation_unknown",
        "receipt_stale_skipped",
        "conflicting_session_receipts",
        "failed_session_receipts",
        "unchecked_session_receipts",
    ],
)
def test_pg_runner_never_counts_uncertain_receipts_as_success(
    monkeypatch, tmp_path, blocked_field
):
    from agent_knowledge.local_embedding import operator_runner, pg_operator_runner, transition

    checkpoint = tmp_path / "receipt-cursor.json"
    plan = operator_runner.build_backfill_plan(
        lane="pg",
        phase="receipts",
        receipt_checkpoint_path=checkpoint,
        receipt_run_id="receipt-failure-fixture",
        receipt_source_identity="sha256:" + "b" * 64,
    )
    inspection_values: dict[str, Any] = {
        "eligible_sessions": 1,
        "ready_sessions": 1,
        "eligible_session_receipts": 1,
        "ready_session_receipts": 1,
        "session_receipts_verified": True,
    }
    receipt_values: dict[str, Any] = {"complete": True}
    process_field = {
        "receipt_conflict": "conflict",
        "receipt_failed": "failed",
        "receipt_mutation_unknown": "mutation_unknown",
        "receipt_stale_skipped": "stale_skipped",
    }.get(blocked_field)
    if process_field:
        receipt_values[process_field] = 1
    else:
        inspection_values[blocked_field] = 1
        if blocked_field == "unchecked_session_receipts":
            inspection_values["session_receipts_verified"] = False

    monkeypatch.setattr(
        transition,
        "inspect_pg_local_embedding_state",
        lambda *_args, **_kwargs: transition.PgBackfillProgress(**inspection_values),
    )
    monkeypatch.setattr(
        transition,
        "process_pg_local_embedding_receipts",
        lambda *_args, **_kwargs: transition.PgReceiptProgress(**receipt_values),
    )

    report = pg_operator_runner.run_pg_receipts_backfill(
        plan,
        store=object(),
        couchdb_store=object(),
        checkpoint_path=checkpoint,
    )

    assert report["success"] is False
    assert (
        report["receipt"]["conflict"]
        + report["receipt"]["failed"]
        + report["receipt"]["mutation_unknown"]
        + report["receipt"]["stale_skipped"]
        + report["receipt"]["unchecked"]
        > 0
    )


def test_pg_receipts_phase_forwards_snapshot_contract_without_model_client(
    monkeypatch, tmp_path
):
    from agent_knowledge.local_embedding import operator_runner, pg_operator_runner, transition

    checkpoint = tmp_path / "receipt-cursor.json"
    plan = operator_runner.build_backfill_plan(
        lane="pg",
        phase="receipts",
        max_seconds=1,
        receipt_page_size=2,
        receipt_max_pages=1,
        receipt_max_rows=2,
        receipt_checkpoint_path=checkpoint,
        receipt_run_id="receipt-resume-fixture",
        receipt_source_identity="sha256:" + "c" * 64,
    )
    calls = []

    def fake_receipt_processing(store, couchdb_store, **kwargs):
        calls.append(kwargs)
        return transition.PgReceiptProgress(complete=True)

    monkeypatch.setattr(
        transition,
        "inspect_pg_local_embedding_state",
        lambda *_args, **_kwargs: transition.PgBackfillProgress(
            eligible_sessions=1,
            ready_sessions=1,
            eligible_session_receipts=1,
            ready_session_receipts=1,
            session_receipts_verified=True,
        ),
    )
    monkeypatch.setattr(
        transition,
        "process_pg_local_embedding_receipts",
        fake_receipt_processing,
    )

    report = pg_operator_runner.run_pg_backfill_phase(
        plan,
        store=object(),
        couchdb_store=object(),
        checkpoint_path=checkpoint,
    )

    assert len(calls) == 1
    assert calls[0]["checkpoint_path"] == checkpoint
    assert calls[0]["page_size"] == 2
    assert calls[0]["max_pages"] == 1
    assert calls[0]["max_rows"] == 2
    assert calls[0]["run_id"] == "receipt-resume-fixture"
    assert calls[0]["expected_source_identity"] == "sha256:" + "c" * 64
    assert calls[0]["target_fingerprint"] == plan.target_fingerprint
    assert report["success"] is True
    assert report["model_calls"] == 0


def test_pg_lease_bound_matches_canonical_chunk_and_retry_limits():
    from agent_knowledge.local_embedding import operator_runner
    from agent_knowledge.local_embedding.canonical import (
        plan_request_batches_v1,
        prepare_canonical_input_v1,
    )
    from agent_knowledge.local_embedding.profile import LOCAL_EMBEDDING_DIM

    text = "\x00" * operator_runner.PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES
    prepared = prepare_canonical_input_v1(text)
    request_batches = plan_request_batches_v1(prepared.chunks)

    assert len(prepared.chunks) <= 3
    assert len(request_batches) == 1

    class _Client:
        def __init__(self):
            self.calls = []

        def embed_document(self, value):
            self.calls.append(value)
            return SimpleNamespace(vector=[1.0] * LOCAL_EMBEDDING_DIM)

    client = _Client()
    counted = operator_runner._CountingLocalEmbedder(client)
    counted.set_max_input_bytes(operator_runner.PG_BACKFILL_MAX_EMBEDDING_INPUT_BYTES)
    assert len(counted.embed(text)) == LOCAL_EMBEDDING_DIM
    assert client.calls == [text]
    with pytest.raises(ValueError, match="embedding_input_exceeds_lease_bound"):
        counted.embed(text + "\x00")


def test_pg_runner_never_reports_a_stale_outbox_cas_as_success(monkeypatch, tmp_path):
    from agent_knowledge.local_embedding import operator_runner, pg_operator_runner, transition

    progress = transition.PgBackfillProgress(
        eligible_cards=1,
        ready_cards=1,
        eligible_sessions=1,
        ready_sessions=1,
        session_receipts_verified=True,
        stale_fenced=1,
    )
    monkeypatch.setattr(transition, "backfill_pg_local_embeddings", lambda *_args, **_kw: progress)
    plan = operator_runner.build_backfill_plan(
        lane="pg", phase="vectors", vector_scope="all"
    )
    report = pg_operator_runner.run_pg_vectors_backfill(
        plan,
        store=object(),
        embedder=lambda _text: [1.0] * LOCAL_EMBEDDING_DIM,
    )

    assert report["success"] is False
    assert report["stop_reason"] == "stale_fenced"


@pytest.mark.parametrize(
    "stop_reason",
    [
        "source_changed_before_batch",
        "stale_fenced",
        "batch_failed",
        "batch_readback_incomplete",
        "operation_conflict",
        "journal_readback_incomplete",
        "fence_release_failed",
        "time_limit",
    ],
)
def test_neo4j_runner_never_reports_noncompleted_batch_state_as_success(
    monkeypatch, stop_reason
):
    from agent_knowledge.local_embedding import operator_runner, transition

    class _Session:
        def run(self, query, **_kwargs):
            if "SHOW CONSTRAINTS" in query:
                return SimpleNamespace(single=lambda: {"names": ["embedding_group_fence_group_id"]})
            if "SHOW INDEXES" in query:
                return SimpleNamespace(
                    single=lambda: {
                        "names": ["embedding_deletion_target_idx", "graph_op_step_receipt_idx"]
                    }
                )
            raise AssertionError("bounded group selection was passed explicitly")

    progress = transition.Neo4jBackfillProgress(
        eligible_entities=1,
        ready_entities=1,
        readback_complete=True,
        readback_verified=True,
        stop_reason=stop_reason,
    )
    monkeypatch.setattr(transition, "backfill_neo4j_local_embeddings", lambda *_args, **_kw: progress)
    monkeypatch.setattr(transition, "inspect_neo4j_local_embedding_state", lambda *_args, **_kw: progress)

    report = operator_runner.run_neo4j_backfill(
        operator_runner.build_backfill_plan(lane="neo4j"),
        session=_Session(),
        embedder=object(),
        group_ids=["private-group-id"],
        pg_store=object(),
    )

    assert report["success"] is False
    assert report["stop_reason"] == stop_reason
    assert "private-group-id" not in json.dumps(report)


def test_neo4j_runner_freezes_group_selection_hashes_it_and_reads_back(monkeypatch):
    from agent_knowledge.local_embedding import operator_runner, transition

    class _Result:
        def __init__(self, record):
            self.record = record

        def single(self):
            return self.record

        def consume(self):
            return None

    class _Session:
        def run(self, query, **_params):
            if query.startswith("CREATE CONSTRAINT") or query.startswith("CREATE INDEX"):
                return _Result({})
            if "SHOW CONSTRAINTS" in query:
                return _Result({"names": ["embedding_group_fence_group_id"]})
            if "SHOW INDEXES" in query:
                return _Result(
                    {"names": ["embedding_deletion_target_idx", "graph_op_step_receipt_idx"]}
                )
            if "RETURN collect(DISTINCT n.group_id) AS groups" in query:
                return _Result({"groups": ["group-b", "group-a", "group-b"]})
            raise AssertionError("unexpected preflight query")

    observed = []
    observed_coordinators = []
    progress = transition.Neo4jBackfillProgress(
        group_ids_scanned=("group-a", "group-b"),
        eligible_entities=4,
        ready_entities=4,
        eligible_edges=3,
        ready_edges=3,
        readback_complete=True,
        readback_verified=True,
    )
    monkeypatch.setattr(
        transition,
        "backfill_neo4j_local_embeddings",
        lambda session, embedder, **kwargs: (
            observed.append(kwargs["group_ids"]),
            observed_coordinators.append(kwargs["journal_coordinator"]),
            progress,
        )[-1],
    )
    readback_group_ids = []
    monkeypatch.setattr(
        transition,
        "inspect_neo4j_local_embedding_state",
        lambda session, **kwargs: readback_group_ids.append(kwargs.get("group_ids")) or progress,
    )
    plan = operator_runner.build_backfill_plan(lane="neo4j", batch_size=16)

    pg_store = object()
    report = operator_runner.run_neo4j_backfill(
        plan,
        session=_Session(),
        embedder=object(),
        pg_store=pg_store,
    )

    assert observed == [["group-a", "group-b"]]
    assert report["groups_scanned"] == 2
    assert report["group_fingerprint"].startswith("sha256:")
    assert report["readback_verified"] is True
    assert readback_group_ids == [["group-a", "group-b"]]
    assert "group-a" not in json.dumps(report)
    assert report["schema"]["ddl_attempted"] is False
    assert observed_coordinators[0]._pg_store is pg_store
    assert observed_coordinators[0]._ensure_schema is False


def _inspect_single_neo4j_entity(
    row,
    *,
    busy_groups=0,
    quarantined_groups=0,
    group_ids=("fixture-group",),
    observed_calls=None,
):
    from agent_knowledge.local_embedding import transition

    class _Session:
        def run(self, query, **params):
            if observed_calls is not None:
                observed_calls.append((query, dict(params)))
            if "RETURN n.uuid AS target_id" in query:
                rows = [row] if params.get("after_uuid") is None else []
                return SimpleNamespace(data=lambda: rows)
            if "RETURN e.uuid AS target_id" in query:
                return SimpleNamespace(data=lambda: [])
            if "MATCH (t:EmbeddingDeletion)" in query:
                return SimpleNamespace(single=lambda: {"tombstones": 0})
            if "MATCH (f:EmbeddingGroupFence)" in query:
                return SimpleNamespace(
                    single=lambda: {
                        "quarantined_groups": quarantined_groups,
                        "busy_groups": busy_groups,
                    }
                )
            if "MATCH (n:Entity)" in query:
                return SimpleNamespace(
                    single=lambda: {
                        "total": 1,
                        "ready": 1,
                        "legacy_preserved": 0,
                        "groups": ["fixture-group"],
                    }
                )
            if "MATCH (:Entity)-[e:RELATES_TO]" in query:
                return SimpleNamespace(
                    single=lambda: {"total": 0, "ready": 0, "legacy_preserved": 0, "groups": []}
                )
            raise AssertionError("unexpected readback query")

    return transition.inspect_neo4j_local_embedding_state(
        _Session(),
        group_ids=None if group_ids is None else list(group_ids),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
    )


def _current_readback_entity():
    from agent_knowledge.local_embedding import transition

    return {
        "target_id": "fixture-entity",
        "group_id": "fixture-group",
        "source_text": "current source text",
        "vector": [0.25] * LOCAL_EMBEDDING_DIM,
        "profile_id": LOCAL_EMBEDDING_PROFILE_ID,
        "input_hash": transition.compute_graph_node_input_hash("current source text"),
        "dirty": False,
        "tombstoned": False,
        "fence_status": "idle",
        "fence_busy": False,
        "legacy_vector": None,
    }


def test_neo4j_readback_accepts_only_complete_current_profile_identity():
    result = _inspect_single_neo4j_entity(_current_readback_entity())

    assert result.eligible_entities == 1
    assert result.ready_entities == 1
    assert result.readback_complete is True
    assert result.readback_verified is True


@pytest.mark.parametrize(
    ("group_ids", "expected_group_ids", "has_group_filter"),
    [(["fixture-group"], ["fixture-group"], True), (None, [], False)],
)
def test_neo4j_readback_passes_group_ids_to_tombstone_and_fence_queries(
    group_ids, expected_group_ids, has_group_filter
):
    observed_calls = []
    _inspect_single_neo4j_entity(
        _current_readback_entity(),
        group_ids=group_ids,
        observed_calls=observed_calls,
    )

    for marker in ("MATCH (t:EmbeddingDeletion)", "MATCH (f:EmbeddingGroupFence)"):
        matches = [(query, params) for query, params in observed_calls if marker in query]
        assert len(matches) == 1
        query, params = matches[0]
        assert params["group_ids"] == expected_group_ids
        assert ("group_id IN $group_ids" in query) is has_group_filter


@pytest.mark.parametrize(
    ("row_change", "expected_metric"),
    [
        ({"profile_id": "other-profile"}, "readback_profile_mismatches"),
        ({"vector": [0.25] * (LOCAL_EMBEDDING_DIM - 1)}, "readback_invalid_vectors"),
        ({"vector": [float("nan")] * LOCAL_EMBEDDING_DIM}, "readback_invalid_vectors"),
        ({"dirty": True}, "readback_dirty_targets"),
        ({"tombstoned": True}, "readback_tombstoned_embeddings"),
    ],
)
def test_neo4j_readback_rejects_profile_vector_dirty_and_tombstone_mismatches(
    row_change, expected_metric
):
    row = {**_current_readback_entity(), **row_change}
    result = _inspect_single_neo4j_entity(row)

    assert result.ready_entities == 0
    assert result.readback_verified is False
    assert getattr(result, expected_metric) == 1


@pytest.mark.parametrize("busy,quarantined", [(1, 0), (0, 1)])
def test_neo4j_readback_does_not_pass_with_busy_or_quarantined_fence(busy, quarantined):
    result = _inspect_single_neo4j_entity(
        _current_readback_entity(),
        busy_groups=busy,
        quarantined_groups=quarantined,
    )

    assert result.readback_verified is False
    assert result.groups_fenced_busy == busy
    assert result.groups_quarantined == quarantined


def test_neo4j_readback_rejects_stale_source_hash_despite_matching_counts():
    entity_row = {
        **_current_readback_entity(),
        "input_hash": "sha256:stale-source-hash",
    }
    result = _inspect_single_neo4j_entity(entity_row)

    assert result.eligible_entities == 1
    assert result.ready_entities == 0
    assert result.readback_complete is True
    assert result.readback_verified is False
    assert result.readback_input_hash_mismatches == 1


def _fake_backfill_graph(entity_count):
    return {
        f"entity-{index:04d}": {"name": f"name-{index}", "ready": False}
        for index in range(entity_count)
    }


def _install_fake_neo4j_backfill(monkeypatch, graph):
    from agent_knowledge.local_embedding import transition

    class _Session:
        def run(self, query, **params):
            if "MATCH (n:Entity {group_id: $group_id})" in query:
                candidates = [
                    {"uuid": uuid, "name": item["name"], "current_hash": None}
                    for uuid, item in sorted(graph.items())
                    if not item["ready"]
                ]
                return SimpleNamespace(data=lambda: candidates[: params["limit"]])
            if "MATCH (:Entity)-[e:RELATES_TO {group_id: $group_id}]" in query:
                return SimpleNamespace(data=lambda: [])
            raise AssertionError("unexpected fake Neo4j query")

    class _Embedder:
        def __init__(self):
            self.calls = []

        def embed_batch(self, texts):
            self.calls.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    class _Lease:
        group_id = "hidden-group"
        epoch = 7
        operation_id = "hidden-operation"

    monkeypatch.setattr(transition, "ensure_neo4j_safety_schema", lambda _session: None)
    monkeypatch.setattr(
        transition,
        "acquire_group_fence_lease",
        lambda *_args, **_kwargs: _Lease(),
    )
    monkeypatch.setattr(transition, "release_group_fence_lease", lambda *_args, **_kwargs: None)

    def write_entity(_session, *, entity_uuid, **_kwargs):
        graph[entity_uuid]["ready"] = True
        return SimpleNamespace(applied=True)

    monkeypatch.setattr(transition, "cas_write_entity_local_embedding", write_entity)
    monkeypatch.setattr(
        transition,
        "inspect_neo4j_local_embedding_state",
        lambda _session, **_kwargs: transition.Neo4jBackfillProgress(
            group_ids_scanned=("hidden-group",),
            eligible_entities=len(graph),
            ready_entities=sum(item["ready"] for item in graph.values()),
            missing_or_dirty_entities=sum(not item["ready"] for item in graph.values()),
        ),
    )
    monkeypatch.setattr(
        transition,
        "_verify_neo4j_embedding_batch",
        lambda *_args, **_kwargs: True,
    )
    return _Session(), _Embedder()


def test_neo4j_batch_readback_requires_exact_current_vector_identity():
    from agent_knowledge.local_embedding import transition

    candidate = {"uuid": "private-entity-id", "name": "current entity name"}
    expected_hash = transition.compute_graph_node_input_hash(candidate["name"])

    class _Session:
        def __init__(self, *, dirty=False, omit=False):
            self.dirty = dirty
            self.omit = omit

        def run(self, query, *, group_id, targets):
            assert group_id == "private-group-id"
            assert "UNWIND $targets AS expected" in query
            assert "CREATE" not in query
            if self.omit:
                rows = []
            else:
                rows = [
                    {
                        "expected_uuid": targets[0]["uuid"],
                        "target_id": targets[0]["uuid"],
                        "group_id": group_id,
                        "source_text": candidate["name"],
                        "vector": [0.25] * LOCAL_EMBEDDING_DIM,
                        "profile_id": LOCAL_EMBEDDING_PROFILE_ID,
                        "input_hash": expected_hash,
                        "dirty": self.dirty,
                        "tombstoned": False,
                    }
                ]
            return SimpleNamespace(data=lambda: rows)

    assert transition._verify_neo4j_embedding_batch(
        _Session(),
        group_id="private-group-id",
        entity_rows=[candidate],
        edge_rows=[],
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
    ) is True
    assert transition._verify_neo4j_embedding_batch(
        _Session(dirty=True),
        group_id="private-group-id",
        entity_rows=[candidate],
        edge_rows=[],
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
    ) is False
    assert transition._verify_neo4j_embedding_batch(
        _Session(omit=True),
        group_id="private-group-id",
        entity_rows=[candidate],
        edge_rows=[],
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
    ) is False


def test_neo4j_stale_write_and_incomplete_batch_readback_do_not_record_step(
    monkeypatch,
):
    from agent_knowledge.local_embedding import transition
    from agent_knowledge.local_embedding.transition import backfill_neo4j_local_embeddings

    class _Coordinator:
        def __init__(self):
            self.step_receipts = []

        def begin_operation(self, _session, **kwargs):
            return SimpleNamespace(group_id=kwargs["group_id"], epoch=1)

        def run_step(self, _session, *, operation_id, effect_payload, action, **_kwargs):
            result = action()
            self.step_receipts.append((operation_id, effect_payload))
            return "executed", result

        def complete_operation(self, _session, *, lease):
            _ = lease

    for failure in ("stale", "readback"):
        graph = _fake_backfill_graph(1)
        session, embedder = _install_fake_neo4j_backfill(monkeypatch, graph)
        coordinator = _Coordinator()
        if failure == "stale":
            monkeypatch.setattr(
                transition,
                "cas_write_entity_local_embedding",
                lambda *_args, **_kwargs: SimpleNamespace(applied=False),
            )
        else:
            def write_successfully(_session, *, entity_uuid, **_kwargs):
                graph[entity_uuid]["ready"] = True
                return SimpleNamespace(applied=True)

            monkeypatch.setattr(
                transition,
                "cas_write_entity_local_embedding",
                write_successfully,
            )
            monkeypatch.setattr(
                transition,
                "_verify_neo4j_embedding_batch",
                lambda *_args, **_kwargs: False,
            )

        progress = backfill_neo4j_local_embeddings(
            session,
            embedder,
            group_ids=["hidden-group"],
            journal_coordinator=coordinator,
        )

        assert coordinator.step_receipts == []
        assert progress.stop_reason == (
            "stale_fenced" if failure == "stale" else "batch_readback_incomplete"
        )


def test_neo4j_operation_identity_is_stable_for_retry_and_changes_with_source(
    monkeypatch,
):
    from agent_knowledge.local_embedding.transition import backfill_neo4j_local_embeddings

    class _Coordinator:
        def __init__(self):
            self.operation_ids = []
            self.step_receipts = []

        def begin_operation(self, _session, **kwargs):
            self.operation_ids.append(kwargs["operation_id"])
            return SimpleNamespace(group_id=kwargs["group_id"], epoch=1)

        def run_step(self, _session, *, operation_id, effect_payload, action, **_kwargs):
            result = action()
            self.step_receipts.append((operation_id, effect_payload))
            return "executed", result

        def complete_operation(self, _session, *, lease):
            _ = lease

    graph = _fake_backfill_graph(1)
    session, embedder = _install_fake_neo4j_backfill(monkeypatch, graph)
    original_embed_batch = embedder.embed_batch
    fail_once = {"pending": True}

    def embed_batch_with_one_failure(texts):
        if fail_once["pending"]:
            fail_once["pending"] = False
            raise RuntimeError("provider failure")
        return original_embed_batch(texts)

    embedder.embed_batch = embed_batch_with_one_failure
    coordinator = _Coordinator()
    first = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        journal_coordinator=coordinator,
    )
    second = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        journal_coordinator=coordinator,
    )
    graph["entity-0000"] = {"name": "changed source revision", "ready": False}
    third = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        journal_coordinator=coordinator,
    )

    assert first.stop_reason == "batch_failed"
    assert second.entities_embedded == 1
    assert third.entities_embedded == 1
    assert len(coordinator.operation_ids) == 3
    assert coordinator.operation_ids[0] == coordinator.operation_ids[1]
    assert coordinator.operation_ids[2] != coordinator.operation_ids[1]
    assert len(coordinator.step_receipts) == 2


def test_neo4j_backfill_handles_more_than_ten_batches_and_resumes_after_restart(monkeypatch):
    from agent_knowledge.local_embedding.transition import backfill_neo4j_local_embeddings

    graph = _fake_backfill_graph(337)
    session, embedder = _install_fake_neo4j_backfill(monkeypatch, graph)

    first = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        batch_size=32,
        max_batches_per_group=10,
        worker_id="operator-run-one",
    )
    assert first.entities_embedded == 320
    assert first.missing_or_dirty_entities == 17

    second = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        batch_size=32,
        max_batches_per_group=10,
        worker_id="operator-run-two",
    )
    assert second.entities_embedded == 17
    assert second.ready_entities == 337
    assert second.missing_or_dirty_entities == 0
    assert all(len(batch) <= 32 for batch in embedder.calls)


def test_neo4j_backfill_stops_between_batches_at_time_limit(monkeypatch):
    from agent_knowledge.local_embedding import transition
    from agent_knowledge.local_embedding.transition import backfill_neo4j_local_embeddings

    graph = _fake_backfill_graph(10)
    session, embedder = _install_fake_neo4j_backfill(monkeypatch, graph)
    clock = {"now": 0.0}
    monkeypatch.setattr(transition.time, "monotonic", lambda: clock["now"])

    def slow_embed_batch(texts):
        embedder.calls.append(list(texts))
        clock["now"] += 0.6
        return [[1.0, 0.0] for _ in texts]

    embedder.embed_batch = slow_embed_batch
    result = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        batch_size=1,
        max_batches_per_group=10,
        max_seconds=0.5,
        worker_id="time-bounded-run",
    )

    assert result.entities_embedded == 1
    assert result.stop_reason == "time_limit"
    assert len(embedder.calls) == 1


def test_neo4j_journal_uses_batch_stable_operation_ids_and_restarts_without_conflict(monkeypatch):
    from agent_knowledge.local_embedding.transition import backfill_neo4j_local_embeddings

    class _Coordinator:
        def __init__(self):
            self.operation_ids = []
            self.effects = []

        def begin_operation(self, _session, **kwargs):
            self.operation_ids.append(kwargs["operation_id"])
            return SimpleNamespace(group_id=kwargs["group_id"], epoch=1)

        def run_step(self, _session, *, operation_id, effect_payload, action, **_kwargs):
            self.effects.append((operation_id, effect_payload))
            return "executed", action()

        def complete_operation(self, _session, *, lease):
            _ = lease

    graph = _fake_backfill_graph(70)
    session, embedder = _install_fake_neo4j_backfill(monkeypatch, graph)
    coordinator = _Coordinator()
    first = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        batch_size=16,
        max_batches_per_group=2,
        worker_id="restart-a",
        journal_coordinator=coordinator,
    )
    first_ids = list(coordinator.operation_ids)
    second = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        batch_size=16,
        max_batches_per_group=10,
        worker_id="restart-b",
        journal_coordinator=coordinator,
    )

    assert first.entities_embedded == 32
    assert second.ready_entities == 70
    assert len(first_ids) == 2
    assert len(set(first_ids)) == 2
    assert len(set(coordinator.operation_ids)) == len(coordinator.operation_ids)
    assert all(operation_id.startswith("model2vec-batch:") for operation_id in coordinator.operation_ids)
    assert all(len(operation_id) <= 128 for operation_id in coordinator.operation_ids)
    assert all("uuid" not in json.dumps(effect) for _, effect in coordinator.effects)


def test_neo4j_busy_quarantine_and_effect_hash_conflict_are_separate(monkeypatch):
    from agent_knowledge.llm_brain_core.local_graph_safety import (
        GraphOperationConflictQuarantinedError,
        GroupFenceBusyError,
        GroupQuarantinedError,
    )
    from agent_knowledge.local_embedding.transition import backfill_neo4j_local_embeddings

    class _Coordinator:
        def __init__(self, error=None, conflict=False):
            self.error = error
            self.conflict = conflict

        def begin_operation(self, *_args, **_kwargs):
            if self.error:
                raise self.error("hidden")
            return SimpleNamespace(group_id="hidden-group", epoch=1)

        def run_step(self, *_args, **_kwargs):
            raise GraphOperationConflictQuarantinedError("hidden")

        def complete_operation(self, *_args, **_kwargs):
            pass

    graph = _fake_backfill_graph(1)
    session, embedder = _install_fake_neo4j_backfill(monkeypatch, graph)
    busy = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        journal_coordinator=_Coordinator(GroupFenceBusyError),
    )
    quarantined = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        journal_coordinator=_Coordinator(GroupQuarantinedError),
    )
    conflict = backfill_neo4j_local_embeddings(
        session,
        embedder,
        group_ids=["hidden-group"],
        journal_coordinator=_Coordinator(conflict=True),
    )

    assert busy.groups_fenced_busy == 1
    assert busy.groups_quarantined == 0
    assert quarantined.groups_quarantined == 1
    assert conflict.groups_quarantined == 1
    assert graph["entity-0000"]["ready"] is False

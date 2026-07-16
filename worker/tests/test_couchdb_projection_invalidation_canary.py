from __future__ import annotations

import json
import os
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
)
from agent_knowledge.couchdb_source.document_model import SourceDocType
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.rag_ingress import projection_invalidation_canary as canary_module
from agent_knowledge.rag_ingress.projection_invalidation_canary import (
    CANARY_OPERATION,
    CANARY_SCHEMA_VERSION,
    CanaryExecutionError,
    build_canary_plan,
    main,
    run_projection_invalidation_canary,
)
from agent_knowledge.rag_ingress.couchdb_delivery_backend import CouchDBDeliveryBackend
from agent_knowledge.rag_ingress.delivery_executor import DeliveryOutcomeUncertain
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB


PROJECT = "neurons"
PROVIDER = "lbrain-temporal-canary"
NONCE = "sha256:" + ("a" * 64)
SOURCE_COMMIT = "0" * 40
OBSERVED_AT = "2026-07-17T01:02:03Z"


def _cli_plan(argv: list[str]) -> dict:
    with patch("sys.stdout", StringIO()) as output:
        assert main(argv) == 0
    return json.loads(output.getvalue())


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def test_bounded_canary_proves_distinct_invalidation_catchup_and_duplicate_nonselection(
    tmp_path: Path,
) -> None:
    state_root = _private_dir(tmp_path / "state")
    state_db = RAGIngressStateDB(state_root / "ingress.sqlite3")
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    source_store = InMemoryCouchDBSourceStore()
    projector = RecordingSessionMemoryProjector()
    graph = FakeGraphMemoryAdapter()

    report = run_projection_invalidation_canary(
        state_db=state_db,
        ledger_path=ledger_path,
        source_store=source_store,
        session_memory_projector=projector,
        graph_adapter=graph,
        runtime_dir=tmp_path / "runtime",
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256=NONCE,
        expected_source_commit=SOURCE_COMMIT,
        observed_at=OBSERVED_AT,
        limit=1,
        max_runtime_seconds=30,
    )

    assert report["schema_version"] == CANARY_SCHEMA_VERSION
    assert report["status"] == "passed"
    assert report["bounded_limit"] == 1
    assert report["timeout_seconds"] == 30
    assert report["ingress_enqueue_count"] == 3
    assert report["delivery_succeeded_count"] == 3
    assert report["source_chunk_insert_count"] == 2
    assert report["baseline_source_hash"].startswith("sha256:")
    assert report["distinct_source_hash"].startswith("sha256:")
    assert report["baseline_source_hash"] != report["distinct_source_hash"]
    assert report["session_memory_projected_source_hash"] == report["distinct_source_hash"]
    assert report["graph_projected_source_hash"] == report["distinct_source_hash"]
    assert report["distinct"] == {
        "source_hash_changed": True,
        "projection_dirty_observed": True,
        "session_memory_selected": 1,
        "session_memory_projected": 1,
        "session_memory_hash_caught_up": True,
        "graph_selected": 1,
        "graph_projected": 1,
        "graph_hash_caught_up": True,
    }
    assert report["duplicate"] == {
        "source_hash_unchanged": True,
        "projection_state_remained_projected": True,
        "session_memory_selected": 0,
        "session_memory_projected": 0,
        "graph_selected": 0,
        "graph_projected": 0,
    }
    assert report["rollback_restore_available"] is True
    assert report["rollback_restore_strategy"] == (
        "fresh_nonce_catch_up_then_probe_from_authoritative_source"
    )
    assert report["resumable_after_partial_failure"] is True
    assert report["resume_requires_fresh_probe_nonce"] is True
    assert report["hard_timeout_required"] is True
    assert report["external_timeout_required"] is True
    assert report["mutation_performed"] is True
    assert report["destructive_mutation_performed"] is False
    assert report["raw_ids_printed"] is False
    assert report["raw_bodies_printed"] is False
    assert report["secret_printed"] is False
    assert report["host_topology_printed"] is False
    assert len(projector.calls) == 2
    assert len(graph._episodes) == 2
    assert len(state_db.list_delivery_jobs(status="succeeded", limit=10)) == 3

    second = run_projection_invalidation_canary(
        state_db=state_db,
        ledger_path=ledger_path,
        source_store=source_store,
        session_memory_projector=projector,
        graph_adapter=graph,
        runtime_dir=tmp_path / "runtime",
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256="sha256:" + ("b" * 64),
        expected_source_commit=SOURCE_COMMIT,
        observed_at="2026-07-17T01:03:03Z",
        limit=1,
        max_runtime_seconds=30,
    )
    assert second["status"] == "passed"
    assert second["source_chunk_insert_count"] == 1
    assert second["baseline_source_initialized"] is False
    assert len(
        source_store.find_by_type(
            SourceDocType.TRANSCRIPT_SESSION,
            selector={"project": PROJECT, "provider": PROVIDER},
        )
    ) == 1


@pytest.mark.parametrize("backend_committed", [False, True])
def test_fresh_nonce_recovers_after_baseline_delivery_outcome_is_quarantined(
    tmp_path: Path, backend_committed: bool
) -> None:
    state_db = RAGIngressStateDB(
        _private_dir(tmp_path / "state") / "ingress.sqlite3"
    )
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    source_store = InMemoryCouchDBSourceStore()
    projector = RecordingSessionMemoryProjector()
    graph = FakeGraphMemoryAdapter()
    common = {
        "state_db": state_db,
        "ledger_path": ledger_path,
        "source_store": source_store,
        "session_memory_projector": projector,
        "graph_adapter": graph,
        "runtime_dir": tmp_path / "runtime",
        "project": PROJECT,
        "provider": PROVIDER,
        "expected_source_commit": SOURCE_COMMIT,
        "limit": 1,
        "max_runtime_seconds": 30,
    }

    original_submit = CouchDBDeliveryBackend.submit

    def _uncertain_submit(self, job):
        if backend_committed:
            original_submit(self, job)
        raise DeliveryOutcomeUncertain("ack lost")

    with patch.object(
        CouchDBDeliveryBackend, "submit", new=_uncertain_submit
    ), pytest.raises(CanaryExecutionError, match="baseline_delivery"):
        run_projection_invalidation_canary(
            **common,
            probe_nonce_sha256=NONCE,
            observed_at=OBSERVED_AT,
        )

    quarantined = state_db.list_delivery_jobs(status="quarantined", limit=10)
    assert len(quarantined) == 1
    recovered = run_projection_invalidation_canary(
        **common,
        probe_nonce_sha256="sha256:" + ("c" * 64),
        observed_at="2026-07-17T01:04:03Z",
    )

    assert recovered["status"] == "passed"
    assert recovered["distinct"]["session_memory_hash_caught_up"] is True
    assert recovered["distinct"]["graph_hash_caught_up"] is True


def test_plan_is_public_safe_read_only_and_digest_bound() -> None:
    plan = build_canary_plan(
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256=NONCE,
        expected_source_commit=SOURCE_COMMIT,
        observed_at=OBSERVED_AT,
        limit=1,
        max_runtime_seconds=30,
    )

    assert plan["schema_version"] == CANARY_SCHEMA_VERSION
    assert plan["status"] == "planned"
    assert plan["plan_digest"].startswith("sha256:")
    assert plan["canary_ref"].startswith("sha256:")
    assert plan["planned_ingress_enqueue_count"] == 3
    assert plan["planned_source_chunk_insert_count"] == 2
    assert plan["mutation_performed"] is False
    assert plan["network_used"] is False
    serialized = json.dumps(plan, sort_keys=True)
    assert PROJECT not in serialized
    assert PROVIDER not in serialized
    assert SOURCE_COMMIT not in serialized


@pytest.mark.parametrize(
    "max_runtime_seconds",
    [float("nan"), float("inf"), float("-inf")],
)
def test_plan_rejects_nonfinite_runtime_bound(max_runtime_seconds: float) -> None:
    with pytest.raises(ValueError, match="bounded range"):
        build_canary_plan(
            project=PROJECT,
            provider=PROVIDER,
            probe_nonce_sha256=NONCE,
            expected_source_commit=SOURCE_COMMIT,
            observed_at=OBSERVED_AT,
            limit=1,
            max_runtime_seconds=max_runtime_seconds,
        )


class _FailOnSecondProjection:
    def __init__(self) -> None:
        self.calls = 0

    def project(self, *, target_profile: str, document: dict) -> str:
        del target_profile, document
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("private backend detail must not escape")
        return "canary-projection-ref"


def test_fresh_nonce_resumes_partial_dirty_source_without_new_session(
    tmp_path: Path,
) -> None:
    state_root = _private_dir(tmp_path / "state")
    state_db = RAGIngressStateDB(state_root / "ingress.sqlite3")
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    source_store = InMemoryCouchDBSourceStore()
    graph = FakeGraphMemoryAdapter()

    with pytest.raises(CanaryExecutionError) as failure:
        run_projection_invalidation_canary(
            state_db=state_db,
            ledger_path=ledger_path,
            source_store=source_store,
            session_memory_projector=_FailOnSecondProjection(),
            graph_adapter=graph,
            runtime_dir=tmp_path / "runtime",
            project=PROJECT,
            provider=PROVIDER,
            probe_nonce_sha256=NONCE,
            expected_source_commit=SOURCE_COMMIT,
            observed_at=OBSERVED_AT,
            limit=1,
            max_runtime_seconds=30,
        )
    assert failure.value.stage == "distinct_session_memory_projection"

    recovered = run_projection_invalidation_canary(
        state_db=state_db,
        ledger_path=ledger_path,
        source_store=source_store,
        session_memory_projector=RecordingSessionMemoryProjector(),
        graph_adapter=graph,
        runtime_dir=tmp_path / "runtime",
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256="sha256:" + ("c" * 64),
        expected_source_commit=SOURCE_COMMIT,
        observed_at="2026-07-17T01:04:03Z",
        limit=1,
        max_runtime_seconds=30,
    )
    assert recovered["status"] == "passed"
    assert recovered["source_chunk_insert_count"] == 1
    assert len(
        source_store.find_by_type(
            SourceDocType.TRANSCRIPT_SESSION,
            selector={"project": PROJECT, "provider": PROVIDER},
        )
    ) == 1


class _DuplicateAfterFirstGraph(FakeGraphMemoryAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def upsert_episode(self, episode):
        self.calls += 1
        result = super().upsert_episode(episode)
        return result if self.calls == 1 else "duplicate"


def test_graph_backend_duplicate_counts_as_projection_ledger_catchup(
    tmp_path: Path,
) -> None:
    state_root = _private_dir(tmp_path / "state")
    report = run_projection_invalidation_canary(
        state_db=RAGIngressStateDB(state_root / "ingress.sqlite3"),
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=InMemoryCouchDBSourceStore(),
        session_memory_projector=RecordingSessionMemoryProjector(),
        graph_adapter=_DuplicateAfterFirstGraph(),
        runtime_dir=tmp_path / "runtime",
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256=NONCE,
        expected_source_commit=SOURCE_COMMIT,
        observed_at=OBSERVED_AT,
        limit=1,
        max_runtime_seconds=30,
    )
    assert report["status"] == "passed"
    assert report["distinct"]["graph_projected"] == 1
    assert report["distinct"]["graph_hash_caught_up"] is True


def test_cli_routes_as_approval_gated_additive_canary_and_defaults_to_dry_run(
    tmp_path: Path,
) -> None:
    assert COMMAND_HANDLERS["couchdb-projection-invalidation-canary"] is main
    assert COMMAND_METADATA["couchdb-projection-invalidation-canary"] == {
        "runtime_category": "human_gated_additive_canary",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    }
    assert CANARY_OPERATION == "couchdb_projection_invalidation_canary"

    argv = [
        "--state-db",
        str(tmp_path / "state.sqlite3"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--limit",
        "1",
        "--max-runtime-seconds",
        "30",
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--probe-nonce-sha256",
        NONCE,
        "--observed-at",
        OBSERVED_AT,
    ]
    with patch("sys.stdout", StringIO()) as output:
        assert main(argv) == 0
    report = json.loads(output.getvalue())
    assert report["status"] == "planned"
    assert report["mutation_performed"] is False

    execute_argv = [
        *argv,
        "--execute",
        "--expected-plan-digest",
        report["plan_digest"],
        "--approval",
        str(tmp_path / "approval.json"),
    ]
    with patch("sys.stdout", StringIO()) as output:
        assert main(execute_argv) == 2
    rejected = json.loads(output.getvalue())
    assert rejected["status"] == "approval_rejected"
    assert rejected["mutation_performed"] is False
    assert rejected["raw_ids_printed"] is False


def test_execute_requires_exact_argv_approval_and_matching_plan_digest(
    tmp_path: Path,
) -> None:
    base = [
        "--state-db",
        str(tmp_path / "state.sqlite3"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--limit",
        "1",
        "--max-runtime-seconds",
        "30",
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--probe-nonce-sha256",
        NONCE,
        "--observed-at",
        OBSERVED_AT,
    ]
    plan = _cli_plan(base)
    approval_path = tmp_path / "approval.json"
    execute_argv = [
        *base,
        "--execute",
        "--expected-plan-digest",
        plan["plan_digest"],
        "--approval",
        str(approval_path),
    ]
    approval_path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": CANARY_OPERATION,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "timeout_seconds": 30,
                "rollback_or_abort_criteria": [
                    "abort on any lane selection or source hash mismatch"
                ],
                "target": {"target_fingerprints": plan["target_fingerprints"]},
                "command": {"argv": execute_argv},
            }
        ),
        encoding="utf-8",
    )
    passed = {
        **plan,
        "status": "passed",
        "mutation_performed": True,
    }

    with patch(
        "agent_knowledge.rag_ingress.projection_invalidation_canary._execute_live",
        return_value=passed,
    ) as execute_live, patch("sys.stdout", StringIO()) as output:
        assert main(execute_argv) == 0
    assert json.loads(output.getvalue())["status"] == "passed"
    execute_live.assert_called_once()

    tampered = list(execute_argv)
    tampered[tampered.index("30")] = "29"
    with patch("sys.stdout", StringIO()) as output:
        assert main(tampered) == 2
    rejected = json.loads(output.getvalue())
    assert rejected["status"] in {"approval_rejected", "plan_digest_mismatch"}
    assert rejected["mutation_performed"] is False


def test_execute_rejects_resolved_writable_target_drift_before_live_call(
    tmp_path: Path,
) -> None:
    base = [
        "--state-db",
        str(tmp_path / "state.sqlite3"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--limit",
        "1",
        "--max-runtime-seconds",
        "30",
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--probe-nonce-sha256",
        NONCE,
        "--observed-at",
        OBSERVED_AT,
    ]
    primary_env = {
        "COUCHDB_URL": "https://primary-couchdb.invalid",
        "COUCHDB_DB": "primary_source",
        "QDRANT_URL": "https://primary-qdrant.invalid",
        "QDRANT_COLLECTION": "primary_collection",
        "LLM_BRAIN_NEO4J_URI": "bolt://primary-graph.invalid:7687",
    }
    with patch.dict(os.environ, primary_env, clear=False), patch(
        "sys.stdout", StringIO()
    ) as output:
        assert main(base) == 0
    plan = json.loads(output.getvalue())
    assert set(plan["target_fingerprints"]) == {
        "couchdb_source",
        "graph_store",
        "ingress_state_db",
        "projection_ledger",
        "qdrant_collection",
        "runtime_workspace",
    }
    assert all(value.startswith("sha256:") for value in plan["target_fingerprints"].values())
    serialized_plan = json.dumps(plan, sort_keys=True)
    assert "primary-couchdb" not in serialized_plan
    assert "primary-qdrant" not in serialized_plan
    assert "primary-graph" not in serialized_plan

    approval_path = tmp_path / "approval.json"
    execute_argv = [
        *base,
        "--execute",
        "--expected-plan-digest",
        plan["plan_digest"],
        "--approval",
        str(approval_path),
    ]
    approval_path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": CANARY_OPERATION,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "timeout_seconds": 30,
                "rollback_or_abort_criteria": ["abort on target fingerprint mismatch"],
                "target": {"target_fingerprints": plan["target_fingerprints"]},
                "command": {"argv": execute_argv},
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.dict(
            os.environ,
            {**primary_env, "QDRANT_COLLECTION": "drifted_collection"},
            clear=False,
        ),
        patch(
            "agent_knowledge.rag_ingress.projection_invalidation_canary._execute_live",
            side_effect=AssertionError("must not start live execution after target drift"),
        ) as execute_live,
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(execute_argv) == 2
    report = json.loads(output.getvalue())
    assert report["status"] == "plan_digest_mismatch"
    assert report["mutation_performed"] is False
    execute_live.assert_not_called()

    mismatched_target_fingerprints = dict(plan["target_fingerprints"])
    mismatched_target_fingerprints["qdrant_collection"] = "sha256:" + ("b" * 64)
    approval_path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": CANARY_OPERATION,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "timeout_seconds": 30,
                "rollback_or_abort_criteria": ["abort on target fingerprint mismatch"],
                "target": {"target_fingerprints": mismatched_target_fingerprints},
                "command": {"argv": execute_argv},
            }
        ),
        encoding="utf-8",
    )
    with (
        patch.dict(os.environ, primary_env, clear=False),
        patch(
            "agent_knowledge.rag_ingress.projection_invalidation_canary._execute_live",
            side_effect=AssertionError("must not start live execution after approval mismatch"),
        ) as execute_live,
        patch("sys.stdout", StringIO()) as output,
    ):
        assert main(execute_argv) == 2
    approval_report = json.loads(output.getvalue())
    assert approval_report["status"] == "approval_rejected"
    assert approval_report["mutation_performed"] is False
    execute_live.assert_not_called()


def test_execute_live_uses_private_resolved_password_snapshot_without_repr_leak(
    tmp_path: Path,
) -> None:
    password_marker = "snapshot-password-marker"
    args = canary_module._build_parser().parse_args(
        [
            "--state-db",
            str(tmp_path / "private-state.sqlite3"),
            "--ledger",
            str(tmp_path / "private-ledger.sqlite3"),
            "--runtime-dir",
            str(tmp_path / "private-runtime"),
            "--project",
            PROJECT,
            "--expected-source-commit",
            SOURCE_COMMIT,
            "--probe-nonce-sha256",
            NONCE,
            "--observed-at",
            OBSERVED_AT,
            "--couchdb-url",
            "https://private-couchdb.invalid",
            "--couchdb-db",
            "private_source",
            "--couchdb-user",
            "private-user",
            "--couchdb-password-env",
            "CANARY_TEST_PASSWORD",
        ]
    )
    targets = canary_module._resolve_canary_targets(
        args,
        {
            "CANARY_TEST_PASSWORD": password_marker,
            "SESSION_MEMORY_PROJECTION_BACKEND": "qdrant",
            "QDRANT_URL": "https://private-qdrant.invalid",
        },
    )
    rendered = repr(targets)
    for private_value in (
        password_marker,
        "private-couchdb.invalid",
        "private-state.sqlite3",
        "CANARY_TEST_PASSWORD",
    ):
        assert private_value not in rendered
    assert targets.couchdb_password == password_marker

    plan = build_canary_plan(
        project=args.project,
        provider=args.provider,
        probe_nonce_sha256=args.probe_nonce_sha256,
        expected_source_commit=args.expected_source_commit,
        observed_at=args.observed_at,
        limit=args.limit,
        max_runtime_seconds=args.max_runtime_seconds,
        target_fingerprints=targets.target_fingerprints,
    )
    source_store = object()
    projector = object()
    graph_adapter = object()
    with (
        patch.dict(
            os.environ,
            {"CANARY_TEST_PASSWORD": "global-password-drift"},
            clear=False,
        ),
        patch(
            "agent_knowledge.llm_brain_core.couchdb_projection_cli._build_source_store",
            return_value=source_store,
        ) as build_source_store,
        patch.object(canary_module, "_build_qdrant_projector", return_value=projector),
        patch.object(canary_module, "build_graph_adapter_from_env", return_value=graph_adapter),
        patch.object(
            canary_module,
            "run_projection_invalidation_canary",
            return_value={"status": "passed"},
        ),
    ):
        assert canary_module._execute_live(args, plan, resolved_targets=targets) == {
            "status": "passed"
        }
    assert build_source_store.call_args.kwargs["couchdb_password"] == password_marker


def test_malformed_approval_timeout_is_rejected_without_echoing_raw_value(
    tmp_path: Path,
) -> None:
    sensitive_marker = "SENSITIVE_VALUE"
    approval_path = tmp_path / "approval.json"
    base = [
        "--state-db",
        str(tmp_path / "state.sqlite3"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--limit",
        "1",
        "--max-runtime-seconds",
        "30",
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--probe-nonce-sha256",
        NONCE,
        "--observed-at",
        OBSERVED_AT,
    ]
    plan = _cli_plan(base)
    execute_argv = [
        *base,
        "--execute",
        "--expected-plan-digest",
        plan["plan_digest"],
        "--approval",
        str(approval_path),
    ]
    approval_path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": CANARY_OPERATION,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "timeout_seconds": sensitive_marker,
                "rollback_or_abort_criteria": ["abort on mismatch"],
                "target": {"target_fingerprints": plan["target_fingerprints"]},
                "command": {"argv": execute_argv},
            }
        ),
        encoding="utf-8",
    )

    with patch("sys.stdout", StringIO()) as output:
        assert main(execute_argv) == 2

    raw_output = output.getvalue()
    report = json.loads(raw_output)
    assert report["status"] == "approval_rejected"
    assert report["failure_stage"] == "approval"
    assert report["mutation_performed"] is False
    assert sensitive_marker not in raw_output


def test_cli_hard_timeout_interrupts_live_execution_and_reports_resumable_state(
    tmp_path: Path,
) -> None:
    approval_path = tmp_path / "approval.json"
    base = [
        "--state-db",
        str(tmp_path / "state.sqlite3"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--limit",
        "1",
        "--max-runtime-seconds",
        "0.01",
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--probe-nonce-sha256",
        NONCE,
        "--observed-at",
        OBSERVED_AT,
    ]
    plan = _cli_plan(base)
    execute_argv = [
        *base,
        "--execute",
        "--expected-plan-digest",
        plan["plan_digest"],
        "--approval",
        str(approval_path),
    ]
    approval_path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": CANARY_OPERATION,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "timeout_seconds": 1,
                "rollback_or_abort_criteria": ["abort on timeout"],
                "target": {"target_fingerprints": plan["target_fingerprints"]},
                "command": {"argv": execute_argv},
            }
        ),
        encoding="utf-8",
    )

    def _slow_execute(*_args, **_kwargs):
        time.sleep(0.1)
        raise AssertionError("hard timeout did not interrupt")

    with patch(
        "agent_knowledge.rag_ingress.projection_invalidation_canary._execute_live",
        side_effect=_slow_execute,
    ), patch("sys.stdout", StringIO()) as output:
        assert main(execute_argv) == 1
    failed = json.loads(output.getvalue())
    assert failed["failure_stage"] == "hard_timeout"
    assert failed["error_class"] == "CanaryTimeout"
    assert failed["mutation_performed"] is True
    assert failed["mutation_may_have_occurred"] is True
    assert failed["resumable_after_partial_failure"] is True
    assert failed["resume_requires_fresh_probe_nonce"] is True

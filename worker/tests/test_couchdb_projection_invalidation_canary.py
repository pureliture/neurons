from __future__ import annotations

import json
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
    plan = build_canary_plan(
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256=NONCE,
        expected_source_commit=SOURCE_COMMIT,
        observed_at=OBSERVED_AT,
        limit=1,
        max_runtime_seconds=30,
    )
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
    plan = build_canary_plan(
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256=NONCE,
        expected_source_commit=SOURCE_COMMIT,
        observed_at=OBSERVED_AT,
        limit=1,
        max_runtime_seconds=30,
    )
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
    plan = build_canary_plan(
        project=PROJECT,
        provider=PROVIDER,
        probe_nonce_sha256=NONCE,
        expected_source_commit=SOURCE_COMMIT,
        observed_at=OBSERVED_AT,
        limit=1,
        max_runtime_seconds=0.01,
    )
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

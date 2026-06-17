from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from agent_knowledge.rag_ingress.product_surface_switch_plan import (
    build_m9_product_surface_switch_plan,
)
from agent_knowledge.rag_ingress.retirement_readiness import (
    build_m9_derived_memory_authority_evidence_packet,
    build_m9_legacy_retirement_plan,
    build_m9_legacy_retirement_readiness_report,
    build_m9_product_surface_evidence_packet,
)
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from agent_knowledge.rag_ingress.state_shadow_readiness import (
    build_state_shadow_readiness_report,
)


def _private_dir(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return private


def _legacy_ledger(path: Path, *, queued: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE knowledge_items (
                knowledge_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                ingress_job_id TEXT DEFAULT ''
            )
            """
        )
        for index in range(queued):
            connection.execute(
                """
                INSERT INTO knowledge_items (knowledge_id, status, ingress_job_id)
                VALUES (?, 'queued', ?)
                """,
                (f"kn_{index}", f"job_{index}"),
            )


def _accepted_product_surface_packet() -> dict[str, object]:
    return build_m9_product_surface_evidence_packet(
        dry_run=True,
        redact_paths=True,
        reason="unit product surface evidence",
        mcp_state_db_recall_configured=True,
        codex_hook_state_db_recall_configured=True,
        session_entry_hook_state_db_recall_configured=True,
        mcp_evidence_mode="config",
        codex_hook_evidence_mode="config",
        session_entry_hook_evidence_mode="config",
    )


def _accepted_authority_packet() -> dict[str, object]:
    return build_m9_derived_memory_authority_evidence_packet(
        dry_run=True,
        redact_paths=True,
        reason="unit authority evidence",
        authority="renamed-ledger-owned",
        reviewed_authority_disposition=True,
    )


def test_state_shadow_readiness_missing_candidate_does_not_create_db(tmp_path: Path):
    private = _private_dir(tmp_path)
    state_db = private / "missing-state.sqlite"
    ledger = private / "legacy-ledger.sqlite"
    queue = private / "queue"
    _legacy_ledger(ledger)
    queue.mkdir()

    report = build_state_shadow_readiness_report(
        state_db_path=state_db,
        legacy_ledger_path=ledger,
        queue_root=queue,
        dry_run=True,
        redact_paths=True,
    )

    assert not state_db.exists()
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["raw_paths_printed"] is False
    assert report["state_db_candidate"]["exists"] is False
    assert "state_db_candidate_missing" in report["blocking_codes"]
    assert report["cutover_status"] == "cutover_blocked"


def test_state_shadow_readiness_empty_candidate_is_green_but_not_cutover(tmp_path: Path):
    private = _private_dir(tmp_path)
    state_db = private / "rag-ingress-state.sqlite"
    ledger = private / "legacy-ledger.sqlite"
    queue = private / "queue"
    RAGIngressStateDB(state_db)
    _legacy_ledger(ledger)
    queue.mkdir()

    report = build_state_shadow_readiness_report(
        state_db_path=state_db,
        legacy_ledger_path=ledger,
        queue_root=queue,
        dry_run=True,
        redact_paths=True,
        now_iso="2026-06-13T00:00:00+00:00",
    )

    assert report["blockers"] == []
    assert report["shadow_readiness_status"] == "shadow_ready_pending_external_gates"
    assert report["production_authority_status"] == "NO-GO"
    assert report["cutover_status"] == "cutover_blocked"
    assert report["soak"]["green_this_run"] is True
    assert report["soak"]["consecutive_green_runs"] == 1


def test_state_shadow_readiness_blocks_undispositioned_legacy_mismatch(tmp_path: Path):
    private = _private_dir(tmp_path)
    state_db = private / "rag-ingress-state.sqlite"
    ledger = private / "legacy-ledger.sqlite"
    queue = private / "queue"
    RAGIngressStateDB(state_db)
    _legacy_ledger(ledger, queued=1)
    queue.mkdir()

    report = build_state_shadow_readiness_report(
        state_db_path=state_db,
        legacy_ledger_path=ledger,
        queue_root=queue,
        dry_run=True,
        redact_paths=True,
    )

    assert report["legacy_ledger"]["queued_ingress_count"] == 1
    assert report["parity_summary"]["legacy_shadow_mismatch_dispositioned"] is False
    assert "legacy_shadow_queued_count_mismatch" in report["blocking_codes"]


def test_product_surface_switch_plan_is_redacted_and_approval_gated():
    raw_ledger = "/private/example-agent-knowledge/ledger.sqlite"
    raw_state = "/private/example-agent-knowledge/rag-ingress-state.sqlite"
    raw_ragflow = "http://127.0.0.1:19380"

    plan = build_m9_product_surface_switch_plan(
        dry_run=True,
        redact_paths=True,
        reason="unit switch plan",
        agent_knowledge_command="agent-knowledge",
        project="workspace-ragflow-advisor",
        ledger_path=raw_ledger,
        state_db_recall=raw_state,
        dataset_ids=("ds_transcript_memory",),
        ragflow_url=raw_ragflow,
        token_env="RAGFLOW_API_KEY",
    )

    encoded = json.dumps(plan, sort_keys=True)
    assert plan["status"] == "ready_to_approve"
    assert plan["mutation_performed"] is False
    assert plan["provider_config_mutation_performed"] is False
    assert plan["network_used"] is False
    assert plan["approval_required_before_live_mutation"] is True
    assert raw_ledger not in encoded
    assert raw_state not in encoded
    assert raw_ragflow not in encoded
    assert "ds_transcript_memory" not in encoded


def test_product_surface_switch_plan_rejects_non_dry_run():
    with pytest.raises(ValueError, match="requires --dry-run"):
        build_m9_product_surface_switch_plan(
            dry_run=False,
            redact_paths=True,
            reason="unit switch plan",
            agent_knowledge_command="agent-knowledge",
            project="workspace-ragflow-advisor",
            ledger_path="/private/example/ledger.sqlite",
            state_db_recall="/private/example/state.sqlite",
            dataset_ids=("ds_transcript_memory",),
        )


def test_retirement_readiness_blocks_without_live_evidence_packets(tmp_path: Path):
    private = _private_dir(tmp_path)
    state_db = private / "rag-ingress-state.sqlite"
    ledger = private / "legacy-ledger.sqlite"
    queue = private / "queue"
    RAGIngressStateDB(state_db)
    _legacy_ledger(ledger)
    queue.mkdir()

    report = build_m9_legacy_retirement_readiness_report(
        state_db_path=state_db,
        legacy_ledger_path=ledger,
        queue_root=queue,
        dry_run=True,
        redact_paths=True,
        derived_memory_authority="not-evaluated",
    )

    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["legacy_retirement_status"] == "NO-GO"
    assert "product_surface_evidence_packet_required_for_live_closure" in report["blocking_codes"]
    assert "derived_memory_authority_evidence_packet_required_for_live_closure" in report["blocking_codes"]


def test_retirement_readiness_accepts_digest_bound_evidence_packets(tmp_path: Path):
    private = _private_dir(tmp_path)
    state_db = private / "rag-ingress-state.sqlite"
    ledger = private / "legacy-ledger.sqlite"
    queue = private / "queue"
    RAGIngressStateDB(state_db)
    _legacy_ledger(ledger)
    queue.mkdir()

    report = build_m9_legacy_retirement_readiness_report(
        state_db_path=state_db,
        legacy_ledger_path=ledger,
        queue_root=queue,
        dry_run=True,
        redact_paths=True,
        derived_memory_authority="not-evaluated",
        product_surface_evidence=_accepted_product_surface_packet(),
        derived_memory_authority_evidence=_accepted_authority_packet(),
    )

    assert report["retirement_readiness_status"] == "retirement_ready_for_operator_approval"
    assert report["legacy_retirement_status"] == "ready_for_operator_approval"
    assert report["blockers"] == []
    assert report["product_surface"]["evidence_source"] == "packet"
    assert report["derived_memory_authority"]["evidence_source"] == "packet"


def test_legacy_retirement_plan_stays_blocked_until_readiness_is_ready(tmp_path: Path):
    private = _private_dir(tmp_path)
    state_db = private / "rag-ingress-state.sqlite"
    ledger = private / "legacy-ledger.sqlite"
    queue = private / "queue"
    RAGIngressStateDB(state_db)
    _legacy_ledger(ledger)
    queue.mkdir()
    readiness = build_m9_legacy_retirement_readiness_report(
        state_db_path=state_db,
        legacy_ledger_path=ledger,
        queue_root=queue,
        dry_run=True,
        redact_paths=True,
        derived_memory_authority="not-evaluated",
    )

    plan = build_m9_legacy_retirement_plan(
        dry_run=True,
        redact_paths=True,
        reason="unit legacy retirement plan",
        readiness_report=readiness,
    )

    assert plan["status"] == "blocked"
    assert plan["mutation_performed"] is False
    assert plan["network_used"] is False
    assert plan["approval_required_before_live_mutation"] is True
    assert "retirement_readiness_report_not_ready" in plan["blocking_codes"]

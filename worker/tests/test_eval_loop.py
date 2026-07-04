from __future__ import annotations

import json

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory import eval_cli
from agent_knowledge.session_memory.eval_loop import run_enabled_eval_queries
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span


PROJECT = "neurons"
PROVIDER = "hermes"


def _candidate(**overrides):
    span = {
        "source_ref": {"source_id": "src_eval"},
        "span_ref": {"span_id": "span_eval"},
        "content_hash": "sha256:eval-card",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": PROVIDER,
        "title": "eval loop implementation",
        "redacted_summary": "Enabled eval queries must write eval_runs and retrieval_audit rows.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "persist eval loop results",
            "blocker": None,
            "owner_hint": PROVIDER,
            "status": "active",
        },
        "confidence": 0.91,
        "confidence_basis": "operator-approved eval fixture",
    }
    span.update(overrides)
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="eval")


def _count_table(ledger: Ledger, table: str) -> int:
    with ledger._connect() as connection:
        row = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def _eval_run_rows(ledger: Ledger) -> list[dict]:
    with ledger._connect() as connection:
        rows = connection.execute("SELECT * FROM eval_runs ORDER BY created_at, run_id").fetchall()
    return [dict(row) for row in rows]


def _context_item_rows(ledger: Ledger) -> list[dict]:
    with ledger._connect() as connection:
        rows = connection.execute("SELECT * FROM context_pack_items ORDER BY pack_id, item_index").fetchall()
    return [dict(row) for row in rows]


def _eval_run_ids(ledger: Ledger) -> list[str]:
    return [row["run_id"] for row in _eval_run_rows(ledger)]


def test_enabled_eval_queries_write_eval_run_and_retrieval_audit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    card = LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        _candidate(), approved_by="ddalkak", decision_id="eval-decision"
    )["accepted_card"]
    ledger.upsert_eval_query(
        {
            "query_id": "eval_current_task",
            "query_hash": "sha256:eval-current-task",
            "query_terms": ["eval", "loop", "current", "task"],
            "project": PROJECT,
            "provider": PROVIDER,
            "expected_memory_ids": [card["memory_id"]],
            "k": 5,
            "min_recall": 1.0,
            "min_precision": 0.2,
            "enabled": True,
        }
    )

    result = run_enabled_eval_queries(
        ledger=ledger,
        project=PROJECT,
        provider=PROVIDER,
        execute=True,
        run_id="eval_run_test",
    )

    assert result["schema_version"] == "llm_brain_eval_loop.v1"
    assert result["status"] == "pass"
    assert result["execute"] is True
    assert result["mutation_performed"] is True
    assert result["network_used"] is False
    assert result["metrics"]["query_count"] == 1
    assert result["metrics"]["passed_count"] == 1
    assert result["metrics"]["failed_count"] == 0
    assert result["run_id"] == "eval_run_test"

    assert _count_table(ledger, "retrieval_audit") == 1
    assert _count_table(ledger, "eval_runs") == 1
    run = _eval_run_rows(ledger)[0]
    assert run["run_id"] == "eval_run_test"
    assert run["status"] == "pass"
    assert json.loads(run["metrics_json"])["query_count"] == 1
    assert json.loads(run["failures_json"]) == []
    assert run["mutation_performed"] == 1

    audit = ledger.list_retrieval_audit()[0]
    assert audit["query_hash"] == "sha256:eval-current-task"
    assert audit["result_count"] >= 1
    assert audit["private_allowed"] == 0

    items = _context_item_rows(ledger)
    assert items[0]["kind"] == "memory_card"
    assert items[0]["reference_id"] == card["memory_id"]
    assert items[0]["summary"] == "[approved-memory-summary-not-persisted]"


def test_eval_cli_returns_success_when_eval_fails_but_storage_succeeds(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        _candidate(), approved_by="ddalkak", decision_id="eval-decision"
    )
    ledger.upsert_eval_query(
        {
            "query_id": "eval_expected_missing",
            "query_hash": "sha256:eval-expected-missing",
            "query_terms": ["eval", "loop", "current", "task"],
            "project": PROJECT,
            "provider": PROVIDER,
            "expected_memory_ids": ["missing-memory-card"],
            "k": 5,
            "min_recall": 1.0,
            "min_precision": 1.0,
            "enabled": True,
        }
    )

    rc = eval_cli.main(
        [
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--provider",
            PROVIDER,
            "--execute",
            "--retain-runs",
            "1",
            "--run-id",
            "eval_run_cli_quality_fail",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "fail"
    assert payload["evaluation_status"] == "fail"
    assert payload["mutation_performed"] is True
    assert payload["network_used"] is False
    assert payload["metrics"]["query_count"] == 1
    assert payload["metrics"]["failed_count"] == 1
    assert payload["failure_count"] == 1
    assert payload["run_id_hash"].startswith("sha256:")
    assert payload["retention"]["enabled"] is True
    assert payload["retention"]["retain_runs"] == 1
    assert payload["retention"]["deleted_run_count"] == 0
    assert "run_id" not in payload
    assert "failures" not in payload
    assert "per_query" not in payload["metrics"]
    assert _count_table(ledger, "eval_runs") == 1
    assert _count_table(ledger, "retrieval_audit") == 1


def test_eval_loop_retains_only_latest_runs_and_prunes_owned_audit_rows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    card = LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        _candidate(), approved_by="ddalkak", decision_id="eval-decision"
    )["accepted_card"]
    ledger.upsert_eval_query(
        {
            "query_id": "eval_current_task",
            "query_hash": "sha256:eval-current-task",
            "query_terms": ["eval", "loop", "current", "task"],
            "project": PROJECT,
            "provider": PROVIDER,
            "expected_memory_ids": [card["memory_id"]],
            "k": 5,
            "min_recall": 1.0,
            "min_precision": 0.2,
            "enabled": True,
        }
    )

    run_enabled_eval_queries(ledger=ledger, project=PROJECT, provider=PROVIDER, execute=True, run_id="eval_run_001")
    run_enabled_eval_queries(ledger=ledger, project=PROJECT, provider=PROVIDER, execute=True, run_id="eval_run_002")
    result = run_enabled_eval_queries(
        ledger=ledger,
        project=PROJECT,
        provider=PROVIDER,
        execute=True,
        run_id="eval_run_003",
        retain_runs=2,
    )

    assert result["retention"] == {
        "enabled": True,
        "retain_runs": 2,
        "candidate_run_count": 3,
        "deleted_run_count": 1,
        "deleted_context_pack_count": 1,
        "deleted_context_pack_item_count": 1,
        "deleted_retrieval_audit_count": 1,
    }
    assert _eval_run_ids(ledger) == ["eval_run_002", "eval_run_003"]
    assert _count_table(ledger, "retrieval_audit") == 2
    assert _count_table(ledger, "context_packs") == 2
    assert _count_table(ledger, "context_pack_items") == 2

import json
from pathlib import Path

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.backfill import (
    build_execute_plan,
    dry_run_backfill,
    inventory_fixture_sources,
)


PROJECT = "workspace-ragflow-advisor"


class ProviderContract:
    def __init__(self, provider: str):
        self.provider = provider

    def to_record(self) -> dict:
        return {
            "provider": self.provider,
            "contract_id": f"{self.provider}-contract",
            "provider_version": "fixture",
            "hook_event": "Stop",
            "source_locator_field": "transcript_path",
            "parser_version": "fixture.v1",
            "verification_status": "source_locator_verified_current_smoke",
            "source_status": "source_locator_verified",
            "hook_install_status": "not_applicable",
            "evidence_hash": f"sha256:{self.provider}",
            "raw_prompt_policy": "never",
        }


def _seed_contracts(ledger: Ledger) -> None:
    for provider in ("claude", "gemini", "codex"):
        ledger.upsert_provider_source_contract(ProviderContract(provider))


def _write_provider_fixture(path: Path, *, provider: str, session_id: str = "phase-07-session") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "provider_transcript_fixture.v1",
        "provider": provider,
        "session_id": session_id,
        "started_at": "2026-05-12T10:00:00+09:00",
        "ended_at": "2026-05-12T10:01:00+09:00",
    }
    if provider == "claude":
        payload["messages"] = [
            {"role": "user", "content": "Fixture-only source."},
            {"role": "assistant", "content": "This is local test data."},
        ]
    else:
        payload["turns"] = [
            {"role": "user", "text": "Fixture-only source."},
            {"role": "assistant", "text": "This is local test data."},
        ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture_tree(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    root = tmp_path / "fixtures"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".agent-knowledge-backfill-fixture-root").write_text("", encoding="utf-8")
    paths = {
        "claude_ok": _write_provider_fixture(root / "claude" / "ok.json", provider="claude"),
        "claude_bad": root / "claude" / "bad.json",
        "gemini_ok": _write_provider_fixture(root / "gemini" / "ok.json", provider="gemini"),
        "codex_ok": _write_provider_fixture(root / "codex" / "ok.json", provider="codex"),
        "unknown": root / "unknown" / "notes.txt",
    }
    paths["claude_bad"].write_text("{not-json", encoding="utf-8")
    paths["unknown"].parent.mkdir(parents=True, exist_ok=True)
    paths["unknown"].write_text("unsupported source", encoding="utf-8")
    return root, paths


def _assert_no_raw_paths(payload: dict, paths: dict[str, Path], root: Path) -> None:
    serialized = json.dumps(payload, sort_keys=True)
    assert str(root) not in serialized
    for path in paths.values():
        assert str(path) not in serialized


def test_backfill_inventory_stores_raw_paths_only_in_private_ledger(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    root, paths = _fixture_tree(tmp_path)
    _seed_contracts(ledger)

    payload = inventory_fixture_sources(ledger=ledger, fixture_source_root=root, project=PROJECT)

    assert payload["schema_version"] == "agent_knowledge_backfill_inventory.v1"
    assert payload["status"] == "inventory_recorded"
    assert payload["private_source_scan_performed"] is False
    assert payload["network_used"] is False
    assert payload["live_mutation_allowed"] is False
    assert payload["summary"] == {"discovered": 5, "raw_paths_redacted": True}
    _assert_no_raw_paths(payload, paths, root)

    rows = ledger.list_backfill_sources()
    assert len(rows) == 5
    assert {row["provider"] for row in rows} == {"claude", "gemini", "codex", "unknown"}
    assert {row["raw_source_path"] for row in rows} == {str(path) for path in paths.values()}
    assert all(row["source_path_hash"].startswith("sha256:") for row in rows)


def test_backfill_dry_run_reports_contract_parser_counts_and_redacted_quarantine(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    root, paths = _fixture_tree(tmp_path)
    _seed_contracts(ledger)
    inventory_fixture_sources(ledger=ledger, fixture_source_root=root, project=PROJECT)

    payload = dry_run_backfill(ledger=ledger, batch_limit=2, rate_limit_per_minute=30)

    assert payload["schema_version"] == "agent_knowledge_backfill_dry_run.v1"
    assert payload["status"] == "dry_run_ready"
    assert payload["private_source_scan_performed"] is False
    assert payload["live_indexing_performed"] is False
    assert payload["mutation_performed"] is False
    assert payload["counts"]["by_status"] == {
        "failed": 0,
        "indexed": 0,
        "quarantined": 2,
        "ready": 3,
        "skipped": 0,
    }
    assert payload["counts"]["by_provider_contract_status"] == {
        "source_locator_verified_current_smoke": 4,
        "unsupported_provider": 1,
    }
    assert payload["counts"]["by_parser_status"] == {
        "parsed_ok": 3,
        "source_parse_failed": 1,
        "unsupported_provider": 1,
    }
    assert payload["quarantine"]["count"] == 2
    assert {item["reason"] for item in payload["quarantine"]["items"]} == {
        "source_parse_failed",
        "unsupported_provider",
    }
    assert all("raw_source_path" not in item for item in payload["sources"])
    ready_sources = [item for item in payload["sources"] if item["inventory_status"] == "ready"]
    assert all(item["quality_manifest"]["status"] == "pass_with_boundaries" for item in ready_sources)
    assert all(item["quality_manifest"]["coverage_manifest"]["parsed_turn_count"] == 2 for item in ready_sources)
    assert all(item["quality_manifest"]["retrieval_quality_boundary"]["status"] == "not_evaluated_no_network" for item in ready_sources)
    _assert_no_raw_paths(payload, paths, root)


def test_backfill_inventory_rejects_non_fixture_source_root_before_scan(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    source_root = tmp_path / "not-fixtures"
    private_source = _write_provider_fixture(source_root / "claude" / "secret.json", provider="claude")

    with pytest.raises(ValueError, match="explicit fixture directory"):
        inventory_fixture_sources(ledger=ledger, fixture_source_root=source_root, project=PROJECT)

    assert ledger.list_backfill_sources() == []
    assert private_source.exists()


def test_backfill_inventory_rejects_fixture_directory_missing_sentinel_before_scan(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    root = tmp_path / "fixtures"
    raw_source = _write_provider_fixture(root / "claude" / "secret.json", provider="claude")

    with pytest.raises(ValueError, match="contain the backfill fixture sentinel"):
        inventory_fixture_sources(ledger=ledger, fixture_source_root=root, project=PROJECT)

    assert ledger.list_backfill_sources() == []
    assert raw_source.exists()


def test_backfill_execute_plan_is_bounded_non_mutating_and_path_redacted(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    root, paths = _fixture_tree(tmp_path)
    _seed_contracts(ledger)
    inventory_fixture_sources(ledger=ledger, fixture_source_root=root, project=PROJECT)
    dry_run_backfill(ledger=ledger, batch_limit=2, rate_limit_per_minute=30)

    payload = build_execute_plan(ledger=ledger, batch_limit=2, rate_limit_per_minute=30)

    assert payload["schema_version"] == "agent_knowledge_backfill_execute_plan.v1"
    assert payload["status"] == "plan_only"
    assert payload["requires_approval_before_execution"] is True
    assert payload["private_source_scan_performed"] is False
    assert payload["live_indexing_performed"] is False
    assert payload["mutation_performed"] is False
    assert payload["limits"] == {"batch_limit": 2, "rate_limit_per_minute": 30}
    assert payload["plan"]["ready_source_count"] == 3
    assert payload["plan"]["batch_count"] == 2
    assert payload["plan"]["batches"][0]["source_count"] == 2
    assert payload["plan"]["batches"][1]["source_count"] == 1
    assert all(
        "raw_source_path" not in source
        for batch in payload["plan"]["batches"]
        for source in batch["sources"]
    )
    _assert_no_raw_paths(payload, paths, root)


def test_backfill_rejects_unbounded_execute_plan_limits(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")

    with pytest.raises(ValueError, match="batch-limit"):
        build_execute_plan(ledger=ledger, batch_limit=0, rate_limit_per_minute=30)

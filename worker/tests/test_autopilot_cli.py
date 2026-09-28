from __future__ import annotations

import json
import sys

import agent_knowledge.session_memory.autopilot_cli as autopilot_cli

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.autopilot_cli import (
    RETIRED_BRIDGE_LIVE_MINING_BLOCKED_EXIT,
    PG_STORE_REQUIRED_EXIT,
    main,
    mine_live_candidates,
    run_autopilot_command,
)


class _FakeRetiredIndexBridge:
    def __init__(self, chunks, completion):
        self._chunks = chunks
        self._completion = completion

    def list_transcript_memory_chunks(self, *, project, query="", limit=200, **_):
        return [dict(c, project=project) for c in self._chunks]

    def list_session_memory_chunks(self, *, project, provider="", limit=200, **_):
        return [dict(c, project=project) for c in self._chunks]

    def chat_completion(self, messages, *, llm_id=""):
        return self._completion


PROJECT = "neurons"


def _candidate(**overrides):
    span = {
        "source_ref": {"source_id": "src"},
        "span_ref": {"span_id": "span"},
        "content_hash": "sha256:2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "auth approach",
        "redacted_summary": "Auth uses JWT.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "ship login",
            "blocker": None,
            "owner_hint": "codex",
            "status": "active",
        },
        "confidence": 0.92,
        "confidence_basis": "operator-approved",
    }
    span.update(overrides)
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="wm")


def test_run_autopilot_command_uses_pg_for_recall(tmp_path):
    from test_autopilot_loop import _StewardStore

    store = _StewardStore()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    result = run_autopilot_command(ledger=ledger, pgvector_store=store,
                                   candidates=[_candidate()], project=PROJECT, refresh_watermark="wm")
    assert result["cycle"]["accepted_count"] == 1
    assert result["recall"]["current_count"] == 1
    assert ledger.list_llm_brain_memory_cards() == []


def test_run_autopilot_command_requires_pg_before_any_sqlite_write(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    with pytest.raises(ValueError, match="PostgreSQL steward store is required"):
        run_autopilot_command(ledger=ledger, candidates=[_candidate()], project=PROJECT,
                              refresh_watermark="wm")
    assert ledger.list_llm_brain_memory_cards() == []


_ENVELOPE_COMPLETION = (
    '[{"card_type": "decision", "title": "auth method", "statement": "Auth now uses OAuth.", '
    '"typed_payload": {"decision": "use OAuth", "rationale": "broader support", '
    '"alternatives": ["JWT"], "consequence": "migration", "authority_ref": "adr-auth"}}]'
)


def test_mine_live_candidates_then_run_command_end_to_end(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge(
        chunks=[{"redacted_text": "auth switched to OAuth", "knowledge_id": "k1", "content_hash": "sha256:d0f631ca1ddba8db3bcfcb9e057cdc98d0379f1bee00e75a545147a27dadd982", "provider": "codex"}],
        completion=_ENVELOPE_COMPLETION,
    )

    candidates = mine_live_candidates(
        retired_index_bridge=retired_index_bridge, project=PROJECT, completion_fn=lambda messages: _ENVELOPE_COMPLETION
    )
    assert len(candidates) == 1
    assert candidates[0]["card_type"] == "decision"
    assert candidates[0]["lifecycle_state"] == "candidate"
    assert candidates[0].get("memory_id")

    ledger = Ledger(tmp_path / "ledger.sqlite")
    with pytest.raises(ValueError, match="PostgreSQL steward store is required"):
        run_autopilot_command(
            ledger=ledger, candidates=candidates, project=PROJECT, refresh_watermark="live"
        )
    assert ledger.list_llm_brain_memory_cards() == []


def test_main_with_candidates_json_blocks_without_pg_before_ledger_construction(tmp_path, capsys, monkeypatch):
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps([_candidate()]), encoding="utf-8")
    def should_not_construct(*args, **kwargs):
        raise AssertionError("SQLite ledger must not be constructed")
    monkeypatch.setattr(autopilot_cli, "Ledger", should_not_construct)
    rc = main(["--ledger", str(tmp_path / "ledger.sqlite"), "--project", PROJECT,
               "--refresh-watermark", "wm", "--candidates-json", str(candidates_path)])
    assert rc != 0
    assert not (tmp_path / "ledger.sqlite").exists()
    assert json.loads(capsys.readouterr().out)["mutation_performed"] is False


def test_main_reads_candidates_json_and_writes_ledger(tmp_path, capsys):
    candidates = [
        _candidate(),
        _candidate(source_ref={"source_id": "s2"}, span_ref={"span_id": "p2"}, content_hash="sha256:d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35"),
    ]
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    ledger_path = tmp_path / "ledger.sqlite"

    rc = main([
        "--ledger", str(ledger_path),
        "--project", PROJECT,
        "--refresh-watermark", "wm",
        "--candidates-json", str(candidates_path),
    ])

    assert rc == PG_STORE_REQUIRED_EXIT
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "blocked_pg_steward_store_required"
    assert out["mutation_performed"] is False
    assert not ledger_path.exists()


def test_main_without_candidates_json_blocks_before_retired_bridge_or_ledger_construction(tmp_path, capsys, monkeypatch):
    def should_not_run(*_args, **_kwargs):
        raise AssertionError("retired bridge live-mining path must not run")

    monkeypatch.setattr(autopilot_cli, "Ledger", should_not_run)
    monkeypatch.setattr(autopilot_cli, "mine_live_candidates", should_not_run)
    monkeypatch.setitem(sys.modules, "agent_knowledge.mcp_server", None)
    monkeypatch.setitem(sys.modules, "agent_knowledge.session_memory.supersede_detector", None)
    monkeypatch.setitem(sys.modules, "agent_knowledge.session_memory.index_projection", None)

    ledger_path = tmp_path / "ledger.sqlite"
    rc = autopilot_cli.main([
        "--ledger", str(ledger_path),
        "--project", PROJECT,
        "--refresh-watermark", "wm",
        "--retired-index-bridge-url", "http://retired.invalid",
        "--retired-index-bridge-token-env", "RETIRED_TOKEN",
        "--policy-proxy-url", "http://policy.invalid",
        "--derived-dataset-id", "legacy-derived-dataset",
        "--llm-id", "legacy-model",
        "--limit", "1",
        "--max-candidates", "1",
    ])

    assert rc == RETIRED_BRIDGE_LIVE_MINING_BLOCKED_EXIT
    assert not ledger_path.exists()
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": "llm_brain_autopilot_command.v1",
        "project": PROJECT,
        "refresh_watermark": "wm",
        "status": "blocked_retired_bridge_live_mining",
        "network_used": False,
        "mutation_performed": False,
    }

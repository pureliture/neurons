from __future__ import annotations

import json

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.autopilot_cli import (
    main,
    mine_live_candidates,
    run_autopilot_command,
)


class _FakeRagflow:
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
        "content_hash": "sha256:x",
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


def test_run_autopilot_command_populates_ledger_and_returns_recall_snapshot(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    candidates = [
        _candidate(),
        _candidate(source_ref={"source_id": "s2"}, span_ref={"span_id": "p2"}, content_hash="sha256:2"),
    ]

    result = run_autopilot_command(
        ledger=ledger,
        candidates=candidates,
        project=PROJECT,
        refresh_watermark="wm",
    )

    assert result["cycle"]["accepted_count"] == 2
    assert result["cycle"]["needs_review_count"] == 0
    assert result["recall"]["current_count"] == 2


_ENVELOPE_COMPLETION = (
    '[{"card_type": "decision", "title": "auth method", "statement": "Auth now uses OAuth.", '
    '"typed_payload": {"decision": "use OAuth", "rationale": "broader support", '
    '"alternatives": ["JWT"], "consequence": "migration", "authority_ref": "adr-auth"}}]'
)


def test_mine_live_candidates_then_run_command_end_to_end(tmp_path):
    ragflow = _FakeRagflow(
        chunks=[{"redacted_text": "auth switched to OAuth", "knowledge_id": "k1", "content_hash": "sha256:c1", "provider": "codex"}],
        completion=_ENVELOPE_COMPLETION,
    )

    candidates = mine_live_candidates(
        ragflow=ragflow, project=PROJECT, completion_fn=lambda messages: _ENVELOPE_COMPLETION
    )
    assert len(candidates) == 1
    assert candidates[0]["card_type"] == "decision"
    assert candidates[0]["lifecycle_state"] == "candidate"
    assert candidates[0].get("memory_id")

    ledger = Ledger(tmp_path / "ledger.sqlite")
    result = run_autopilot_command(
        ledger=ledger, candidates=candidates, project=PROJECT, refresh_watermark="live"
    )
    assert result["cycle"]["accepted_count"] == 1
    assert result["recall"]["current_count"] == 1


def test_main_reads_candidates_json_and_writes_ledger(tmp_path, capsys):
    candidates = [
        _candidate(),
        _candidate(source_ref={"source_id": "s2"}, span_ref={"span_id": "p2"}, content_hash="sha256:2"),
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

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cycle"]["accepted_count"] == 2
    stored = Ledger(ledger_path).list_llm_brain_memory_cards(accepted_only=True, current_only=True)
    assert len(stored) == 2

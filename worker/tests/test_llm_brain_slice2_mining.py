from __future__ import annotations

import json

import pytest

from agent_knowledge.memory_miner import (
    build_immediate_candidate_enqueue,
    build_memory_card_candidate_from_source_span,
    memory_card_candidate_idempotency_key,
    mine_refresh_cycle_candidates,
)


PROJECT = "workspace-ragflow-advisor"


def _decision_payload():
    return {
        "decision": "Use refresh-cycle candidate mining.",
        "rationale": "Provider-specific session end events are ambiguous.",
        "alternatives": ["Mine only on session end"],
        "consequence": "Mining can run idempotently on every refresh cycle.",
        "authority_ref": "session-memory-decision-2",
    }


def _drift_payload(*, severity="high"):
    return {
        "subject": "Architecture currentness",
        "expected_state": "Latest session-memory decision is authority.",
        "observed_state": "Historical docs remain searchable.",
        "drift_kind": "currentness",
        "severity": severity,
        "authority_lane": "design",
        "source_precedence_rank": 1,
        "resolution_action": "needs_review",
        "suggested_action": "Create a drift review candidate.",
        "basis_refs": [{"source_id": "src_drift", "span_id": "span_drift"}],
    }


def _source_span(**overrides):
    span = {
        "source_ref": {"source_id": "src_decision_2"},
        "span_ref": {"span_id": "span_decision_2"},
        "content_hash": "sha256:decision2",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "Refresh-cycle mining",
        "redacted_summary": "Refresh-cycle mining creates idempotent candidates.",
        "typed_payload": _decision_payload(),
        "confidence": 0.74,
        "confidence_basis": "redacted session-memory span",
    }
    span.update(overrides)
    return span


def test_refresh_candidate_identity_ignores_refresh_watermark_for_same_span():
    first = build_memory_card_candidate_from_source_span(
        _source_span(), refresh_watermark="2026-06-13T00:00:00Z"
    )
    second = build_memory_card_candidate_from_source_span(
        _source_span(), refresh_watermark="2026-06-13T01:00:00Z"
    )

    assert first["candidate_id"] == second["candidate_id"]
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["refresh_watermark"] != second["refresh_watermark"]
    assert first["lifecycle_state"] == "candidate"
    assert first["approval_state"] == "suggested"


def test_refresh_cycle_batch_dedupes_by_idempotency_key():
    report = mine_refresh_cycle_candidates(
        [_source_span(), _source_span()],
        refresh_watermark="2026-06-13T00:00:00Z",
    )

    assert report["candidate_count"] == 1
    assert report["skipped"] == [{"index": 1, "reason": "duplicate_idempotency_key"}]


def test_candidate_key_changes_when_span_or_content_changes():
    base = memory_card_candidate_idempotency_key(_source_span())
    changed_span = memory_card_candidate_idempotency_key(
        _source_span(span_ref={"span_id": "span_decision_3"})
    )
    changed_content = memory_card_candidate_idempotency_key(
        _source_span(content_hash="sha256:decision3")
    )

    assert len({base, changed_span, changed_content}) == 3


def test_candidate_mining_rejects_raw_transcript_copy_and_private_locator():
    with pytest.raises(ValueError, match="raw transcript"):
        build_memory_card_candidate_from_source_span(
            _source_span(raw_transcript="private session body"),
            refresh_watermark="w",
        )
    with pytest.raises(ValueError, match="opaque|forbidden"):
        build_memory_card_candidate_from_source_span(
            _source_span(source_ref={"path": "/Users/example/.codex/transcripts/raw.jsonl"}),
            refresh_watermark="w",
        )
    with pytest.raises(ValueError, match="forbidden"):
        build_memory_card_candidate_from_source_span(
            _source_span(redacted_summary="raw transcript body: private text"),
            refresh_watermark="w",
        )


def test_candidate_contains_refs_hashes_and_redacted_summary_not_source_body():
    candidate = build_memory_card_candidate_from_source_span(
        _source_span(), refresh_watermark="2026-06-13T00:00:00Z"
    )

    serialized = json.dumps(candidate, sort_keys=True)
    assert candidate["source_refs"][0]["source_id"] == "src_decision_2"
    assert candidate["source_refs"][0]["source_owner"] == "transcript_memory_canonical_store"
    assert candidate["source_refs"][0]["access_mode"] == "source_ref_only"
    assert candidate["span_refs"][0]["span_id"] == "span_decision_2"
    assert candidate["span_refs"][0]["source_owner"] == "transcript_memory_canonical_store"
    assert candidate["span_refs"][0]["access_mode"] == "span_ref_only"
    assert candidate["evidence_hashes"] == ["sha256:decision2"]
    assert "private session body" not in serialized
    assert "raw_transcript" not in serialized


def test_immediate_candidate_enqueue_accepts_high_signal_event_without_write():
    event = _source_span(
        event_kind="commit",
        refresh_watermark="immediate-commit",
        content_hash="sha256:commit1",
        source_ref={"source_id": "commit_abc"},
        span_ref={"span_id": "commit_abc"},
    )

    record = build_immediate_candidate_enqueue(event)

    assert record["event_kind"] == "commit"
    assert record["enqueue_mode"] == "fast_path"
    assert record["write_performed"] is False
    assert record["candidate"]["mining_reason"] == "commit"


def test_immediate_high_severity_drift_requires_high_drift_payload():
    low_event = _source_span(
        event_kind="high_severity_drift",
        card_type="drift",
        typed_payload=_drift_payload(severity="medium"),
        content_hash="sha256:drift1",
    )
    with pytest.raises(ValueError, match="severity=high"):
        build_immediate_candidate_enqueue(low_event)

    high_event = _source_span(
        event_kind="high_severity_drift",
        card_type="drift",
        title="High severity drift",
        typed_payload=_drift_payload(severity="high"),
        content_hash="sha256:drift2",
    )
    record = build_immediate_candidate_enqueue(high_event)

    assert record["candidate"]["card_type"] == "drift"
    assert record["candidate"]["typed_payload"]["severity"] == "high"

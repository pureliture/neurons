"""Batch response resilience: tail-only repairs + one retry.

Captured production slips (2026-10-12): a missing final slot brace, a stray
trailing closer, and code-fence wrappers. Repairs are strictly tail/wrapper
level; slot identity, order, duplicate and typed_payload validation must keep
rejecting real contract violations (no slot repair, no content repair).
"""

import json

import pytest

from agent_knowledge.session_memory.batch_miner import (
    BatchExtractionError,
    BatchedEnvelopeMiner,
    _batch_source_key,
    _parse_batch_items,
)
from agent_knowledge.session_memory.extraction_llm import ExtractionTransportError

KEY_A = "sha256:" + "a" * 64
KEY_B = "sha256:" + "b" * 64


def _decision_candidate():
    return {
        "card_type": "decision",
        "title": "Boundary approved",
        "statement": "Keep the approved boundary.",
        "typed_payload": {
            "decision": "Keep the approved boundary",
            "rationale": "Explicit approval",
            "alternatives": [],
            "consequence": "Boundary remains",
            "authority_ref": "session",
        },
    }


def _slot(key):
    return {"source_key": key, "candidate": _decision_candidate()}


def _good(keys=(KEY_A,)):
    return json.dumps([_slot(k) for k in keys], separators=(",", ":"), ensure_ascii=False)


def test_exact_response_parses():
    items = _parse_batch_items(_good(), [KEY_A])
    assert len(items) == 1
    assert items[0]["card_type"] == "decision"


def test_code_fenced_response_is_stripped():
    items = _parse_batch_items("```json\n" + _good() + "\n```", [KEY_A])
    assert items[0]["card_type"] == "decision"
    items = _parse_batch_items("```json " + _good() + " ```", [KEY_A])
    assert items[0]["card_type"] == "decision"


def test_missing_final_slot_brace_is_completed():
    good = _good()
    assert good.endswith("}}}]")
    broken = good[:-4] + "}}]"
    items = _parse_batch_items(broken, [KEY_A])
    assert items[0]["card_type"] == "decision"


def test_stray_trailing_closers_are_ignored():
    for stray in ("}", "]", "]}", "}]", "}}", "]]"):
        items = _parse_batch_items(_good() + stray, [KEY_A])
        assert items[0]["card_type"] == "decision", stray


def test_trailing_whitespace_is_fine():
    items = _parse_batch_items(_good() + "\n\n  ", [KEY_A])
    assert items[0]["card_type"] == "decision"


def test_truncated_midway_is_rejected():
    good = _good()
    with pytest.raises(BatchExtractionError):
        _parse_batch_items(good[: len(good) // 2], [KEY_A])


def test_source_order_mismatch_still_rejected():
    raw = _good((KEY_A, KEY_B))
    with pytest.raises(BatchExtractionError):
        _parse_batch_items(raw, [KEY_B, KEY_A])


def test_duplicate_slot_still_rejected():
    raw = _good((KEY_A, KEY_A))
    with pytest.raises(BatchExtractionError):
        _parse_batch_items(raw, [KEY_A, KEY_A])


def test_invalid_typed_payload_still_rejected():
    bad = {
        "source_key": KEY_A,
        "candidate": {
            "card_type": "evidence",
            "title": "audit",
            "statement": "audit ran",
            "typed_payload": {
                "evidence_kind": "forensic_integrity_audit",
                "result_status": "CLEAN",
                "hash_refs": ["c254cf1"],
                "count_refs": {"files_changed": 1},
            },
        },
    }
    raw = json.dumps([bad], separators=(",", ":"))
    with pytest.raises(BatchExtractionError):
        _parse_batch_items(raw, [KEY_A])


def _chunk():
    return {
        "project": "neurons",
        "content_hash": "sha256:" + "c" * 64,
        "redacted_text": "session text",
    }


def test_retry_recovers_after_one_invalid_response():
    chunk = _chunk()
    key = _batch_source_key(chunk, scope="project")
    good = _good((key,))
    calls = []

    def fn(messages):
        calls.append(messages)
        if len(calls) == 1:
            return good[: len(good) // 2]
        return good

    miner = BatchedEnvelopeMiner(completion_fn=fn, scope="project")
    candidates, _report = miner.mine_batches([chunk], refresh_watermark="w")
    assert len(calls) == 2
    assert len(candidates) == 1


def test_retry_exhausted_raises():
    chunk = _chunk()
    key = _batch_source_key(chunk, scope="project")
    good = _good((key,))
    calls = []

    def fn(messages):
        calls.append(messages)
        return good[: len(good) // 2]

    miner = BatchedEnvelopeMiner(completion_fn=fn, scope="project")
    with pytest.raises(BatchExtractionError):
        miner.mine_batches([chunk], refresh_watermark="w")
    assert len(calls) == 2


def test_transport_errors_are_not_retried():
    chunk = _chunk()
    calls = []

    def fn(messages):
        calls.append(messages)
        raise ExtractionTransportError("boom")

    miner = BatchedEnvelopeMiner(completion_fn=fn, scope="project")
    with pytest.raises(ExtractionTransportError):
        miner.mine_batches([chunk], refresh_watermark="w")
    assert len(calls) == 1


def test_prompt_reports_allowed_enum_values():
    chunk = _chunk()
    key = _batch_source_key(chunk, scope="project")
    seen = {}

    def fn(messages):
        seen["system"] = messages[0]["content"]
        return _good((key,))

    miner = BatchedEnvelopeMiner(completion_fn=fn, scope="project")
    miner.mine_batches([chunk], refresh_watermark="w")
    system_text = seen["system"]
    for token in ("user_approval", "transcript", "result_status", "explicit", "inferred"):
        assert token in system_text, token

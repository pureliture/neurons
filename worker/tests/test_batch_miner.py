from __future__ import annotations

import json

import pytest

from agent_knowledge.session_memory.batch_miner import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_CALL_CHARS,
    BatchedEnvelopeMiner,
    BatchMiningReport,
    plan_batches,
)
from agent_knowledge.session_memory.llm_brain_miner import LlmBrainEnvelopeMiner


PROJECT = "neurons"


def _chunk(index: int, body: str | None = None) -> dict:
    return {
        "content": body if body is not None else f"session body for chunk {index}",
        "content_hash": "sha256:" + str(index) * 64,
        "metadata": {
            "knowledge_id": f"session-{index}",
            "project": PROJECT,
            "provider": "codex",
        },
    }


def _decision_item(title: str = "Authority store") -> dict:
    return {
        "card_type": "decision",
        "title": title,
        "statement": "PostgreSQL is the authority store.",
        "typed_payload": {
            "decision": "Use PostgreSQL",
            "rationale": "One authority store",
            "alternatives": [],
            "consequence": "Graph stays derived",
            "authority_ref": "design-note",
        },
    }


def _completion_returning(count: int, title: str = "Authority store"):
    """Reply with exactly ``count`` items, one per session in the batch."""

    def completion(messages):
        return json.dumps([_decision_item(title) for _ in range(count)])

    return completion


class CallCounter:
    def __init__(self, reply_fn):
        self.reply_fn = reply_fn
        self.calls = 0
        self.payload_sizes = []

    def __call__(self, messages):
        self.calls += 1
        self.payload_sizes.append(len(messages[1]["content"]))
        return self.reply_fn(messages)


# ------------------------------------------------------------------- batch planning


def test_default_batch_size_is_five_and_char_cap_is_bounded():
    assert DEFAULT_BATCH_SIZE == 5
    assert 0 < DEFAULT_MAX_CALL_CHARS <= 20_000


def test_plan_batches_groups_by_size_and_never_drops_a_chunk():
    chunks = [_chunk(i) for i in range(12)]
    batches = plan_batches(chunks, batch_size=5)
    assert [len(batch) for batch in batches] == [5, 5, 2]
    assert [item for batch in batches for item in batch] == chunks


def test_plan_batches_splits_on_char_cap_without_losing_the_chunk():
    big = _chunk(1, body="x" * 9000)
    other = _chunk(2, body="y" * 9000)
    batches = plan_batches([big, other], batch_size=5, max_call_chars=10_000)
    assert [len(batch) for batch in batches] == [1, 1]
    assert [item for batch in batches for item in batch] == [big, other]


def test_a_single_oversized_chunk_is_sent_alone():
    huge = _chunk(1, body="z" * 50_000)
    batches = plan_batches([huge], batch_size=5, max_call_chars=1_000)
    assert len(batches) == 1
    assert len(batches[0]) == 1


@pytest.mark.parametrize("batch_size", [0, -1])
def test_invalid_batch_size_is_rejected(batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        plan_batches([_chunk(1)], batch_size=batch_size)


def test_invalid_char_cap_is_rejected():
    with pytest.raises(ValueError, match="max_call_chars"):
        plan_batches([_chunk(1)], max_call_chars=0)


# -------------------------------------------------------------------- cost shape


def test_batched_mining_costs_one_call_per_batch_not_per_chunk():
    counter = CallCounter(_completion_returning(3))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=3)
    chunks = [_chunk(i) for i in range(3)]

    candidates, report = miner.mine_batches(chunks, refresh_watermark="wm")

    assert counter.calls == 1
    assert report.model_calls == 1
    assert report.lower_bound_calls == 3
    assert report.calls_saved() if hasattr(report, "calls_saved") else True
    assert report.to_dict()["calls_saved"] == 2
    assert len(candidates) == 3


def test_unbatched_miner_costs_one_call_per_chunk():
    counter = CallCounter(_completion_returning(1))
    miner = LlmBrainEnvelopeMiner(completion_fn=counter, max_candidates=5)
    chunks = [_chunk(i) for i in range(3)]

    for chunk in chunks:
        miner.mine_chunk(chunk, refresh_watermark="wm")

    assert counter.calls == 3


def test_twenty_chunks_cost_four_calls_at_batch_size_five():
    counter = CallCounter(_completion_returning(5))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=5)
    chunks = [_chunk(i) for i in range(20)]

    _candidates, report = miner.mine_batches(chunks, refresh_watermark="wm")

    assert report.model_calls == 4
    assert report.lower_bound_calls == 20
    assert report.to_dict()["calls_saved"] == 16


# ------------------------------------------------------- evidence stays per chunk


def test_each_candidate_keeps_its_own_chunk_evidence():
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(3), batch_size=3)
    chunks = [_chunk(i) for i in range(3)]

    candidates, _report = miner.mine_batches(chunks, refresh_watermark="wm")

    assert len(candidates) == 3
    evidence = [candidate["content_hash"] for candidate in candidates]
    assert evidence == ["sha256:" + str(i) * 64 for i in range(3)]
    source_ids = [candidate["source_refs"][0]["source_id"] for candidate in candidates]
    assert source_ids == [f"session-{i}" for i in range(3)]
    projects = {candidate["project"] for candidate in candidates}
    assert projects == {PROJECT}
    brain_ids = {candidate["brain_id"] for candidate in candidates}
    assert brain_ids == {f"/project/{PROJECT}"}


def test_batched_candidate_matches_single_chunk_candidate_identity():
    """Batching must not change the identity of what a single chunk produces."""
    single = LlmBrainEnvelopeMiner(completion_fn=_completion_returning(1), max_candidates=5)
    batched = BatchedEnvelopeMiner(completion_fn=_completion_returning(1), batch_size=5)

    single_out = single.mine_chunk(_chunk(0), refresh_watermark="wm")
    batched_out, _report = batched.mine_batches([_chunk(0)], refresh_watermark="wm")

    assert len(single_out) == 1
    assert len(batched_out) == 1
    assert batched_out[0]["idempotency_key"] == single_out[0]["idempotency_key"]
    assert batched_out[0]["memory_id"] == single_out[0]["memory_id"]


# ----------------------------------------------------------------- failure shape


def test_short_reply_pads_without_crashing_or_misaligning():
    # The model returns one item for a three-chunk batch.
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(1), batch_size=3)
    chunks = [_chunk(i) for i in range(3)]

    candidates, report = miner.mine_batches(chunks, refresh_watermark="wm")

    assert len(candidates) == 1
    assert candidates[0]["content_hash"] == "sha256:" + "0" * 64
    assert report.candidate_count == 1
    assert report.chunk_count == 3


def test_invalid_json_reply_yields_no_candidates_and_no_crash():
    def bad(_messages):
        return "sorry, not json"

    miner = BatchedEnvelopeMiner(completion_fn=bad, batch_size=3)
    candidates, report = miner.mine_batches([_chunk(i) for i in range(3)], refresh_watermark="wm")

    assert candidates == []
    assert report.chunk_count == 3
    assert report.model_calls == 1


def test_chunks_without_a_project_are_skipped_not_crashed():
    broken = _chunk(1)
    del broken["metadata"]["project"]
    good = _chunk(2)
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(2), batch_size=2)

    candidates, report = miner.mine_batches([broken, good], refresh_watermark="wm")

    assert len(candidates) == 1
    assert candidates[0]["project"] == PROJECT
    assert report.chunk_count == 2


def test_malformed_item_is_dropped_but_its_neighbour_survives():
    counter = CallCounter(lambda _messages: json.dumps(
        [{"card_type": "not_a_type", "title": "x", "statement": "y", "typed_payload": {}},
         _decision_item()]
    ))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=2)
    candidates, _report = miner.mine_batches([_chunk(0), _chunk(1)], refresh_watermark="wm")

    assert len(candidates) == 1
    assert candidates[0]["content_hash"] == "sha256:" + "1" * 64


def test_empty_input_makes_no_model_call():
    counter = CallCounter(_completion_returning(1))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=3)

    candidates, report = miner.mine_batches([], refresh_watermark="wm")

    assert candidates == []
    assert counter.calls == 0
    assert report.model_calls == 0
    assert isinstance(report, BatchMiningReport)


def test_non_mapping_chunks_are_ignored():
    counter = CallCounter(_completion_returning(1))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=3)
    candidates, _report = miner.mine_batches([_chunk(0), "not a chunk"], refresh_watermark="wm")

    assert len(candidates) == 1
    assert counter.calls == 1


def test_invalid_max_candidates_is_rejected():
    with pytest.raises(ValueError, match="max_candidates"):
        BatchedEnvelopeMiner(completion_fn=_completion_returning(1), max_candidates=0)


def test_report_has_no_raw_session_body():
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(2), batch_size=2)
    _candidates, report = miner.mine_batches(
        [_chunk(0, body="a very private session body"), _chunk(1, body="another private body")],
        refresh_watermark="wm",
    )
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "private session body" not in serialized
    assert "another private body" not in serialized

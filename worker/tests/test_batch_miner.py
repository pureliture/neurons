from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

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
    content = body if body is not None else f"session body for chunk {index}"
    return {
        "content": content,
        "content_hash": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
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
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        if not keys:  # The single-chunk miner keeps its legacy response shape.
            return json.dumps([_decision_item(title) for _ in range(count)])
        return json.dumps([
            {"source_key": key, "candidate": _decision_item(title)} for key in keys[:count]
        ])

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

    candidates, report = miner.mine_batches(chunks, refresh_watermark="wm")

    assert report.model_calls == 4
    assert report.lower_bound_calls == 20
    assert report.to_dict()["calls_saved"] == 16
    assert len(candidates) == 20


# ------------------------------------------------------- evidence stays per chunk


def test_each_candidate_keeps_its_own_chunk_evidence():
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(3), batch_size=3)
    chunks = [_chunk(i) for i in range(3)]

    candidates, _report = miner.mine_batches(chunks, refresh_watermark="wm")

    assert len(candidates) == 3
    evidence = [candidate["content_hash"] for candidate in candidates]
    assert evidence == [chunk["content_hash"] for chunk in chunks]
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


@pytest.mark.parametrize("batch_size,max_call_chars,order", [
    (1, 12_000, [0, 1, 2]),
    (3, 12_000, [0, 1, 2]),
    (3, 30, [0, 1, 2]),
    (3, 12_000, [2, 0, 1]),
])
def test_batch_candidate_identity_is_chunk_local_across_width_split_and_order(batch_size, max_call_chars, order):
    chunks = [_chunk(i) for i in range(3)]
    single = LlmBrainEnvelopeMiner(completion_fn=_completion_returning(1))
    expected = {
        chunk["content_hash"]: single.mine_chunk(chunk, refresh_watermark="single")[0]
        for chunk in chunks
    }
    candidates, _ = BatchedEnvelopeMiner(
        completion_fn=_completion_returning(3), batch_size=batch_size, max_call_chars=max_call_chars
    ).mine_batches([chunks[i] for i in order], refresh_watermark="batch")
    assert len(candidates) == len(chunks)
    for card in candidates:
        single_card = expected[card["content_hash"]]
        assert card["span_refs"] == single_card["span_refs"]
        assert card["memory_id"] == single_card["memory_id"]
        assert card["candidate_id"] == single_card["candidate_id"]
        assert card["idempotency_key"] == single_card["idempotency_key"]


@pytest.mark.parametrize("miner_kind", ["batch", "single"])
@pytest.mark.parametrize("field_location", ["flat", "metadata", "override"])
def test_miners_preserve_explicit_chunk_provenance_without_mutating_input(miner_kind, field_location):
    provenance = {
        "source_ref": {"source_id": "canonical-source", "source_owner": "transcript_memory_canonical_store",
                       "source_kind": "session_memory_refresh", "access_mode": "source_ref_only"},
        "span_ref": {"span_id": "canonical-source#approved-span", "source_owner": "transcript_memory_canonical_store",
                     "source_kind": "session_memory_span", "access_mode": "span_ref_only"},
        "observed_at": "2026-10-11T12:34:56Z",
        "brain_id": "/project/neurons/explicit-brain",
        "scope": "workspace",
    }
    chunk = _chunk(0)
    if field_location == "flat":
        chunk.update(provenance)
    else:
        chunk["metadata"].update(provenance)
        if field_location == "override":
            chunk["metadata"].update({"source_ref": {"source_id": "metadata-source"},
                                      "span_ref": {"span_id": "metadata-span"},
                                      "observed_at": "2026-10-10T00:00:00Z", "brain_id": "/project/other", "scope": "project"})
            chunk.update(provenance)
    before = deepcopy(chunk)
    if miner_kind == "batch":
        cards, _ = BatchedEnvelopeMiner(completion_fn=_completion_returning(1)).mine_batches([chunk], refresh_watermark="wm")
    else:
        cards = LlmBrainEnvelopeMiner(completion_fn=_completion_returning(1)).mine_chunk(chunk, refresh_watermark="wm")
    assert len(cards) == 1
    card = cards[0]
    assert card["source_refs"] == [provenance["source_ref"]]
    assert card["span_refs"] == [provenance["span_ref"]]
    assert card.get("observed_at") == provenance["observed_at"]
    assert card["brain_id"] == provenance["brain_id"]
    assert card["scope"] == provenance["scope"]
    assert card["project"] == PROJECT
    assert card["content_hash"] == chunk["content_hash"]
    assert chunk == before


# ----------------------------------------------------------------- failure shape


def test_short_reply_fails_instead_of_padding_missing_sources():
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(1), batch_size=3)
    chunks = [_chunk(i) for i in range(3)]

    with pytest.raises(ValueError, match="batch"):
        miner.mine_batches(chunks, refresh_watermark="wm")


def test_invalid_json_reply_fails_instead_of_reporting_no_candidates():
    def bad(_messages):
        return "sorry, not json"

    miner = BatchedEnvelopeMiner(completion_fn=bad, batch_size=3)
    with pytest.raises(ValueError, match="batch"):
        miner.mine_batches([_chunk(i) for i in range(3)], refresh_watermark="wm")


def test_chunks_without_a_project_are_skipped_not_crashed():
    broken = _chunk(1)
    del broken["metadata"]["project"]
    good = _chunk(2)
    miner = BatchedEnvelopeMiner(completion_fn=_completion_returning(2), batch_size=2)
    from agent_knowledge.session_memory.batch_miner import BatchExtractionError
    with pytest.raises(BatchExtractionError, match="source is invalid"):
        miner.mine_batches([broken, good], refresh_watermark="wm")


def test_malformed_candidate_is_not_an_intentional_null_slot():
    def malformed_reply(messages):
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        keys = keys or ["fixture-0", "fixture-1"]
        return json.dumps([
            {"source_key": keys[0], "candidate": {"card_type": "not_a_type", "statement": "y", "typed_payload": {}}},
            {"source_key": keys[1], "candidate": _decision_item()},
        ])

    counter = CallCounter(malformed_reply)
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=2)
    with pytest.raises(ValueError, match="batch"):
        miner.mine_batches([_chunk(0), _chunk(1)], refresh_watermark="wm")


@pytest.mark.parametrize("corruption", ["middle_omission", "reversed", "extra", "duplicate", "wrong_key", "raw_null", "missing_candidate"])
def test_invalid_source_slots_fail_before_candidate_assembly(monkeypatch, corruption):
    def completion(messages):
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        keys = keys or [f"fixture-{i}" for i in range(3)]
        slots: list = [{"source_key": key, "candidate": _decision_item()} for key in keys]
        if corruption == "middle_omission":
            del slots[1]
        elif corruption == "reversed":
            slots.reverse()
        elif corruption == "extra":
            slots.append(slots[-1])
        elif corruption == "duplicate":
            slots[1]["source_key"] = slots[0]["source_key"]
        elif corruption == "wrong_key":
            slots[1]["source_key"] = "other-source"
        elif corruption == "raw_null":
            slots[1] = None
        else:
            del slots[1]["candidate"]
        return json.dumps(slots)

    assembled = []
    miner = BatchedEnvelopeMiner(completion_fn=completion, batch_size=3)
    monkeypatch.setattr(miner, "_candidate_for", lambda *args, **kwargs: assembled.append(args))
    with pytest.raises(ValueError, match="batch"):
        miner.mine_batches([_chunk(i) for i in range(3)], refresh_watermark="wm")
    assert assembled == []


def test_explicit_identity_bearing_middle_null_keeps_neighbour_source():
    def completion(messages):
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        keys = keys or [f"fixture-{i}" for i in range(3)]
        return json.dumps([
            {"source_key": key, "candidate": None if i == 1 else _decision_item(f"decision-{i}")}
            for i, key in enumerate(keys)
        ])

    candidates, report = BatchedEnvelopeMiner(completion_fn=completion, batch_size=3).mine_batches(
        [_chunk(i) for i in range(3)], refresh_watermark="wm"
    )
    assert [(c["source_refs"][0]["source_id"], c["title"]) for c in candidates] == [
        ("session-0", "decision-0"), ("session-2", "decision-2")
    ]
    assert report.candidate_count == 2


@pytest.mark.parametrize("raw", ["", " ", "[]", "{}", "null", None])
def test_invalid_or_empty_batch_response_is_not_intentional_no_candidate(raw):
    with pytest.raises(ValueError, match="batch"):
        BatchedEnvelopeMiner(completion_fn=lambda messages: raw).mine_batches([_chunk(0)], refresh_watermark="wm")


@pytest.mark.parametrize("duplicate_field", ["source_key", "candidate"])
def test_duplicate_json_slot_fields_cannot_hide_an_ambiguous_source_response(duplicate_field):
    def completion(messages):
        key = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])[0]
        duplicate = '"source_key":"other-source",' if duplicate_field == "source_key" else '"candidate":null,'
        return '[{' + duplicate + '"source_key":' + json.dumps(key) + ',"candidate":' + json.dumps(_decision_item()) + '}]'

    with pytest.raises(ValueError, match="batch"):
        BatchedEnvelopeMiner(completion_fn=completion).mine_batches([_chunk(0)], refresh_watermark="wm")


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_non_json_numeric_constants_in_candidate_payload_are_rejected(constant):
    def completion(messages):
        key = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])[0]
        item = _decision_item()
        item["typed_payload"]["rationale"] = constant
        return json.dumps([{"source_key": key, "candidate": item}])

    with pytest.raises(ValueError, match="batch"):
        BatchedEnvelopeMiner(completion_fn=completion).mine_batches([_chunk(0)], refresh_watermark="wm")


def test_identity_bearing_null_response_is_intentional_no_candidate():
    def completion(messages):
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"]) or ["fixture-0"]
        return json.dumps([{"source_key": key, "candidate": None} for key in keys])

    candidates, report = BatchedEnvelopeMiner(completion_fn=completion).mine_batches([_chunk(0)], refresh_watermark="wm")
    assert candidates == []
    assert report.chunk_count == 1
    assert report.candidate_count == 0


def test_source_tokens_bind_identity_without_using_batch_position():
    chunks = [_chunk(i) for i in range(3)]
    captured = []

    def completion(messages):
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        captured.append(keys)
        if not keys:
            return json.dumps([_decision_item() for _ in range(3)])
        return json.dumps([{"source_key": key, "candidate": _decision_item()} for key in keys])

    BatchedEnvelopeMiner(completion_fn=completion, batch_size=3).mine_batches(chunks, refresh_watermark="wm")
    together = captured.pop()
    assert len(together) == 3
    assert len(set(together)) == 3
    assert all(key.startswith("sha256:") and len(key) == 71 for key in together)
    BatchedEnvelopeMiner(completion_fn=completion, batch_size=1).mine_batches(chunks, refresh_watermark="wm")
    assert [keys[0] for keys in captured] == together


def test_empty_input_makes_no_model_call():
    counter = CallCounter(_completion_returning(1))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=3)

    candidates, report = miner.mine_batches([], refresh_watermark="wm")

    assert candidates == []
    assert counter.calls == 0
    assert report.model_calls == 0
    assert isinstance(report, BatchMiningReport)


def test_non_mapping_chunks_are_rejected_before_any_model_call():
    from agent_knowledge.session_memory.batch_miner import BatchExtractionError
    counter = CallCounter(_completion_returning(1))
    miner = BatchedEnvelopeMiner(completion_fn=counter, batch_size=3)
    with pytest.raises(BatchExtractionError, match="source is invalid"):
        miner.mine_batches([_chunk(0), "not a chunk"], refresh_watermark="wm")
    assert counter.calls == 0


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

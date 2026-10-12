"""The batch mining report must count provider calls actually made."""
from __future__ import annotations

import json
import re

from agent_knowledge.session_memory.batch_miner import BatchedEnvelopeMiner


def _chunk() -> dict:
    body = "Synthetic durable choice for retry accounting"
    return {
        "content": body,
        "content_hash": "sha256:" + "e" * 64,
        "metadata": {"knowledge_id": "synthetic-retry-accounting", "project": "retry-test", "provider": "codex"},
    }


def _decision() -> dict:
    return {
        "card_type": "decision",
        "title": "Synthetic decision",
        "statement": "Keep the boundary",
        "typed_payload": {
            "decision": "Keep the boundary",
            "rationale": "Synthetic explicit decision",
            "alternatives": [],
            "consequence": "No authority change",
            "authority_ref": "synthetic",
        },
    }


def test_successful_retry_reports_every_actual_model_call() -> None:
    attempts: list[int] = []

    def completion(messages):
        attempts.append(1)
        if len(attempts) == 1:
            # First response is unusable; the bounded retry is a real provider call.
            return "synthetic invalid response"
        keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
        return json.dumps([{"source_key": key, "candidate": _decision()} for key in keys])

    miner = BatchedEnvelopeMiner(completion_fn=completion)
    candidates, report = miner.mine_batches([_chunk()], refresh_watermark="synthetic")

    assert len(attempts) == 2
    # The previous contract reported len(batches)=1 and hid the retry's cost.
    assert report.model_calls == 2
    assert report.batch_count == 1
    assert len(candidates) == 1

"""TDD: durable PG chunk-state seam with real fence/lease semantics."""

from __future__ import annotations

import os
import threading

import uuid

PREFIX = uuid.uuid4().hex[:8] + "-"
import pytest

from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import (
    DuplicateRejected,
    FencingRejected,
    PgChunkState,
)

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no local PG")


def _state():
    return PgChunkState(os.environ["TRIAL_PG_DSN"])


def test_register_plan_idempotent_and_pending_keys():
    state = _state()
    keys = tuple(PREFIX + item for item in ("k1", "k2", "k3"))
    state.register_plan(
        run_id="r1", project=PREFIX, session_hash="s", policy_version="v1",
        source_hash="src", map_digest="map", chunk_keys=keys,
    )
    state.register_plan(
        run_id="r1", project=PREFIX, session_hash="s", policy_version="v1",
        source_hash="src", map_digest="map", chunk_keys=keys,
    )
    assert state.pending_keys(keys) == list(keys)
    assert state.completed_chunks(PREFIX, "v1") == []


def test_complete_idempotent_and_rejects_changed_payload():
    state = _state()
    key = PREFIX + "k"
    state.register_plan(run_id="r2", project=PREFIX, session_hash="s", policy_version="v2",
                        source_hash="src", map_digest="map", chunk_keys=(key,))
    token = state.claim(key=key, owner="w1")
    state.complete(key=key, fence_token=token, output_digest="o1", write_receipt="r1")
    token2 = state.claim(key=key, owner="w1")
    state.complete(key=key, fence_token=token2, output_digest="o1", write_receipt="r1")
    assert state.is_complete(key) and state.all_verified((key,))
    token3 = state.claim(key=key, owner="w1")
    with pytest.raises(DuplicateRejected):
        state.complete(key=key, fence_token=token3, output_digest="o2", write_receipt="r2")


def test_stale_fence_token_rejected():
    state = _state()
    key = PREFIX + "stale"
    state.register_plan(run_id="r3", project=PREFIX, session_hash="s", policy_version="v3",
                        source_hash="src", map_digest="map", chunk_keys=(key,))
    t1 = state.claim(key=key, owner="w1")
    state.force_expire(key)
    t2 = state.claim(key=key, owner="w2")
    assert t2 > t1
    with pytest.raises(FencingRejected):
        state.complete(key=key, fence_token=t1, output_digest="o", write_receipt="r")


def test_concurrent_claims_only_latest_token_completes():
    state = _state()
    key = PREFIX + "race"
    state.register_plan(run_id="r4", project=PREFIX, session_hash="s", policy_version="v4",
                        source_hash="src", map_digest="map", chunk_keys=(key,))
    tokens: list[int] = []

    def claim():
        tokens.append(_state().claim(key=key, owner="thread"))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(tokens)) == 4
    latest = max(tokens)
    state.complete(key=key, fence_token=latest, output_digest="o", write_receipt="r")
    assert state.is_complete(key)

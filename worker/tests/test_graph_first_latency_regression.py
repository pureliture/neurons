"""Regression tests for GraphFirstResolver latency optimizations (Item 3 & Item 1)."""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.llm_brain_core.models import OntologyEpisode
from agent_knowledge.llm_brain_core.runtime import _extraction_text_from_evidence
from agent_knowledge.mcp_payload import tool_result_bytes
from agent_knowledge.public_safe_util import ensure_public_safe


class _FakeChunkStore:
    def __init__(
        self,
        *,
        chunks: list[dict[str, Any]],
        cards: list[dict[str, Any]] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._chunks = chunks
        self._cards = cards or []
        self._delay = delay_seconds
        self.search_calls = 0

    def search_session_chunks(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        self.search_calls += 1
        if self._delay > 0:
            time.sleep(self._delay)
        return [dict(row) for row in self._chunks]

    def list_authorized_cards(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cards]

    def get_cards_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        return {c["memory_id"]: dict(c) for c in self._cards if c["memory_id"] in ids}

    def graph_projection_health(self, *_args, **_kwargs) -> dict[str, Any]:
        return {"unprojected": False, "projection_lag_ms": None}


def test_serialize_and_session_chunk_cards_memoize_redaction_and_bound_chunk_summary(monkeypatch):
    import agent_knowledge.public_safe_util as public_safe_util

    calls: list[int] = []
    original = public_safe_util.redact_public_ingress_text

    def spy_redact(value: str) -> str:
        calls.append(len(value))
        return original(value)

    monkeypatch.setattr(public_safe_util, "redact_public_ingress_text", spy_redact)

    # 12 oversized chunks (200KB each) mimicking real prod session_chunk rows
    large_body = (
        "Authorization: Bearer secret_token_abc123456789 "
        + ("compact session context note alpha beta " * 5000)
    )
    chunks = [
        {
            "chunk_id": f"seg_{idx:052d}",
            "project": "neurons",
            "title": f"Session Segment {idx}",
            "content_markdown": f"session {idx}\n{large_body}",
            "content_hash": "sha256:" + hashlib.sha256(f"seg:{idx}".encode()).hexdigest(),
            "distance": 0.08 + idx * 0.01,
            "currentness": "current",
            "segment_turn_range": {"start": idx * 5, "end": idx * 5 + 4},
        }
        for idx in range(12)
    ]
    store = _FakeChunkStore(chunks=chunks)
    resolver = GraphFirstResolver(
        store=store,
        graph_adapter=FakeGraphMemoryAdapter(),
        embed_query=lambda _q: [1.0, 0.0],
    )

    result = resolver.resolve(project="neurons", query="compact session context")
    ensure_public_safe(result, field="graph_first_large_chunk_result")
    # Bodies are never truncated; the page keeps whole cards under the byte budget.
    assert result["items"]
    assert len(result["items"]) <= 1 and len(result["items"]) >= 1
    assert result["next_cursor"]
    assert "secret_token_abc123456789" not in str(result)
    # Redaction runs once per card text (no repeated scans across trimming loops).
    assert sum(calls) < 3_000_000


def test_resolve_query_overlaps_pg_chunks_with_graph_search_preserving_quality():
    adapter = FakeGraphMemoryAdapter()
    ep = OntologyEpisode(
        episode_id="ep_card_1",
        event_id="evt_card_1",
        idempotency_key="idem_card_1",
        entity_type="Decision",
        natural_id="mem_card_1",
        lifecycle_state="human_accepted",
        currentness="current",
        source_event_ids=("evt_card_1",),
        source_ref_ids=(),
        valid_from="2026-11-17T00:00:00Z",
        valid_to="",
        observed_at="2026-11-17T00:00:00Z",
        reference_time="2026-11-17T00:00:00Z",
        content_hash="sha256:" + hashlib.sha256(b"card_1_hash").hexdigest(),
        ontology_version="v1",
        extractor_version="v1",
        payload={
            "brain_id": "/project/neurons",
            "project": "neurons",
            "summary": "Use official GoCD release pipeline",
            "authority_memory_id": "mem_card_1",
            "content_hash": "sha256:" + hashlib.sha256(b"card_1_hash").hexdigest(),
        },
    )
    adapter.upsert_episode(ep)

    orig_search = adapter.search_context

    def slow_search_context(**kwargs):
        time.sleep(0.18)
        return orig_search(**kwargs)

    adapter.search_context = slow_search_context  # type: ignore[method-assign]

    cards = [
        {
            "memory_id": "mem_card_1",
            "project": "neurons",
            "card_type": "decision",
            "title": "GoCD release rule",
            "summary": "Use official GoCD release pipeline",
            "typed_payload": {"decision": "official GoCD release"},
            "currentness": "current",
            "content_hash": "sha256:" + hashlib.sha256(b"card_1_hash").hexdigest(),
            "lifecycle_state": "human_accepted",
            "authorization_status": "active",
            "updated_at": "2026-11-17T00:00:00Z",
        }
    ]
    chunks = [
        {
            "chunk_id": "seg_" + "a" * 52,
            "project": "neurons",
            "title": "Deployment conversation",
            "content_markdown": "Verified official GoCD release pipeline details",
            "content_hash": "sha256:" + hashlib.sha256(b"chunk_a").hexdigest(),
            "distance": 0.09,
            "currentness": "current",
        }
    ]
    store = _FakeChunkStore(chunks=chunks, cards=cards, delay_seconds=0.18)

    embed_lock = threading.Lock()
    embed_calls = 0

    def counting_embed(_query: str) -> list[float]:
        nonlocal embed_calls
        with embed_lock:
            embed_calls += 1
        return [1.0, 0.0]

    resolver = GraphFirstResolver(
        store=store,
        graph_adapter=adapter,
        embed_query=counting_embed,
    )

    t0 = time.monotonic()
    result = resolver.resolve(project="neurons", query="GoCD release pipeline")
    elapsed = time.monotonic() - t0

    assert elapsed < 0.30, f"Expected overlapped graph+PG search (<0.30s), took {elapsed:.3f}s"
    assert embed_calls == 1
    assert store.search_calls == 1
    assert result["metadata"]["retrieval_path"] == "graph_neo4j"
    assert result["metadata"]["fallback_used"] is False
    ids = [item["id"] for item in result["items"]]
    assert "mem_card_1" in ids
    assert ("chunk:seg_" + "a" * 52) in ids


def test_runtime_extraction_text_from_evidence_stops_reading_after_budget():
    body_reads = 0

    class _TrackedBundle(dict):
        def get(self, key, default=None):
            nonlocal body_reads
            if key == "body":
                body_reads += 1
            return super().get(key, default)

    bundles = [
        _TrackedBundle(
            _id=f"bundle:{idx:04d}",
            part_index=idx,
            body=f"### {idx} command/ok\n- tool: kubectl\n- command: kubectl get pods\n- result: " + ("x" * 250),
        )
        for idx in range(500)
    ]

    text = _extraction_text_from_evidence(bundles, max_chars=1200)
    assert len(text) == 1200
    assert body_reads < 20

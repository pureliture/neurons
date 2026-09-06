from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from agent_knowledge.mcp_payload import tool_result_bytes

from agent_knowledge.llm_brain_core.graph_first_resolver import (
    GraphFirstResolver,
    _ResolveRequest,
    _shrink_card,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _card(memory_id: str = "mem_a", content_hash: str = HASH_A, card_type: str = "decision") -> dict:
    return {"memory_id": memory_id, "project": "neurons", "card_type": card_type, "title": "결정", "summary": "PostgreSQL을 권위 저장소로 사용", "typed_payload": {"decision": "PostgreSQL 사용"}, "currentness": "current", "content_hash": content_hash}


def _episode(memory_id: str = "mem_a", content_hash: str = HASH_A):
    return SimpleNamespace(payload={"authority_memory_id": memory_id, "content_hash": content_hash})


class Store:
    def __init__(self, cards=None, *, unprojected=False, calls=None, failure=None, evidence=None):
        self.cards = list(cards or [_card()])
        self.unprojected = unprojected
        self.calls = calls if calls is not None else []
        self.failure = failure
        self.evidence = evidence if evidence is not None else {"edges": [], "truncated": False}

    def graph_projection_health(self, project, *, as_of=None):
        self.calls.append("health")
        if self.failure == "health":
            raise RuntimeError("database down")
        return {"unprojected": self.unprojected, "projection_lag_ms": 42 if self.unprojected else None}

    def list_authorized_cards(self, **kwargs):
        self.calls.append(("list", kwargs.get("memory_ids")))
        if self.failure == "list":
            raise RuntimeError("database down")
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        if wanted is None:
            cards = [c for c in sorted(cards, key=lambda c: c["memory_id"])
                     if c["memory_id"] > (kwargs.get("after_memory_id") or "")]
        return cards[:kwargs.get("limit", 100)]

    def hybrid_search(self, **kwargs):
        self.calls.append("hybrid")
        if self.failure == "hybrid":
            raise RuntimeError("database down")
        return self.cards

    def read_authorized_evidence(self, **kwargs):
        self.calls.append(("evidence", kwargs))
        if self.failure == "evidence":
            raise RuntimeError("database down")
        return {"root_hashes": {c["memory_id"]: c["content_hash"] for c in self.cards
                                if c["memory_id"] in kwargs["root_memory_ids"]}, **self.evidence}


class Graph:
    def __init__(self, episodes=(), status="available", calls=None):
        self.episodes, self.status = tuple(episodes), status
        self.calls = calls if calls is not None else []

    def search_context(self, **kwargs):
        self.calls.append("graph")
        assert kwargs["brain_id"] == "/project/neurons"
        return SimpleNamespace(status=self.status, episodes=self.episodes)


def test_query_is_graph_first_then_authority_join_and_preserves_graph_order():
    calls = []
    store = Store(cards=[_card("mem_b", HASH_B), _card("mem_a", HASH_A)], calls=calls)
    graph = Graph([_episode("mem_a", HASH_A), _episode("mem_b", HASH_B)], calls=calls)

    result = GraphFirstResolver(store, graph).resolve(project="neurons", query="authority")

    assert calls == ["graph", "health", ("list", ["mem_a", "mem_b"])]
    assert [item["id"] for item in result["items"]] == ["mem_a", "mem_b"]
    assert result["metadata"] == {"retrieval_path": "graph_neo4j", "graph_status": "available", "authority_join_status": "verified", "fallback_used": False, "projection_lag_ms": None, "has_more": False}


def test_unprojected_graph_keeps_verified_graph_order_and_supplements_from_pgvector():
    calls = []
    store = Store(cards=[_card("mem_a", HASH_A), _card("mem_b", HASH_B)], unprojected=True, calls=calls)
    graph = Graph([_episode()], calls=calls)
    embedded = []

    result = GraphFirstResolver(store, graph, lambda query: embedded.append(query) or [0.1, 0.2]).resolve(project="neurons", query="x")

    assert calls == ["graph", "health", ("list", ["mem_a"]), "hybrid", ("list", ["mem_a", "mem_b"])]
    assert embedded == ["x"]
    assert [item["id"] for item in result["items"]] == ["mem_a", "mem_b"]
    assert result["metadata"]["retrieval_path"] == "graph_neo4j"
    assert result["metadata"]["graph_status"] == "projection_lag"
    assert result["metadata"]["fallback_used"] is True


def test_healthy_empty_fully_projected_stays_empty_without_fallback():
    calls = []
    result = GraphFirstResolver(Store(calls=calls), Graph([], calls=calls), lambda _: [0.1]).resolve(project="neurons", query="none")

    assert calls == ["graph", "health"]
    assert result["items"] == []
    assert result["metadata"]["retrieval_path"] == "graph_neo4j"


def test_missing_and_mismatched_graph_keys_are_excluded_without_false_fallback():
    store = Store()
    graph = Graph([_episode(), SimpleNamespace(payload={}), _episode("mem_a", HASH_B)])

    result = GraphFirstResolver(store, graph, lambda _: [0.1]).resolve(project="neurons", query="x")

    assert [item["id"] for item in result["items"]] == ["mem_a"]
    assert result["metadata"]["authority_join_status"] == "mismatch"
    assert result["metadata"]["fallback_used"] is False


def test_missing_embedder_and_database_failure_are_explicit_not_empty_success():
    unavailable = GraphFirstResolver(Store(), None).resolve(project="neurons", query="x")
    assert unavailable["metadata"]["retrieval_path"] == "none"
    assert unavailable["error_code"] == "embedding_unavailable"

    failed = GraphFirstResolver(Store(failure="list"), Graph([_episode()])).resolve(project="neurons", query="x")
    assert failed["metadata"]["authority_join_status"] == "unavailable"
    assert failed["error_code"] == "authority_store_unavailable"

    no_store = GraphFirstResolver(None, Graph([_episode()])).resolve(project="neurons", query="x")
    assert no_store["metadata"]["graph_status"] == "unavailable"
    assert no_store["error_code"] == "authority_store_unavailable"


def test_invalid_as_of_fails_closed():
    resolver = GraphFirstResolver(Store(), Graph([]))
    with pytest.raises(ValidationError):
        resolver.resolve(project="neurons", query="x", as_of="2026/09/05")


def test_utc_date_as_of_normalizes_to_midnight_and_slim_guardrails_field_is_kept():
    request = _ResolveRequest(project="neurons", mode="list", as_of="2026-09-05")
    result = GraphFirstResolver(Store()).resolve(project="neurons", mode="list")

    assert request.as_of == "2026-09-05T00:00:00+00:00"
    assert result["active_guardrails"] == []


def test_query_mode_rejects_missing_or_whitespace_query():
    resolver = GraphFirstResolver(Store(), Graph([]))
    with pytest.raises(ValidationError, match="non-empty query"):
        resolver.resolve(project="neurons")
    with pytest.raises(ValidationError, match="non-empty query"):
        resolver.resolve(project="neurons", query=" \t ")


def test_single_unicode_item_fits_final_default_json_with_cursor_and_keeps_identity():
    cards = [_card("mem_a", HASH_A), _card("mem_b", HASH_B)]
    cards[0]["title"] = "한글😀" * 2000
    cards[0]["summary"] = "근거😀" * 2000
    cards[0]["typed_payload"] = {"decision": "결정😀" * 2000, "rationale": "이유😀" * 2000}

    result = GraphFirstResolver(Store(cards=cards)).resolve(project="neurons", mode="list", limit=1)

    assert len(__import__("json").dumps(result, ensure_ascii=False).encode("utf-8")) <= 3072
    assert result["next_cursor"] is not None
    assert result["items"][0]["id"] == "mem_a"
    assert result["items"][0]["content_hash"] == HASH_A


def test_fallback_drops_stale_ranked_hash_and_labels_embedding_failure():
    store = Store(cards=[_card("mem_a", HASH_A)])
    store.hybrid_search = lambda **kwargs: [_card("mem_a", HASH_B)]
    stale = GraphFirstResolver(store, None, lambda _: [0.1]).resolve(project="neurons", query="x")
    assert stale["items"] == []
    assert stale["metadata"]["fallback_used"] is True

    failed = GraphFirstResolver(Store(), None, lambda _: (_ for _ in ()).throw(RuntimeError("embed"))).resolve(project="neurons", query="x")
    assert failed["error_code"] == "embedding_unavailable"


def test_partial_graph_error_code_is_capped_and_shrinking_always_progresses():
    card = _card()
    card["title"] = "😀" * 2000
    card["summary"] = "한글" * 2000
    card["typed_payload"] = {"decision": "결정" * 2000}
    result = GraphFirstResolver(
        Store(cards=[card], unprojected=True),
        Graph([_episode()]),
        lambda _: (_ for _ in ()).throw(RuntimeError("embed")),
    ).resolve(project="neurons", query="x")

    two_chars = {"title": "ab"}
    assert _shrink_card(two_chars) is True
    assert two_chars["title"] == ""
    assert result["error_code"] == "embedding_unavailable"
    assert len(__import__("json").dumps(result, ensure_ascii=False).encode("utf-8")) <= 3072


def test_unicode_budget_and_cursor_are_stable_and_bound_to_candidates():
    cards = [_card(f"mem_{i}", "sha256:" + f"{i:064x}") for i in range(4)]
    for card in cards:
        card["title"] = "한글😀" * 100
        card["summary"] = "상세 내용😀" * 200
    resolver = GraphFirstResolver(Store(cards=cards))

    first = resolver.resolve(project="neurons", mode="list", limit=2)
    pages = [first]
    while pages[-1]["next_cursor"]:
        pages.append(resolver.resolve(project="neurons", mode="list", limit=2, cursor=pages[-1]["next_cursor"]))

    assert all(tool_result_bytes(page) <= 3072 for page in pages)
    assert [item["id"] for page in pages for item in page["items"]] == ["mem_0", "mem_1", "mem_2", "mem_3"]
    with pytest.raises(ValueError, match="cursor"):
        resolver.resolve(project="other", mode="list", limit=2, cursor=first["next_cursor"])


def _edge(root_id="mem_a", src_id="mem_a", dst_id="mem_b", depth=1):
    return {
        "root_id": root_id,
        "src_id": src_id,
        "dst_id": dst_id,
        "rel_type": "supports",
        "provenance_hash": "sha256:" + "c" * 64,
        "src_content_hash": HASH_A,
        "dst_content_hash": HASH_B,
        "depth": depth,
        "visited_path": [src_id, dst_id],
        "inferred_summary": "must not escape the explicit PG projection",
        "source_ref": "/private/transcript.md",
    }


def test_with_evidence_uses_one_bounded_batch_and_emits_only_explicit_hash_projection():
    calls = []
    store = Store(cards=[_card("mem_a", HASH_A, "note")], calls=calls,
                  evidence={"edges": [_edge()], "truncated": False, "max_depth": 5})

    result = GraphFirstResolver(store).resolve(
        project="neurons", mode="list", limit=2, response_mode="with_evidence"
    )

    evidence_calls = [call for call in calls if isinstance(call, tuple) and call[0] == "evidence"]
    assert len(evidence_calls) == 1
    assert evidence_calls[0][1] == {
        "project": "neurons", "root_memory_ids": ["mem_a"],
        "as_of": None, "max_depth": 5, "limit": 100,
    }
    assert result["schema_version"] == "lbrain_slim_context.v1"
    assert result["evidence"]["content_hashes"] == {"mem_b": HASH_B}
    assert result["evidence"]["explicit_edges"] == [{
        key: _edge()[key] for key in ("src_id", "dst_id", "rel_type", "provenance_hash")
    }]
    assert result["metadata"]["evidence_authority"] == "postgresql_explicit_edges"
    assert result["metadata"]["evidence_max_depth"] == 5
    assert result["metadata"]["evidence_truncated"] is False


def test_with_evidence_never_returns_edges_for_cards_dropped_by_byte_pagination():
    cards = [_card("mem_a", HASH_A), _card("mem_b", HASH_B)]
    for card in cards:
        card["title"] = "한글😀" * 2000
        card["summary"] = "근거😀" * 2000
        card["typed_payload"] = {"decision": "결정😀" * 2000}
    store = Store(cards=cards, evidence={
        "edges": [_edge("mem_a"), _edge("mem_b", "mem_b", "mem_a")], "truncated": False,
    })

    result = GraphFirstResolver(store).resolve(
        project="neurons", mode="list", limit=2, response_mode="with_evidence"
    )

    returned_ids = {item["id"] for item in result["items"]}
    assert len(returned_ids) == 1
    assert {edge["src_id"] for edge in result["evidence"]["explicit_edges"]} <= returned_ids
    assert tool_result_bytes(result) <= 3072


def test_with_evidence_marks_overflow_and_db_failure_as_explicit_tool_errors():
    edges = [_edge(dst_id=f"child_{index:03d}") for index in range(100)]
    large = GraphFirstResolver(Store(evidence={"edges": edges, "truncated": False})).resolve(
        project="neurons", mode="list", response_mode="with_evidence"
    )
    assert large["metadata"]["evidence_truncated"] is True
    assert len(large["evidence"]["explicit_edges"]) < len(edges)
    assert large["items"][0]["summary"] == _card()["summary"]
    assert large["decisions"][0]["decision"] == _card()["typed_payload"]["decision"]
    assert tool_result_bytes(large) <= 3072

    unavailable = GraphFirstResolver(Store(failure="evidence")).resolve(
        project="neurons", mode="list", response_mode="with_evidence"
    )
    assert unavailable["error_code"] == "evidence_unavailable"
    assert tool_result_bytes(unavailable) <= 3072


def test_evidence_revision_or_authorization_change_between_reads_fails_closed():
    store = Store(evidence={"edges": [], "root_hashes": {"mem_a": HASH_B}})
    changed = GraphFirstResolver(store).resolve(project="neurons", mode="list", response_mode="with_evidence")
    assert changed["error_code"] == "evidence_unavailable"
    assert changed["items"] == []
    store.evidence["root_hashes"] = {}
    revoked = GraphFirstResolver(store).resolve(project="neurons", mode="list", response_mode="with_evidence")
    assert revoked["error_code"] == "evidence_unavailable"

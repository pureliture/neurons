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
    def __init__(self, cards=None, *, unprojected=False, calls=None, failure=None, evidence=None,
                 context_cards=None):
        self.cards = list(cards or [_card()])
        self.context_cards = list(context_cards) if context_cards is not None else None
        self.unprojected = unprojected
        self.calls = calls if calls is not None else []
        self.failure = failure
        self.evidence = evidence if evidence is not None else {"edges": [], "truncated": False}

    def graph_projection_health(self, project, *, as_of=None, steward_only=False):
        assert steward_only is True
        self.calls.append("health")
        if self.failure == "health":
            raise RuntimeError("database down")
        return {"unprojected": self.unprojected, "projection_lag_ms": 42 if self.unprojected else None}

    def list_authorized_cards(self, **kwargs):
        assert kwargs["steward_only"] is True
        self.calls.append(("list", kwargs.get("memory_ids")))
        if self.failure == "list":
            raise RuntimeError("database down")
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        if wanted is None:
            cards = [c for c in sorted(cards, key=lambda c: c["memory_id"])
                     if c["memory_id"] > (kwargs.get("after_memory_id") or "")]
        return cards[:kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        assert kwargs["steward_only"] is True
        self.calls.append(("context", kwargs))
        if self.failure == "context":
            raise RuntimeError("database down")
        return list(self.context_cards or self.cards)[:kwargs.get("limit", 100)]

    def hybrid_search(self, **kwargs):
        assert kwargs["steward_only"] is True
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
    # The PG scorer supplies finite scores; this test isolates graph/PG ordering.
    for card in store.cards:
        card["semantic_score"] = 0.9
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


def test_context_uses_preselected_current_work_decision_status_and_guardrail():
    task = _card("mem_task", card_type="task")
    task["typed_payload"] = {"task_state": "Finish latest work", "next_action": "Run tests"}
    decision = _card("mem_decision", card_type="decision")
    decision["typed_payload"] = {"decision": "Use the approved current design"}
    status = _card("mem_status", card_type="status")
    status["typed_payload"] = {"status_value": "blocked pending verification"}
    guardrail = _card("mem_guardrail", card_type="preference")
    guardrail["typed_payload"] = {"rule": "Keep proposal writes review-only", "scope": "neurons"}
    old_preference = _card("mem_old_preference", card_type="preference")
    old_preference["summary"] = "old preference " * 700
    old_preference["typed_payload"] = {"rule": "old preference " * 700, "scope": "general"}
    selected = [task, decision, status, guardrail, old_preference]
    store = Store(cards=[old_preference, task, decision, status, guardrail], context_cards=selected)

    result = GraphFirstResolver(store).resolve(
        project="neurons", query="latest task decision", mode="context", limit=4,
    )

    assert result.get("error_code") is None, result
    context_call = next(call for call in store.calls if call[0] == "context")
    assert context_call[1]["query"] == "latest task decision"
    assert context_call[1]["limit"] == 5
    assert [item["id"] for item in result["items"]] == [
        "mem_task", "mem_decision", "mem_status", "mem_guardrail",
    ]
    assert result["tasks"][0]["task"] == "Finish latest work"
    assert result["decisions"][0]["decision"] == "Use the approved current design"
    assert result["statuses"][0]["status"] == "blocked pending verification"
    assert result["active_guardrails"] == ["Keep proposal writes review-only"]
    assert tool_result_bytes(result) <= 3072


def test_list_keeps_stable_memory_id_keyset_contract_after_context_split():
    cards = [_card("mem_b", HASH_B), _card("mem_a", HASH_A)]
    store = Store(cards=cards)
    resolver = GraphFirstResolver(store)

    first = resolver.resolve(project="neurons", mode="list", limit=1)
    second = resolver.resolve(
        project="neurons", mode="list", limit=1, cursor=first["next_cursor"],
    )

    assert [item["id"] for item in first["items"] + second["items"]] == ["mem_a", "mem_b"]
    assert [call[0] for call in store.calls if isinstance(call, tuple)] == ["list", "list"]


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


def test_fallback_uses_query_text_to_rank_equally_embedded_authorized_cards():
    class TextRankingStore(Store):
        def hybrid_search(self, **kwargs):
            assert kwargs["steward_only"] is True
            assert kwargs["project"] == "neurons"
            assert kwargs["text_query"] == "postgres authority"
            # The PG hybrid scorer ranks the lexical match ahead of a vector tie.
            return [self.cards[1], self.cards[0]]

    cards = [_card("mem_a", HASH_A), _card("mem_b", HASH_B)]
    cards[0]["title"] = "unrelated topic"
    cards[1]["title"] = "postgres authority"
    result = GraphFirstResolver(TextRankingStore(cards=cards), None, lambda _: [0.1]).resolve(
        project="neurons", query="postgres authority", limit=1,
    )
    assert [item["id"] for item in result["items"]] == ["mem_b"]
    assert result["metadata"]["retrieval_path"] == "pgvector_fallback"


def test_fallback_drops_stale_ranked_hash_and_labels_embedding_failure():
    store = Store(cards=[_card("mem_a", HASH_A)])
    store.hybrid_search = lambda **kwargs: [_card("mem_a", HASH_B)]
    stale = GraphFirstResolver(store, None, lambda _: [0.1]).resolve(project="neurons", query="x")
    assert stale["items"] == []
    assert stale["metadata"]["fallback_used"] is True

    failed = GraphFirstResolver(Store(), None, lambda _: (_ for _ in ()).throw(RuntimeError("embed"))).resolve(project="neurons", query="x")
    assert failed["error_code"] == "embedding_unavailable"


def test_fallback_rejects_unrelated_negative_semantic_score():
    card = _card("mem_unrelated", HASH_A)
    card["title"] = "The current database uses PostgreSQL"
    card["summary"] = "Storage implementation notes"
    card["similarity_score"] = -0.18
    store = Store(cards=[card])

    result = GraphFirstResolver(store, None, lambda _query: [0.1] * 3072).resolve(
        project="neurons", query="unrelated astronomy question", limit=5
    )

    assert result["items"] == []
    assert result["metadata"]["retrieval_path"] == "pgvector_fallback"


@pytest.mark.parametrize("score", [None, float("nan"), float("inf"), True, "0.99"])
def test_fallback_rejects_missing_or_invalid_semantic_score_without_lexical_match(score):
    card = _card()
    if score is not None:
        card["similarity_score"] = score
    result = GraphFirstResolver(Store(cards=[card]), None, lambda _: [0.1]).resolve(
        project="neurons", query="unrelated astronomy question",
    )
    assert result["items"] == []
    assert result["metadata"]["authority_join_status"] == "verified"


@pytest.mark.parametrize(
    ("semantic_score", "blended_score", "expected_ids"),
    [(0.75, 0.6, ["mem_a"]), (0.74, 0.99, [])],
)
def test_fallback_uses_existing_semantic_threshold_not_blended_rank(
    semantic_score, blended_score, expected_ids,
):
    card = _card()
    card.update(semantic_score=semantic_score, similarity_score=blended_score)
    result = GraphFirstResolver(Store(cards=[card]), None, lambda _: [0.1]).resolve(
        project="neurons", query="unrelated astronomy question",
    )
    assert [item["id"] for item in result["items"]] == expected_ids


def test_fallback_keeps_lexically_relevant_card_even_with_negative_semantic_score():
    card = _card()
    card.update(title="postgres authority", similarity_score=-0.18)
    result = GraphFirstResolver(Store(cards=[card]), None, lambda _: [0.1]).resolve(
        project="neurons", query="postgres authority",
    )
    assert [item["id"] for item in result["items"]] == ["mem_a"]


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


def test_non_card_session_episodes_and_session_facts_do_not_degrade_authority_join_status():
    session_episode = SimpleNamespace(
        entity_type="Session",
        payload={"brain_id": "/project/neurons", "provider": "codex", "summary": "session trace"},
    )
    session_fact = SimpleNamespace(
        entity_type="GraphFact",
        valid_from="2026-08-01T00:00:00+00:00",
        valid_to="",
        payload={
            "fact": "session fact without authority card",
            "authority_sources": [],
            "source_entity_types": ["Session"],
        },
    )
    only_session_result = GraphFirstResolver(
        Store(),
        Graph([session_episode, session_fact]),
        lambda _: [0.1],
    ).resolve(project="neurons", query="session trace")
    assert only_session_result["items"] == []
    assert only_session_result["metadata"]["authority_join_status"] == "verified"

    mixed_result = GraphFirstResolver(
        Store(),
        Graph([session_episode, session_fact, _episode("mem_a", HASH_A)]),
        lambda _: [0.1],
    ).resolve(project="neurons", query="session trace")
    assert [item["id"] for item in mixed_result["items"]] == ["mem_a"]
    assert mixed_result["metadata"]["authority_join_status"] == "verified"


def test_session_chunk_semantic_fallback_returns_work_in_progress_status_when_cards_empty():
    class ChunkFallbackStore(Store):
        def list_context_cards(self, **kwargs):
            self.calls.append(("context", kwargs))
            return []

        def hybrid_search(self, **kwargs):
            self.calls.append("hybrid")
            return []

        def search_session_chunks(self, query_vector, project=None, limit=5, conn=None):
            self.calls.append(("chunks", project))
            return [
                {
                    "chunk_id": "chk_hermes_s3",
                    "project": project or "hermes-stocks-agent",
                    "provider": "hermes",
                    "content_markdown": "# Hermes Stocks S3 진행 상태\nS1, S2 완료 후 S3 모의투자 기간별 성과 API 및 체결 원장 검증 진행 중",
                    "content_hash": HASH_A,
                    "distance": 0.18,
                }
            ]

    store = ChunkFallbackStore(cards=[])
    resolver = GraphFirstResolver(store, Graph([]), lambda _: [0.1, 0.2])

    query_res = resolver.resolve(
        project="hermes-stocks-agent",
        mode="query",
        query="모의투자 에이전트 개발 어디까지 진행했지?",
    )
    assert [item["id"] for item in query_res["items"]] == ["chunk:chk_hermes_s3"]
    assert "S3" in query_res["statuses"][0]["status"]
    assert query_res["metadata"]["retrieval_path"] == "pgvector_fallback"
    assert query_res["metadata"]["fallback_used"] is True

    ctx_res = resolver.resolve(
        project="hermes-stocks-agent",
        mode="context",
        query="모의투자 에이전트 개발 진행",
    )
    assert [item["id"] for item in ctx_res["items"]] == ["chunk:chk_hermes_s3"]
    assert "S3" in ctx_res["statuses"][0]["status"]
    assert ctx_res["metadata"]["fallback_used"] is True


_SESSION_CHUNK_HEADER = (
    "# session-memory hermes hermes-stocks-agent\n"
    "session_id_hash: sha256:{sid}\n\n"
    "## conversation\n\n"
    "---\n"
    "schema_version: agent_knowledge_document.v2\n"
    "provider: hermes\n"
    "project: hermes-stocks-agent\n"
    "---\n"
    "# Conversation Chunk\n\n"
    "## Context\n\n"
    "- provider: hermes\n\n"
    "## Transcript\n\n"
)


def test_session_chunk_cards_use_transcript_body_not_shared_header():
    from agent_knowledge.llm_brain_core.graph_first_resolver import _session_chunk_body

    body = _session_chunk_body(
        _SESSION_CHUNK_HEADER.format(sid="a1")
        + "user: LANGUAGE CONTRACT: 모든 자연어 응답은 한국어로 작성한다.\n\n"
        "[paper-trader-kr-decision] KR sleeve 모의투자 판단 실행\n"
    )
    assert body.startswith("[paper-trader-kr-decision]")
    assert "session_id_hash" not in body
    assert "LANGUAGE CONTRACT" not in body

    # marker가 없는 청크도 front matter와 session-memory 헤더는 건너뛴다.
    no_marker = _session_chunk_body(
        "# session-memory hermes x\nsession_id_hash: sha256:b2\n\n## conversation\n\n"
        "---\nprovider: hermes\n---\nS3 체결 원장 검증 진행 중\n"
    )
    assert no_marker == "S3 체결 원장 검증 진행 중"

    # 본문이 전혀 없으면 원문을 그대로 돌려줘 카드가 사라지지 않는다.
    assert _session_chunk_body("plain text only") == "plain text only"


def test_session_chunk_fallback_titles_differ_across_sessions():
    class DistinctChunkStore(Store):
        def list_context_cards(self, **kwargs):
            return []

        def hybrid_search(self, **kwargs):
            return []

        def search_session_chunks(self, query_vector, project=None, limit=5, conn=None):
            return [
                {
                    "chunk_id": f"chk_{idx}",
                    "project": project or "hermes-stocks-agent",
                    "content_markdown": _SESSION_CHUNK_HEADER.format(sid=f"sid{idx}") + f"user: 모의투자 작업 {label}\n",
                    "content_hash": (HASH_A, HASH_B)[idx],
                    "distance": 0.1 + idx * 0.01,
                }
                for idx, label in enumerate(["S1 초기화", "S2 검증"])
            ]

    result = GraphFirstResolver(DistinctChunkStore(cards=[]), Graph([]), lambda _: [0.1, 0.2]).resolve(
        project="hermes-stocks-agent", mode="query", query="모의투자 작업", limit=5
    )
    titles = [item["title"] for item in result["items"]]
    # 라벨은 응답 예산(_MAX_BYTES) 안에 두 카드가 모두 직렬화되는 길이로 유지한다.
    assert titles == ["모의투자 작업 S1 초기화", "모의투자 작업 S2 검증"]
    assert all("session-memory" not in status["status"] for status in result["statuses"])

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.native_memory_mirror import (
    NativeMemoryMirrorStore,
    brain_id_for_project,
)
from agent_knowledge.session_memory.native_memory_recall import (
    NATIVE_MEMORY_OVERFETCH_THRESHOLD,
    filter_active_native_memory,
    recall_active_native_memory,
)


FIXED = datetime(2026, 6, 8, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 9, tzinfo=timezone.utc)


def _store(tmp_path) -> NativeMemoryMirrorStore:
    return NativeMemoryMirrorStore(Ledger(tmp_path / "ledger.sqlite3"))


def _hit(session_id, *, content="c", message_type="semantic", **extra):
    item = {
        "content": content,
        "message_type": message_type,
        "session_id": session_id,
        "status": True,
    }
    item.update(extra)
    return item


# --- C4.1: filter_active_native_memory 순수 필터 ---


def test_filter_keeps_active_drops_superseded_and_unregistered(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    store.upsert_statement(
        statement_id="1002", brain_id="/a", original_content_hash="h2", now=FIXED
    )
    store.mark_superseded("1002", superseded_by="1001", now=LATER)

    hits = [
        _hit("mem:1001"),
        _hit("mem:1002"),
        _hit("mem:9999"),
    ]
    kept = filter_active_native_memory(hits, store)

    assert len(kept) == 1
    assert kept[0]["session_tag"] == "mem:1001"


def test_filter_metadata_shape(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/profile/x", original_content_hash="h1", now=FIXED
    )
    hits = [_hit("mem:1001", content="prefers spaces", message_type="procedural")]
    kept = filter_active_native_memory(hits, store)

    item = kept[0]
    assert item["kind"] == "native_memory"
    assert item["approval_state"] == "active"
    assert item["policy_reason"] == "native_memory_active_mirror_match"
    assert item["currentness"] == "active_native_memory"
    assert item["brain_id"] == "/profile/x"
    assert item["message_type"] == "procedural"
    assert item["content"] == "prefers spaces"
    assert item["session_tag"] == "mem:1001"


# --- Auto-1: brain_id(project) 필터 — 단일 Memory multi-project 혼재 제거 ---


def test_brain_id_for_project_helper():
    assert brain_id_for_project("workspace-x") == "/project/workspace-x"


def test_filter_by_brain_id_keeps_only_matching_project(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="a1", brain_id="/project/A", original_content_hash="h1", now=FIXED)
    store.upsert_statement(statement_id="b1", brain_id="/project/B", original_content_hash="h2", now=FIXED)
    hits = [_hit("mem:a1"), _hit("mem:b1")]

    kept = filter_active_native_memory(hits, store, brain_id="/project/A")

    assert [k["session_tag"] for k in kept] == ["mem:a1"]


def test_filter_empty_brain_id_keeps_all_active(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="a1", brain_id="/project/A", original_content_hash="h1", now=FIXED)
    store.upsert_statement(statement_id="b1", brain_id="/project/B", original_content_hash="h2", now=FIXED)
    hits = [_hit("mem:a1"), _hit("mem:b1")]

    kept = filter_active_native_memory(hits, store)  # brain_id 미지정 = 전체(하위호환)

    assert {k["session_tag"] for k in kept} == {"mem:a1", "mem:b1"}


def test_recall_passes_brain_id_filter(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="a1", brain_id="/project/A", original_content_hash="h1", now=FIXED)
    store.upsert_statement(statement_id="b1", brain_id="/project/B", original_content_hash="h2", now=FIXED)

    class _FakeRagflow:
        def search_messages(self, **kwargs):
            return {"status_code": 200, "json": {"code": 0, "data": [_hit("mem:a1"), _hit("mem:b1")]}}

    kept = recall_active_native_memory(
        ragflow=_FakeRagflow(), store=store, memory_id="m", query="q", brain_id="/project/A",
    )

    assert [k["session_tag"] for k in kept] == ["mem:a1"]


def test_filter_score_defaults_none_when_absent(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    store.upsert_statement(
        statement_id="1002", brain_id="/a", original_content_hash="h2", now=FIXED
    )
    hits = [
        _hit("mem:1001", score=0.42),
        _hit("mem:1002"),
    ]
    kept = filter_active_native_memory(hits, store)
    by_tag = {h["session_tag"]: h for h in kept}
    assert by_tag["mem:1001"]["score"] == 0.42
    assert by_tag["mem:1002"]["score"] is None


def test_filter_preserves_input_order(tmp_path):
    store = _store(tmp_path)
    for sid in ("1001", "1002", "1003"):
        store.upsert_statement(
            statement_id=sid, brain_id="/a", original_content_hash="h" + sid, now=FIXED
        )
    hits = [_hit("mem:1003"), _hit("mem:1001"), _hit("mem:1002")]
    kept = filter_active_native_memory(hits, store)
    assert [h["session_tag"] for h in kept] == ["mem:1003", "mem:1001", "mem:1002"]


def test_filter_empty_hits_returns_empty(tmp_path):
    store = _store(tmp_path)
    assert filter_active_native_memory([], store) == []


def test_filter_raw_only_active_passes(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    hits = [_hit("mem:1001", message_type="raw")]
    kept = filter_active_native_memory(hits, store)
    assert len(kept) == 1
    assert kept[0]["message_type"] == "raw"


def test_filter_paraphrased_content_still_passes_by_session_join(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h_original", now=FIXED
    )
    hits = [_hit("mem:1001", content="The user has a habit of running lint checks")]
    kept = filter_active_native_memory(hits, store)
    assert len(kept) == 1
    assert kept[0]["content"] == "The user has a habit of running lint checks"


class _ExplodingStore:
    def get_by_session_tags(self, tags):
        raise RuntimeError("store unavailable")


def test_filter_propagates_store_exception():
    hits = [_hit("mem:1001")]
    with pytest.raises(RuntimeError, match="store unavailable"):
        filter_active_native_memory(hits, _ExplodingStore())


# --- C4.2: recall_active_native_memory orchestration ---


def _envelope(chunks):
    return {"status_code": 200, "json": {"code": 0, "data": {"chunks": list(chunks)}}}


class _FakeSearchRagflow:
    """search_messages 덕타입. top_n 별 응답 주입 + 호출 기록.

    duck-type for RagflowHttpClient.search_messages(*, query, memory_id, top_n) —
    실 client 시그니처(query/memory_id/top_n keyword-only)와 일치시킨다. client
    시그니처가 바뀌면 이 fake 도 함께 갱신할 것.
    """

    def __init__(self, by_top_n):
        # by_top_n: dict[int, list[hit]]
        self.by_top_n = by_top_n
        self.search_calls: list[dict] = []

    def search_messages(self, *, query, memory_id, top_n=10):
        self.search_calls.append({"query": query, "memory_id": memory_id, "top_n": top_n})
        return _envelope(self.by_top_n.get(top_n, []))


def _seed_active(store, *sids):
    for sid in sids:
        store.upsert_statement(
            statement_id=sid, brain_id="/a", original_content_hash="h" + sid, now=FIXED
        )


def test_recall_filters_to_active(tmp_path):
    store = _store(tmp_path)
    _seed_active(store, "1001", "1002")
    store.mark_superseded("1001", superseded_by="1002", now=LATER)
    # active 가 1개(<threshold)라 over-fetch 가 한 번 더 돌므로 50-응답도 동일하게 준다.
    both = [_hit("mem:1001"), _hit("mem:1002")]
    ragflow = _FakeSearchRagflow({10: both, 50: both})
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="preference"
    )
    assert [h["session_tag"] for h in out] == ["mem:1002"]


def test_recall_overfetch_refetches_once_when_below_threshold(tmp_path):
    store = _store(tmp_path)
    _seed_active(store, "1001", "1002")
    ragflow = _FakeSearchRagflow(
        {
            10: [_hit("mem:1001")],
            50: [_hit("mem:1001"), _hit("mem:1002")],
        }
    )
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="q"
    )
    assert len(out) >= NATIVE_MEMORY_OVERFETCH_THRESHOLD
    assert len(ragflow.search_calls) == 2
    assert ragflow.search_calls[1]["top_n"] == 50


def test_recall_no_overfetch_when_threshold_met(tmp_path):
    store = _store(tmp_path)
    _seed_active(store, "1001", "1002")
    ragflow = _FakeSearchRagflow(
        {10: [_hit("mem:1001"), _hit("mem:1002")]}
    )
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="q"
    )
    assert len(out) == 2
    assert len(ragflow.search_calls) == 1


def test_recall_overfetch_bounded_to_one_even_if_still_short(tmp_path):
    store = _store(tmp_path)
    _seed_active(store, "1001")
    ragflow = _FakeSearchRagflow(
        {
            10: [_hit("mem:1001")],
            50: [_hit("mem:1001")],
        }
    )
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="q"
    )
    assert len(out) == 1
    assert len(ragflow.search_calls) == 2


def test_recall_empty_hits_returns_empty(tmp_path):
    store = _store(tmp_path)
    ragflow = _FakeSearchRagflow({10: []})
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="q"
    )
    assert out == []


def test_recall_extracts_hits_from_envelope_data_chunks(tmp_path):
    store = _store(tmp_path)
    _seed_active(store, "1001", "1002")
    ragflow = _FakeSearchRagflow(
        {10: [_hit("mem:1001"), _hit("mem:1002")]}
    )
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="q"
    )
    assert {h["session_tag"] for h in out} == {"mem:1001", "mem:1002"}


class _MalformedRagflow:
    """search_messages 가 비정상 envelope 를 반환하는 fake. _extract_hits fail-closed 검증."""

    def __init__(self, result):
        self.result = result

    def search_messages(self, *, query, memory_id, top_n=10):
        return self.result


@pytest.mark.parametrize(
    "result",
    [
        {"status_code": 200, "json": None},
        {"status_code": 200, "json": {"code": 0, "data": None}},
        {"status_code": 200, "json": "All search done."},
        {"status_code": 200, "json": {"code": 0, "data": {"chunks": None}}},
        {"status_code": 200, "json": {"code": 0}},
    ],
)
def test_recall_malformed_envelope_returns_empty(tmp_path, result):
    store = _store(tmp_path)
    _seed_active(store, "1001")
    ragflow = _MalformedRagflow(result)
    out = recall_active_native_memory(
        ragflow=ragflow, store=store, memory_id="mem_main", query="q"
    )
    assert out == []


class _ExplodingStoreSearch:
    def get_by_session_tags(self, tags):
        raise RuntimeError("store unavailable")


def test_recall_propagates_store_exception(tmp_path):
    ragflow = _FakeSearchRagflow({10: [_hit("mem:1001")]})
    with pytest.raises(RuntimeError, match="store unavailable"):
        recall_active_native_memory(
            ragflow=ragflow,
            store=_ExplodingStoreSearch(),
            memory_id="mem_main",
            query="q",
        )


# --- U3(completion): recall 메타 tier 노출 (card_type 기준) ---


def test_filter_tier_from_card_type_high(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1",
        card_type="user_preference", now=FIXED,
    )
    kept = filter_active_native_memory([_hit("mem:1001")], store)
    assert kept[0]["tier"] == "high"


def test_filter_tier_empty_card_type_fail_closed_high(tmp_path):
    # 기존 slice1 row(card_type='')는 tier='high' 로 표시되나 keep/drop 판정엔 영향 없음.
    store = _store(tmp_path)
    store.upsert_statement(statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED)
    kept = filter_active_native_memory([_hit("mem:1001")], store)
    assert len(kept) == 1
    assert kept[0]["tier"] == "high"


def test_filter_tier_uses_card_type_not_message_type(tmp_path):
    # 어휘 경계: hit.message_type='raw' 이어도 tier 는 row.card_type='procedural_rule'(low) 기준.
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1",
        card_type="procedural_rule", now=FIXED,
    )
    raw_hit = _hit("mem:1001", message_type="raw")
    semantic_hit = _hit("mem:1001", message_type="semantic")
    kept = filter_active_native_memory([raw_hit, semantic_hit], store)
    assert [k["tier"] for k in kept] == ["low", "low"]

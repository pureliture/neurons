from __future__ import annotations

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.native_memory_mirror import NativeMemoryMirrorStore
from agent_knowledge.session_memory.native_memory_reconcile import (
    RECONCILE_NATIVE_MEMORY_SCHEMA_VERSION,
    NativeMemoryReconcileConfig,
    NativeMemoryReconcileRunner,
)


def _store(tmp_path) -> NativeMemoryMirrorStore:
    return NativeMemoryMirrorStore(Ledger(tmp_path / "ledger.sqlite3"))


def _ok():
    return {"status_code": 200, "json": {"code": 0, "data": None, "message": True}}


def _fail():
    return {"status_code": 200, "json": {"code": 102, "message": "not found"}}


def _search_envelope(items):
    return {"status_code": 200, "json": {"code": 0, "data": items}}


def _item(session_id, message_id, message_type="semantic"):
    return {"session_id": session_id, "message_id": message_id, "message_type": message_type, "status": True}


class _FakeRagflow:
    def __init__(self, *, search_result, disable_results=None):
        self.search_result = search_result
        # message_id(str) → envelope. 기본 모두 성공.
        self.disable_results = disable_results or {}
        self.search_calls: list[dict] = []
        self.disable_calls: list[dict] = []

    def search_messages(self, *, query, memory_id, top_n=10):
        self.search_calls.append({"query": query, "memory_id": memory_id, "top_n": top_n})
        return self.search_result

    def disable_message(self, *, memory_id, message_id):
        self.disable_calls.append({"memory_id": memory_id, "message_id": message_id})
        return self.disable_results.get(message_id, _ok())


def _superseded_row(store, statement_id="1001", *, search_text="prefers tabs"):
    store.upsert_statement(statement_id=statement_id, brain_id="/a", original_content_hash="h1", search_text=search_text)
    store.mark_superseded(statement_id, superseded_by="2002")


def _runner(ragflow, store, *, memory_id="mem_main", **cfg):
    config = NativeMemoryReconcileConfig(memory_id=memory_id, **cfg)
    return NativeMemoryReconcileRunner(ragflow=ragflow, store=store, config=config, now_func=lambda: "2026-06-09T00:00:00+00:00")


def test_reconcile_disables_all_session_tag_items_and_marks(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    # session_tag 공유 raw 1 + 추출 2 = 3 item(게이트 사실: 전부 disable).
    ragflow = _FakeRagflow(search_result=_search_envelope([
        _item("mem:1001", 41, "raw"),
        _item("mem:1001", 42, "semantic"),
        _item("mem:1001", 43, "procedural"),
    ]))
    report = _runner(ragflow, store).run()
    assert [c["message_id"] for c in ragflow.disable_calls] == ["41", "42", "43"]
    assert report["disabled_total"] == 3
    assert report["rows_fully_disabled"] == 1
    assert report["mutation_performed"] is True
    assert report["schema_version"] == RECONCILE_NATIVE_MEMORY_SCHEMA_VERSION
    assert store.list_pending_reconcile() == []


def test_reconcile_filters_foreign_session_tags(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    ragflow = _FakeRagflow(search_result=_search_envelope([
        _item("mem:1001", 42),
        _item("mem:9999", 99),  # 다른 session_tag — 절대 disable 안 함(거짓양성 0).
    ]))
    _runner(ragflow, store).run()
    assert [c["message_id"] for c in ragflow.disable_calls] == ["42"]


def test_reconcile_no_match_leaves_row_pending(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    ragflow = _FakeRagflow(search_result=_search_envelope([_item("mem:9999", 99)]))
    report = _runner(ragflow, store).run()
    assert ragflow.disable_calls == []
    assert report["rows_no_match"] == 1
    assert [r["statement_id"] for r in store.list_pending_reconcile()] == ["1001"]


def test_reconcile_search_failed_leaves_row_pending(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    ragflow = _FakeRagflow(search_result=_fail())
    report = _runner(ragflow, store).run()
    assert ragflow.disable_calls == []
    assert report["rows_search_failed"] == 1
    assert [r["statement_id"] for r in store.list_pending_reconcile()] == ["1001"]


def test_reconcile_partial_disable_does_not_mark(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    ragflow = _FakeRagflow(
        search_result=_search_envelope([_item("mem:1001", 42), _item("mem:1001", 43)]),
        disable_results={"43": _fail()},  # 둘 중 하나 disable 실패 → partial.
    )
    report = _runner(ragflow, store).run()
    assert report["rows_partial"] == 1
    assert report["rows_fully_disabled"] == 0
    # ragflow_disabled_at 미기록 → row 잔존(다음 run 재시도).
    assert [r["statement_id"] for r in store.list_pending_reconcile()] == ["1001"]


def test_reconcile_idempotent_second_run_no_pending(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    ragflow = _FakeRagflow(search_result=_search_envelope([_item("mem:1001", 42)]))
    _runner(ragflow, store).run()
    assert store.list_pending_reconcile() == []
    # 두 번째 run: pending 0 → 처리 0(재disable 없음).
    ragflow2 = _FakeRagflow(search_result=_search_envelope([_item("mem:1001", 42)]))
    report2 = _runner(ragflow2, store).run()
    assert report2["processed"] == 0
    assert ragflow2.disable_calls == []


def test_reconcile_bounded_max_rows_per_run(tmp_path):
    store = _store(tmp_path)
    for sid in ("1001", "1002", "1003"):
        _superseded_row(store, sid)
    # 각 row 의 session_tag 에 맞는 단일 hit 를 모두 담아 반환하되, max_rows_per_run=2 로 제한.
    ragflow = _FakeRagflow(search_result=_search_envelope([
        _item("mem:1001", 11), _item("mem:1002", 12), _item("mem:1003", 13),
    ]))
    report = _runner(ragflow, store, max_rows_per_run=2).run()
    assert report["processed"] == 2
    # 1 row 는 다음 run 으로 잔류.
    assert len(store.list_pending_reconcile()) == 1


def test_reconcile_one_returns_outcome_shape(tmp_path):
    store = _store(tmp_path)
    _superseded_row(store)
    ragflow = _FakeRagflow(search_result=_search_envelope([_item("mem:1001", 42)]))
    runner = _runner(ragflow, store)
    pending = store.list_pending_reconcile()
    outcome = runner.reconcile_one(pending[0])
    assert outcome["ok"] is True
    assert outcome["session_tag"] == "mem:1001"
    assert outcome["matched"] == 1
    assert outcome["disabled"] == 1

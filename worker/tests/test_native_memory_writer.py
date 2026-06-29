from __future__ import annotations

from datetime import datetime, timezone

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.native_memory_mirror import NativeMemoryMirrorStore
from agent_knowledge.session_memory.native_memory_writer import (
    ApprovedStatement,
    NativeMemoryMirrorWriter,
)


FIXED = datetime(2026, 6, 8, tzinfo=timezone.utc)


class _FakeRetiredIndexBridge:
    """add_message 덕타입. 호출 기록 + 주입 envelope 반환."""

    def __init__(self, add_result=None):
        self.add_result = add_result or {"status_code": 200, "json": {"code": 0, "data": None}}
        self.add_calls: list[dict] = []

    def add_message(self, **kwargs):
        self.add_calls.append(kwargs)
        return self.add_result


def _store(tmp_path) -> NativeMemoryMirrorStore:
    return NativeMemoryMirrorStore(Ledger(tmp_path / "ledger.sqlite3"))


def _writer(retired_index_bridge, store) -> NativeMemoryMirrorWriter:
    return NativeMemoryMirrorWriter(
        retired_index_bridge=retired_index_bridge,
        store=store,
        memory_id="mem_main",
        agent_id="agent_x",
        user_id="user_y",
    )


def _stmt(**overrides) -> ApprovedStatement:
    fields = {
        "statement_id": "1001",
        "brain_id": "/profile/x",
        "text": "prefers spaces",
        "original_content_hash": "h1",
        "card_type": "semantic_fact",
        "approved": True,
        "provenance_status": "pass",
        "eval_status": "pass",
    }
    fields.update(overrides)
    return ApprovedStatement(**fields)


# --- C3.1: 신규 statement happy path ---


def test_write_new_statement_calls_add_message_and_records(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt()

    result = writer.write(stmt, now=FIXED)

    assert len(retired_index_bridge.add_calls) == 1
    call = retired_index_bridge.add_calls[0]
    assert call["memory_id"] == ["mem_main"]
    assert call["agent_id"] == "agent_x"
    assert call["session_id"] == "mem:1001"
    assert call["user_input"] == "prefers spaces"
    assert call["agent_response"] == ""
    assert call["user_id"] == "user_y"

    assert result == {"written": True, "session_tag": "mem:1001", "tier": "low"}

    rows = store.get_by_session_tags(["mem:1001"])
    assert rows["mem:1001"]["status"] == "active"
    assert rows["mem:1001"]["index_memory_id"] == ""


# --- C3.2: 원문 dedup skip ---


def test_write_duplicate_active_skips_add_message(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt()

    writer.write(stmt, now=FIXED)
    second = writer.write(stmt, now=FIXED)

    assert len(retired_index_bridge.add_calls) == 1
    assert second == {"written": False, "reason": "duplicate_active"}


def test_write_superseded_same_hash_reactivates(tmp_path):
    # dedup 은 status=='active' 인 row 에만 적용된다. superseded 상태면 hash 가 같아도
    # dedup 을 통과해 add_message 를 재호출하고 upsert_statement 가 active 로 재활성화한다.
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt(brain_id="/a", text="t")

    writer.write(stmt, now=FIXED)
    store.mark_superseded("1001", superseded_by="1002")

    result = writer.write(stmt, now=FIXED)

    assert len(retired_index_bridge.add_calls) == 2
    assert result == {"written": True, "session_tag": "mem:1001", "tier": "low"}
    assert store.get_by_session_tags(["mem:1001"])["mem:1001"]["status"] == "active"


def test_write_same_id_different_hash_not_dedup(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    first = _stmt()
    second = _stmt(text="prefers tabs", original_content_hash="h2")

    writer.write(first, now=FIXED)
    result = writer.write(second, now=FIXED)

    assert len(retired_index_bridge.add_calls) == 2
    assert result == {"written": True, "session_tag": "mem:1001", "tier": "low"}


# --- C3.3: add_message envelope 실패 시 store 미변경 ---


def test_write_add_message_rejected_does_not_record(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge(
        add_result={"status_code": 200, "json": {"code": 101, "message": "boom"}}
    )
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt()

    result = writer.write(stmt, now=FIXED)

    assert result == {
        "written": False,
        "reason": "add_message_rejected",
        "envelope_code": 101,
    }
    assert store.get_by_session_tags(["mem:1001"]) == {}


# --- U3(completion): governance tier 적용 ---


def test_write_unapproved_rejected_no_add(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt(text="prefers tabs", card_type="user_preference", approved=False)
    result = writer.write(stmt, now=FIXED)
    assert result == {
        "written": False,
        "reason": "operator_approval_required",
        "tier": "high",
        "card_type": "user_preference",
        "provenance_status": "pass",
        "eval_status": "pass",
    }
    assert retired_index_bridge.add_calls == []
    assert store.get_by_session_tags(["mem:1001"]) == {}


def test_write_high_risk_approved_mirrors(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt(text="prefers tabs", card_type="user_preference")
    result = writer.write(stmt, now=FIXED)
    assert result == {"written": True, "session_tag": "mem:1001", "tier": "high"}
    assert len(retired_index_bridge.add_calls) == 1
    row = store.get_by_session_tags(["mem:1001"])["mem:1001"]
    assert row["card_type"] == "user_preference"
    assert row["search_text"] == "prefers tabs"


def test_write_low_risk_unapproved_is_still_rejected(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    stmt = _stmt(
        brain_id="/a",
        text="run lint before deploy",
        card_type="procedural_rule",
        approved=False,
    )
    result = writer.write(stmt, now=FIXED)
    assert result == {
        "written": False,
        "reason": "operator_approval_required",
        "tier": "low",
        "card_type": "procedural_rule",
        "provenance_status": "pass",
        "eval_status": "pass",
    }
    assert retired_index_bridge.add_calls == []
    assert store.get_by_session_tags(["mem:1001"]) == {}


def test_write_blocks_when_provenance_has_not_passed(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    result = writer.write(_stmt(provenance_status="missing"), now=FIXED)

    assert result["written"] is False
    assert result["reason"] == "provenance_required"
    assert retired_index_bridge.add_calls == []


def test_write_blocks_when_eval_has_not_passed(tmp_path):
    retired_index_bridge = _FakeRetiredIndexBridge()
    store = _store(tmp_path)
    writer = _writer(retired_index_bridge, store)
    result = writer.write(_stmt(eval_status="fail"), now=FIXED)

    assert result["written"] is False
    assert result["reason"] == "eval_required"
    assert retired_index_bridge.add_calls == []

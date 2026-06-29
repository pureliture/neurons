from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.native_memory_mirror import (
    NativeMemoryMirrorStore,
    session_tag_for,
)


FIXED = datetime(2026, 6, 8, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 9, tzinfo=timezone.utc)


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


# --- C1.1: DDL + schema_migrations ---


def test_native_memory_mirror_table_exists(tmp_path):
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='native_memory_mirror'"
        ).fetchall()
    assert len(rows) == 1


def test_schema_migration_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        rows = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='agent_knowledge_native_memory_mirror.v1'"
        ).fetchall()
    assert len(rows) == 1


def test_brain_status_index_exists(tmp_path):
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_native_memory_mirror_brain_status'"
        ).fetchall()
    assert len(rows) == 1


# --- C1.2: session_tag_for + store skeleton + write-mode guard ---


def test_session_tag_for():
    assert session_tag_for("1001") == "mem:1001"


def test_store_rejects_read_only_ledger(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    # 선행: write-mode Ledger 로 파일을 디스크에 만든다(없으면 open_read_only 가
    # "ledger path does not exist" 로 먼저 터져 store 가드에 도달하지 못함).
    Ledger(path)
    with pytest.raises(ValueError, match="requires a write-mode Ledger"):
        NativeMemoryMirrorStore(Ledger.open_read_only(path))


def test_store_connect_preserves_ledger_busy_timeout(tmp_path):
    store = NativeMemoryMirrorStore(_ledger(tmp_path))
    with store._connect() as connection:
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout == 60000


def _store(tmp_path) -> NativeMemoryMirrorStore:
    return NativeMemoryMirrorStore(_ledger(tmp_path))


# --- C1.3: upsert_statement (insert + re-upsert) ---

_FULL_COLUMNS = {
    "statement_id",
    "brain_id",
    "session_tag",
    "status",
    "superseded_by",
    "original_content_hash",
    "index_memory_id",
    "index_disabled_at",
    "created_at",
    "superseded_at",
}


def test_upsert_inserts_active_row(tmp_path):
    store = _store(tmp_path)
    row = store.upsert_statement(
        statement_id="1001",
        brain_id="/profile/x",
        original_content_hash="h1",
        now=FIXED,
    )
    assert row["status"] == "active"
    assert row["session_tag"] == "mem:1001"
    assert row["created_at"] == FIXED.isoformat()
    assert row["superseded_by"] == ""
    assert row["index_disabled_at"] == ""


def test_upsert_return_is_full_row_not_written_flag(tmp_path):
    store = _store(tmp_path)
    row = store.upsert_statement(
        statement_id="1001",
        brain_id="/profile/x",
        original_content_hash="h1",
        now=FIXED,
    )
    assert _FULL_COLUMNS.issubset(set(row.keys()))
    assert "written" not in row


def test_reupsert_preserves_created_at_and_keeps_active(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001",
        brain_id="/profile/x",
        original_content_hash="h1",
        now=FIXED,
    )
    row = store.upsert_statement(
        statement_id="1001",
        brain_id="/profile/y",
        original_content_hash="h2",
        now=LATER,
    )
    assert row["status"] == "active"
    assert row["created_at"] == FIXED.isoformat()
    assert row["brain_id"] == "/profile/y"
    assert row["original_content_hash"] == "h2"


# --- C1.4: mark_superseded + 재활성 ---


def test_mark_superseded_sets_fields(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    assert store.mark_superseded("1001", superseded_by="1002", now=LATER) is True
    rows = store.get_by_session_tags(["mem:1001"])
    row = rows["mem:1001"]
    assert row["status"] == "superseded"
    assert row["superseded_by"] == "1002"
    assert row["superseded_at"] == LATER.isoformat()


def test_mark_superseded_missing_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.mark_superseded("9999", superseded_by="1") is False


def test_mark_superseded_already_superseded_returns_false_and_preserves_first(tmp_path):
    # AND status='active' 가드: 이미 superseded 인 row 재호출은 False + 첫 superseder 보존.
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    assert store.mark_superseded("1001", superseded_by="1002", now=FIXED) is True
    assert store.mark_superseded("1001", superseded_by="9999", now=LATER) is False
    row = store.get_by_session_tags(["mem:1001"])["mem:1001"]
    assert row["superseded_by"] == "1002"
    assert row["superseded_at"] == FIXED.isoformat()


def test_reupsert_reactivates_superseded(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    store.mark_superseded("1001", superseded_by="1002", now=LATER)
    row = store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=LATER
    )
    assert row["status"] == "active"
    assert row["superseded_by"] == ""
    assert row["superseded_at"] == ""
    assert row["created_at"] == FIXED.isoformat()


# --- C1.5: 조회 메서드 ---


def test_get_by_session_tags_batch(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    store.upsert_statement(
        statement_id="1002", brain_id="/a", original_content_hash="h2", now=FIXED
    )
    rows = store.get_by_session_tags(["mem:1001", "mem:1002", "mem:9999"])
    assert set(rows.keys()) == {"mem:1001", "mem:1002"}
    assert rows["mem:1001"]["statement_id"] == "1001"


def test_get_by_session_tags_empty_returns_empty_dict(tmp_path):
    store = _store(tmp_path)
    assert store.get_by_session_tags([]) == {}


def test_get_active_session_tags_filters_brain_and_status(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    store.upsert_statement(
        statement_id="1002", brain_id="/a", original_content_hash="h2", now=FIXED
    )
    store.mark_superseded("1002", superseded_by="1003", now=LATER)
    store.upsert_statement(
        statement_id="2001", brain_id="/b", original_content_hash="h3", now=FIXED
    )
    assert store.get_active_session_tags("/a") == {"mem:1001"}


def test_get_active_session_tags_empty_brain_id_exact_match(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="", original_content_hash="h1", now=FIXED
    )
    store.upsert_statement(
        statement_id="2001", brain_id="/x", original_content_hash="h2", now=FIXED
    )
    assert store.get_active_session_tags("") == {"mem:1001"}


def test_list_pending_reconcile_only_superseded_not_disabled(tmp_path):
    store = _store(tmp_path)
    # active
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED
    )
    # superseded, not disabled
    store.upsert_statement(
        statement_id="1002", brain_id="/a", original_content_hash="h2", now=FIXED
    )
    store.mark_superseded("1002", superseded_by="1003", now=LATER)
    # superseded + index_disabled_at filled (직접 SQL — 후속 reconcile 소관)
    store.upsert_statement(
        statement_id="1003", brain_id="/a", original_content_hash="h3", now=FIXED
    )
    store.mark_superseded("1003", superseded_by="1004", now=LATER)
    with store._connect() as connection:
        connection.execute(
            "UPDATE native_memory_mirror SET index_disabled_at=? WHERE statement_id=?",
            (FIXED.isoformat(), "1003"),
        )
    pending = store.list_pending_reconcile()
    assert [row["statement_id"] for row in pending] == ["1002"]


# --- U1(completion): search_text / card_type 컬럼 + ON CONFLICT 갱신 ---


def test_search_text_and_card_type_columns_exist(tmp_path):
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        cols = {r[1] for r in connection.execute("PRAGMA table_info(native_memory_mirror)").fetchall()}
    assert {"search_text", "card_type"}.issubset(cols)


def test_upsert_stores_search_text_and_card_type(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1",
        search_text="탭 들여쓰기 선호", card_type="user_preference", now=FIXED,
    )
    row = store.get_by_session_tags(["mem:1001"])["mem:1001"]
    assert row["search_text"] == "탭 들여쓰기 선호"
    assert row["card_type"] == "user_preference"


def test_upsert_defaults_search_text_and_card_type_empty(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED)
    row = store.get_by_session_tags(["mem:1001"])["mem:1001"]
    assert row["search_text"] == ""
    assert row["card_type"] == ""


def test_reupsert_updates_search_text_and_card_type(tmp_path):
    # ON CONFLICT 갱신: re-upsert 시 최신 텍스트/타입 반영(stale-query 방지), session_tag/created_at 불변.
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1",
        search_text="old", card_type="semantic_fact", now=FIXED,
    )
    row = store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h2",
        search_text="new", card_type="user_preference", now=LATER,
    )
    assert row["search_text"] == "new"
    assert row["card_type"] == "user_preference"
    assert row["session_tag"] == "mem:1001"
    assert row["created_at"] == FIXED.isoformat()


# --- U1(completion): mark_index_disabled ---


def test_mark_index_disabled_records_and_drops_from_pending(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED)
    store.mark_superseded("1001", superseded_by="1002", now=LATER)
    assert store.mark_index_disabled("1001", index_disabled_at=LATER.isoformat(), index_memory_id="m1") is True
    row = store.get_by_session_tags(["mem:1001"])["mem:1001"]
    assert row["index_disabled_at"] == LATER.isoformat()
    assert row["index_memory_id"] == "m1"
    assert store.list_pending_reconcile() == []


def test_mark_index_disabled_active_row_returns_false(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED)
    assert store.mark_index_disabled("1001", index_disabled_at=LATER.isoformat()) is False


def test_mark_index_disabled_idempotent(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="1001", brain_id="/a", original_content_hash="h1", now=FIXED)
    store.mark_superseded("1001", superseded_by="1002", now=LATER)
    assert store.mark_index_disabled("1001", index_disabled_at=LATER.isoformat()) is True
    assert store.mark_index_disabled("1001", index_disabled_at=FIXED.isoformat()) is False


def test_mark_index_disabled_does_not_overwrite_existing_memory_id(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(
        statement_id="1001", brain_id="/a", original_content_hash="h1",
        index_memory_id="existing", now=FIXED,
    )
    store.mark_superseded("1001", superseded_by="1002", now=LATER)
    store.mark_index_disabled("1001", index_disabled_at=LATER.isoformat(), index_memory_id="new")
    row = store.get_by_session_tags(["mem:1001"])["mem:1001"]
    assert row["index_memory_id"] == "existing"


# --- Phase 1: list_active_statements (supersede-sync 입력) ---


def test_list_active_statements_returns_active_only(tmp_path):
    store = _store(tmp_path)
    store.upsert_statement(statement_id="s1", brain_id="/project/p", original_content_hash="h1", now=FIXED)
    store.upsert_statement(statement_id="s2", brain_id="/project/p", original_content_hash="h2", now=FIXED)
    store.mark_superseded("s2", superseded_by="ledger", now=LATER)

    rows = store.list_active_statements()

    assert [r["statement_id"] for r in rows] == ["s1"]
    assert rows[0]["brain_id"] == "/project/p"
    assert rows[0]["original_content_hash"] == "h1"


def test_list_active_statements_empty(tmp_path):
    assert _store(tmp_path).list_active_statements() == []

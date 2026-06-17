from agent_knowledge.db_adapter import ILedgerCoreDbAdapter, SqliteLedgerDbAdapter
from agent_knowledge.ledger import Ledger


def test_sqlite_adapter_is_seam():
    assert issubclass(SqliteLedgerDbAdapter, ILedgerCoreDbAdapter)


def test_ledger_routes_all_connects_through_injected_adapter(tmp_path):
    # B 엔진 seam: 주입된 어댑터가 Ledger의 모든 connect를 경유한다(= C에서 엔진 교체 지점).
    path = tmp_path / "l.sqlite"
    delegate = SqliteLedgerDbAdapter(path, read_only=False)
    counter = {"n": 0}

    class _CountingAdapter(ILedgerCoreDbAdapter):
        def connect(self, *, configure_journal: bool = False):
            counter["n"] += 1
            return delegate.connect(configure_journal=configure_journal)

    ledger = Ledger(path, db_adapter=_CountingAdapter())
    # _initialize가 어댑터를 경유해 connect
    assert counter["n"] >= 1
    before = counter["n"]
    # 일반 쿼리도 동일 어댑터 경유
    assert ledger.get_by_knowledge_id("nonexistent") is None
    assert counter["n"] > before


def test_default_adapter_is_behavior_preserving(tmp_path):
    # db_adapter 미지정 시 기본 SQLite 어댑터로 _initialize + read가 동작한다.
    # (전체 테스트 스위트가 모두 이 기본 경로를 지나므로 동작 보존의 실제 증명은 그쪽이다.)
    ledger = Ledger(tmp_path / "l.sqlite")
    assert ledger.list_memory_gc_audit() == []
    assert ledger.get_by_knowledge_id("nonexistent") is None

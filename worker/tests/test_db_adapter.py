from agent_knowledge import postgres_db_adapter
from agent_knowledge.db_adapter import ILedgerCoreDbAdapter, SqliteLedgerDbAdapter
from agent_knowledge.ledger import Ledger
from agent_knowledge.postgres_db_adapter import PostgresLedgerDbAdapter


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


def test_postgres_adapter_binds_remaining_deadline_to_connect_and_statement(monkeypatch):
    calls = []

    class _Connection:
        read_only = False

        def close(self):
            return None

    def _connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return _Connection()

    monkeypatch.setattr(postgres_db_adapter.time, "monotonic", lambda: 8.75)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", _connect)

    connection = PostgresLedgerDbAdapter(
        "postgresql://test.invalid/ledger",
        read_only=True,
        deadline_monotonic=10.0,
    ).connect()

    connection.close()
    assert calls == [
        (
            "postgresql://test.invalid/ledger",
            {
                "connect_timeout": 1,
                "options": "-c statement_timeout=1250",
                "row_factory": postgres_db_adapter._pg_row_factory,
            },
        )
    ]


def test_postgres_adapter_preserves_dsn_options_when_adding_deadline(monkeypatch):
    calls = []

    class _Connection:
        read_only = False

        def close(self):
            return None

    def _connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return _Connection()

    monkeypatch.setattr(postgres_db_adapter.time, "monotonic", lambda: 8.75)
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", _connect)

    connection = PostgresLedgerDbAdapter(
        "host=ledger.invalid port=5432 dbname=neurons user=rebuild "
        "options='-c search_path=ledger_a'",
        read_only=True,
        deadline_monotonic=10.0,
    ).connect()

    connection.close()
    assert calls[0][1]["options"] == (
        "-c search_path=ledger_a -c statement_timeout=1250"
    )


def test_postgres_adapter_refreshes_statement_timeout_before_each_execute(monkeypatch):
    calls = []
    now = [8.5]

    class _Cursor:
        def execute(self, sql, params=None):
            calls.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Connection:
        read_only = False

        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    monkeypatch.setattr(postgres_db_adapter.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(postgres_db_adapter.psycopg, "connect", lambda *_args, **_kwargs: _Connection())

    connection = PostgresLedgerDbAdapter(
        "postgresql://test.invalid/ledger",
        read_only=True,
        deadline_monotonic=10.0,
    ).connect()
    now[0] = 9.0
    connection.execute("SELECT 1")
    now[0] = 9.5
    connection.execute("SELECT 2")

    assert calls == [
        ("SELECT set_config('statement_timeout', %s, false)", ("1000ms",)),
        ("SELECT 1", None),
        ("SELECT set_config('statement_timeout', %s, false)", ("500ms",)),
        ("SELECT 2", None),
    ]


def test_postgres_adapter_does_not_mark_network_attempt_before_short_budget_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(postgres_db_adapter.time, "monotonic", lambda: 9.5)
    monkeypatch.setattr(
        postgres_db_adapter.psycopg,
        "connect",
        lambda *_args, **_kwargs: calls.append(True),
    )
    adapter = PostgresLedgerDbAdapter(
        "postgresql://test.invalid/ledger",
        deadline_monotonic=10.0,
    )

    try:
        adapter.connect()
    except TimeoutError as exc:
        assert "insufficient connect budget" in str(exc)
    else:
        raise AssertionError("short PostgreSQL connect budget must fail closed")

    assert calls == []
    assert adapter.network_attempted is False

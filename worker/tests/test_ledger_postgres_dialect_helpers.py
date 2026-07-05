from agent_knowledge.ledger import _column_names, _copy_index_targets_from_legacy_table, _table_exists


class _FakeRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakePostgresConnection:
    dialect = "postgres"

    def __init__(self, *, table_exists=True, columns=None):
        self._table_exists = table_exists
        self._columns = (
            [_FakeRow(column_name="knowledge_id"), _FakeRow(column_name="project")]
            if columns is None
            else columns
        )
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "information_schema.tables" in sql:
            return _FakeResult([_FakeRow(exists=1)] if self._table_exists else [])
        if "information_schema.columns" in sql:
            return _FakeResult(self._columns)
        if "INSERT INTO index_targets" in sql:
            return _FakeResult([])
        raise AssertionError(f"unexpected SQL for postgres helper: {sql}")


def test_table_exists_uses_information_schema_for_postgres():
    conn = _FakePostgresConnection()

    assert _table_exists(conn, "knowledge_items") is True

    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "information_schema.tables" in sql
    assert "sqlite_master" not in sql
    assert "knowledge_items" not in conn.statements[0][0]
    assert conn.statements[0][1] == ("knowledge_items",)


def test_column_names_uses_information_schema_for_postgres():
    conn = _FakePostgresConnection()

    assert _column_names(conn, "knowledge_items") == {"knowledge_id", "project"}

    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "information_schema.columns" in sql
    assert "PRAGMA table_info" not in sql
    assert all("knowledge_items" not in statement for statement, _ in conn.statements)
    assert [params for _, params in conn.statements] == [("knowledge_items",), ("knowledge_items",)]


def test_column_names_short_circuits_when_postgres_table_is_missing():
    conn = _FakePostgresConnection(table_exists=False)

    assert _column_names(conn, "missing_table") == set()

    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "information_schema.tables" in sql
    assert "information_schema.columns" not in sql
    assert conn.statements == [
        (
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ?",
            ("missing_table",),
        )
    ]


def test_column_names_does_not_stringify_null_postgres_column_name():
    conn = _FakePostgresConnection(columns=[_FakeRow(column_name=None), _FakeRow(column_name="project")])

    assert _column_names(conn, "knowledge_items") == {"", "project"}


def test_legacy_index_target_copy_uses_postgres_conflict_clause():
    required_columns = [
        _FakeRow(column_name="logical_name"),
        _FakeRow(column_name="dataset_id"),
        _FakeRow(column_name="embedding_model"),
        _FakeRow(column_name="chunk_method"),
        _FakeRow(column_name="metadata_policy_version"),
        _FakeRow(column_name="contract_version"),
        _FakeRow(column_name="created_at"),
        _FakeRow(column_name="enabled"),
        _FakeRow(column_name="disabled_at"),
    ]
    conn = _FakePostgresConnection(columns=required_columns)

    _copy_index_targets_from_legacy_table(conn, "ragflow_datasets")

    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "ON CONFLICT DO NOTHING" in sql
    assert "INSERT OR IGNORE" not in sql

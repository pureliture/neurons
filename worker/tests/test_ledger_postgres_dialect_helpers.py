from agent_knowledge.ledger import _column_names, _table_exists


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

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "information_schema.tables" in sql:
            return _FakeResult([_FakeRow(exists=1)])
        if "information_schema.columns" in sql:
            return _FakeResult([_FakeRow(column_name="knowledge_id"), _FakeRow(column_name="project")])
        raise AssertionError(f"unexpected SQL for postgres helper: {sql}")


def test_table_exists_uses_information_schema_for_postgres():
    conn = _FakePostgresConnection()

    assert _table_exists(conn, "knowledge_items") is True

    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "information_schema.tables" in sql
    assert "sqlite_master" not in sql


def test_column_names_uses_information_schema_for_postgres():
    conn = _FakePostgresConnection()

    assert _column_names(conn, "knowledge_items") == {"knowledge_id", "project"}

    sql = "\n".join(statement for statement, _ in conn.statements)
    assert "information_schema.columns" in sql
    assert "PRAGMA table_info" not in sql

from agent_knowledge.pg_paramstyle import qmark_to_pyformat


def test_basic_placeholders():
    assert qmark_to_pyformat("INSERT INTO t (a, b) VALUES (?, ?)") == "INSERT INTO t (a, b) VALUES (%s, %s)"


def test_question_mark_inside_string_literal_preserved():
    # 문자열 리터럴 내부의 ? 는 placeholder가 아니므로 변환 금지.
    sql = "SELECT * FROM t WHERE note = 'is this?' AND x = ?"
    assert qmark_to_pyformat(sql) == "SELECT * FROM t WHERE note = 'is this?' AND x = %s"


def test_escaped_single_quote_inside_literal():
    sql = "SELECT * FROM t WHERE s = 'it''s a ? mark' AND y = ?"
    assert qmark_to_pyformat(sql) == "SELECT * FROM t WHERE s = 'it''s a ? mark' AND y = %s"


def test_literal_percent_is_escaped_for_psycopg():
    # psycopg는 파라미터 쿼리의 모든 리터럴 %를 %%로 요구(리터럴 내부 포함).
    assert qmark_to_pyformat("WHERE id = ? AND r = 50%") == "WHERE id = %s AND r = 50%%"
    assert qmark_to_pyformat("WHERE name LIKE 'a%' AND id = ?") == "WHERE name LIKE 'a%%' AND id = %s"


def test_double_quoted_identifier_with_question_preserved():
    sql = 'SELECT "weird?col" FROM t WHERE id = ?'
    assert qmark_to_pyformat(sql) == 'SELECT "weird?col" FROM t WHERE id = %s'


def test_on_conflict_portable_sql_roundtrip():
    sql = "INSERT INTO t (a) VALUES (?) ON CONFLICT DO NOTHING"
    assert qmark_to_pyformat(sql) == "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING"

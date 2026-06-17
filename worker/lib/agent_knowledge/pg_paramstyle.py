"""qmark(`?`) → pyformat(`%s`) 파라미터 플레이스홀더 변환 (Phase C).

Ledger의 SQL은 B2/B3에서 *의미*가 표준화돼(ON CONFLICT / CURRENT_TIMESTAMP) 양 엔진 공통이다.
남은 차이는 placeholder 문법뿐: sqlite3는 ``?``, psycopg는 ``%s``. 이 변환은 *기계적*이며
(SQL 의미를 건드리지 않음) 단위 테스트로 falsifiable하게 검증된다 — audit가 경고한 "런타임
dialect(의미) 번역"이 아니다.

규칙: 문자열 리터럴(작은따옴표) 내부의 ``?``는 건드리지 않는다. psycopg가 리터럴 ``%``를
이스케이프(``%%``)로 요구하므로, 리터럴/식별자 밖의 ``%`` 도 ``%%``로 이스케이프한다.
"""

from __future__ import annotations


def qmark_to_pyformat(sql: str) -> str:
    out: list[str] = []
    i = 0
    n = len(sql)
    in_squote = False  # '...' 문자열 리터럴
    in_dquote = False  # "..." 식별자
    while i < n:
        ch = sql[i]
        # psycopg는 파라미터 쿼리의 모든 리터럴 %를 %%로 요구(리터럴 내부 포함).
        if ch == "%":
            out.append("%%")
            i += 1
            continue
        if in_squote:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":  # '' = escape된 작은따옴표
                    out.append("'")
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dquote:
            out.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            out.append(ch)
        elif ch == '"':
            in_dquote = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)

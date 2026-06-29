# ADR-0003: 서버 ledger 엔진 seam + SQLite→PostgreSQL cutover

Status: Accepted (구현 완료 / live cutover는 operator-gated 대기)
Date: 2026-06-16
Deciders: local operator
Related: ADR-0001 (producer-side advisor ledger 경계), `worker/lib/agent_knowledge/`

---

## Context

`neurons`가 server ledger authority를 소유하게 되면서(commit `5eb100e` "add server ledger
authority"), server-side ledger는 `worker/lib/agent_knowledge/ledger.py`에 산다. 초기 구현은
`sqlite3`에 직접 묶여 있었다.

서버 권위 저장소가 단일 파일 SQLite에 고정되면 다음 한계가 있다.

1. **단일 writer / 파일 기반.** 동시성·내구성·원격 접근이 서버 authority 규모에서 천장에 부딪힌다.
2. **엔진 교체 불가.** 연결 생성과 SQL dialect가 곳곳에 흩어져 PostgreSQL 같은 서버형 엔진으로
   옮기려면 광범위한 수정과 big-bang cutover가 필요하다.
3. **되돌리기 어려움.** in-place 재작성은 롤백 경로가 없다.

> 참고: 여기서 다루는 것은 **server-side ledger**다. `workspace-index-advisor`의 producer-side
> advisor `ledger.py`(외부 문서 상태표, `neurons`가 소유하지 않음 — ADR-0001 참조)와는 별개다.
> 이 ADR은 후자의 경계를 바꾸지 않는다.

## Decision

server ledger를 **엔진 중립 seam 뒤로 격리하고, SQL을 이식 가능한 형태로 표준화한 뒤,
parity가 증명된 PostgreSQL 어댑터를 추가하고, read-only 이관 도구와 단일 env 플립으로
cutover를 게이트**한다. 기본 엔진은 operator가 플립하기 전까지 SQLite로 둔다.

구현된 단계(증거: 아래 References의 커밋):

- **B1 — 연결 seam.** `ILedgerCoreDbAdapter`(`db_adapter.py`)가 connection 생성을 한 점으로 모은다.
  `SqliteLedgerDbAdapter`는 기존 `Ledger._connect`를 그대로 옮겨 byte-identical(RO URI, busy_timeout/
  WAL/synchronous PRAGMA, sidecar 권한 하드닝 포함). `is_file_backed` 플래그로 file-specific 단계를 분기한다.
- **B2/B3 — SQL 이식성.** SQLite 전용 upsert(`INSERT OR IGNORE`/방언 `ON CONFLICT`)와 `datetime('now')`를
  표준 SQL(`ON CONFLICT`, `CURRENT_TIMESTAMP`)로 통일했다. SQL *의미*는 양 엔진 공통이 됐다.
- **C1 — paramstyle 변환.** `psycopg` 의존성 추가 + `pg_paramstyle.qmark_to_pyformat`로 `?`→`%s` 변환.
- **C2–C4 — PostgreSQL 어댑터.** `PostgresLedgerDbAdapter`(`postgres_db_adapter.py`)가 psycopg connection을
  sqlite3 DBAPI 인터페이스(`execute(sql, ?params)`, `executescript`, `dict(row)`, `with conn:`)로 얇게
  wrap한다. 어댑터가 흡수하는 차이는 (1) placeholder, (2) row dict 접근, (3) `sqlite_master`→
  `information_schema` 스키마 헬퍼 분기, (4) file-backed 아님뿐. SQLite↔PG parity를 테스트로 증명.
- **C5 — 이관 도구.** `ledger_pg_migrate.py`가 SQLite 전 행을 새 PostgreSQL ledger로 복사하고
  per-table 행수를 검증한다. **원본 SQLite는 읽기 전용**(롤백 = 원본 그대로).
- **C6 — env 플립.** `Ledger.__init__(db_adapter=None)`이 `NEURON_LEDGER_PG_DSN`을 확인해 값이 있으면
  `PostgresLedgerDbAdapter`, 없으면 `SqliteLedgerDbAdapter`를 쓴다. **env 하나로 엔진 전환.**

## Options Considered

### Option A: SQLite 단독 유지

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Concurrency / durability | Weak — 단일 writer 파일 DB |
| Reversibility | N/A |

Pros: 가장 단순. Cons: 서버 authority 규모에서 동시성·원격 접근 천장. 미래 확장 불가.

### Option B: psycopg/SQLAlchemy로 직접 재작성 (SQLite 제거)

| Dimension | Assessment |
|---|---|
| Complexity | High |
| Migration | Big-bang, 롤백 경로 없음 |
| Test/Local | 무거워짐(로컬 PG 필수) |

Pros: 최종 형태 깔끔. Cons: 일괄 cutover 리스크, 로컬/테스트 단순성 상실, 되돌리기 어려움.

### Option C: 엔진 seam + 이식 SQL + parity PG 어댑터 + read-only 이관 + env 플립 (선택안)

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Migration | 점진적·가역적, 두 엔진 공존 |
| Test/Local | SQLite 유지(빠름) |

Pros: 단계적, 가역적, env 하나로 플립, 테스트는 SQLite로 빠르게 유지, 운영은 PostgreSQL로 확장.
Cons: 이중 dialect 유지 표면(paramstyle, 스키마 헬퍼 분기) → parity 테스트로 방어.

## Consequences

### Positive
- **가역적 cutover.** 이관 도구가 원본 SQLite를 변경하지 않으므로 문제 시 SQLite로 즉시 롤백.
- **엔진 교체 = env 1개.** `NEURON_LEDGER_PG_DSN` 설정/해제로 코드 변경 없이 전환.
- 테스트·로컬은 SQLite로 가볍게 유지, 운영은 PostgreSQL로 동시성/내구성 확보.

### Negative
- 이중 dialect 유지 비용(placeholder, 스키마 헬퍼 dialect 분기) — parity 테스트로 강제.

### Neutral
- producer-side advisor ledger(외부, 비소유) 경계는 변하지 않는다.

## Done Criteria

1. `ILedgerCoreDbAdapter` seam 존재, SQLite 어댑터 동작 byte-identical. ✅
2. SQL이 이식 가능(`ON CONFLICT`, `CURRENT_TIMESTAMP`). ✅
3. `PostgresLedgerDbAdapter`가 SQLite↔PG parity 테스트 통과. ✅
4. 이관 도구가 행 복사 + 행수 검증, 원본 read-only. ✅
5. `NEURON_LEDGER_PG_DSN`이 코드 변경 없이 엔진을 플립. ✅
6. **live cutover(Ubuntu, operator go): 대기 중** — 정지/RO → 이관 → 행수/검증 통과 → 엔진 플립.

## References

- 커밋: B1 `f61e678`, B2 `95a4ffb`, B3 `df2decd`, C1 `79f41e0`, C2–C4 `98d5e5c`, C5 `908b8b6`, C6 `ff8bf0a`, server ledger `5eb100e`
- 코드: `worker/lib/agent_knowledge/{ledger.py, db_adapter.py, postgres_db_adapter.py, pg_paramstyle.py, ledger_pg_migrate.py}`
- 테스트: `worker/tests/{test_ledger_core.py, test_ledger_pg_migrate.py, test_ledger_seam_invariants.py}`, `worker/eval/ledger_seam_invariants.py`
- ADR-0001: producer-side advisor ledger 경계(외부 상태표, 비소유)

# Ledger SQLite → PostgreSQL Cutover — Production-Data Dry-Run Evidence

작성: 2026-06-16 (autopilot, 무인). 브랜치 `claude/ledger-autopilot` (tip 기준 cutover-ready).

이 문서는 live 운영 cutover의 **guardrail 충족용 evidence/postcheck 번들**이다. 라이브
시스템은 **read-only로만 접촉**했고(일관 snapshot 1회), origin push·엔진 flip·패키지
설치·런타임 정지는 **하지 않았다**. 모든 식별자/private path는 redaction한다.

## 1. 절차 (실행한 것)

1. 라이브 brain-server의 ledger SQLite를 **online backup API**(`sqlite3.Connection.backup`,
   source는 `mode=ro`)로 일관 snapshot 생성 → Mac temp로 복사 → SHA256 일치 확인 →
   서버 temp 즉시 삭제. 라이브 DB 파일은 변경 없음.
2. Mac에 **ephemeral PostgreSQL 16.14**(`initdb --locale=C`, unix-socket, 비표준 포트)
   기동. 작업 후 teardown.
3. snapshot에 이관 도구 실행: `ledger-pg-migrate --sqlite <snapshot> --pg-dsn <ephemeral>`
   (원본 read-only). per-table 행수 검증.
4. 3단계 parity 검증(아래 §3).
5. ephemeral PG teardown + snapshot 삭제.

## 2. 라이브 snapshot 사실 (집계만)

- 스키마: 34 tables (Ledger 스키마 일치), nonempty 5.
- 총 행수: **4,471**.
  | table | rows |
  |---|---|
  | session_memory_coverage_edges | 4,278 |
  | dirty_session_memory | 98 |
  | knowledge_items | 57 |
  | session_memory_active_snapshots | 21 |
  | schema_migrations | 17 |
- snapshot 무결성: SHA256 prefix `f65b87ad896f2e4c` (서버 생성 == Mac 수신 동일).

## 3. Parity 결과 — 전부 PASS

**(a) 이관 도구 per-table 행수 검증**
- `tables_migrated: 34`, `count_mismatches: []`, `ok: true`. 34개 테이블 전부 행수 일치.

**(b) Ledger read-API parity (운영 read 경로 그대로, 실 데이터)**
- `get_by_knowledge_id`: **57/57 match**, mismatch 0.
- `get_session_memory_active_snapshot`: **21/21 match**, mismatch 0.
- `list_memory_gc_audit`: sqlite==pg (0/0).

**(c) Column-level byte parity (type-normalized, 전 nonempty 테이블)**
- session_memory_coverage_edges: 4278/4278 **full_match**, diff_cols 0.
- dirty_session_memory: 98/98 **full_match**, diff_cols 0.
- knowledge_items: 57/57 **full_match**, diff_cols 0.
- session_memory_active_snapshots: 21/21 **full_match**, diff_cols 0.

→ 실 운영 데이터의 shape/volume에서 SQLite↔PostgreSQL 이관이 **무손실·동일 관측**임을 증명.
B2/B3 표준 SQL(ON CONFLICT / CURRENT_TIMESTAMP) + psycopg paramstyle 변환이 실데이터에서
dialect 누수 없음.

## 4. Cutover 실행 계약 (guardrail 항목 충족)

| guardrail 요구 | 충족 |
|---|---|
| current evidence | §3 — 오늘자 라이브 snapshot 기준 parity 전부 PASS |
| explicit user intent | 사용자 명시 go (cutover까지 진행 지시) |
| exact argv | §4.1 |
| timeout | 이관 도구는 단발 batch(수천 행, 초 단위). watchdog: 이관 60s 초과 시 abort |
| redaction | 본 문서 — 식별자/private path 전부 redaction |
| postcheck | §4.2 |
| rollback/abort | §4.3 |

### 4.1 exact argv (라이브, 미실행)
```
# 0) 라이브 worker 정지(또는 ledger read-only) — healthy restart guardrail 대상, 별도 승인
# 1) 대상 PostgreSQL 준비 (apt 또는 docker) — package mutation guardrail 대상, 별도 승인
# 2) 이관 (원본 SQLite read-only, 미변경)
ledger-pg-migrate --sqlite <live-ledger-path> --pg-dsn <pg-dsn>
# 3) 엔진 flip: worker env에 NEURON_LEDGER_PG_DSN=<pg-dsn> 설정 후 worker 재기동
```

### 4.2 postcheck (라이브 flip 직후 실행)
- 이관 출력 `ok==true && count_mismatches==[]`.
- flip 후 smoke read: `get_by_knowledge_id` / active snapshot N건이 flip 전 SQLite와 동일.
- worker 프로세스 정상 기동 + `--show-boundary` 불변.

### 4.3 rollback / abort
- **abort 조건**: 이관 `ok==false`, 임의 count/parity mismatch, smoke read 불일치,
  worker 기동 실패 중 하나라도.
- **rollback**: `NEURON_LEDGER_PG_DSN` 환경변수 unset → SQLite 엔진 복귀. 원본 SQLite는
  이관 중에도 미변경이므로 즉시 복원. 단, flip 이후 PG에 들어온 신규 write는 rollback 시
  유실되므로 flip은 라이브 트래픽 정지 창에서만.

## 5. 남은 미충족 항목 (라이브 실행 본체 — 무인 범위 밖)

- §4.1 step 0(healthy runtime 정지)·step 1(package mutation): **각각 별도 guardrail**.
  evidence·argv는 위에 준비됨, 실행은 사용자 입회 또는 명시 승인 필요.
- origin push: 미수행(코드는 로컬 브랜치). 라이브 배포는 push 선행 필요.

이 dry-run으로 **이관 정확성 evidence/postcheck/argv/rollback 번들은 완성**됐다. 남은 것은
라이브 mutation 실행 본체뿐이며 그 게이트는 guardrail상 의도적 실행(사용자 승인)을 요구한다.

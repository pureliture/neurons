# Ledger Refactor Autopilot — 설계 (spec)

- 작성일: 2026-06-15
- 브랜치/worktree: `claude/ledger-autopilot` (`.worktrees/ledger-autopilot`)
- 통합 브랜치: `claude/ledger-autopilot` (autopilot이 phase별로 무인 auto-merge하는 대상; `main` 불가촉)
- 입력 근거: `docs/architecture/ledger-review-deepdive-20260614.html`(개정판 = `/tmp` 원본 + audit override), `docs/architecture/ledger-review-initial-20260614.html`, 그리고 현재 main 코드 검증 결과.

## 1. 목표

`worker/lib/agent_knowledge/ledger.py`를 중심으로 한 결합을, **에이전틱 Workflow 피드백 루프 autopilot**으로 점진 리팩토링한다. autopilot은 과거 llm-brain RAG curation autopilot의 *계약*(blind-propose → gate → grade → iterate → finish-gate, standing 안전 기제, forbidden ops hard-block)을 재사용하되, 메모리 도메인 코드가 아니라 코드 리팩토링을 구동한다. 유한 작업이므로 finish-gate 충족 시 정지한다(영구 cron 데몬 아님).

최종 운영 목표(사용자 명시): 운영 서비스에 부적합한 raw SQLite를 **PostgreSQL로 이관**(phase C). 단 이관을 안전하게 하려면 DB 접근을 어댑터 seam 뒤로 먼저 넣어야 하므로(phase B), in-process 리팩토링이 DB 이관의 *전제조건*이다.

## 2. 현재 아키텍처 (검증된 토폴로지, 3층)

main 코드 검증 결과(2026-06-15):

- **프로세스 층 — 이미 분리됨**: `worker/pyproject.toml`의 13개 entry-point가 각자 프로세스로 실행. `neuron-knowledge`(CLI + `mcp_server.run_stdio_server`), `rag-ingress-queue`(`server_runtime` = `ThreadingHTTPServer`, 상주), `rag-ingress-worker`(`shadow_worker`), 5개 GC CLI, memory build/regen/sync CLI 등. 즉 모놀리스는 프로세스가 아니다.
- **코드 결합 층 — 모놀리식**: 20개 모듈이 `from ..ledger import Ledger`로 직접 결합. `Ledger`는 raw `sqlite3`(4178줄, `_initialize`가 34개 `CREATE TABLE`, 그중 `schema_migrations` 1개는 인프라). 인터페이스 seam 없음. 유일 예외: `brain_query.py`가 `BrainReadModel` Protocol로 부분 격리(ledger phase-out 제약 2026-06-11), `brain_read_model.py`의 `LegacyLedgerBrainReadModel`이 유일 어댑터.
- **데이터 층 — 공유 raw-sqlite3**: 3개 SQLite DB 클래스 — `Ledger`(ledger.py), `RAGIngressStateDB`(rag_ingress/state_db.py), `IngestStateStore`(rag_ingress/shadow_worker.py) — + 외부 RAGFlow HTTP API. WAL로 다중 프로세스 협조.

GC 결합(phase A 타깃)의 구체상: GC 감사(`memory_gc_audit`), 비가역 hard-delete(`_mark_gc_deleted`, `mark_disabled`/`mark_enabled`), RAGFlow `delete_documents`가 단일 `Ledger` 클래스 + 5개 GC 스크립트에 inline 결합. `session_memory_gc.py`/`transcript_volume_gc.py`는 같은 for-loop 안에서 RAGFlow delete ↔ ledger audit write를 호출(seam 0).

## 3. 범위 결정 (fork 정리)

- **분리 깊이**: in-process 모듈 seam (A/B/D). 물리 네트워크 분리(REST/gRPC Ledger 서비스)는 **범위 밖** — 프로세스는 이미 분리됐고 단일 호스트 규모에 분산 트랜잭션·SPOF 비용이 큼. C 이후 필요해지면 별도 검토.
- **DB 엔진 이관(C)**: 범위 안, 운영 목표. 단 B(어댑터) 완료가 전제. C는 SQLAlchemy(Core 우선) 또는 단일 엔진 통일로 PostgreSQL 이관 — 상세 설계는 C 도달 시 별도 spec.
- **phase 우선순위(fork 2)**: autopilot 런타임 gate 분석에 위임, 기본값 phase A.
- **이 spec 범위**: autopilot harness + Phase A. B/D/C는 §6 로드맵으로 기록하고 각자 독립 spec→plan→autopilot 사이클.

## 4. Autopilot 설계 (에이전틱 Workflow 피드백 루프)

### 4.1 루프 골격 (phase당)

- **Phase 0 (1회, setup)**: worktree/통합 브랜치 확인. **baseline 오라클 동결** — 편집 전 현 코드에서 풀 gate 통과 확인 + `brain.query` recall 스냅샷 + GC dry-run 출력을 regression 기준으로 캡처해 동결. implementer agent는 이 오라클을 재생성할 수 없다("자기가 안 진 기준으로 채점" 규율).
- **step 루프**: phase를 bounded step backlog로 분해(A는 ~5 step). 각 step:
  1. `apply` (agent, **opus** — 구현/변경 책임): worktree에 bounded 편집.
  2. `gate` (결정론): §4.2.
  3. `review` (멀티에이전트): §4.3.
  4. pass(결정론 gate green ∧ 리뷰 패널 blocking 0) → 커밋 + 통합 브랜치 merge. fail → self-correct(finding 환류) **최대 3 라운드** → red 지속 시 **freeze**(중단, 브랜치 보존, 보고).
- **finish-gate**: step backlog 소진 ∧ 풀 gate green ∧ recall regression=0 ∧ dry-run pre==post → **정지 + 알림**. main ff·origin push는 보류(사람 행위).

### 4.2 결정론 gate (= "refactor golden")

- 기존 테스트 green: `JAVA_HOME=... gradle test`, `cd worker && uv run pytest -q`(현 504), `cd worker && uv run neuron-knowledge --show-boundary`.
- 구조 불변식 lint(신규, `worker/eval/ledger_seam_invariants.py`): phase별 정의. phase A 예 — (i) GC 스크립트가 ledger GC 메서드(`record_memory_gc_audit`/`mark_disabled`/`_mark_gc_deleted`) 직접 호출 0, (ii) 비가역 delete는 seam 경유만 도달 가능, (iii) `--show-boundary` 출력 불변.
- recall regression=0(baseline 스냅샷 대비), GC dry-run pre==post.
- **seam 경로 커버리지 증명**: 변경한 비가역 경로가 테스트로 실제 실행됨을 단언(audit→delete 순서, audit 없이 delete 도달 불가). 현재 GC live 경로는 테스트에서 `blocked_live_execution`이라 "green이 hollow"일 위험이 있어 이 증명이 사람 리뷰를 대체한다.

### 4.3 멀티에이전트 리뷰 게이트 (사람 승인 대체)

결정론 gate green 후 발화. opus 패널 4 렌즈(적대적):
1. correctness / behavior-preservation
2. seam 완전성 vs spec (비가역 op이 실제로 격리됐나)
3. security / irreversibility (seam 우회 delete 가능? secret 노출?)
4. test adequacy (gate가 바뀐 경로를 실제로 덮나)

**fail-closed**: 한 명이라도 blocking finding이면 merge 차단. finding은 self-correct 루프로 환류(implementer가 수정 → re-gate → re-review), 최대 3 라운드 후 freeze.

### 4.4 자율성 / merge 착지

- 사람 diff-승인 게이트 0. 무인 → 통합 브랜치 `claude/ledger-autopilot` auto-merge(green일 때만).
- finish-gate green이면 **통합 브랜치에서 정지 + 알림**. `main` ff는 사람이 green 확인 후 수행. `origin` push는 무인 보류(머신 밖 출구 = 유일한 사람 행위).
- 모든 쓰기 worktree 한정, `main` 불가촉. 로컬 브랜치라 reset/revert로 가역.

### 4.5 안전 envelope

- forbidden ops hard-block(standing pre-approval 무관): live GC execute, RAGFlow dataset delete/disable, secret/raw transcript 노출, runtime mutation. 리팩토링은 순수 코드 재구조화 — live 삭제 0, dry-run만.
- tripwire freeze: red gate = 자동 중단 + 브랜치 보존.
- baseline 오라클 편집 전 동결.
- C(엔진 이관)는 위 게이트 위에 데이터 안전 추가: 백업 + rollback 증명 + DB 복제본 dry-run + old/new 엔진 parity 검증.

## 5. Phase A 상세 (첫 타깃)

- **인터페이스**: `IGCSafetyAuditor`(ABC) — `log_gc_start(gc_kind, knowledge_id, document_id) -> audit_id`, `log_gc_success(audit_id, replacement_id="")`, `log_gc_failure(audit_id, error_msg)`.
- **어댑터**: `LedgerGCSafetyAuditor(ledger)` — 기존 `record_memory_gc_audit`/`mark_disabled`/`mark_enabled`/`_mark_gc_deleted` 래핑.
- **전환 대상 5 스크립트**: `session_memory_gc.py`, `transcript_memory_gc.py`, `transcript_volume_gc.py`, `zombie_snapshot_repair.py`, `terminal_skipped_quarantine.py` — 주입된 `IGCSafetyAuditor` 의존으로 전환. delete 호출과 audit write를 seam 뒤에 co-locate(audit 없이 delete 불가).
- **동작 보존**: 모든 변경은 behavior-preserving. dry-run 출력 동일이 gate.
- step 분해(예): 인터페이스 정의 → 어댑터 구현 → 스크립트 전환 ×N → seam 불변식 lint + 커버리지 테스트 추가.

## 6. 로드맵 (B → D → C, 확정)

순차 무인. 각 phase 독립 spec→plan→gate→통합 merge.

- **B — Ledger Core 어댑터** (`ILedgerCoreDbAdapter`): 20곳 직접 결합 → 어댑터 경유. `brain_query`/`brain_read_model`의 Protocol 패턴 확장. **C의 전제조건**(엔진 교체 지점 확보). 위험 중–고.
- **D — 4-area 모듈 경계**: area 간 import lint 강제. 위험 중.
- **C — 엔진 이관 → PostgreSQL** (운영 목표): 어댑터 뒤 엔진 교체. 3개 DB × SQLite 종속 구문(`INSERT OR IGNORE`/`ON CONFLICT`/`PRAGMA`) 이관. 데이터 안전 게이트(§4.5). 위험 높음. 상세는 별도 spec.

## 7. 기존 autopilot 머신 재사용 (도메인 중립 seam)

llm-brain autopilot에서 포팅 가능한 중립 구성요소: 루프 골격(`run_autopilot_cycle` DI 구조), accept guard(`classify_candidate_block_reason`), golden grader harness(`worker/eval/golden_grader.py`의 격리 패턴), approval gate(`_projection_write_gate_error`), self-mint approval, fail-closed supersede 구조. 단 RAGFlow/MemoryCard 하드와이어 4점(retrieve/chat_completion/upsert_memory_card/list_*chunks)은 코드-리팩토링 I/O(diff 읽기/patch apply/test gate)로 대체. 주의: 기존 autopilot의 "3 cohort finish-gate"·per-lane F1·tripwire는 **docstring만 있고 코드 미구현**이므로 이 autopilot에서 새로 구현한다(Workflow 루프가 그 역할).

## 8. 산출물

- Workflow 오케스트레이션 스크립트(주 deliverable).
- 프로덕션 코드: `IGCSafetyAuditor` + `LedgerGCSafetyAuditor` + 전환된 5 GC 스크립트.
- `worker/eval/ledger_seam_invariants.py`(구조 불변식 lint).
- baseline 오라클 캡처 스크립트 + seam 커버리지 테스트.

## 9. 권한 / 경계

- 이 task 한정 명시 승인(코드 mutation 무인 + 통합 브랜치 auto-merge). standing pre-approval은 코드 mutation 미포함이므로 별개. main ff·origin push는 사람.
- CLAUDE.md guardrails 준수: `RAGFLOW_API_KEY` 단일, live write/delete/disable·live GC·runtime mutation 금지(리팩토링은 이를 트리거하지 않음).

## 10. 리스크 / 미해결

- "green이 hollow": GC live 경로 테스트 커버리지 약함 → §4.2 seam 커버리지 증명으로 완화.
- 정본 보고서 내부 불일치: override 배너는 in-process 모듈인데 본문 다이어그램은 REST/gRPC+PostgreSQL → 본 spec은 배너 의도(in-process) 채택, 물리 분리 범위 밖.
- C 마이그레이션 parity 검증의 구체 기준(데이터 동등성 oracle)은 C 도달 시 spec에서 확정.

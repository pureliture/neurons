# Ledger Refactor Autopilot — 설계 (spec) · rev2

- 작성일: 2026-06-15 (rev2 = 멀티에이전트 리뷰 게이트 self-correct 반영)
- worktree: `.worktrees/ledger-autopilot` (work 브랜치 베이스)
- 통합 브랜치: `claude/ledger-autopilot-integration` (autopilot이 phase별 무인 auto-merge 대상). work는 step별 `claude/ledger-autopilot-aN` → 통합 브랜치로 merge. `main` 불가촉.
- 입력 근거: `docs/architecture/ledger-review-deepdive-20260614.html`(개정판), `architecture_audit_report.md`, 현재 main 코드 검증 + rev1 spec에 대한 4-렌즈 적대적 리뷰(2026-06-15).

## 1. 목표

`ledger.py` 중심 결합을 **에이전틱 Workflow 피드백 루프 autopilot**으로 점진 리팩토링. 유한 작업이므로 finish-gate 충족 시 정지. 최종 운영 목표: raw SQLite → PostgreSQL 이관(phase C), 단 B(어댑터 seam)가 *필요조건*.

## 2. 현재 아키텍처 (검증된 토폴로지, 3층)

- **프로세스 층 — 이미 분리됨**: `worker/pyproject.toml [project.scripts]` **15개** entry-point가 각자 프로세스. `neuron-knowledge`(CLI + `mcp_server.run_stdio_server`), `rag-ingress-queue`(`server_runtime` = `ThreadingHTTPServer`, 상주), `rag-ingress-worker`, GC CLI 다수, memory build/regen/sync CLI. 모놀리스는 프로세스가 아니다.
- **코드 결합 층 — 모놀리식**: **22개 비-테스트 모듈**(`worker/eval/` 2개 제외 시 20개)이 `from ..ledger import Ledger` 직접 결합. `Ledger`는 raw `sqlite3`(4178줄, `_initialize` 34 `CREATE TABLE` = 도메인 33 + `schema_migrations`). 유일 부분 seam: `brain_query.py`의 `BrainReadModel` Protocol + `brain_read_model.py`의 `LegacyLedgerBrainReadModel`. **주의**: 이 어댑터조차 `self._ledger._connect()` + raw SQLite SQL(`brain_read_model.py:35`)로 dialect를 누수 — engine-agnostic 아님.
- **데이터 층 — 공유 raw-sqlite3**: 3 DB 클래스 — `Ledger`(ledger.py), `RAGIngressStateDB`(state_db.py), `IngestStateStore`(shadow_worker.py) + 외부 RAGFlow HTTP API. WAL 다중 프로세스 협조. SQLite 종속 구문 규모: ledger.py에 `INSERT OR IGNORE/REPLACE` ×22, `ON CONFLICT` ×22, `PRAGMA` ×6, raw `execute/executemany` ×142; 나머지 2 DB에 ~24개 추가.

## 3. 비가역 GC 표면 (phase A의 근거, 정확 인벤토리)

rev1의 결함은 비가역 op 인벤토리가 틀렸던 것. 정정:

- **진짜 비가역 op = `ragflow.delete_documents`(네트워크 하드 삭제)**. 호출 3곳: `session_memory_gc.py:115`, `transcript_session_gc.py:135`, `transcript_volume_gc.py:113`. 모두 `if config.execute` 블록 안.
- **co-located 부수효과**: `_mark_gc_deleted`(스크립트-로컬, `session_memory_gc.py:324`, raw `UPDATE knowledge_items`), `_backup_before_delete`, `record_memory_gc_audit`(ledger.py:1719). **현재 audit는 삭제 성공 *후* 기록**(session_memory_gc.py:115-122).
- **`record_memory_gc_audit`는 keyword-only 14 params**(`dataset_id`, `replacement_knowledge_id`, `dirty_at`, `snapshot_updated_at`, `approval_operation`, `age_gate_seconds`, `mutated` 등) — A2/E3 reconstructability 필수.
- **misfit(삭제 아님, phase A에서 제외)**: `zombie_snapshot_repair.py`(requeue 마커, delete·audit 없음), `terminal_skipped_quarantine.py`(자기 테이블 raw INSERT, document_id 없음). 가역 작업이라 별도 seam.
- **추가 확인 대상**: `transcript_memory_gc.py`(live는 `blocked_live_execution` 반환, `:159`), `gc_backup.py`. phase A 착수 step 0에서 `session_memory/` 트리 전수 grep로 delete/ledger-GC-method 호출 사이트를 **완전 열거**하고 allowlist 고정.

## 4. 범위 결정

- 분리 깊이: **in-process 모듈 seam**(A/B/D). 물리 네트워크 분리(REST/gRPC) 범위 밖.
- **Phase A 타깃 = 진짜 audit-then-irreversible-delete 3 스크립트**: `session_memory_gc.py`, `transcript_volume_gc.py`, `transcript_session_gc.py`. zombie/terminal/gc_backup은 별도(後).
- DB 엔진 이관(C): 범위 안, 운영 목표. B 완료가 필요조건이나 **충분조건 아님**(§7).
- 이 spec 범위: autopilot harness + Phase A. B/D/C는 §7 로드맵, 각자 독립 spec.

## 5. Autopilot 설계 (에이전틱 Workflow 피드백 루프)

### 5.1 루프 골격 (phase당)

- **Phase 0 (1회)**: delete-site 전수 열거 + allowlist 고정. **baseline 특성화(characterization) 동결** — §5.3의 행동 트레이스 baseline을 편집 전 캡처·동결. work 브랜치 생성.
- **step 루프**: phase를 bounded step으로 분해. step별 work 브랜치 `claude/ledger-autopilot-aN`. 각 step: `apply`(opus) → `gate`(§5.2, 결정론) → `review`(§5.4, 멀티에이전트) → pass면 work→**통합 브랜치** merge(no-op 아님) / fail면 self-correct ≤3 라운드 → red 지속 freeze.
- **finish-gate**: step 소진 ∧ §5.2 전 항목 green ∧ §5.3 특성화 트레이스 일치 → **통합 브랜치에서 정지 + 알림**.

### 5.2 결정론 gate

- 기존 테스트 green: `gradle test`, `cd worker && uv run pytest -q`, `neuron-knowledge --show-boundary`.
- **구조 불변식 lint**(`worker/eval/ledger_seam_invariants.py`), **`ragflow.delete_documents` 호출 사이트를 키로**(ledger-method 이름이 아니라): (i) §3 열거된 모든 delete 사이트가 `IGCSafetyAuditor` seam 경유만 도달, (ii) phase별 shrinking allowlist 밖에서 직접 호출 0, (iii) `--show-boundary` 불변.
- §5.3 특성화 트레이스 일치(behavior-preserving 핵심 오라클).
- **삭제된 오라클**: recall regression=0 / dry-run pre==post는 **phase A 행동 보존 오라클에서 제외** — 검증 결과 둘 다 GC 비가역 경로에 구조적으로 눈멈(recall은 active snapshot만 읽어 disjoint; dry-run은 `if execute` 블록 전체 skip). 회귀 보조 신호로만 유지.

### 5.3 특성화 테스트 게이트 (사람 리뷰 대체 — 구체·falsifiable)

rev1의 "seam 커버리지 증명"은 fake 상대 통과라 무의미했다. 대체:

- **recording-fake 하니스**: `execute=True` 경로를 seam 통해 구동, **순서화된 부수효과 트레이스 단언**: `backup → delete → tombstone(_mark_gc_deleted) → audit(audit_id 연결)`. 편집 전 baseline 동결.
- **음성 불변식**: 성공한 backup 없이 delete 도달 불가; 모든 delete에 paired audit(루프 중간 exception/break로 삭제 후 audit 누락되는 `session_memory_gc.py:124-127` 경로 포함); `_still_qualifies` 재확인 호출됨.
- **계약 테스트**: `RagflowHttpClient.delete_documents` 시그니처(args·예외) 고정 → fake가 real에서 drift 못 함.
- **mutation 체크**: delete↔audit 순서 바꾸거나 backup 제거하면 테스트가 **반드시 fail**. 안 그러면 증명이 unfalsifiable → merge 금지.
- **audit 동등성**: 픽스처 GC run의 `memory_gc_audit` row 필드가 pre/post **byte-identical**(성공·실패 경로 모두). dry-run stdout 일치로 대체 불가.

### 5.4 멀티에이전트 리뷰 게이트 (사람 승인 대체)

결정론 gate green 후 발화. opus 패널 4 렌즈(적대적): correctness/behavior-preservation, seam 완전성 vs §3 인벤토리, security/irreversibility, gate-completeness(green이 hollow인가). fail-closed — blocking 1개면 merge 차단, finding은 self-correct로 환류 ≤3 라운드.

### 5.5 자율성 / merge 착지

- 사람 diff-승인 0. 무인 work→통합 브랜치 auto-merge(green만). finish-gate green이면 통합 브랜치 정지 + 알림.
- **`main`/`master` 무인 쓰기·merge 절대 금지** — main ff는 사람 수동 post-finish. tripwire: autopilot이 main checkout/merge 시도하면 freeze. (rev1의 "로컬 main 자동 ff"는 안전 리뷰로 철회 — §6 참조. 데이터는 git-가역 아님.)
- origin push 무인 보류.

### 5.6 안전 envelope (런타임 강제 — prose 아님)

rev1의 "forbidden ops hard-block"은 memory-card 문자열 분류기(`memory_evaluation.py:19-44`)일 뿐 agent tool 호출을 못 막았다. 실 런타임 강제 추가:

- **deny-by-default tool/permission 정책**(apply·gate agent): live-mutating CLI argv hard-block(`session_memory_gc`/`transcript_volume_gc`/`transcript_session_gc`/`transcript_memory_gc`/`zombie_snapshot_repair`/`terminal_skipped_quarantine` + `--execute`), worktree 밖 쓰기 차단.
- **gate 네트워크 격리/무자격**: gate는 RAGFLOW 토큰 env 미설정 + base_url을 loopback/forbidden sink로 + egress 차단 환경에서 실행 → 버그가 live 삭제 경로에 도달해도 connection/credential error로 **fail-closed**.
- **불변식**: "gate 중 live `RagflowHttpClient` 인스턴스화 0"을 `ledger_seam_invariants.py` 결정론 불변식으로.
- forbidden ops(live GC execute, dataset delete/disable, secret 노출, runtime mutation)는 hard-block. `FORBIDDEN_AUTO_POLICY_OPERATIONS`는 memory-card 데이터 가드일 뿐 tool 호출 미커버임을 명시.
- tripwire freeze(red gate, main 접근). baseline 동결.
- Phase 0 pre-flight: live RAGFlow dataset이 autopilot gate/apply 환경에서 **도달 불가**임을 단언.

## 6. 데이터 가역성 vs 코드 가역성 (no-human-gate 근거)

- 코드/git: 가역(work 브랜치, reset/revert).
- 데이터/RAGFlow+ledger: **git로 불가역**. 우발적 live `delete_documents` 1회는 `git reset`으로 복구 안 됨(GC backup 자체도 `--backup-dir`/`--execute` 배선에 의존, 그걸 동시에 리팩토링 중).
- 따라서 사람-게이트 제거 결정은 오직 §5.6 데이터-뮤테이션 envelope가 강제될 때만 성립. main 무인 merge는 안 함(§5.5).

## 7. 로드맵 (B → D → C, 확정) + 재사용 현실

순차 무인, 각 phase 독립 spec.

- **B — Ledger Core 어댑터**(`ILedgerCoreDbAdapter`): 22 결합 → 어댑터. **C의 필요조건이나 충분조건 아님**. C는 추가로 (a) 어댑터 경계 넘는 raw-SQL/`_connect()` 누수 제거(brain_read_model 포함), (b) 3 DB의 SQLite 종속 DML/DDL 이관, (c) SQLAlchemy-Core vs 단일엔진 결정 필요. **phase-B exit 기준**: 어댑터 경계를 넘는 raw-SQL/`_connect()` 접근 0 — 안 되면 C 불가. `architecture_audit_report.md:42`의 "런타임 dialect 변환 = 거대한 버그의 온상" 경고를 C 설계 제약으로.
- **D — 4-area 모듈 경계**: import lint 강제.
- **C — 엔진 이관 → PostgreSQL**(운영 목표): 어댑터 뒤 교체 + §7-B (a)(b)(c). 데이터 안전 게이트(백업·복제본·old/new parity). 상세 별도 spec.

**재사용 현실**(rev1 과장 정정): `run_autopilot_cycle`은 **118줄 단일-pass 분류기** — step 루프·결정론 gate·멀티에이전트 리뷰·self-correct·freeze·finish-gate **전부 없음**. 즉 피드백-루프 제어면은 **전량 신규 코드**. 진짜 포팅 가능: accept guard(`classify_candidate_block_reason`), golden_grader 격리 패턴, self-mint approval 형태. `_projection_write_gate_error`·`mine_live_candidates`는 RAGFlow-하드와이어라 비재사용.

## 8. 산출물

- Workflow 오케스트레이션 스크립트(제어면 신규).
- 프로덕션 코드: `IGCSafetyAuditor`(전체 audit payload 보유 — 14-field `record_memory_gc_audit` 손실 없게 typed `AuditContext`) + 어댑터 + 전환된 3 스크립트. **`_mark_gc_deleted` 소유권 먼저 해결**(Ledger로 승격 or 어댑터가 소유) 후 co-location.
- `worker/eval/ledger_seam_invariants.py`(delete-site 키 불변식 + "gate 중 live client 0").
- §5.3 특성화 하니스 + 계약 테스트 + mutation 체크.

## 9. 권한 / 경계

- 이 task 한정 명시 승인(코드 mutation 무인 + 통합 브랜치 auto-merge). main 무인 금지(§5.5). standing pre-approval은 코드 mutation 미포함.
- CLAUDE.md guardrails: `RAGFLOW_API_KEY` 단일, live write/delete/disable·live GC·runtime mutation 금지 — §5.6 런타임 envelope로 강제.

## 10. 리스크 / 미해결

- **behavior-preserving 정의**: `IGCSafetyAuditor`의 자연스런 사용(start-before/success-after)은 현재(삭제 성공 후 audit) 대비 audit 타이밍 변경 = 행동 변경. §5.3이 타이밍·내용·실패경로를 byte-identical로 고정해 방지.
- C parity oracle(데이터 동등성) 구체 기준은 C spec.
- 정본 보고서 내부 불일치(배너=in-process, 본문 다이어그램=REST/gRPC) → 본 spec은 in-process 채택.
- **사용자 결정 역전 1건**: rev1의 "로컬 main 자동 ff"를 안전 리뷰(데이터 비가역) 근거로 "main 무인 금지"로 변경. 사용자 재확인 필요.

# Ledger Refactor Autopilot — 설계 (spec) · rev4

- 작성일: 2026-06-15 (rev4 = Phase A를 A1 풀무인 / A2 사람-게이트로 분할; 리뷰 라운드3 잔여 fix)
- worktree: `.worktrees/ledger-autopilot`. 통합 브랜치 `claude/ledger-autopilot-integration`. step별 work `claude/ledger-autopilot-aN`. **`main`/`master` 무인 쓰기·merge 금지**.
- 입력 근거: 개정판 보고서, `architecture_audit_report.md`, main 코드 검증 + rev1~3 4-렌즈 적대적 리뷰(3라운드). 라운드3에서 soundness 렌즈 pass, 잔여 blocker는 "풀무인 × 비가역 live-delete" 전제에서 비롯 → **Phase A 분할로 해소**.

## 1. 목표

`ledger.py` 결합을 에이전틱 Workflow 피드백 루프 autopilot으로 점진 리팩토링. 유한 작업. 최종: SQLite → PostgreSQL(C), B가 필요조건.

## 2. 현재 아키텍처 (검증)

- 프로세스 층: `pyproject.toml [project.scripts]` 15 entry-point, 이미 분리.
- 코드 결합 층: **20개 production 모듈**(+ `worker/eval/` 2개 = 비-테스트 22)이 `from ..ledger import Ledger` 직접. `Ledger`=raw sqlite3(4178줄/131 def/34 테이블). `brain_query`/`brain_read_model`은 유일 Protocol-매개 READ seam이나 engine-agnostic 아님(`brain_read_model.py:35` `_connect`+raw SQL). raw `_connect()` dialect 누수 광범위(~9 모듈: `session_memory_gc.py:243/334`, `transcript_volume_gc.py:147`, `terminal_skipped_quarantine`×5, `zombie_snapshot_repair`×2, `memory_regeneration`, `dirty_session_memory_sync`, `native_memory_mirror`).
- 데이터 층: 3 DB + RAGFlow HTTP. SQLite 종속 구문: `INSERT OR IGNORE/REPLACE`×22, `ON CONFLICT`×22, `PRAGMA`×6, raw execute×142.

## 3. 비가역 live 표면 + Phase A 분할

**키 = RagflowHttpClient의 비가역/forbidden 메서드** {`delete_documents`, `disable_document`, `disable_message`, `delete_memory`}. live 사이트: `delete_documents`(`session_memory_gc.py:115`, `transcript_session_gc.py:135`, `transcript_volume_gc.py:113`), `disable_document`(`sync_roundtrip.py:40` via `dirty_session_memory_sync.py:246/305`), `disable_message`(`native_memory_reconcile.py:111` via `native_memory_write_runner.py:180`). backup 전제 네트워크: `list_document_chunks`(`session_memory_gc.py:177` 등; real `ragflow_client.py:155`). Phase 0 전수 grep `delete_*`/`disable_*`/`delete_memory`로 allowlist 동결.

**3 타깃 스크립트 계약(서로 다름)**: `session_memory_gc`=backup→delete→tombstone(`_mark_gc_deleted:116` raw UPDATE)→audit(`record_memory_gc_audit`), 즉시 break, Ledger import O. `transcript_volume_gc`=backup→delete만(audit·tombstone 0), 즉시 break. `transcript_session_gc`=backup→delete만(Ledger import 안 함), skip-and-continue + circuit-breaker `MAX_FAILURES=10`, self-heal 재-eligible.

**Phase A 분할(라운드3 freeze 해소)**:
- **A1 = 풀무인**: seam 구조 — `IGCSafetyAuditor`+`AuditContext` 정의, 어댑터, `_mark_gc_deleted` 소유권 이동(비가역 delete **미연결** 상태), 비삭제/read-scan 경로 정리. **live delete를 auto-merge에 맡기지 않음** → fake-bound 게이트로 충분.
- **A2 = 사람 게이트 1개**: 3 스크립트의 `delete_documents`/tombstone/audit를 seam 경유로 실제 라우팅(co-location). **이 merge들에서만 사람 승인 1회** + §5.0-A2 증명을 리뷰 입력으로.
- misfit 제외: `zombie_snapshot_repair`(requeue), `terminal_skipped_quarantine`(자기 테이블 INSERT). `sync_roundtrip`/`native_memory_reconcile`/`dirty_session_memory_sync`/`native_memory_write_runner`는 phase A 밖이나 envelope 안.

## 4. 범위 / 기록된 결정

- 분리 깊이: in-process 모듈 seam(A/B/D). **[결정 D-1]** 보고서 배너(:46)+`architecture_audit_report.md:42`가 물리 네트워크 분리를 오버엔지니어링으로 기각(본문 다이어그램 :529/545/633/661-663은 REST/gRPC mandate하여 배너와 모순). 본 spec·모든 B/D/C spec은 in-process 채택, 네트워크 분리 범위 밖, 재도입엔 새 결정 필요.
- 이 spec 범위: autopilot harness + Phase A(A1+A2).

## 5. Autopilot 설계

### 5.0 envelope (Phase 0; A1·A2 공통 기본 + A2 추가)

apply step 전 완성·self-test 통과 못 하면 시작 거부.

**기본(저비용, 유지)**:
1. **PreToolUse deny hook**(`.claude/settings.json` 신규): apply·gate agent Bash argv가 live-mutating CLI 名 + `--execute`, **또는 `agent_knowledge_live_approval.v1` 승인파일 쓰기**(self-mint 차단)면 non-zero. worktree 경계 밖 쓰기 차단.
2. **agent 네트워크/자격 격리(apply·gate)**: RAGFLOW 토큰 미주입, base_url loopback, egress 차단(구체 기제: netns/loopback resolver/blocked DNS).
3. **코드-레벨 kill-switch**: `RagflowHttpClient.__init__`(`ragflow_client.py:36`)가 env `AUTOPILOT_LIVE_OK` 없으면 생성 거부 — 이 토큰은 apply/gate agent에 **절대 미주입**. → argv 없는 프로그램적 `Runner(config=execute=True).run()` 경로도 fail-closed.
4. **preventive main guard**: main/master ref 쓰기를 git이 거부(가드 remote의 server-side update hook이 이상적; 로컬 hook은 best-effort) + 오케스트레이션이 매 merge 직전 `target==통합브랜치` 단언·abort. main reflog 변화를 감시하는 **orchestrator-external watchdog**(스냅샷 후 변하면 freeze).
5. **orchestrator root-of-trust(자기수정 방지)**: 가드레일(hook들)·`ledger_seam_invariants.py`·동결 baseline을 S0에서 **hash-pin**. **부모 orchestrator**(agent 밖)가 매 gate·merge 전 hash 재검증, 변하면 abort. baseline/invariant 편집으로 green이 된 gate는 **fail 처리**. apply/self-correct agent는 이 파일군 수정 금지(deny-hook path list, orchestrator 소유).
6. **harness self-test**: (a) synthetic `--execute` argv가 hook(1)에 차단됨, (b) **프로그램적 `execute=True` repro 1줄을 agent sandbox에서 실제 실행 → connection/credential error로 fail-closed**, (c) main ref 쓰기 거부됨 — 3개 단언. 실패/부재면 시작 거부.

**A2 추가(사람 게이트 입력, 비가역 merge 전)**:
7. **wire-contract gate**: cassette/sandboxed RAGFlow로 real DELETE `/datasets/{id}/documents` 요청(메서드·escape된 path·`{'ids':[...]}` body, `ragflow_client.py:297-300`)을 핀. fake byte-match만으로 불충분.
8. **backup→delete→restore 복구 왕복 증명**: restore가 retrievable-equivalent임을 단언(`gc_backup.py:104-129` restore가 새 doc_id·lossy임을 감안) + post-delete 삭제 확인(code:0 신뢰 금지, `ragflow_client.py:363`).
9. **orphan-delete 주입 테스트**: delete(`:115`) 성공 후 `_mark_gc_deleted`/`_record_audit` raise→`except:124-127` break 경로에 durable orphan 신호 단언 + delete/tombstone/audit 순서 flip mutation이 gate fail.

### 5.1 루프 골격

step별 work 브랜치. 각 step: apply(opus) → gate(§5.2) → review(§5.4) → A1 pass면 통합 merge / **A2 pass면 사람 승인 1회 후 merge** / fail면 self-correct ≤3 → freeze. finish-gate: step backlog(§10) done ∧ §5.2 exit0 ∧ §5.3 트레이스 일치.

### 5.2 결정론 gate

- **모든 step(Ledger·공유 모듈 touch 포함)에서 full**: `gradle test` + `cd worker && uv run pytest -q` + `neuron-knowledge --show-boundary`. (라운드3: S2/S3도 full — partial gate 금지.)
- 구조 불변식 lint(`ledger_seam_invariants.py`): (i) §3 사이트가 allowlist 밖 직접 도달 0(delete는 seam 경유만), (ii) **gate 중 live `RagflowHttpClient`가 real network sink 도달 0**(§5.0-2 격리로 강제; constructor-count 아님 — `transcript_session_gc.py:101`이 read-scan용 무조건 생성), (iii) `--show-boundary` 불변.
- §5.3 per-script 트레이스 일치.
- recall regression=0/dry-run pre==post는 보조 신호만(GC 경로에 구조적 눈멈).

### 5.3 per-script 특성화 게이트 (결정적)

각 타깃 실제 baseline 독립 동결. **결정성**: `now_fn`/`id_fn` 주입(또는 frozen clock)으로 캡처·replay. **결정적 필드는 동등 비교, volatile 필드(created_at/audit_id/deleted_at/updated_at)는 정규화/마스킹** — swap/drop은 fail, 타임스탬프 jitter는 spurious red/green 아님. `session_memory_gc`만 paired-audit 불변식. **non-introduction**: audit/tombstone이 baseline에 없던 transcript 2종에 추가되면 fail. per-script 실패경로 mutation(continue↔break, circuit-breaker threshold, 재-eligibility). 계약 테스트: `delete_documents` + `list_document_chunks` + `disable_*` 시그니처·반환·예외 핀.

### 5.4 멀티에이전트 리뷰 게이트

opus 4렌즈(correctness/behavior-preservation, seam 완전성, security/irreversibility, gate-completeness). fail-closed, self-correct ≤3.

### 5.5 자율성 / merge

- **A1**: 사람 diff-승인 0, 무인 work→통합 merge(green만).
- **A2**: 비가역 delete-touching merge마다 **사람 승인 1회**(§5.0-7~9 증명이 리뷰 입력).
- `main`/`master` 무인 금지(사용자 확정). main ff 사람 수동. origin push 보류.

## 6. 가역성

코드/git 가역(단 main reflog 의존→§5.0-4~5 고정). 데이터/RAGFlow+ledger git-불가역. **no-human-gate는 A1에만 적용; A2(비가역 경로)는 사람 게이트라 데이터 비가역 텐션 해소.**

## 7. 로드맵 (B→D→C)

- **B — Ledger Core 어댑터**(`ILedgerCoreDbAdapter`): **20 production 결합 → 어댑터**(eval 2개 별도). C 필요조건·충분조건 아님. C 추가요건 (a) 어댑터 경계 raw-SQL/`_connect` 누수 0(§2 ~9 모듈), (b) 3 DB SQLite 구문 이관, (c) SQLAlchemy-Core vs 단일엔진. phase-B exit: 경계 넘는 raw-SQL/`_connect` 0.
- **D — 4-area 경계 lint**.
- **C — PostgreSQL 이관**: 어댑터 뒤 교체 + (a)(b)(c). **C-spec exit gate에 명시**: MVCC isolation divergence(SQLite serial vs PG Read Committed) parity + connection-lifecycle/lock-timeout(`architecture_audit_report.md:46-50`) + 데이터 parity. 상세 別 spec.

**재사용 현실**: `run_autopilot_cycle`(`autopilot_loop.py:25-118`)은 118줄 단일-pass 분류기 — 루프 제어면(step loop/gate/리뷰/self-correct/freeze/finish-gate) **전량 신규**(최대 빌드). 포팅: accept guard, golden_grader 격리 패턴, self-mint approval 형태.

## 8. 산출물

Phase 0 envelope(§5.0 1~6 기본, 7~9는 A2) + Workflow 제어면(신규) + `IGCSafetyAuditor`/typed `AuditContext`(14-field 손실 없음)/어댑터/전환 3 스크립트 + `ledger_seam_invariants.py` + per-script 특성화·계약·mutation 하니스.

## 9. 권한 / 경계

이 task 한정 명시 승인: A1 코드 mutation 무인 + 통합 auto-merge; **A2 delete merge는 사람 승인 1회**; main 무인 금지. standing pre-approval은 코드 mutation 미포함. CLAUDE.md guardrails는 §5.0 런타임으로 강제(prose 아님).

## 10. Phase A step backlog

**A1 (풀무인, 각 step full §5.2 gate)**:
- S0 envelope §5.0 1~6 + self-test 3단언.
- S1 §3 전수 grep allowlist + §5.3 per-script baseline(결정적 clock) 동결. gate: baseline 존재 + lint 현코드 green.
- S2 `_mark_gc_deleted` 소유권 이동(Ledger 승격 or 어댑터). gate: **full §5.2** + session_memory_gc 트레이스.
- S3 `IGCSafetyAuditor`+`AuditContext`+어댑터 정의(미연결). gate: **full §5.2**.
- S4 read-scan/비삭제 경로 seam 준비(delete 미연결). gate: full §5.2 + 트레이스.

**A2 (각 delete-touching merge에 사람 승인 1회; §5.0-7~9 증명 입력)**:
- S5 session_memory_gc delete co-location(audit/tombstone 타이밍 보존). gate: full §5.2 + §5.3 + wire-contract + restore 왕복 + orphan 주입. → 사람 승인.
- S6 transcript_volume_gc(non-introduction). → 사람 승인.
- S7 transcript_session_gc(circuit-breaker/self-heal 보존). → 사람 승인.
- S8 seam 불변식 shrink: allowlist Phase-A 목표로, delete/disable 직접 도달 0. gate: §5.2(i)(ii).

## 11. 리스크 / 미해결

- §5.3 결정성 contract(필드 정규화) 미정의 시 gate 영구 red→freeze(안전 방향).
- C parity oracle 상세 = C spec(§7).
- envelope(§5.0)가 리팩토링보다 큰 빌드 — 사용자 확정(풀무인 A1 + A2 1게이트).

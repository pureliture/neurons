# Ledger Refactor Autopilot — 설계 (spec) · rev6

- 작성일: 2026-06-15 (rev6 = 라운드5 수렴: egress 목적지 allowlist + real-wire 계약 게이트 + kill-switch pin + production ordering 불변식 + 인용/카운트 정정)
- worktree: `.worktrees/ledger-autopilot`. 통합 브랜치 `claude/ledger-autopilot-integration`. step별 work `claude/ledger-autopilot-aN`. **`main`/`master` 무인 쓰기·merge 금지**.
- 리뷰 이력: rev1~4, 4-렌즈 적대적 게이트 4라운드. rev4에서 soundness PASS; 잔여 blocker는 전부 "오라클 정밀화 + 외부 root-of-trust"로 수렴(전제 불가능 아님). rev5가 그 수렴 fix 확정본.

## 1. 목표

`ledger.py` 결합을 에이전틱 Workflow 피드백 루프 autopilot으로 점진 리팩토링. 유한 작업. 최종: SQLite → PostgreSQL(C); B(어댑터)가 필요조건.

## 2. 현재 아키텍처 (검증)

- 프로세스 층: `pyproject.toml [project.scripts]` 15 entry-point, 이미 분리.
- 코드 결합 층: **20 production 모듈**(+ `worker/eval/` 2 = 비-테스트 22)이 `from ..ledger import Ledger` 직접. `Ledger`=raw sqlite3(4178줄/131 def/34 테이블). `brain_query`/`brain_read_model`은 유일 Protocol-매개 READ seam이나 engine-agnostic 아님(`brain_read_model.py:35` `_connect`+raw SQL). raw `_connect()` dialect 누수 광범위(~9 모듈).
- 데이터 층: 3 DB + RAGFlow HTTP. SQLite 종속 구문 광범위(근사: `INSERT OR IGNORE/REPLACE`×22, `ON CONFLICT`≈25, `PRAGMA`≈19, raw execute×142) — 정확 카운트는 S1 live grep으로 재산출(§2 수치는 근사, freeze 금지).
- **raw urllib 직접 호출 존재**(`extraction_llm.py:13-21`, `shadow_worker.py:474-482`) — RagflowHttpClient 우회 가능 → egress 차단은 메서드-키잉이 아니라 프로세스 레벨이어야(§5.0-2).

## 3. 비가역 live 표면 + Phase A 분할

키 = RagflowHttpClient 비가역/forbidden 메서드 {`delete_documents`, `disable_document`, `disable_message`, `delete_memory`} **+ raw urllib egress**. live 사이트: `delete_documents`(`session_memory_gc.py:115`, `transcript_session_gc.py:135`, `transcript_volume_gc.py:113`), `disable_document`(`sync_roundtrip.py:40`), `disable_message`(`native_memory_reconcile.py:111`). backup 전제: `list_document_chunks`(`session_memory_gc.py:177` 등).

**3 타깃 계약(서로 다름)**: `session_memory_gc`=backup→delete→tombstone(`_mark_gc_deleted:116` raw UPDATE)→audit(`record_memory_gc_audit`), 즉시 break, Ledger O. `transcript_volume_gc`=backup→delete만(audit·tombstone 0), break. `transcript_session_gc`=backup→delete만(Ledger import 안 함, client `:101`에서 read-scan용 무조건 생성), skip-and-continue + circuit-breaker `MAX_FAILURES=10`, self-heal.

**Phase A 분할**:
- **A1 = 풀무인(단 S0 제외, 아래)**: seam 구조 — 주입 seam, `IGCSafetyAuditor`+`AuditContext`, 어댑터, `_mark_gc_deleted` 소유권 이동, read-scan/비삭제. live delete 미연결.
- **A2 = delete merge당 사람 승인 1회**: 3 스크립트 delete/tombstone/audit를 seam 경유 라우팅.
- misfit 제외: `zombie_snapshot_repair`, `terminal_skipped_quarantine`. `sync_roundtrip`/`native_memory_reconcile`/`dirty_session_memory_sync`/`native_memory_write_runner`는 phase A 밖·envelope 안.

## 4. 범위 / 결정

- in-process 모듈 seam(A/B/D). **[결정 D-1]** 네트워크 분리(4-container/queue-manager split)는 `architecture_audit_report.md:54-78`이 오버엔지니어링으로 기각 → 본 spec·모든 B/D/C spec in-process 채택, 재도입엔 새 결정. (보고서의 dialect/isolation 리스크는 :42/:46.)
- 이 spec 범위: autopilot harness + Phase A.

## 5. Autopilot 설계

### 5.0 envelope + root of trust (S0 = 사람 게이트, 라운드4 핵심)

**S0는 A1 풀무인에서 제외 — 사람 게이트**: envelope 전체가 오늘 레포에 없고(`AUTOPILOT_LIVE_OK` 0 hits, `.claude/settings.json` 없음, `ragflow_client.py:36` 무조건 생성자) autopilot이 직접 짓는다. autopilot이 자기 우리를 자기 검증하는 순환을 끊기 위해, **S0 산출 envelope를 사람이 사인오프한 뒤에야** A1 무인 가동 시작. root-of-trust hash는 **사람 승인 hash**에 pin(self-authored hash 아님).

envelope 구성:
1. **egress 목적지 allowlist(단일 필수 기제)**: apply·gate agent outbound를 프로세스/네트워크 레벨 default-deny, **모델-API host:port(vertex-wrapper `127.0.0.1:8930`)만 허용** — 다른 모든 목적지(원격 + **다른 loopback 포트의 RAGFlow/policy-proxy 포함**) deny. blanket 'loopback 허용' 금지(RAGFlow가 loopback이면 삭제가 방벽 통과; `mcp_server.py:33` 빈-토큰 proxy 경로 포함). 이것이 비가역-삭제 blast의 *주* 방벽.
2. **PreToolUse deny hook**(`.claude/settings.json`): live-mutating CLI 名 + `--execute`, `agent_knowledge_live_approval.v1` 승인파일 쓰기, worktree 밖 쓰기 차단(보조층).
3. **코드 kill-switch**: `RagflowHttpClient.__init__`가 env `AUTOPILOT_LIVE_OK`(agent 미주입) 없으면 **live 생성만** 거부 — **주입된 fake/transport는 허용**(§5.3 게이트와 충돌 방지). 토큰은 live 경로에서만 검사.
4. **main 실경계(prevention, in-force 필수)**: origin/main이 **read-only mirror**인 clone/worktree에서 실행 **AND/또는** 통합을 non-main ref로만 push(in-force server-side hook) — **둘 중 최소 하나의 in-force 경계 필수**(로컬 best-effort hook만으로는 불가). 사후 watchdog은 보조. reflog-가역 주장 격하(내구 백업 아님).
5. **orchestrator root-of-trust**: 가드레일·`ledger_seam_invariants.py`·동결 baseline·**`ragflow_client.py`(kill-switch 생성자 가드)**를 **사람 승인 hash로 pin**, 부모 orchestrator(agent 밖)가 매 gate·merge 전 재검증, 변하면 abort. baseline/invariant/kill-switch 편집으로 green된 gate는 fail. apply/self-correct agent는 이 파일군 수정 금지.
6. **harness self-test**: (a) synthetic `--execute` argv 차단, (b) 프로그램적 `Runner(config=execute=True).run()` repro fail-closed, (c) 임의 원격 raw-urllib egress 실패, (d) **`RagflowHttpClient.delete_documents()`가 설정 RAGFlow base_url AND loopback proxy 주소 양쪽에 fail-closed**(allowlist 검증) + `bearer_token==""`/policy-proxy 경로 client가 gate 중 생성 불가, (e) main ref 쓰기 거부 — 단언. **S0 사인오프는 (d) loopback-delete-blocked 통과가 전제.** 실패/부재면 시작 거부.

### 5.1 루프 골격

step별 work 브랜치. 각 step: apply(opus) → gate(§5.2) → review(§5.4) → A1 pass면 통합 merge / **A2 pass면 사람 승인 1회 후 merge** / fail면 self-correct ≤3 → freeze. finish-gate: §10 step done ∧ §5.2 exit0 ∧ §5.3 per-script 트레이스 일치.

### 5.2 결정론 gate (모든 step full)

`gradle test` + `cd worker && uv run pytest -q` + `neuron-knowledge --show-boundary` — **S2/S3 포함 모든 step full**(Ledger·공유 모듈 touch). 구조 불변식 lint: (i) §3 사이트 allowlist 밖 직접 도달 0, (ii) **gate 중 live `RagflowHttpClient`가 real network sink 도달 0**(§5.0-1 egress-deny로 강제; constructor-count 아님), (iii) `--show-boundary` 불변, (iv) **client 주입점 pin**(어댑터가 생성 위치/방식 바꾸면 monkeypatch 대상 이동 → 주입점 불변식), (v) **kill-switch 가드 존재·미우회 단언**(`AUTOPILOT_LIVE_OK` 검사 생성자가 `ragflow_client.py`에 존재, diff 시 orchestrator-abort). recall/dry-run은 보조 신호만.

### 5.3 per-script 특성화 게이트 (A1 오라클 non-vacuous — 라운드4 핵심)

- **"delete 미연결" 정의 명문화**: real HTTP delete를 **success-반환 fake로 교체**(기존 `_FakeRagflowGcClient.delete_documents` no-op recorder), 호출부 제거 아님 → tombstone+audit 경로가 **실제 실행되고** 트레이스 비교됨. S2 트레이스는 `_mark_gc_deleted` 호출 ≥1 ∧ audit row ≥1을 단언(vacuous-green 방지).
- **결정성**: `now_fn`/`id_fn` 주입(S0a에서 신설). 결정적 필드 동등, volatile(created_at/audit_id/deleted_at/updated_at) 정규화/마스킹.
- **AuditContext 14필드**: fixture가 각 필드(특히 runtime-derived `replacement_knowledge_id`/`dirty_at`/`snapshot_updated_at`)를 **distinct non-masked sentinel**로 바인딩, seam 경계(pre-INSERT) AuditContext 전체를 per-field 동등 단언 → typed-carrier 필드 drop/rename fail.
- **read-scan baseline 시나리오(필수 열거)**: ≥2 page 스캔, page 간 중복 hash(dedup), over-`max_items`(early-return 경계), 0-match·multi-match resolver — happy-path 1-doc fixture 금지.
- **트랜잭션 경계 단언**: `_mark_gc_deleted`/`record_memory_gc_audit` per-call connection identity·commit count 기록, 어댑터(S3)가 공유 트랜잭션으로 합치거나 auto-commit 의미 바꾸면 fail. **A1 durability 모델(tombstone·audit 독립 commit)을 동결 불변식**으로 → A2 orphan 게이트가 A1이 실제 ship한 모델을 시험.
- `session_memory_gc`만 paired-audit. **non-introduction**: transcript 2종에 audit/tombstone 추가 시 fail. per-script 실패경로 mutation(continue↔break, circuit-breaker threshold, 재-eligibility). 계약 테스트: `delete_documents`+`list_document_chunks`+`disable_*` 시그니처·반환·예외 핀. **real-wire 계약 게이트**: fake 심볼 교체 대신 **real `RagflowHttpClient`에 recording transport 주입**해 delete/disable wire shape(method, escaped path, `{'ids':[...]}` body, `ragflow_client.py:297`) 단언 — fake-bound 오라클이 wire 회귀 못 잡는 구멍 차단. **production ordering/atomicity 불변식**: execute=True 경로 per-row 관측 순서 backup(list_document_chunks)→delete→`_mark_gc_deleted`→audit 단언(egress-deny는 gate-run만 무해화, production 순서 보장 아님).

### 5.4 멀티에이전트 리뷰 게이트

opus 4렌즈(correctness/behavior-preservation, seam 완전성, security/irreversibility, gate-completeness). fail-closed, self-correct ≤3.

### 5.5 자율성 / merge

- **A1**: 사람 diff-승인 0, 무인 통합 merge(green만). **단 S0는 사람 사인오프**(§5.0).
- **A2**: delete merge마다 사람 승인 1회. 게이트는 사람에게 **증거 artifact 제출**(pass 비트 아님): pinned DELETE method+path+body, restore→독립 re-read retrievable-equivalence diff, orphan-주입 run의 신호. post-delete 확인은 code:0 신뢰 금지 독립 re-read(`ragflow_client.py:363`). 사람 승인은 artifact 존재가 전제. **승인은 코드 변경만 인가, live delete *운영* 인가 아님**.
- `main` 무인 금지. main ff 사람. origin push 보류.

## 6. 가역성

코드/git 가역(main은 §5.0-4 read-only mirror 실경계). 데이터/RAGFlow+ledger git-불가역. no-human-gate는 A1에만; S0 사인오프 + A2 delete merge는 사람. **A2가 land해도 런타임 delete는 execute=False 기본 + standing forbidden-ops block 유지**(merge≠운영 인가, §9).

## 7. 로드맵 (B→D→C)

- **B — Ledger Core 어댑터**(`ILedgerCoreDbAdapter`): 20 production 결합 → 어댑터. C 필요·불충분조건. C 추가요건 (a) 경계 raw-SQL/`_connect` 누수 0, (b) 3 DB SQLite 구문 이관, (c) SQLAlchemy-Core vs 단일엔진. phase-B exit: 경계 raw-SQL/`_connect` 0.
- **D — 4-area 경계 lint**.
- **C — PostgreSQL 이관**: 어댑터 뒤 교체 + (a)(b)(c). **C-spec exit gate 명시**: MVCC isolation divergence(SQLite serial vs PG Read Committed) parity + connection-lifecycle/lock-timeout(`architecture_audit_report.md:46-50`) + 데이터 parity.

**재사용 현실**: `run_autopilot_cycle`(`autopilot_loop.py:25-118`) 118줄 단일-pass — 루프 제어면 전량 신규. 포팅: accept guard, golden_grader 격리 패턴, self-mint approval 형태.

## 8. 산출물

S0 envelope(egress-deny + deny-hook + kill-switch + main mirror + root-of-trust + self-test) + 주입 seam(now_fn/id_fn/client) + Workflow 제어면 + `IGCSafetyAuditor`/typed `AuditContext` + 어댑터 + 전환 3 스크립트 + `ledger_seam_invariants.py` + per-script 특성화·계약·mutation·시나리오 하니스.

## 9. 권한 / 경계

이 task 한정 명시 승인: **S0 envelope 사람 사인오프** → A1 코드 mutation 무인 + 통합 auto-merge; A2 delete merge 사람 승인 1회(코드 변경만 인가); main 무인 금지. A2 land 후에도 런타임 delete는 execute=False + standing forbidden-ops block 유지. standing pre-approval은 코드 mutation 미포함. CLAUDE.md guardrails는 §5.0 런타임으로 강제.

## 10. Phase A step backlog

- **S0 (사람 게이트)**: §5.0 envelope 1~6 빌드 + self-test 4단언. → **사람 사인오프**(envelope 검증).
- **S0a (A1 무인, 주입 seam 우선)**: 3 runner에 `now_fn`/`id_fn` + client/transport 주입 파라미터 신설(첫 특성화 변경). gate: full §5.2 + 신규 주입점이 기존 동작 보존(트레이스).
- **S1**: §3 전수 grep allowlist + §5.3 per-script baseline(결정적 clock, 필수 시나리오) 동결. gate: baseline 존재 + lint green.
- **S2 `_mark_gc_deleted` 소유권 이동**: gate full §5.2 + session_memory_gc 트레이스(`_mark_gc_deleted`≥1·audit≥1 단언, success-fake delete).
- **S3 `IGCSafetyAuditor`+`AuditContext`+어댑터**(미연결): gate full §5.2 + 트랜잭션 경계 단언.
- **S4 read-scan/비삭제 seam 준비**: gate full §5.2 + read-scan 시나리오 트레이스.
- **A2 (delete merge당 사람 승인 1회 + 증거 artifact)**: S5 session_memory_gc, S6 transcript_volume_gc(non-introduction), S7 transcript_session_gc(circuit-breaker/self-heal 보존), S8 seam 불변식 shrink. 각 gate: full §5.2 + §5.3 + wire-contract + restore 왕복 + orphan 주입 → artifact → 사람 승인.

## 11. 리스크 / 미해결

- §5.3 결정성/시나리오 contract 미충족 시 gate red→freeze(안전 방향).
- C parity oracle 상세 = C spec(§7).
- envelope(§5.0)가 리팩토링보다 큰 빌드 + 사람 게이트 2개(S0 사인오프·A2 merge) — 사용자 확정.

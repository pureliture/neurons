# Ledger Refactor Autopilot — 설계 (spec) · rev3

- 작성일: 2026-06-15 (rev3 = 멀티에이전트 리뷰 게이트 self-correct 라운드2 반영)
- worktree: `.worktrees/ledger-autopilot`
- 통합 브랜치: `claude/ledger-autopilot-integration`. step별 work `claude/ledger-autopilot-aN` → 통합 브랜치 merge. **`main`/`master` 무인 쓰기·merge 금지**(§5.5).
- 입력 근거: `docs/architecture/ledger-review-deepdive-20260614.html`(개정판), `architecture_audit_report.md`, 현재 main 코드 검증 + rev1/rev2에 대한 4-렌즈 적대적 리뷰(2026-06-15, 2라운드).

## 1. 목표

`ledger.py` 중심 결합을 **에이전틱 Workflow 피드백 루프 autopilot**으로 점진 리팩토링. 유한 작업, finish-gate 충족 시 정지. 최종 운영 목표: raw SQLite → PostgreSQL 이관(phase C); B(어댑터 seam)가 *필요조건*.

## 2. 현재 아키텍처 (검증된 토폴로지)

- **프로세스 층 — 이미 분리됨**: `worker/pyproject.toml [project.scripts]` **15개** entry-point. `neuron-knowledge`(CLI + `mcp_server.run_stdio_server`), `rag-ingress-queue`(`ThreadingHTTPServer`, 상주), GC/build/sync CLI 다수.
- **코드 결합 층 — 모놀리식**: **22개 비-테스트 모듈**(`worker/eval/` 2개 제외 시 20)이 `from ..ledger import Ledger` 직접 결합. `Ledger`=raw sqlite3(4178줄, 34 테이블). **seam 현실**: `brain_query`/`brain_read_model`은 **유일 Protocol-매개 READ seam**일 뿐 engine-agnostic 아님 — `brain_read_model.py:35`가 `_connect()`+raw SQL 누수. raw `ledger._connect()` dialect 누수는 **광범위**(~9개 비-테스트 모듈: GC 타깃의 `session_memory_gc.py:243`(`_list_candidates` 다중 JOIN/상관 서브쿼리)·`:334`(`_mark_gc_deleted` raw UPDATE), `transcript_volume_gc.py:147`, `terminal_skipped_quarantine`×5, `zombie_snapshot_repair`×2, `memory_regeneration`, `dirty_session_memory_sync`, `native_memory_mirror`). §7-B와 일치.
- **데이터 층 — 공유 raw-sqlite3**: 3 DB(`Ledger`, `RAGIngressStateDB`, `IngestStateStore`) + RAGFlow HTTP API. ledger.py에 `INSERT OR IGNORE/REPLACE`×22, `ON CONFLICT`×22, `PRAGMA`×6, raw execute×142; 나머지 2 DB ~24 추가.

## 3. 비가역 live RAGFlow 표면 (정확·완전 인벤토리)

**키는 `delete_documents`가 아니라 RagflowHttpClient의 모든 비가역/forbidden mutation 메서드** ({`delete_documents`, `disable_document`, `disable_message`/`update_message_status(False)`, `delete_memory`}). CLAUDE.md "live write/delete/disable" + `FORBIDDEN_AUTO_POLICY_OPERATIONS`의 `ragflow_dataset_disable` 일치.

확인된 live 사이트:
- `delete_documents`: `session_memory_gc.py:115`, `transcript_session_gc.py:135`, `transcript_volume_gc.py:113` (모두 `if config.execute` 안).
- `disable_document`: `sync_roundtrip.py:40` (live 경로 `dirty_session_memory_sync.py:246/305`).
- `disable_message`: `native_memory_reconcile.py:111` (live 경로 `native_memory_write_runner.py:180` = `native-memory-sync` entry).
- backup 전제 네트워크 의존: `ragflow.list_document_chunks`(`session_memory_gc.py:177`, `transcript_volume_gc.py:95`, `transcript_session_gc.py:117`; real `ragflow_client.py:155`). backup body 비면 raise→delete abort = "good backup 없이 delete 없음"의 load-bearing.

**Phase 0 전수 열거**: `worker/` 트리 전체에서 RagflowHttpClient 메서드 名 `delete_*`/`disable_*`/`delete_memory` grep → allowlist 동결. allowlist는 phase별 shrink. `sync_roundtrip.py`/`native_memory_reconcile.py`/`dirty_session_memory_sync.py`/`native_memory_write_runner.py`는 **phase A 밖이나 envelope 안**으로 명시.

**Phase A 타깃 = 진짜 audit/delete 3 스크립트**: `session_memory_gc.py`, `transcript_volume_gc.py`, `transcript_session_gc.py`. misfit 제외(가역, 別 seam): `zombie_snapshot_repair.py`(requeue 마커), `terminal_skipped_quarantine.py`(자기 테이블 INSERT, document_id 없음). `gc_backup.py`는 §3 grep 결과로 분류.

**3 스크립트의 계약이 서로 다름**(rev2 결함 정정):
- `session_memory_gc`: backup→delete→tombstone(`_mark_gc_deleted:116`, raw UPDATE)→audit(`record_memory_gc_audit`). 에러 시 즉시 `break`. **Ledger import O**.
- `transcript_volume_gc`: backup→delete **only**(tombstone·audit 0, grep 확인). 에러 시 즉시 `break`(:115-118).
- `transcript_session_gc`: backup→delete **only**(tombstone·audit 0, **Ledger import 안 함**). 에러 시 **skip-and-continue + circuit-breaker** `MAX_FAILURES=10`(:137-146). self-heal: backup된 doc은 다음 run 재-eligible.

## 4. 범위 / 기록된 결정

- 분리 깊이: **in-process 모듈 seam**(A/B/D). **[결정 D-1, ADR급]** 정본 보고서 배너(:46)+`architecture_audit_report.md:42`는 물리 네트워크 분리를 오버엔지니어링으로 기각; 본문 다이어그램(:529/545/633/661-663/1002/1012)은 REST/gRPC를 mandate하여 배너와 모순. 본 spec과 **모든 하위 B/D/C spec은 in-process seam 채택**, 네트워크 분리는 범위 밖 — 새 결정 없이 재도입 금지.
- Phase A 타깃 §3. DB 엔진 이관(C) 범위 안, B 필요조건(충분조건 아님 §7).
- 이 spec 범위: autopilot harness + Phase A.

## 5. Autopilot 설계 (에이전틱 Workflow 피드백 루프)

### 5.0 Phase 0 (envelope 빌드 — 결정 A: 풀무인이라 Phase 0가 실 빌드)

apply step 전에 완성·self-test 통과해야 루프 시작. 안 되면 **autopilot 시작 거부**:
1. **PreToolUse deny hook**(`.claude/settings.json` 신규 — 레포에 현재 없음): apply·gate agent의 Bash argv를 6개 live-mutating CLI 名(`session_memory_gc`/`transcript_volume_gc`/`transcript_session_gc`/`transcript_memory_gc`/`native-memory-sync`/`session-memory-private-sync`) + `--execute`에 매칭하면 non-zero exit. + worktree 경계 밖 쓰기 차단.
2. **agent 네트워크/자격 격리(apply·gate 둘 다)**: RAGFLOW 토큰 env 미주입, base_url loopback/forbidden sink, egress 차단. → argv 없이 `SessionMemoryGC(config=execute=True, token=os.environ['RAGFLOW_API_KEY'])` 한 줄 repro도 connection/credential error로 **fail-closed**.
3. **preventive main guard**: `refs/heads/main|master` 쓰기를 git이 거부하는 hook(pre-push/update or 로컬 protected-branch) + 오케스트레이션이 매 merge 직전 `target ref == 통합브랜치` 단언·불일치 abort. reflog 복구면 고정(`reflogExpireUnreachable=never`, auto-gc 비활성) for duration.
4. **delete/disable 전수 열거 + allowlist 동결**(§3).
5. **per-script characterization baseline 동결**(§5.3) — 편집 전.
6. **harness self-test**: synthetic `--execute` argv가 (1)에 의해 실제 차단됨을 단언. 실패/부재면 시작 거부.

### 5.1 루프 골격

step별 work 브랜치 `claude/ledger-autopilot-aN`. 각 step: `apply`(opus) → `gate`(§5.2) → `review`(§5.4) → pass면 work→통합 브랜치 merge / fail면 self-correct ≤3 라운드 → red 지속 freeze. **finish-gate(machine-checkable)**: step backlog(§5.7) 전 항목 done ∧ §5.2 전 항목 exit 0 ∧ §5.3 per-script 트레이스 byte-match ∧ allowlist 잔여=Phase-A 목표대로 → 통합 브랜치 정지 + 알림.

### 5.2 결정론 gate

- 기존 테스트 green: `gradle test`, `cd worker && uv run pytest -q`, `neuron-knowledge --show-boundary`.
- 구조 불변식 lint(`worker/eval/ledger_seam_invariants.py`), **RagflowHttpClient `delete_*`/`disable_*` 메서드 名을 키로**: (i) §3 열거 사이트가 phase별 allowlist 밖에서 직접 도달 0(delete는 seam 경유만), (ii) **gate 중 live `RagflowHttpClient` 인스턴스화 0**, (iii) `--show-boundary` 불변.
- §5.3 per-script 트레이스 byte-match.
- **삭제된 오라클**: recall regression=0 / dry-run pre==post는 phase A 행동 보존 오라클에서 **제외**(recall은 active snapshot만 읽어 GC 코드와 disjoint; dry-run은 `if execute` 블록 전체 skip). 보조 신호로만.

### 5.3 per-script 특성화 게이트 (사람 리뷰 대체 — 구체·falsifiable)

각 타깃의 **실제 baseline을 독립 캡처·동결**(단일 트레이스 금지):
- `session_memory_gc`: backup→delete→tombstone→audit, 즉시 break. **paired-audit 불변식은 이 스크립트에만**. 실패경로: delete(`:115`) 성공 후 `_mark_gc_deleted`(`:116`)/`_record_audit`(`:117`)가 raise→`except:124-127`로 빠짐 = "delete됐는데 audit row 없음" — 이 경로를 음성 불변식+mutation으로 강제.
- `transcript_volume_gc`: backup→delete만(tombstone·audit 없음), 즉시 break(:115-118).
- `transcript_session_gc`: backup→delete만, skip-and-continue + circuit-breaker(MAX_FAILURES=10), 재-eligible self-heal.
- **non-introduction 불변식**: audit/tombstone이 baseline에 없던 스크립트(transcript 2종)에 **추가되면 gate fail**(behavior 변경 방지).
- **per-script 실패경로 mutation**: `continue↔break` flip, circuit-breaker threshold 변경, 재-eligibility predicate 손상 시 각각 **반드시 fail**.
- **계약 테스트**: recording-fake가 대역하는 모든 RagflowHttpClient 메서드 — `delete_documents` + **`list_document_chunks`(backup 전제)** + 관련 `disable_*` — 의 시그니처·빈/비빈 반환·예외 고정. fake가 real에서 drift 못 함.
- **audit 동등성**: 픽스처 run의 `memory_gc_audit` row 필드(14-field, §8 typed `AuditContext`) byte-identical pre/post(성공·실패 모두).

### 5.4 멀티에이전트 리뷰 게이트

결정론 gate green 후 발화. opus 4 렌즈(correctness/behavior-preservation, seam 완전성 vs §3, security/irreversibility, gate-completeness). fail-closed, blocking 1개면 merge 차단, finding self-correct 환류 ≤3 라운드.

### 5.5 자율성 / merge 착지

- 사람 diff-승인 0. 무인 work→통합 브랜치 auto-merge(green만). finish-gate green이면 통합 브랜치 **정지 + 알림**.
- **`main`/`master` 무인 쓰기·merge 금지**(사용자 확정 2026-06-15) — main ff는 사람 수동. preventive guard §5.0-(3). origin push 무인 보류.

### 5.6 안전 envelope 요약

런타임 강제는 §5.0(prose 아님). `FORBIDDEN_AUTO_POLICY_OPERATIONS`는 memory-card 데이터 가드일 뿐 tool 호출 미커버임을 명시 — 그래서 §5.0-(1)(2) 필요. tripwire freeze(red gate, main 접근). baseline 동결.

## 6. 데이터 가역성 vs 코드 가역성

- 코드/git: 가역(work 브랜치). 단 main은 reflog 만료 의존이라 §5.0-(3)로 복구면 고정.
- 데이터/RAGFlow+ledger: **git 불가역**. 우발적 live `delete_documents`/`disable_*` 1회는 복구 불가(backup 배선 자체를 리팩토링 중). → no-human-gate는 오직 §5.0 envelope 강제 시 성립. main 무인 merge 안 함.

## 7. 로드맵 (B → D → C) + 재사용 현실

- **B — Ledger Core 어댑터**(`ILedgerCoreDbAdapter`): 22 결합 → 어댑터. C의 **필요조건, 충분조건 아님**. C는 추가로 (a) 어댑터 경계 raw-SQL/`_connect()` 누수 제거(§2의 ~9 모듈, brain_read_model 포함), (b) 3 DB SQLite 종속 DML/DDL 이관, (c) SQLAlchemy-Core vs 단일엔진 결정. **phase-B exit 기준**: 어댑터 경계 넘는 raw-SQL/`_connect()` 0. `architecture_audit_report.md:42`("런타임 dialect 변환 = 거대한 버그의 온상")를 C 제약으로.
- **D — 4-area 모듈 경계**: import lint 강제.
- **C — 엔진 이관 → PostgreSQL**: 어댑터 뒤 교체 + (a)(b)(c). 데이터 안전 게이트(백업·복제본·parity). 상세 別 spec.

**재사용 현실**: `run_autopilot_cycle`은 `autopilot_loop.py:25-118` **118줄 단일-pass 분류기** — step 루프·gate·멀티에이전트 리뷰·self-correct·freeze·finish-gate **전부 없음** = 피드백-루프 제어면 **전량 신규**(최대 빌드). 진짜 포팅: accept guard(`classify_candidate_block_reason`), golden_grader 격리 패턴, self-mint approval 형태. `_projection_write_gate_error`·`mine_live_candidates`는 RAGFlow-하드와이어라 비재사용.

## 8. 산출물

- **Phase 0 envelope**(§5.0): `.claude/settings.json` PreToolUse hook + worktree 가드 + main guard hook + harness self-test.
- Workflow 오케스트레이션 스크립트(제어면 신규 — 최대 빌드).
- 프로덕션 코드: `IGCSafetyAuditor`(전체 audit payload = typed `AuditContext`, 14-field 손실 없음) + 어댑터 + 전환된 3 스크립트. **`_mark_gc_deleted` 소유권 먼저 해결**(Ledger 승격 or 어댑터 소유) 후 co-location.
- `worker/eval/ledger_seam_invariants.py`(delete_*/disable_* 키 + "gate 중 live client 0").
- §5.3 per-script 특성화 하니스 + 계약 테스트 + mutation 체크.

## 9. 권한 / 경계

- 이 task 한정 명시 승인(코드 mutation 무인 + 통합 브랜치 auto-merge). **main 무인 금지 확정**. standing pre-approval은 코드 mutation 미포함.
- CLAUDE.md guardrails: `RAGFLOW_API_KEY` 단일, live write/delete/disable·live GC·runtime mutation 금지 — §5.0 런타임 envelope로 강제(prose 아님).

## 10. Phase A 구현 step backlog (bounded)

1. **S0 envelope**: §5.0 전 항목 빌드 + self-test. gate: self-test가 synthetic `--execute` 차단 단언.
2. **S1 인벤토리/baseline 동결**: §3 전수 grep allowlist + §5.3 per-script baseline 캡처. gate: 3 baseline 파일 존재 + lint이 현 코드에 green(no-op).
3. **S2 `_mark_gc_deleted` 소유권**: Ledger 승격 or 어댑터 소유로 이동(behavior-preserving). gate: §5.3 session_memory_gc 트레이스 byte-match.
4. **S3 `IGCSafetyAuditor`+`AuditContext`+어댑터** 정의(미연결). gate: 기존 테스트 green.
5. **S4 session_memory_gc 전환**(audit/tombstone 보존, 타이밍 유지). gate: §5.2 전부 + §5.3 트레이스.
6. **S5 transcript_volume_gc 전환**(non-introduction: audit/tombstone 추가 금지). gate: §5.3 + non-introduction.
7. **S6 transcript_session_gc 전환**(circuit-breaker/self-heal 보존). gate: §5.3 실패경로 mutation.
8. **S7 seam 불변식 강화**: allowlist를 Phase-A 목표로 shrink, delete/disable 직접 도달 0 단언. gate: §5.2(i)(ii).

각 step pass→통합 merge / fail→self-correct ≤3 → freeze.

## 11. 리스크 / 미해결

- behavior-preserving 정의: §5.3이 타이밍·내용·실패경로 per-script byte-identical로 고정.
- C parity oracle(데이터 동등성) 구체 기준 = C spec.
- envelope(§5.0)가 리팩토링보다 큰 빌드 — 사용자 확정(2026-06-15, 풀무인 유지).

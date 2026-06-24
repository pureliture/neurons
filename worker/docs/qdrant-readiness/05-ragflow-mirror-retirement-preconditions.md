# 05. RAGFlow 벡터 미러 은퇴(M9/M10) 전제조건 — 삭제 가능 판정

이 문서는 "RAGFlow는 이제 안 쓰고 데이터도 불필요하니 지워도 되지 않나?"라는 질문에
**증거 기반으로 답하는 dry-run 게이트**다. 결론부터: **아직 아니다.** 라이브 RAGFlow
delete/disable은 비가역이므로, 아래 blocker가 전부 해소되고 per-action 증거가 갖춰진
뒤 operator가 호스트에서 실행한다. 이 세션/오토파일럿은 라이브 delete/disable을
실행하지 않는다.

## A. "RAGFlow 안 쓴다"가 아직 거짓인 이유 (audit 근거)

recall은 ledger-first로 옮겨졌지만(문서 02 §0), RAGFlow에는 여전히 **라이브 writer**와
**fallback 없는 reader**가 붙어 있다(문서 01 참조). 이들이 먼저 이전/은퇴되기 전에는
"불필요"가 아니다.

### A-1. 아직 RAGFlow에 쓰는 라이브 writer
- `dirty_session_memory_sync` → `SessionMemoryRegenerationRunner`(sync): 상시 라이브
  session-memory upload.
- `ragflow_projection`: autopilot MemoryCard projection(`--allow-write`).
- (delivery) `shadow_worker`(SHADOW_DELIVER=1) RAGFlow sink.

### A-2. fallback 없는 reader (끄면 깨지거나 정지)
- autopilot live mining: session/transcript-memory를 RAGFlow에서만 읽음.
- `ragflow_read_sot` session-memory build.
- GC 러너 3종(session/transcript-session/transcript-volume): 후보 열거를 RAGFlow에서.
- transcript-backfill seed, supersede 벡터 후보, native-memory reconcile, brain.query
  archive/evidence lane, status reconciler.

> 이 목록이 비어야("전부 CouchDB/Qdrant/ledger로 이전 또는 은퇴") RAGFlow 벡터 미러가
> "불필요"가 된다. 코드 경로 기준이며, 호스트에서 실제 활성 여부(예 RAGFLOW_DELIVERY
> 설정)는 라이브 점검으로 별도 확인한다.

## B. 삭제 전 blocker 체크리스트 (전부 충족돼야 M10 진입)

- [ ] A-1 writer가 RAGFlow로 더는 쓰지 않음(write cutover, M9). 신규 ingest는 Qdrant
      미러 + canonical(CouchDB/ledger)로만.
- [ ] A-2 reader 전부 이전/은퇴(각 소비자별 source 전환 test).
- [ ] read 미러 lane이 Qdrant로 전환되고 recall regression 0(M8).
- [ ] recall parity: recall@k 목표 + exact mismatch 0, soak window 연속 green(M7
      harness 산출 evidence).
- [ ] Qdrant GC hard-delete chokepoint 연결 + dry-run/coverage proof(M9).

## C. 비가역 delete/disable per-action 증거 (M10, 호스트 operator)

AGENTS.md GC 규약에 따라 **각 건마다 분리 보고**:

- dry-run evidence(무엇을, 몇 건, 어느 dataset에서 지우는지 — raw id 미출력).
- coverage proof(지울 문서가 다른 곳(CouchDB/ledger/Qdrant)에 보존됨).
- retention/stability window(즉시 hard delete 금지; disable 후 보존 기간).
- backup/rollback evidence(durable 백업 — transcript-memory 70k 백업 선례, RC5).
- recall regression gate(삭제 후 recall 무회귀 측정).
- operator approval 기록(exact argv, bounded timeout, redaction, postcheck, abort 기준).

## D. 가역/비가역 경계

- **가역**: M6 dual-write(추가형, RAGFlow 무영향), M8 read 재배선(rollback manifest로
  복원). 이 단계까지는 끄면 원복.
- **비가역**: M10 hard delete. backup 없으면 절대 금지. disable(가시성 차단)까지는
  re-enable로 복귀 가능하지만 hard delete는 백업에서만 복구.

## E. 이 세션/오토파일럿의 한계 (명시)

- 라이브 RAGFlow delete/disable/GC execute, `compose up`, 호스트 mutation은 이 세션이
  실행하지 않는다(Mac thin-client + forbidden-op 가드 + 비가역).
- 여기서 제공하는 건 **전제조건·게이트·증거 요구사항(code-only 문서)**이다. 실제
  실행은 Ubuntu 호스트에서 위 증거를 갖춰 operator가 단계별로.

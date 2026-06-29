# Recall Cutover Requirements (transcript-memory → session-memory)

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 진행 방식: 자문자답(self-Q&A) grilling. 사용자 위임으로 자체 도출 후
  `/agentic-execution`으로 실행한다.

## 배경 (라이브 증거)

- 실행 중 recall은 `neuron-knowledge mcp-stdio --retired-index-bridge-direct-recall
  --dataset-id <transcript-memory>` 로 **transcript-memory(cca3d0)를 직접** 읽는다.
- session-memory(f6c55b, 6668 docs)는 RetiredIndexBridge retrieval에서 대표 쿼리 4개 모두
  **0 hits** (현재 recall surface로 동작 안 함).
- CouchDB transcript-source에는 3567 세션(clean 3326 + index_fallback 241)이
  적재·검증 완료. 14 세션은 leak-blocked로 저장 불가(백업 전용).
- transcript-memory write(delivery)는 `RETIRED_INDEX_BRIDGE_DELIVERY_ENABLED=true`로 활성.
- 결론: transcript-memory를 안전하게 삭제(은퇴)하려면, recall을 **동작하는
  session-memory surface로 컷오버**하는 것이 선결이다.

## 질문-답변 흐름 (자문자답)

### Q1: 컷오버 후 normal recall의 권위 surface는 무엇인가?

RetiredIndexBridge `session-memory`(f6c55b)다.

- design.md 계약: RetiredIndexBridge `session-memory`가 brain-facing recall surface이고
  CouchDB는 source/evidence 전용이다. normal recall은 CouchDB evidence fetch에
  의존하지 않아야 한다.
- 따라서 CouchDB-direct recall이나 transcript-memory 유지가 아니라, CouchDB
  source에서 **derived session-memory**를 RetiredIndexBridge에 구축해 recall을 받는다.

### Q2: session-memory가 지금 0 hits인 원인 가설과, 컷오버가 보장해야 하는 상태는?

원인은 session-memory가 CouchDB 3567 세션에 대해 **충분히 materialize·parse되어
있지 않기 때문**으로 본다(기존 6668 docs는 구 파이프라인 파생물, 신규 CouchDB
source 미반영 + retrieval 미동작).

- 컷오버 done 상태: session-memory가 CouchDB source(conversation + tool evidence
  요약)에서 재생성되어 **RetiredIndexBridge retrieval로 hit가 나오는** 상태여야 한다.
- 단일 docs 수가 아니라 recall smoke 통과가 기준이다.

### Q3: recall 품질 성공 기준(no-regression)은 어떻게 정의하나?

`recall smoke`: 대표 쿼리 집합에 대해 session-memory가 transcript-memory 대비
**동등 수준의 관련 결과**를 반환한다.

- 대표 쿼리 집합(최소 8개): 최근/오래된 세션, 여러 provider/project를 섞어 구성.
- 통과 기준: 각 쿼리에서 session-memory hits ≥ 1 이고, 관련 top 결과의
  유사도가 transcript-memory 대비 합리적 범위(예: top similarity가 TM의 70%
  이상이거나 사람이 관련성 확인). 단일 검증이 아니라 쿼리 셋 전체로 판정한다.
- 회귀가 보이면 컷오버/삭제를 중단하고 recall을 transcript-memory로 되돌린다.

### Q4: recall은 어떻게 재배선하나?

recall 진입점의 dataset 타깃을 transcript-memory → session-memory로 바꾼다.

- Mac 측: `mcp-stdio` recall 실행 인자/런치 설정의 `--dataset-id`를
  session-memory로 전환(또는 recall이 읽는 dataset 해석을 session-memory로).
- 서버 측: brain.query/recall이 dataset을 환경/설정에서 해석한다면 그 설정도
  session-memory로 정렬한다.
- 재배선 후 실제 실행 중 recall이 session-memory를 읽는지 증거로 확인한다.

### Q5: write(신규 ingest)는 어떻게 처리하나?

신규 transcript는 transcript-memory에 더 이상 write하지 않고 CouchDB source +
session-memory projection 경로로만 흐른다.

- `RETIRED_INDEX_BRIDGE_DELIVERY`의 transcript-memory 타깃을 비활성/제거한다.
- 신규 세션이 CouchDB로 들어가고 session-memory가 갱신되는지 확인한다(M4 shadow
  로직 재사용 가능).
- 최종 상태에서 transcript-memory 신규 write가 0이어야 한다.

### Q6: 삭제 전 백업과 롤백 기준은?

transcript-memory(70k docs, 14 leak-blocked 포함)를 삭제 전에 **durable backup**
한다.

- 백업은 rollback source이며, 14 leak-blocked 세션의 유일한 보존처다.
- 롤백 트리거: 컷오버 후 recall 회귀 발견 시. 삭제 전이면 recall을
  transcript-memory로 되돌리고, 삭제 후면 백업에서 복원한다.

### Q7: 삭제까지의 단계 순서(staged cutover)는?

다음 순서를 강제한다. 각 단계는 증거 게이트를 통과해야 다음으로 간다.

1. session-memory를 CouchDB source에서 재생성·parse.
2. recall smoke 통과(Q3).
3. recall 재배선(Q4) + 짧은 안정 window 관측(recall이 session-memory에서 정상).
4. 신규 write 컷오버(Q5).
5. transcript-memory 백업(Q6).
6. transcript-memory dataset 삭제(파괴적, 마지막).

3까지는 가역(recall 되돌리기), 6은 비가역(백업이 rollback).

### Q8: 범위 밖(YAGNI)은?

- redaction 로직 변경, 재migration, project-memory/procedural-memory dataset 변경,
  CouchDB-direct recall 경로 신설은 범위 밖.
- 14 leak-blocked 세션의 CouchDB 저장 시도는 범위 밖(안전상 불가, 백업만).

## 기능 요구사항

- CouchDB source(3567 세션)에서 RetiredIndexBridge `session-memory`를 재생성하고 retrieval
  가능 상태(parse 완료)로 만든다.
- recall smoke(대표 쿼리 ≥8)로 session-memory recall이 transcript-memory 대비
  무회귀임을 검증한다.
- 실행 중 recall 진입점을 transcript-memory → session-memory로 재배선한다.
- 신규 ingest의 transcript-memory write를 중단하고 CouchDB + session-memory로만
  흐르게 한다.
- 삭제 전 transcript-memory 전체를 durable backup한다(14 leak-blocked 포함).
- 위 게이트(재생성·smoke·재배선·안정관측·write컷오버·백업) 통과 후에만
  transcript-memory dataset을 삭제한다.
- 각 단계는 dry-run/증거/rollback 기준을 분리해 보고한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Recall no-regression | session-memory recall이 대표 쿼리 셋에서 transcript-memory 대비 동등 수준 |
| Reversibility | 삭제 전 단계는 가역(recall 되돌리기), 삭제는 백업으로 rollback |
| Safety | leak-blocked 본문은 CouchDB 저장 금지(fail-closed 유지), 백업으로만 보존 |
| Backup | 삭제 전 transcript-memory 70k docs 백업 필수(rollback 증거) |
| Staged order | 재생성→smoke→재배선→안정관측→write컷오버→백업→삭제 순서 강제 |
| Authority | normal recall은 session-memory만, CouchDB evidence fetch 비의존 |
| Idempotency | session-memory 재생성/projection은 결정적·재실행 안전 |
| Approval | 파괴적 삭제는 사전승인(오토파일럿) + 증거 분리 보고 후 실행 |

## 사용자 시나리오

- 운영자는 CouchDB source에서 session-memory를 재생성하고, recall smoke로
  session-memory가 transcript-memory만큼 회수하는지 확인한다.
- 운영자는 recall을 session-memory로 재배선하고 짧은 window 동안 정상 동작을
  관측한다.
- 운영자는 신규 ingest가 transcript-memory에 더는 쓰지 않음을 확인한다.
- 운영자는 transcript-memory를 백업한 뒤(14 leak-blocked 포함) dataset을
  삭제하고, recall은 session-memory에서 계속 동작한다.
- recall 회귀가 보이면 운영자는 recall을 되돌리거나 백업에서 복원한다.

## 미결정 항목

- session-memory 0-hits의 정확한 근인(미parse vs 내용 부재 vs 다른 recall 경로):
  Phase 2/실행 초기에 진단해 재생성 방식을 확정한다.
- recall smoke의 정량 임계(유사도 비율 vs 사람 확인)의 최종 수치.
- recall 재배선의 정확한 지점(Mac mcp-stdio 런치 설정 vs 서버 설정)의 운영 방식.

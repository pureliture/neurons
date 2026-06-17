# Recall Cutover Design Spec (transcript-memory → session-memory)

## Overview

RAGFlow `transcript-memory`를 은퇴(삭제)하기 위해, 라이브 recall을 CouchDB
source에서 재생성한 RAGFlow `session-memory` surface로 무회귀 컷오버한다. 데이터
이관/검증은 이미 완료(3567 세션); 본 spec은 recall surface 전환과 파괴적 삭제의
안전 순서를 다룬다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- 핵심: session-memory를 동작하는 recall surface로 만들고(재생성+parse), recall
  smoke 무회귀 검증, recall 재배선, write 컷오버, 백업 후 transcript-memory 삭제.

## Approach Proposal

### Recommended: Rebuild session-memory + staged recall cutover

CouchDB source에서 M3 materializer로 session-memory 문서를 생성해 RAGFlow
`session-memory` dataset에 live projection + parse하고, recall을 거기로 재배선한
뒤, 백업하고 transcript-memory를 삭제한다. 이미 구현된 M3(materializer/projection
seam)를 재사용한다.

### Alternative A: CouchDB-direct recall

recall이 CouchDB를 직접 읽음. design 계약(“normal recall은 session-memory만,
CouchDB evidence 비의존”) 위반 + 새 recall 경로 신설 비용. 기각.

### Alternative B: transcript-memory 유지 + write만 중단

삭제(은퇴) 목표 미달성. 기각.

## Architecture

```text
CouchDB transcript-source (3567 sessions)
        | M3 materialize_session_memory (embed tool evidence)
        v
SessionMemoryRebuilder ── live projector ──► RAGFlow session-memory (f6c55b)
        |                                          | parse/index
        v                                          v
RecallSmoke (session-memory vs transcript-memory) ─ verdict(no-regression)
        | pass
        v
RecallRepointer (recall dataset → session-memory) ─ observe window
        v
WriteCutover (stop transcript-memory delivery)
        v
TranscriptMemoryBackup (70k docs → durable) ─ rollback source (14 leak-blocked 포함)
        v
TranscriptMemoryRetirer (delete dataset) ── 비가역, 마지막
```

## Data Flow

1. 진단: session-memory 0-hits 근인 확인(parse status / 내용 부재 / recall 경로).
2. 재생성: 각 CouchDB 세션 → materialize_session_memory → session-memory doc →
   RAGFlow session-memory upload + parse. 결정적 id로 idempotent.
3. recall smoke: 대표 쿼리 ≥8을 session-memory와 transcript-memory에 retrieval,
   hits/유사도 비교 → 무회귀 verdict.
4. 재배선: recall 진입점 dataset → session-memory. 실행 중 recall이 session-memory
   를 읽는지 증거 확인. 짧은 안정 window 관측.
5. write 컷오버: transcript-memory delivery 비활성, 신규 ingest는 CouchDB +
   session-memory로만.
6. 백업: transcript-memory dataset export → durable file.
7. 삭제: transcript-memory dataset 삭제(게이트 통과 후).

## Component Details

- **SessionMemoryRebuilder**: 입력 CouchDB source(conversation chunk + tool
  evidence bundle); 출력 RAGFlow session-memory docs(parsed). 의존 M3
  materializer + RAGFlow upload/parse. 실패 시 부분 진행 + 재실행 안전.
- **RecallSmoke**: 입력 대표 쿼리 + 두 dataset; 출력 per-query hits/유사도 +
  무회귀 verdict. 의존 RAGFlow retrieval API.
- **RecallRepointer**: 입력 recall 설정; 출력 session-memory를 읽는 recall. 의존
  mcp-stdio 런치/서버 설정. 가역(되돌리기).
- **WriteCutover**: 입력 delivery 설정; 출력 transcript-memory write 0. 의존
  RAGFLOW_DELIVERY/dataset 설정.
- **TranscriptMemoryBackup**: 입력 dataset; 출력 durable 백업(14 포함). rollback
  source.
- **TranscriptMemoryRetirer**: 입력 dataset id + 게이트 verdict; 출력 dataset
  삭제. 비가역, backup 선행 필수.

## Error Handling

- session-memory 재생성 실패/부분: 재실행(idempotent), 미완 세션은 삭제 게이트에서
  제외.
- recall smoke 회귀: 컷오버 중단, recall 유지(transcript-memory).
- 재배선 후 recall 이상: recall 되돌리기(가역).
- 백업 실패: 삭제 차단(백업 없는 삭제 금지).
- 삭제 후 회귀: 백업에서 복원.
- leak 잔존 세션: CouchDB 저장 금지(fail-closed), 백업으로만 보존.

## Testing Strategy

- 단위: RecallSmoke verdict 로직, SessionMemoryRebuilder doc 생성(fake projector),
  WriteCutover 설정 전이 — fake/in-memory로.
- 통합: 소량 세션 재생성 → RAGFlow parse → retrieval hit 확인(라이브 소규모).
- 라이브 게이트: recall smoke 무회귀, 재배선 후 recall 출처 확인, 백업 무결성,
  삭제 후 recall 지속.

## Milestones

- RC1: session-memory 0-hits 진단 + CouchDB에서 session-memory 재생성 → RAGFlow
  parse 완료, retrieval에서 hit 발생. (done: 대표 쿼리에서 session-memory hits>0)
- RC2: recall smoke 무회귀 — 대표 쿼리 ≥8에서 session-memory가 transcript-memory
  대비 동등. (done: smoke verdict pass)
- RC3: recall 재배선 → session-memory + 안정 window 관측. (done: 실행 recall이
  session-memory를 읽고 정상 결과)
- RC4: write 컷오버 — transcript-memory 신규 write 0. (done: 신규 ingest가 CouchDB+
  session-memory로만)
- RC5: transcript-memory 백업(70k, 14 포함). (done: durable 백업 + 무결성 확인)
- RC6: transcript-memory dataset 삭제. (done: dataset 부재 + recall 지속 + rollback
  근거 보존)

## Open Questions

- recall 재배선의 운영 지점(Mac 런치 설정 vs 서버 설정) 확정은 RC3 착수 시 라이브
  확인으로 결정.
- recall smoke 정량 임계 최종값은 RC2에서 TM 기준선 측정 후 고정.

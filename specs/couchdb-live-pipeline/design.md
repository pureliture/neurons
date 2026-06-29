# CouchDB Live Pipeline Design Spec

## Overview

transcript-memory 은퇴 후 끊긴 "신규 대화 → session-memory(검색)" 경로를, transcript-memory에
의존하지 않고 **CouchDB 중심**으로 재구축한다. 이미 존재하는 부품(dendrite transcript-migrate,
`couchdb_source.couchdb_http_store`, `couchdb_source.session_memory_materializer`,
`document_model` build 함수)을 **통합·배포**하는 것이 핵심이며, 신규 알고리즘 개발은 최소화한다.

## Requirements Reference

- Phase 1 source: `specs/couchdb-live-pipeline/requirements.md`
- 핵심: FR1 ingest→CouchDB, FR2 CouchDB→session-memory builder, FR3 transcript-memory 제거,
  FR4 컨테이너 배포, FR5 approval-gated, FR6 갭 backfill, FR7 e2e+rollback.

## Architecture

세 컴포넌트, 단방향:

```
[Mac dendrite]                 [Ubuntu brain-server]                  [RetiredIndexBridge]
 transcript-migrate   --ship-->  couchdb ingest sink   --put-->  CouchDB(transcript_source)
 (incremental, watermark)        (CouchDBHttpSourceStore)              |
                                                                       | (projection_state dirty)
                                                              couchdb session-memory builder
                                                              (materialize_and_project)  --proj--> session-memory(db831f)
```

- **C1 dendrite incremental ship (Mac)**: 기존 `transcript-migrate`를 watermark 기반
  incremental로 주기 실행(launchd/cron). 이미 ingest된 세션은 skip, 신규/변경만 ship.
- **C2 server couchdb ingest sink (Ubuntu)**: dendrite가 보낸 transcript를 받아
  `document_model` build 함수로 6 doc family 생성 → `CouchDBHttpSourceStore.put`로 CouchDB
  적재 + 해당 세션 `projection_state`를 dirty로 표시. redaction fail-closed.
- **C3 couchdb session-memory builder (Ubuntu, 컨테이너)**: CouchDB `projection_state`에서
  미projection/dirty 세션 선별 → `materialize_and_project(session_id_hash, store, projector)`
  로 session-memory(db831f) 카드 생성. fail-closed(미완 세션 projection 거부).

## Data Flow

1. 대화 종료 → dendrite가 로컬 transcript locator 발견(C1).
2. C1이 watermark 이후 신규만 brain-server로 ship.
3. C2가 수신 → doc family 빌드 → CouchDB put → projection_state=dirty.
4. C3 scheduler(예: */3m)가 dirty 세션 materialize_and_project → db831f.
5. 다음 recall이 db831f에서 신규 세션 검색.

## Component Details

### C1 dendrite incremental ship
- 입력: 로컬 AI CLI transcript(claude/codex/gemini/antigravity 레인).
- 출력: brain-server ingest 엔드포인트로 ship된 transcript payload.
- 의존: 기존 `dendrite transcript-migrate`(merged), watermark store(신규: 마지막 ship된
  세션/시각). 의존 최소화 위해 server-side가 idempotent(deterministic id)이므로 watermark는
  성능 최적화용이며 정확성은 server upsert가 보장.

### C2 server couchdb ingest sink
- 입력: transcript payload(historical_import가 다루는 형태와 동형).
- 출력: CouchDB 6 doc family + projection_state dirty.
- 재사용: `historical_import` build 경로 + `document_model` build_* + `couchdb_http_store.put`.
- 경계: `redact_public_ingress_text` + leak gate(fail-closed, raw /Users 누설 시 reject).

### C3 couchdb session-memory builder
- 입력: CouchDB store(`CouchDBHttpSourceStore`), dirty 세션 목록.
- 출력: db831f session-memory 카드(public-safe body 임베드).
- 재사용: `materialize_and_project` / `materialize_session_memory`.
- dataset: `--dataset-name session-memory`(db831f 이름 해석). raw id 하드코딩 금지.
- approval: live-approval gate(autopilot self-mint, 사전승인). dataset/argv/timeout/redaction
  /rollback 바인딩.

## Error Handling

- materialization-loss: 세션의 chunk/tool-evidence가 coverage_manifest 기대치보다 적으면
  not-fully-materialized → projection 거부(CouchDB 원본 보존).
- RetiredIndexBridge 도달 실패/타임아웃: 해당 사이클 abort, watermark/dirty 미전진, 재시도(at-least-once).
- ES 부하(과거 overflow incident): db831f는 소형(6663)이라 재발 위험 낮음. terms 쿼리 경계는
  builder가 batch(--limit/--max-processed-per-run)로 제한.
- approval 불일치/미승인: fail-closed(빌드 중단). 운영자 중단은 approval 재차단으로 수행.

## Testing Strategy

- 단위: C2 ingest(payload→doc family, redaction fail-closed, idempotent upsert),
  C3 builder(dirty 선별, fully/partially materialized 분기) — InMemory store로.
- 통합: 합성 세션 1건을 C2→CouchDB→C3→(가짜 projector)로 흘려 카드 생성 검증.
- 라이브 전: one-off dry-run/관찰(컨테이너 1사이클, mutation 전 plan).
- e2e(라이브): backfill 또는 신규 1건이 db831f에 나타나고 recall hits>0, 기존 6663 무회귀.
- 회귀: `cd worker && uv run pytest -q`, `gradle test`, `neuron-knowledge --show-boundary`.

## Milestones

- **M1 통합 베이스**: worktree(claude/couchdb-live-pipeline, main 기반)에 ledger-autopilot
  deploy scaffolding(Dockerfile.session-memory, deploy/* 패턴) vendor + builder entry를
  CouchDB materializer로 교체. dendrite ship 타깃/서버 sink 수신부 실재 확인.
  - done: 컴포넌트 경계 확정, 컴파일/임포트 통과.
- **M2 C2 ingest sink**: payload→doc family→CouchDB put + projection_state dirty. 단위 테스트.
  - done: 합성 payload가 CouchDB(InMemory)에 6 family로 적재 + dirty 표시, redaction green.
- **M3 C3 builder entry**: dirty 선별 + materialize_and_project CLI/엔트리. 단위+통합 테스트.
  - done: dirty 세션이 (가짜 projector로) 카드 생성, fail-closed 분기 검증.
- **M4 컨테이너/스케줄 배포물**: compose + entrypoint(ingest drain + build cycle), state mount,
  approval self-mint(autopilot). 빌드.
  - done: 이미지 빌드, 컨테이너 one-off가 dry-run/plan까지 통과.
- **M5 라이브 배포 + 갭 backfill**: 컨테이너 가동, dendrite incremental 가동, 22h 갭 backfill.
  - done: 신규/backfill 세션이 db831f에 projection, 기존 6663 무회귀.
- **M6 e2e 검증 + 마무리**: 실제 1건 end-to-end(대화→CouchDB→db831f→recall), 롤백 문서,
  stale GC approval(cca3d0 타깃) 정리, main↔ledger 머지 권고 명시.
  - done: e2e 증거, 회귀 0, 운영 정리.

## Open Questions

- dendrite→server ingest 수신부가 이미 있는지(서버 sink endpoint) 또는 신규로 노출할지 — M1에서
  실재 확인 후 확정.
- projection_state dirty 선별을 full-scan vs incremental watermark로 — M3에서 데이터 규모 보고 확정.
- 컨테이너를 ledger-autopilot의 session-memory-worker와 합칠지/별도 project로 둘지 — M4.

## Rollback

- 컨테이너 stop + approval 재차단 → 즉시 정지(가역).
- db831f 무회귀 보장(materialize fail-closed). 필요 시 session-memory GC로 카드 정리.
- dendrite incremental schedule 비활성으로 ingest 중단.
- 본 작업은 worktree 격리; main 영향 없음(머지는 별도 게이트).

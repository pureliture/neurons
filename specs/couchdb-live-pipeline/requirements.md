# CouchDB Live Pipeline Requirements

## 승인 대상

- Source of truth: `specs/couchdb-live-pipeline/requirements.md`
- Preview companion: (생성 시) `requirements.html`

## 배경 / 문제

transcript-memory(RetiredIndexBridge) 은퇴 마이그레이션으로 raw transcript의 SoT가 CouchDB로
이동하고 transcript-memory dataset은 영구 삭제됨. 그런데 기존 session-memory 빌더
(`neuron_session_memory.run_neuron_session_memory_build_once`)는 **transcript-memory를
read-SoT로 읽어** session-memory 카드를 만드는 구조라, 원천이 사라져 동작 불가
(`list_datasets("transcript-memory")` 단계에서 실패). 또한 신규 캡처를 CouchDB에 적는
**live ingest 경로 자체가 없음**(마이그레이션은 1회성 과거 적재였음). 결과적으로 "새 대화 →
검색(session-memory)" 파이프라인이 ~22h+ 정지.

기존 컨테이너(session-memory-worker, rag-ingress-live)는 22h 전 ES terms-query overflow
incident으로 approval이 의도적 차단(approved:false)되고 stop됨. ES는 현재 green(거대
transcript-memory 삭제로 overflow 조건 해소).

## 목표 (What)

새 대화가 다시 검색 가능해지도록, **transcript-memory에 의존하지 않는** live 파이프라인을
구축한다. 두 반쪽:

- **A. Live CouchDB ingest**: 신규 캡처(Mac dendrite → brain-server NATS) → CouchDB
  source plane(`transcript_session` / `conversation_chunk` / `tool_evidence_bundle` /
  `coverage_manifest` 등)에 적재.
- **B. CouchDB → session-memory builder**: CouchDB에서 dirty/미projection 세션을 읽어
  `materialize_and_project`로 RetiredIndexBridge session-memory(db831f) 카드 생성.

## 질문-답변 흐름 (자문자답)

### Q1. Ingest는 새 서비스인가, 기존 NATS 경로 재배선인가?
A. **기존 재배선.** brain-server는 이미 Mac→ingress-api→NATS→delivery worker 경로가 있고,
`rag_ingress/shadow_worker.py`가 server-side consume→deliver→state 패턴을 제공한다. 신규
서비스 대신 이 consume 경로의 **sink를 transcript-memory(RetiredIndexBridge)에서 CouchDB
(`CouchDBHttpSourceStore.put`)로 교체**한다. 재사용이 표면적·운영적으로 가장 작은 변경.

### Q2. Builder는 무엇으로 만드나?
A. **기존 main `couchdb_source.session_memory_materializer` 사용.** `materialize_and_project`
(fail-closed, public-safe body 임베드)가 정확히 CouchDB→session-memory 빌더다. 새로 짜지
않고 이를 deploy entry로 노출한다. 기존 transcript-memory-read 빌드(`neuron_session_memory`)는
이 용도로는 **은퇴**.

### Q3. Builder는 어느 세션을 (재)projection할지 어떻게 아나?
A. CouchDB **`projection_state` 문서**(document_model 존재)로 미projection/변경 세션을
판별한다. ingest가 세션을 적/갱신하면 projection 대기 상태가 되고, builder가 이를 소비한다.
shadow_ingest_log(transcript-memory 전달 기록) 의존을 제거.

### Q4. 배포 형태는?
A. ledger-autopilot의 컨테이너 scaffolding(별도 compose project, host-network,
state bind-mount, 경량 scheduler) 패턴을 **재사용**하되 builder entry만 CouchDB
materializer로 교체. RetiredIndexBridge/PG compose 미수정 원칙 유지(host-published endpoint).

### Q5. 라이브 쓰기 승인 게이트는?
A. 기존 fail-closed live-approval gate 유지. autopilot 사전승인(사용자, 연장됨)으로
self-mint. 금지 op(live delete/disable/GC, Docker/firewall mutation)은 사전승인 밖.

### Q6. session-memory dataset은 어떻게 지정?
A. **이름으로 해석**(`--dataset-name session-memory` → db831f). raw dataset_id 하드코딩 금지.

### Q7. 정지 22h 동안의 신규 캡처 갭은?
A. 원본은 Mac dendrite에 보존. dendrite `transcript-migrate`(기구축)로 CouchDB backfill →
builder가 projection. **유실 없음, 복구 가능.**

### Q8. 코드 정본/브랜치 통합은?
A. main(= couchdb_source 보유)을 베이스로, ledger-autopilot의 **deploy scaffolding만**
가져와 builder entry를 CouchDB로 적응. main↔ledger 전체 머지는 본 작업의 선결조건이 아님
(별도 후속). 단, 운영 충돌 없도록 배포 산출물 출처를 명시.

### Q9. 안전/검증 기준은?
A. (1) 모든 코드 변경 worktree에서, 테스트 green. (2) 라이브 배포 전 dry-run/one-off 관찰.
(3) end-to-end 증거: 신규(또는 backfill) 세션 1건이 CouchDB→session-memory(db831f)→recall로
흐름. (4) db831f 기존 6663 무회귀. (5) public 출력 redaction 무누설.

## 기능 요구사항

- FR1. NATS consume → CouchDB source 적재(redaction fail-closed, deterministic ids).
- FR2. CouchDB `projection_state` 기반 dirty 세션 선별 → `materialize_and_project`로
  session-memory(db831f) 카드 생성.
- FR3. session-memory는 이름 해석(db831f). transcript-memory 참조 전면 제거.
- FR4. 컨테이너 배포(scheduler: ingest drain + build cycle), host-network, state bind-mount.
- FR5. live write는 approval-gated(autopilot self-mint, 사전승인).
- FR6. 22h 갭 backfill 경로(dendrite→CouchDB) 문서화 및 실행.
- FR7. end-to-end 검증 + 롤백 절차.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 데이터 안전 | fail-closed materialization(미완 세션 projection 거부), 무유실 |
| 가역성 | 컨테이너 stop + approval 재차단으로 즉시 중단; db831f 백업/무회귀 |
| 보안 경계 | public 출력에 raw transcript/path/token/dataset_id/document_id 금지 |
| 자격증명 | `RETIRED_INDEX_BRIDGE_API_KEY` 단일 |
| RetiredIndexBridge 변경 | RetiredIndexBridge/PG compose 미수정, host-published endpoint만 사용 |

## 사용자 시나리오

- 새 대화 종료 → dendrite 캡처 → brain-server NATS → CouchDB 적재 → builder가 수 분 내
  session-memory(db831f) 카드 생성 → 다음 recall에 반영.
- 운영자가 중단을 원하면 approval 재차단 + 컨테이너 stop으로 즉시 정지(가역).

## 미결정 항목

- ingest sink 교체를 shadow_worker 내부 옵션으로 둘지, 별도 thin delivery-to-couchdb
  어댑터로 둘지 (design에서 확정).
- builder dirty 선별을 projection_state full-scan vs watermark/incremental 중 무엇으로 할지
  (design에서 확정).
- backfill 시점(빌더 가동 전/후) 및 1회 규모 (design에서 확정).

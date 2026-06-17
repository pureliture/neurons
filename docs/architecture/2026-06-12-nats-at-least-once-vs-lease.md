# ADR: NATS at-least-once ↔ lease 통합 — 단일 at-least-once + natural-key dedup 채택

- Date: `2026-06-12`
- Status: `Accepted`
- Scope: co-located Python delivery worker (`worker/`). 라이브 재배포는 별도 goal.
- 관련: 이관 audit brief `2026-06-12-rag-ingress-queue-migration-audit-brief.md`(advisor) §6.2,
  `worker/README.md`.

## Context

이관 audit brief는 미해결 설계 질문으로 다음을 남겼다(§6.2):

> NATS at-least-once ↔ lease 이중 dedup: JetStream redelivery와 `delivery_jobs`
> lease/replayable가 충돌·중복배달 가능. 통합 설계 필요(미해결).

이 질문은 "durable `delivery_jobs` 상태기계(lease 포함)가 라이브 worker 소유"라는
brief 가정 위에 서 있었다. 이관 착수 전 **live read-only 재검증**(2026-06-12,
`ssh ragflow-ubuntu`, durable SQLite `mode=ro`)에서 그 가정이 사실이 아님이 드러났다:

- 라이브 worker(`rag-ingress-live-ingress-worker-py-1`, `shadow_worker.py`)의 durable
  SQLite에는 테이블이 **`shadow_ingest_log` 하나뿐**이다(1473행, 전부 delivered).
  `delivery_jobs`/`delivery_payloads`/`commands`/`inbox_events`는 **존재하지 않는다.**
- `state_db.py`/`delivery_executor.py`/`delivery_reconcile.py`/`delivery_backend.py`/
  `idempotency.py`의 durable 상태기계는 이미지에는 설치돼 있으나(전체 lib COPY)
  **worker 런타임에서 import되지 않는 dead code**다(ENTRYPOINT=`shadow_worker`).
- 따라서 라이브에는 **lease가 없다.** 재시도/at-least-once는 전적으로 NATS JetStream에
  의존한다: `RAG_INGRESS_QUEUE`(WorkQueue retention) + Explicit ack + ackWait 30s +
  maxDeliver(라이브 override 100000≈무제한) + fail-open pressure gate.
- 실제 문제는 brief가 우려한 "이중 dedup"이 아니라 정반대 **under-dedup**이었다:
  `index_backend.submit_document`이 dedup 조회 없이 무조건 upload→metadata→parse를
  수행하고, `process_payload`도 submit 전에 delivered 여부를 확인하지 않는다. 따라서
  JetStream 재배달 시 동일 문서가 RAGFlow에 **중복 생성**될 수 있다. 은퇴한 Java
  worker(`RagFlowTargetAdapter`)는 오히려 `RecentDeliveryCache`(in-memory 3단계) +
  `findByContentHash`(RAGFlow 폴백) 2계층 dedup을 갖고 있었다 — Python 라이브가 더 약했다.

## Decision

**at-least-once 단일 모델 + natural-key dedup을 채택하고, lease 레이어는 도입하지 않는다.
co-located worker는 durable `delivery_jobs`/lease 기계를 가져오지 않는다(drop).**

1. **delivery-attempt 드라이버 = NATS JetStream**. `RAG_INGRESS_QUEUE`(WorkQueue) +
   Explicit ack + maxDeliver가 재배달·poison 경계를 단독 관리한다. 성공=ack, 일시실패=nak,
   maxDeliver 초과=quarantine(ack-drop, 라이브는 사실상 비활성).
2. **lease 없음**. WorkQueue + 단일 컨슈머(durable `rag_target_delivery_worker`)가 이미
   "한 번에 한 처리자"를 보장하므로, in-flight 소유권을 위한 별도 durable lease는 불필요하다.
   lease를 도입하면 JetStream redelivery와 **두 개의 독립 dedup/타이밍 권원**이 생겨
   brief가 우려한 충돌(이중 dedup·중복배달)을 *새로* 만든다. 도입하지 않음으로써 충돌 자체를 없앤다.
3. **idempotency = 2계층 natural-key dedup**(은퇴한 Java worker 설계 복원):
   - 로컬: worker의 durable `shadow_ingest_log`(idempotency_key→delivered+document_ref)를
     submit 전에 조회. delivered면 재업로드하지 않고 기존 ref 재사용. 재시작 안전.
   - 폴백: 로컬 row가 없으면 `find_by_natural_key`로 RAGFlow에서 동일
     content_hash/idempotency_key 문서를 찾아 재사용(첫 시도가 업로드 후 기록 전 사망,
     또는 로컬 볼륨 소실 케이스 커버).
   - 폴백이 매칭되도록 `submit_document`가 content_hash+idempotency_key를 업로드
     메타데이터에 주입한다(Java contentHash 기록과 동등 parity).
4. **durable `delivery_jobs`/payloads/executor/reconciler/idempotency-classifier는 co-locate
   대상에서 제외(drop)**. defer(나중에 기본 도입)가 아니라, 라이브에서 실제로 쓰이지 않는
   배선이므로 가져오지 않는다. `rag_ingress/__init__.py`도 `outbox_client`/`state_store`
   (Ledger) import를 제거하도록 트림했다.

## Why not lease (충돌 해소 논리)

| 우려(brief §6.2) | at-least-once + natural-key에서의 처리 |
|---|---|
| JetStream redelivery ↔ lease 이중 dedup | lease가 없으므로 dedup 권원이 하나(natural-key). 충돌 불성립. |
| 재시작 중복 upload | 로컬 durable log + RAGFlow natural-key 폴백이 막음(restart-safe). |
| poison/head-of-line | maxDeliver→quarantine(ack-drop)로 NATS가 단독 처리. |
| in-flight 소유권 | WorkQueue + 단일 durable 컨슈머가 보장. 분산 lock 불필요. |

## Consequences

- **장점**: 단일 진실(JetStream)로 타이밍/재시도 일원화. dead code/Ledger 미반입(이미지·표면 축소).
  은퇴한 Java worker의 dedup 강도 회복(중복문서 갭 폐쇄). 코드/테스트로 검증 가능.
- **한계(명시)**:
  - dedup이 "정확히 한 번"은 아니다. content_hash로 자연키 동등성을 보장하는 *효과적*
    at-least-once+idempotent-submit이다(중복문서 미생성이 목표, 메시지 1회 처리 보장 아님).
  - `find_by_natural_key`는 RAGFlow `list_documents` keyword/페이지 폴백에 의존하므로
    대용량 dataset에서 비용이 있다. 로컬 log 히트가 우선이라 평시 경로는 RAGFlow 조회 0.
  - 메타데이터에 content_hash/idempotency_key가 추가된다(additive, recall 무해).

## Revisit conditions (이 결정을 다시 열어야 할 때)

다음 중 하나가 사실이 되면 durable `delivery_jobs`+lease 재도입을 재검토한다(현재는 불필요):

1. **다중 동시 컨슈머로 수평 확장**이 필요해질 때(WorkQueue 단일 컨슈머 가정이 깨짐) →
   per-key lease로 TOCTOU 윈도우를 닫아야 함.
2. **운영자 replay/감사**를 위해 과거 delivery 결과를 durable 조회해야 할 때
   (현재는 `shadow_ingest_log` + 로그로 충분).
3. **NATS WorkQueue 외 전송**(ack 의미가 약한 백엔드)으로 바꿀 때.

그 시점의 재도입은 advisor의 기존 `state_db`/`delivery_executor`를 그대로 끌어오는 것이
아니라, 위 표의 "단일 dedup 권원" 원칙을 유지하는 새 통합 설계로 한다.

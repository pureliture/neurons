# rag-ingress-queue Python delivery worker (co-located)

이 디렉터리는 rag-ingress-queue가 소유하는 **Python live delivery worker**다.
G2부터 라이브 delivery는 Java `ingress-worker`가 아니라 이 Python worker가 수행하며
(은퇴한 Java worker는 `compose.yaml`에서 `profiles: [retired]`), 그 소스가 그동안
`workspace-ragflow-advisor`의 `agent-knowledge` lib에만 물리적으로 존재했다.
이 패키지는 그 **delivery 서브셋을 co-locate(벤더링)** 한 것이다. Java 코드는 바뀌지 않는다.

- 출처(provenance): `agent-knowledge` advisor source revision `d571800`의 벤더링 사본.
- 외부 의존: `nats-py>=2.6` 하나(런타임). 그 외 표준 라이브러리만 사용.

## 무엇을 가져왔고(vendored) 무엇을 뺐나

라이브 worker가 실제로 실행하는 경로(`shadow_worker.run_consume` →
`process_payload` → normalize → server full redaction → leak check → RAGFlow
submit)와 그 의존 폐포만 가져왔다.

vendored (`lib/agent_knowledge/`):
- `rag_ingress/shadow_worker.py` — NATS JetStream pull-consume worker 엔트리포인트
- `rag_ingress/server_runtime.py` — `apply_server_redaction` / `normalize_ingest_job_payload` / `document_from_ingress_payload` / `public_ingress_leak_violations`
- `rag_ingress/index_backend.py` — `RAGFlowIndexBackendAdapter` (upload/metadata/parse + `find_by_natural_key`)
- `rag_ingress/rag_ready_document.py` — backend-neutral 문서 모델 + content_hash/idempotency_key 빌더
- `redaction.py` — server full public redaction 본체(inline 정규식, denylist 파일 의존 없음)
- `events.py`, `spool.py`, `ragflow_client.py` — 위 모듈의 폐포 의존

**의도적으로 제외(가져오지 않음):**
- `state_db.py` / `delivery_executor.py` / `delivery_reconcile.py` / `delivery_backend.py` / `idempotency.py` / `domain_state.py`
  — durable `delivery_jobs` 상태기계. 2026-06-12 live read-only 재검증 결과
  **라이브 worker 런타임에서 미사용 dead code**였다(라이브 durable SQLite에는 `shadow_ingest_log` 테이블 하나뿐). worker는 NATS at-least-once + natural-key dedup에 의존하므로 lease 기계를 가져오지 않는다. 근거: `docs/architecture/2026-06-12-nats-at-least-once-vs-lease.md`.
- `outbox_client.py` — client(producer) 측 코드. server worker 불필요.
- `state_store.py` (`LedgerIngestStateStore`) — Ledger 직접 의존. 가져오지 않는다.
  → `rag_ingress/__init__.py`는 이 둘의 import를 제거하도록 트림했다(패키지 import만으로 Ledger가 끌려오지 않게).

## redelivery dedup (under-dedup 갭 수정)

라이브 worker는 lease가 없어 JetStream 재배달 시 동일 메시지를 재처리할 수 있고,
기존 코드의 `submit_document`는 무조건 upload→metadata→parse 했다(중복문서 위험,
은퇴한 Java worker보다 약함). 이를 닫기 위해 은퇴한 Java worker의 2계층 dedup을 복원했다:

1. **로컬 계층** — worker 자신의 durable `shadow_ingest_log`(idempotency_key→delivered+document_ref)를
   submit 전에 조회. 이미 delivered면 재업로드하지 않고 기존 ref 재사용. 재시작 안전.
2. **RAGFlow 폴백** — 로컬 row가 없으면 `find_by_natural_key`로 RAGFlow에서 동일
   content_hash/idempotency_key 문서를 찾아 재사용. 첫 시도가 업로드 후 기록 전에
   죽었거나 로컬 볼륨이 소실된 경우를 커버.

폴백이 실제로 매칭되도록 `submit_document`가 `content_hash`+`idempotency_key`를
업로드 메타데이터에 주입한다(Java worker의 contentHash 기록과 동등 parity).

## 실행

- 이미지 기본값은 안전(격리 shadow 스트림, delivery off). compose `ingress-worker-py`
  서비스도 라이브 consume/deliver를 **opt-in**으로 둬서 `compose up`(ubuntu-smoke)은
  안전하다. 라이브 배포는 env-file로 주입한다:
  `RAG_INGRESS_STREAM=RAG_INGRESS_QUEUE`, `RAG_INGRESS_SUBJECT=rag.ingress.>`,
  `RAG_INGRESS_DURABLE=rag_target_delivery_worker`, `RAG_INGRESS_ALLOW_LIVE_QUEUE=1`,
  `RAG_INGRESS_DELIVER=1`, 7개 `RAGFLOW_*_DATASET_ID`(profile별 라우팅),
  `RAGFLOW_BASE_URL`/`RAGFLOW_API_KEY`, `RAG_INGRESS_PRESSURE_URL`.
- 라이브 재배포 자체는 이 작업 범위 밖(별도 goal).

## 테스트

```
cd worker && python -m pytest          # or: uv run --with pytest python -m pytest
```

# 격리 검증 절차: NATS JetStream 이벤트 기반 session-memory watch

> 합성 데이터·loopback 전용. 운영 cluster/`neurons-ops`/실제 세션/외부 LLM API는 사용하지 않는다.
> `deploy/isolated-vm/compose.yaml`(PR #363 branch) 중 CouchDB/PostgreSQL 서비스만 가져오고
> NATS JetStream 노드를 추가했다. 실행 로그·측정값은 세션 보고서에 있다.

## 1. 스택 (compose 발췌)

```yaml
# 기존 couchdb(127.0.0.1:25984), postgres pgvector(127.0.0.1:25432)에 추가
  nats:
    image: nats:2.10-alpine
    command: ["-js", "-sd", "/data", "-m", "8222"]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:8222/healthz || exit 1"]
      interval: 5s
      retries: 30
    ports:
      - "127.0.0.1:${ISO_NATS_PORT:-24222}:4222"
      - "127.0.0.1:${ISO_NATS_MONITOR_PORT:-28222}:8222"
    volumes: [nats-data:/data]
```

- CouchDB: `_users`, `_replicator`, `transcript_source` DB 생성. 비밀번호는 `secrets.token_urlsafe`로 생성.
- PostgreSQL: `worker/lib/agent_knowledge/postgres_store/pgvector_schema.sql` 적용.
- 임베딩: 로컬 Model2Vec 서비스(`neuron-local-embedding install --download` 후 `serve --host 127.0.0.1 --port 18120`),
  `LLM_BRAIN_EMBEDDING_PROFILE_ID=lbrain-memory-model2vec-256-v1`(PG local profile 경로).

## 2. 프로세스

| 역할 | 명령 | 핵심 env |
| --- | --- | --- |
| ingress delivery (publisher) | `python -m agent_knowledge.rag_ingress.shadow_worker --mode consume` | `SHADOW_DELIVER=1`, `INGRESS_DELIVERY_BACKEND=couchdb`, `SESSION_MEMORY_EVENTS_PUBLISH=1`, `RAG_INGRESS_NATS_URL` |
| session-memory consumer | `python -m agent_knowledge.cli couchdb-session-memory-watch --consume` | `SESSION_MEMORY_PROJECTION_BACKEND=postgres_pgvector`, `NEURON_LBRAIN_PGVECTOR_DSN`, `SESSION_MEMORY_BUILD_SCRIPT`, `SESSION_MEMORY_APP_DIR`(컨테이너 밖에서 `build-once.sh`를 돌릴 때만) |
| 합성 ingress 발행 | nats-py로 `rag.shadow.iso.<tag>`에 `rag_ingress_enqueue.v1` 이벤트 N건 publish, 발행 시각 기록 | — |
| 지연 측정 | PG `session_memory_chunks`를 0.2s 간격으로 polling, 세션별 첫 관측 시각 − 발행 시각 | — |

## 3. 시나리오

1. **지연**: N개 합성 세션 ingress publish → CouchDB 쓰기 → `session_memory.changed` → consumer → PG 반영까지의 시간.
2. **재시작**: 6개 세션 publish 후 debounce 종료 직후(2.5s) consumer `SIGKILL` → 12초 뒤 재기동.
   기대: 기동 시 sweep + JetStream 미ack 메시지 재전달(`ack_wait` 경과 후)로 세션 누락 0, PG 세션별 chunk 행 1개(중복 0).
3. **DLQ**: `SESSION_MEMORY_BUILD_SCRIPT`를 항상 `exit 124`(timeout 모사)하는 스크립트로 바꾸고
   `SESSION_MEMORY_WATCH_NAK_BACKOFF_SECONDS=2,2,2,2` → 5회 전달 후 `session_memory.dead_letter`에
   `{event, attempts, error_class}`가 쌓이고 원 메시지가 `term`되는지 확인(`js.get_last_msg`).
4. **임베딩 장애**: 로컬 임베딩 서비스를 내린 상태에서 1세션 publish → build 보고서 `pending`(ack) →
   서비스 복구 후 sweep/outbox로 완료되는지 확인.

## 4. 확인 포인트

- NATS 모니터 `GET /jsz?streams=true&consumers=true`: `num_ack_pending`, `num_redelivered`, `ack_floor`.
- PG: `SELECT count(*), count(distinct session_id_hash), count(distinct chunk_id) FROM session_memory_chunks`.
- heartbeat: `SESSION_MEMORY_WATCH_HEARTBEAT` JSON(`acked`, `naked`, `dead_lettered`, `last_build_rc`, `phase`).

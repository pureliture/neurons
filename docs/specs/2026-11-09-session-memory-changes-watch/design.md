# session-memory build: 180초 cron → NATS JetStream 이벤트 기반 전환 설계 (v2)

> v1(CouchDB `_changes` longpoll bridge)은 사용자 결정으로 폐기했다. 재시도/dead-letter를
> 자체 구현하지 않고 ingress lane이 이미 쓰는 NATS JetStream(`max_deliver` + nak backoff +
> DLQ)에 위임한다. v1과 v2의 재시도 소유권 비교는 마지막 절에 남긴다.

## 배경

- `worker/deploy/session-memory/entrypoint.sh`는 `RUN_MODE=cron`으로
  `SESSION_MEMORY_BUILD_INTERVAL_SECONDS`(기본 180초)마다 `build-once.sh`
  (`couchdb-session-memory-build`)를 실행한다. CouchDB 저장 후 PG
  `session_memory_chunks` 반영까지 최대 3분 지연된다.
- 세션 문서를 CouchDB에 쓰는 주체는 Python live ingress worker
  (`rag_ingress/shadow_worker.py` → `CouchDBDeliveryBackend.submit`)와
  `rag-ingress-state drain-deliveries`(`state_cli._build_live_backend`)다. 둘 다
  `build_couchdb_delivery_backend()`로 backend를 만든다.
- 기존 JetStream 자산: Java `NatsJetStreamProvisioner`(stream `rag.ingress.>`
  WorkQueue, consumer `maxDeliver 5`/`ackWait 30s`), Java `IngestWorker`와 Python
  `shadow_worker.run_consume`의 `MAX_DELIVER` 초과 → ack + quarantine(DEAD_LETTER)
  패턴, 비-live stream에 대한 Python `stream_info`/`add_stream` ensure 패턴.
- 세션 단위 중복 방지와 pending 재시도는 이미 `build_cli._select_sessions_for_cycle`
  checkpoint(`last_seq`, `pending_session_ids`, `pending_session_attempts`) 와
  `projection_state`(`projected_source_hash`)에 있다. 이 경로는 그대로 둔다.

## 목표 / 비목표

- 목표: ingress 쓰기 성공 → 수 초 내 projection 시작. consumer 재시작 후 누락/중복
  없음. 세션 단위 실패는 JetStream 재전달과 DLQ로 관리. `RUN_MODE=cron`/`build` 호환.
- 비목표: `_changes` 구독 bridge, 자체 jsonl dead-letter, Java provisioner 수정,
  `neurons-ops` 변경(제안만).

## 전체 흐름

```
ingress worker ──submit() 성공──▶ CouchDB (authoritative)
      │ best-effort, 실패해도 ingress ack에 영향 없음
      └─▶ js.publish("session_memory.changed", {project, provider, session_id_hash, ...})
                               │ stream SESSION_MEMORY_EVENTS (LIMITS, file, max_age 7d)
                               ▼
session-memory 컨테이너 RUN_MODE=watch
  entrypoint(bash) ──재시작 backoff──▶ couchdb-session-memory-watch --consume (Python 상주)
      durable pull consumer "session_memory_projector" (filter session_memory.changed)
      fetch → debounce 2s/최대 10s → 세션 hash dedup → build-once.sh 1 cycle
          (SESSION_MEMORY_BUILD_SESSION_IDS=h1,h2 → `--session-id-hash` 명시 선택)
      per-session 결과 → ack / nak(backoff) / max_deliver 초과 → DLQ publish + term
      idle 900s(또는 기동 시) → build-once.sh 일반 incremental cycle (checkpoint 사용)
```

## 상세 설계

### 1. publisher (ingress 측)

- `couchdb_delivery_backend.CouchDBDeliveryBackend`에 `on_session_changed` hook을
  추가한다(`on_mirror_outcome`과 같은 post-authoritative, best-effort 자리).
  `submit()`이 authoritative 성공으로 끝날 때마다
  `SessionChangedEvent(project, provider, session_id_hash, payload_hash)`를 전달한다.
  redacted 식별자만 담고 본문/raw id는 담지 않는다.
- 새 모듈 `session_memory/change_events.py`:
  - `SESSION_MEMORY_EVENTS_STREAM="SESSION_MEMORY_EVENTS"`,
    `SESSION_MEMORY_CHANGED_SUBJECT="session_memory.changed"`,
    `SESSION_MEMORY_DEAD_LETTER_SUBJECT="session_memory.dead_letter"`.
  - `SessionChangeCollector`: thread-safe 큐. `submit()`은 `asyncio.to_thread` 안에서
    돌기 때문에 hook은 큐에만 넣고, 실제 publish는 이벤트 루프 쪽에서 한다.
  - `ensure_session_memory_stream(js)`: `stream_info` → 없으면 `add_stream`
    (subjects `session_memory.>`, LIMITS, File, `max_age` 7d, `duplicate_window` 2m).
  - `publish_session_changes(js, events)`: `Nats-Msg-Id = session_id_hash[:16]:payload_hash[:16]`
    로 같은 delivery의 재publish만 dedup한다(세션 단위 dedup은 consumer debounce가 담당;
    stream dedup 창 안에서 세션 단위로 합치면 consumer가 이미 처리한 뒤 들어온 변경을
    잃을 수 있다).
- 배선:
  - `shadow_worker.run_consume.handle_msg`: `process_payload` 성공 후 collector를 비워
    `js.publish`(같은 NATS 연결). 예외는 `session_change_publish_failed error=<class>`
    로그 + 카운트만 하고 ingress 메시지 ack는 그대로 진행한다.
  - `state_cli drain-deliveries`: 실행 끝에 collector를 비워 `asyncio.run`으로 publish.
  - opt-in: `SESSION_MEMORY_EVENTS_PUBLISH=1`일 때만 hook을 연결한다(기본 0 → 기존
    배포 동작 불변). NATS URL은 기존 `RAG_INGRESS_NATS_URL`을 재사용한다.
- publish 실패 정책: **best-effort**. ingress의 authoritative 성공은 CouchDB 쓰기로
  확정되며 publish 실패는 ingress 결과를 바꾸지 않는다. 유실된 이벤트는 consumer의
  idle safety sweep(900초) 및 기동 시 sweep이 incremental checkpoint 경로로 보완한다.

### 2. consumer (`couchdb-session-memory-watch --consume`)

- `couchdb_source/watch_cli.py`, `neuron-knowledge couchdb-session-memory-watch`.
- 기동: `nats.connect(RAG_INGRESS_NATS_URL)` → `ensure_session_memory_stream` →
  `pull_subscribe(subject, durable="session_memory_projector",
  config=ConsumerConfig(ack_policy=explicit, max_deliver, ack_wait, max_ack_pending=limit))`.
  기동 직후 sweep cycle 1회(`SESSION_MEMORY_WATCH_STARTUP_SWEEP=1`, 기본 on).
- 루프:
  1. `fetch(limit, timeout=idle_remaining)`; 첫 메시지를 받으면 debounce 창
     (`SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS=2`)만큼 추가 fetch를 반복하고, 변경이
     계속 와도 `SESSION_MEMORY_WATCH_DEBOUNCE_MAX_SECONDS=10`에서 멈춘다.
  2. 메시지의 `session_id_hash`를 dedup해 `SESSION_MEMORY_BUILD_SESSION_IDS`(쉼표
     구분)로 `build-once.sh`를 1회 실행한다. 잘못된 payload(hash 아님)는 즉시 `term`.
  3. build 보고서의 `sessions[]`(hash, status)를 메시지에 매핑한다.
     `projected`/`pending`/`skipped` → ack(pending은 다음 chunk 이벤트가 다시 깨운다),
     `failed`/보고서 누락/CLI 비정상 종료 → nak(backoff).
  4. `num_delivered >= max_deliver`인 failed 메시지는 DLQ subject로 원본 이벤트 +
     `attempts`, `error_class`, `dead_lettered_at`를 publish한 뒤 `term()`한다.
     DLQ publish 자체가 실패하면 `nak(delay=60)`으로 보존한다(ingress quarantine과 동일).
  5. 마지막 build 후 `SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS=900` 동안 메시지가 없으면
     세션 지정 없는 일반 cycle(`build-once.sh`)을 실행한다. 0이면 sweep 비활성.
- nak backoff: `num_delivered` 기준 `30s, 60s, 120s, 300s, 300s…`
  (`SESSION_MEMORY_WATCH_NAK_BACKOFF_SECONDS="30,60,120,300"`).
- `ack_wait` 기본 = `SESSION_MEMORY_BUILD_TIMEOUT_SECONDS`(300) + 60 = 360s. build 1 cycle
  이 timeout 안에 끝나므로 처리 중 재전달이 생기지 않는다.
- `max_deliver` 기본 5(ingress `NatsJetStreamProvisioner`와 동일). `SESSION_MEMORY_WATCH_MAX_DELIVER`.

### 3. build CLI 확장 (`couchdb-session-memory-build`)

- `--session-id-hash H` (반복 가능). 지정되면 checkpoint 선택을 건너뛰고 해당 세션만
  `materialize_and_project`한다. **selection checkpoint는 읽지도 쓰지도 않는다**
  (checkpoint writer는 여전히 일반 cycle 하나). 보고서에 `sessions: [{session_id_hash,
  status}]`를 추가한다(일반 cycle에도 추가; 기존 키는 유지).
- `build-once.sh`: `SESSION_MEMORY_BUILD_SESSION_IDS`가 있으면 `--session-id-hash`를
  argv에 붙인다. approval/timeout/no-progress guard는 그대로.

### 4. 실패 / 재시도 / dead-letter 소유권

| 단계 | 소유 | 비고 |
|---|---|---|
| ingress → CouchDB | 기존 ingress JetStream(`rag.ingress.>`) | 변경 없음 |
| CouchDB → publish | best-effort | 실패는 로그/카운터, sweep이 보완 |
| 이벤트 전달 재시도 | JetStream `max_deliver`+nak backoff | consumer 재시작 시 durable ack floor부터 재전달 |
| 세션 단위 terminal 입력 실패 | 기존 `projection_state` blocked source hash | 이벤트는 ack(skipped), 소스가 바뀌면 다시 활성 |
| 종단 실패 기록 | `session_memory.dead_letter` subject(같은 stream, max_age 7d) | advisory `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.*`도 그대로 발생하므로 외부 모니터링은 그것을 구독해도 된다 |

### 5. idempotency / 중복 방지

- at-least-once 재전달 → 같은 세션 build 재실행 → `projected_source_hash == source_hash`
  면 재projection 없음. PG 쪽은 `PgSessionMemoryProjector` natural-scope +
  `acquire_session_identity_lock`으로 중복 row를 막는다(기존).
- debounce 창 안의 같은 세션 메시지는 한 번만 build하고 모두 ack.
- `build-once.sh`는 한 번에 하나만 돈다(consumer 단일 프로세스, sweep도 같은 루프 안).

### 6. backpressure

- fetch 상한 = `SESSION_MEMORY_BUILD_LIMIT`(10) = `max_ack_pending`. 1 cycle이 끝나야
  다음 fetch가 간다. PG embedding 예산(기존)은 build CLI 안에서 유지.
- stream은 LIMITS 보존이므로 consumer가 느려도 publisher는 막히지 않는다(`max_age` 7d).

### 7. health

- heartbeat `SESSION_MEMORY_WATCH_HEARTBEAT=state/runtime/session-memory-watch.json`
  (`phase`, `last_reason`, `pending_in_batch`, `consecutive_failures`, `dead_lettered`,
  `last_build_rc`, `updated_at`; raw 식별자 없음). fetch idle마다 갱신.
- `--health`: `RUN_MODE=watch`에서 heartbeat 나이가
  `SESSION_MEMORY_WATCH_HEALTH_MAX_AGE_SECONDS`(기본 idle fetch 창 + build timeout + 60)
  이내면 0. cron/build 모드는 `not_applicable`로 0.
- consumer가 NATS 오류로 종료하면 entrypoint bash 루프가 5s→…→300s backoff로 재기동.

### 8. provisioning 선택: Python ensure-stream

- Java `NatsJetStreamProvisioner`는 `api|worker` profile에서 `rag.ingress.>` WorkQueue만
  소유하고 Java worker는 retired다. session-memory lane은 publisher/consumer 모두 Python
  이며, `shadow_worker`가 비-live stream에 이미 같은 ensure 패턴을 쓴다. 따라서
  `ensure_session_memory_stream()`을 publisher 기동 1회 + consumer 기동 1회 호출한다
  (idempotent). Java 수정 없음 → Gradle 검사 대상 없음.
- `neurons-ops` 제안(접근하지 않음): session-memory 컨테이너 env `RUN_MODE=watch`,
  `RAG_INGRESS_NATS_URL`(host network 기준 `nats://127.0.0.1:4222`); live ingress worker
  env `SESSION_MEMORY_EVENTS_PUBLISH=1`; NATS 계정에 `session_memory.>` publish/consumer
  권한. stream/consumer는 코드가 만들지만, 운영이 선언형으로 관리하려면 같은 설정
  (LIMITS, file, max_age 7d, durable `session_memory_projector`, max_deliver 5)을 ops에
  정의하면 된다.

### 9. cron 모드와의 공존

- `compose.yaml` 기본 `RUN_MODE=cron` 유지. watch는 `RUN_MODE=watch` opt-in.
- 둘은 같은 `/app/state`와 checkpoint를 쓰므로 `RUN_MODE`만 되돌리면 rollback된다.
  cron과 watch를 동시에 띄우지는 않는다(build 동시 실행 방지).

## 검증 계획 (격리 compose, 합성 데이터, loopback only)

1. PR #363 branch의 `deploy/isolated-vm/compose.yaml`에 `nats:2-alpine -js`를 loopback
   포트로 추가(검증용 overlay; 운영 정의 아님).
2. 합성 세션 → `rag-ingress-state`/ingress 경로로 CouchDB 쓰기 + publish → consumer →
   PG `session_memory_chunks` 반영 시각 측정(목표 수 초).
3. consumer SIGKILL 후 재기동: 중단 중 넣은 세션이 재전달로 처리되고 PG row 중복 없음.
4. 강제 실패 세션(projector 오류 주입)으로 `max_deliver` 초과 → DLQ subject 도달 확인.

## 재시도 소유권 비교: `_changes`+checkpoint(v1) vs JetStream(v2)

- v1: CouchDB `_changes`+`since`가 이미 replay 로그이고 세션 재시도는 build checkpoint
  (`pending_session_attempts`, 6회 cap)에 있다. 외부 의존 없음. 대신 drop된 세션의 기록이
  없어 자체 jsonl dead-letter를 만들어야 했고, 재시도 간격/관측이 코드 안에 묻힌다.
- v2: 전달/재시도/DLQ를 JetStream 표준(`num_delivered`, nak backoff, advisory)으로 옮겨
  ingress lane과 운영 방식이 같아진다. 비용은 (1) ingress에 publish bridge가 생기고
  publish 실패를 sweep으로 보완해야 함, (2) 오프셋이 이중화됨 — stream ack floor(세션
  이벤트)와 build checkpoint `last_seq`(sweep)가 둘 다 존재. 이중화는 "이벤트는 빠른
  경로, checkpoint는 완전성 보장"으로 역할을 나눠 충돌을 피한다.

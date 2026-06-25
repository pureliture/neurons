# Model Connectors Boundary Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 현재 단계: Phase 1 Requirements Discovery
- 승인 전 제한: `design.md` 작성, 구현 코드 변경, live Graphiti/Neo4j/Qdrant/RAGFlow mutation은 하지 않는다.

## 배경

- `GraphitiNeo4jGraphMemoryAdapter` 생성자는 이미 Graphiti instance를 받을 수 있지만,
  `from_env` / `from_config` / `_build_graphiti()` 경로가 env 해석, 모델 endpoint 선택,
  Graphiti client 조립, fallback Graphiti 조립을 같은 adapter 파일에 묶고 있다.
- Qdrant searchable mirror 문서와 구현은 기존 OpenAI-compatible embedding/rerank
  endpoint를 재사용한다. 현재 `qdrant_embedding.py`와 `qdrant_rerank.py`에도
  유사한 env 해석과 lazy live wiring이 있다.
- repo의 import guard는 `rag_ingress` 경로가 heavy `llm_brain_core` package를
  끌어오지 않는 것을 중요하게 본다.
- 이번 작업은 새 모델 선택이나 live routing 변경이 아니라, 모델 연결 책임의 소유
  경계를 분명히 하는 요구사항 정리에서 출발한다.

## 질문-답변 흐름

### Q1: 1차 성공 범위는 어디까지로 잡을까?

**라이브 canary 포함**으로 확정한다.

- code-only refactor와 fake/no-network test만으로 끝내지 않고, 이후 실제 Graphiti
  schema extraction canary까지 요구사항에 포함한다.
- 단, Phase 1/2 중에는 live 실행하지 않는다. 구현 단계에서 current evidence,
  exact argv, bounded timeout, redaction, postcheck, rollback/abort 기준을 제시한
  operator gate를 통과해야 한다.
- 이 선택은 Graphiti/Neo4j data-plane을 검증 대상으로 포함하지만, RAGFlow
  disable/delete, Qdrant cutover, native-memory authority 이전은 여전히 범위 밖이다.

### Q2: Graphiti schema extraction canary는 어느 깊이까지 확인할까?

**단일 synthetic episode**로 확정한다.

- public-safe synthetic episode 하나를 사용해 Graphiti extraction, upsert, search를
  확인한다.
- 실제 projection slice나 live project memory를 태우지 않는다.
- canary는 isolated `brain_id` / group scope를 사용하고, raw transcript/source body,
  private path, secret-like 값, raw dataset/document id를 출력하지 않는다.
- 성공 기준은 Graphiti/Neo4j 연결, 모델 기반 schema extraction 호출, episode upsert,
  bounded search/readback이 모두 통과하는 것이다.

### Q3: 공용 model connector 책임은 어디가 소유할까?

**낮은 shared layer**로 확정한다.

- model connector는 Graphiti/Qdrant adapter보다 낮은 shared layer가 소유한다.
- `llm_brain_core`와 `rag_ingress`는 그 계층을 소비하되 서로를 import하지 않는다.
- 이 결정은 구현 package 이름을 확정하지 않는다. 실제 위치와 public import path는
  Phase 2 design에서 import guard와 compatibility를 검증하며 정한다.

### Q4: `GraphitiNeo4jConfig`는 어떻게 다룰까?

**장기 facade 유지**로 확정한다.

- 기존 `GraphitiNeo4jConfig`, `GraphitiNeo4jGraphMemoryAdapter.from_config`,
  `GraphitiNeo4jGraphMemoryAdapter.from_env` public surface를 유지한다.
- 내부 구현은 새 model connector / backend builder 경로로 delegate할 수 있지만,
  기존 import path와 behavior는 깨지지 않아야 한다.
- deprecation warning, 즉시 제거, 내부 전용화는 이번 요구사항 범위 밖이다.

### Q5: `agent_knowledge` root package rename은 이번 scope에 포함할까?

**이번 scope에서는 현재 root 유지**로 확정한다.

- 이번 요구사항은 `model_connectors` leaf package와 model connection boundary만 다룬다.
- 기존 root import path인 `agent_knowledge`는 유지한다.
- `agent_knowledge` rename은 import path, CLI, tests, docs, packaging에 걸친 별도
  repo identity/API migration이므로 별도 요구사항/설계로 분리한다.

## 기능 요구사항

- Graphiti adapter의 도메인 역할은 Graphiti instance를 통해 episode upsert/search를
  수행하는 일로 제한한다.
- 모델 연결 책임은 env 해석, non-secret model spec, provider/capability policy,
  OpenAI-compatible embedding/rerank/LLM client 생성, fallback 구성으로 분리한다.
- model connector shared layer는 `llm_brain_core`와 `rag_ingress`보다 낮은 위치에서
  제공되어야 하며, 두 상위 영역이 서로를 import하지 않게 해야 한다.
- 이번 scope에서는 기존 `agent_knowledge` root import path를 바꾸지 않는다.
- 기존 `GraphitiNeo4jConfig.from_env`, `GraphitiNeo4jGraphMemoryAdapter.from_env`,
  Qdrant embedding/rerank builder의 public compatibility는 유지한다.
- `GraphitiNeo4jConfig`는 장기 compatibility facade로 유지하고 기존 caller가
  새 connector 세부 구조를 알 필요 없게 한다.
- canonical `LLM_BRAIN_*` env precedence와 legacy fallback 계약을 보존한다.
- secret-like 값은 config repr, log, error, preview artifact에 노출하지 않는다.
- Graphiti structured extraction normalization, fallback attempts, read/write timeout,
  best-effort vs required graph activation behavior는 무회귀여야 한다.
- 구현 검증은 fake/no-network test에 더해 public-safe synthetic episode 기반의
  isolated live Graphiti schema extraction canary를 포함한다.
- Qdrant import guard를 보존한다. `rag_ingress` 경로가 heavy `llm_brain_core` package를
  import하지 않아야 한다.
- 정책 판정은 provider 이름만이 아니라 role/capability 단위로 한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Safety | code-only 변경을 우선하며 live Graphiti/Neo4j/Qdrant/RAGFlow mutation은 별도 승인 전 금지한다. |
| Compatibility | 기존 public import path, env precedence, test fixture behavior, `GraphitiNeo4jConfig` facade를 유지한다. |
| Privacy | raw host, private path, token, API key, raw transcript, dataset_id, document_id를 출력하지 않는다. |
| Import locality | shared connector는 Graphiti/Qdrant adapter보다 낮은 layer에 있어야 하며 `llm_brain_core`와 `rag_ingress` 사이 circular import를 만들지 않는다. |
| Observability | policy block, fallback use, timeout, vector dim mismatch, required graph probe failure는 redacted signal로 구분 가능해야 한다. |
| Testability | 기본 test는 fake/no-network로 통과하고 live endpoint wiring은 lazy path로 유지한다. |
| Live gate | Graphiti schema extraction canary는 구현 단계의 별도 operator gate 이후에만 실행한다. |
| Canary scope | 단일 public-safe synthetic episode만 사용하며 실제 projection slice는 범위 밖이다. |
| YAGNI | 새 모델 선택, live cutover, RAGFlow disable/delete, native-memory authority 이전, `agent_knowledge` root package rename은 이번 요구사항 밖으로 둔다. |

## 사용자 시나리오

- 개발자는 기존 Graphiti graph path를 그대로 쓰면서 모델 연결 계층만 정리하고, 기존 env로
  동일하게 동작하는지 unit/integration fake test와 synthetic episode live canary로 확인한다.
- 개발자는 Qdrant mirror의 embedding/rerank 경로가 Graphiti와 같은 model connector
  규칙을 따르는지 확인하되, delivery worker import guard를 깨지 않는다.
- 운영자는 graph required path가 실패할 때 backend 연결 실패, policy block, fallback 실패,
  timeout을 구분해 볼 수 있다.
- 리뷰어는 이번 변경이 모델 선택 변경이나 live cutover가 아님을 요구사항 문서만 보고
  판단할 수 있다.

## 미결정 항목

- 없음. `requirements.md` 승인 후 Phase 2에서 접근안을 비교한다.

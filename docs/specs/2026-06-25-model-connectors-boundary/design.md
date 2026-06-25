# Model Connectors Boundary Design Spec

## Overview

Graphiti adapter 안에 묶인 model/env/client assembly 책임을 낮은 shared layer로
분리한다. 기존 `GraphitiNeo4jConfig`와 Qdrant builder public surface는 깨지지 않게
facade로 유지하고, 구현 검증은 fake/no-network test와 gated synthetic Graphiti
canary로 닫는다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- 승인된 접근: Compatibility-first shared seam

핵심 요구사항:

- model connector는 Graphiti/Qdrant adapter보다 낮은 shared layer가 소유한다.
- `llm_brain_core`와 `rag_ingress`는 서로 import하지 않는다.
- `GraphitiNeo4jConfig`는 장기 compatibility facade로 유지한다.
- live canary는 public-safe synthetic episode 하나만 사용한다.
- 새 모델 선택, live cutover, RAGFlow disable/delete, native-memory authority 이전은
  범위 밖이다.

## Approach

### Compatibility-first shared seam

낮은 shared layer에 non-secret model specs, env precedence, capability policy,
OpenAI-compatible factories를 둔다. 기존 Graphiti/Qdrant public entrypoint는 유지하고
내부에서 shared layer로 delegate한다.

선택 이유:

- 기존 `GraphitiNeo4jConfig.from_env`와 Qdrant embedding/rerank builder의 caller를
  깨지 않는다.
- Qdrant delivery/import guard를 지킬 수 있다.
- Graphiti-only 리팩터보다 중복 env 해석을 더 많이 줄인다.
- policy/observability를 한 곳에 모을 수 있다.

## Architecture

Package leaf name is fixed as `model_connectors`. The current root package name
`agent_knowledge` is used below only as the existing codebase import root. Renaming
that root package is a broader repo identity/API migration and is out of scope for
this design.

```text
agent_knowledge/
  model_connectors/
    specs.py
    env.py
    policy.py
    openai_compatible.py
    graphiti_components.py

  llm_brain_core/
    graphiti_backend.py
    graphiti_adapter.py

  rag_ingress/
    qdrant_embedding.py
    qdrant_rerank.py
```

Dependency direction:

```text
llm_brain_core ─┐
                ├── model_connectors
rag_ingress ────┘
```

`model_connectors`는 `llm_brain_core`나 `rag_ingress`를 import하지 않는다.
Graphiti-specific optional imports는 Graphiti component factory 경계 안에서 lazy로만
발생한다. Qdrant live client imports도 기존처럼 lazy path에 남긴다.

## Data Flow

### Graphiti adapter construction

```text
GraphitiNeo4jGraphMemoryAdapter.from_env
  -> GraphitiNeo4jConfig.from_env facade
  -> ModelConnectionConfig.from_env
  -> ModelPolicy.validate(capability set)
  -> GraphitiComponentBundle build
  -> GraphitiBackendBuilder.build
  -> GraphitiNeo4jGraphMemoryAdapter(graphiti, fallback_graphiti, ...)
```

### Qdrant embedding/rerank construction

```text
build_openai_embedding_provider / build_openai_reranker
  -> ModelConnectionConfig.from_env
  -> ModelPolicy.validate(embedding/rerank)
  -> OpenAI-compatible provider wrapper
```

## Component Details

### `model_connectors.specs`

- 입력: env에서 읽은 raw string, capability role.
- 출력: secret을 제외한 immutable spec objects.
- 의존성: standard library only.

### `model_connectors.env`

- 입력: mapping-style environ.
- 출력: canonical `LLM_BRAIN_*` precedence가 적용된 model connection config.
- 의존성: standard library only.

### `model_connectors.policy`

- 입력: provider id, model role, capability.
- 출력: allow/deny 또는 typed policy error.
- 의존성: standard library only.

### `model_connectors.openai_compatible`

- 입력: validated specs.
- 출력: OpenAI-compatible LLM/embed/rerank client factories.
- 의존성: live client imports are lazy.

### `model_connectors.graphiti_components`

- 입력: Graphiti model specs and policy result.
- 출력: `GraphitiComponentBundle`.
- 의존성: `graphiti_core` imports are lazy and scoped to Graphiti construction.

### `llm_brain_core.graphiti_backend`

- 입력: Neo4j graph store spec, `GraphitiComponentBundle`.
- 출력: Graphiti instance.
- 의존성: `graphiti_core`.

### `llm_brain_core.graphiti_adapter`

- 입력: Graphiti instance, fallback Graphiti instance, group/timeout/attempt settings.
- 출력: existing graph adapter behavior.
- 의존성: domain models and Graphiti runtime object only; no direct model env policy.

## Error Handling

- Missing model config: fail closed on live build path with a bounded, non-secret error.
- Policy denial: return/raise typed error with redacted provider/capability detail.
- Wrong embedding dimension: preserve existing fail-closed `ValueError` behavior.
- Fallback used: expose redacted signal without leaking model endpoint or secret.
- Required graph probe failure: fail fast when graph is required; best-effort graph remains
  degraded/unavailable instead of false healthy.
- Live canary failure: abort implementation verification, report redacted failure class,
  do not mutate RAGFlow/Qdrant/native-memory.

## Testing Strategy

- Unit tests for env precedence and legacy fallback behavior.
- Unit tests for secret omission from repr/loggable config.
- Policy allow/deny tests by capability: structured extraction, embedding, rerank.
- Graphiti component bundle assembly with fake clients.
- `GraphitiNeo4jConfig.from_env/from_config` compatibility tests.
- `runtime_graph` best-effort vs required behavior tests.
- Qdrant embedding/rerank existing tests updated to shared config path.
- Fresh interpreter import guard: Qdrant builder imports must not load `llm_brain_core`.
- Gated live canary: single public-safe synthetic episode extraction/upsert/search.

## Milestones

- M1: Shared specs/env/policy with compatibility tests green.
- M2: Qdrant embedding/rerank delegates to shared model connector without import guard regression.
- M3: Graphiti backend builder delegates model assembly outside adapter while preserving facade behavior.
- M4: Gated synthetic Graphiti canary evidence.

## Open Questions

- None. `agent_knowledge` root package rename is explicitly out of scope and should
  use a separate requirements/design cycle.

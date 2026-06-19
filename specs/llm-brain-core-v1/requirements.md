# LLM-Brain Core v1 Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- Phase 2 output: `design.md`는 이 `requirements.md`가 사용자에게 명시 승인된 뒤에만 작성한다.

## 범위

이 요구사항은 `neurons` repo 안에 LLM-Brain Core v1을 설계하기 위한 Phase 1 source다. 목표는 기존 `dendrite`/`neurons` 세션 수집 파이프라인을 유지하면서, RAGFlow 중심 recall surface를 장기 기억 core로 승격하지 않고, 세션·작업·결정·장애·페르소나·파일 참조를 다루는 로컬 우선 brain memory 계층을 만드는 것이다.

## 질문-답변 흐름

### Q1. 우리가 만들려는 것은 문서 검색기인가, 작업 기억 시스템인가?

답: 작업 기억 시스템이다. 문서 검색 품질보다 현재 작업 복원, 과거 결정 재사용, 트러블슈팅 재현, persona constraint, source evidence 추적이 우선이다. RAG 문서 검색은 필요하면 adapter로 붙인다.

### Q2. 새 repo를 만들 것인가, `neurons`에 구현할 것인가?

답: v1은 `neurons`에 구현한다. 현재 `neurons`가 server-side brain authority이며, `MemoryCard`, ledger, `brain_query`, candidate mining, RAGFlow projection mirror 관련 코드가 이미 있다. 새 repo 분리는 graph-core vertical slice와 per-PC/central mode가 안정화된 뒤 검토한다.

### Q3. RAGFlow는 core인가?

답: 아니다. 기존 Ubuntu RAGFlow는 보존하지만, LLM-Brain Core v1의 canonical memory, ontology store, persona DB, default vector DB로 쓰지 않는다. RAGFlow는 기존 corpus와 PDF/PPT/Excel/scan/citation 같은 complex document class를 위한 external bridge로만 둔다.

### Q4. session-memory는 어디에 저장되어야 하는가?

답: raw SoT와 derived index를 분리한다. CouchDB는 AI session raw store로 유지하고, NATS/ledger는 replay 가능한 event source로 둔다. `session-memory` artifact는 `neurons`가 소유하는 durable store에 materialize하고, graph store는 검색과 관계 탐색을 위한 derived index로 취급한다.

### Q5. PC에 있는 파일은 중앙 graph가 어떻게 참조해야 하는가?

답: 중앙에 raw file을 무작정 복제하지 않는다. `dendrite`가 PC별 local source catalog를 만들고, `neurons`는 `SourceRef`/`SpanRef`/hash/permission/sync policy로만 연결한다. 원문 fetch는 explicit allowlist와 redaction gate를 거친 별도 action으로 남긴다.

### Q6. Agent와 MCP/API boundary는 왜 필요한가?

답: OpenClaw, Codex, Claude Code 같은 runtime agent가 `Graphiti`, `Neo4j`, `CouchDB`, RAGFlow를 직접 알면 기술 교체와 safety boundary가 깨진다. v1은 새 gateway 제품을 만들지 않고, `neurons` 내부 read-oriented brain API와 thin MCP/stdio adapter만 제공한다.

### Q7. 기존 llm-brain/autopilot 개발은 버리는가?

답: 버리지 않는다. `MemoryCard`, candidate mining, approval/supersession, ledger write, brain query read-model 아이디어는 재사용한다. 다만 `autopilot_cli.main`, live RAGFlow mining, self-minted RAGFlow projection approval, `ragflow_projection.py` write path는 bridge 단계 전까지 core 재사용 대상에서 제외한다.

### Q8. 가장 큰 기술 부채는 무엇인가?

답: RAGFlow projection mirror와 canonical memory 역할이 코드와 문서에서 섞여 있는 점이다. 이 부채는 앞으로 모든 brain 기능 변경을 RAGFlow availability, dataset shape, parse state에 묶이게 한다. v1 설계는 이 coupling을 명시적으로 끊어야 한다.

### Q9. 오토파일럿 구현을 위해 무엇이 먼저 고정되어야 하는가?

답: `requirements.md`와 `design.md`가 SoT가 되어야 한다. 구현 autopilot은 승인된 design의 milestones만 소비한다. 구현 중 SoT 변경이 필요하면 임의 수정하지 않고 grill-to-spec 상류로 되돌린다.

## 기능 요구사항

- 기존 `dendrite`/`neurons` 수집 파이프라인을 유지한다.
- CouchDB를 AI session raw store로 유지한다.
- NATS/ledger event를 replay 가능한 source로 유지한다.
- `neurons`가 `session-memory` artifact, MemoryCard, brain query, graph projection, sync state를 소유한다.
- AI session에서 `Session`, `Turn`, `ToolCall`, `Task`, `Decision`, `Incident`, `Attempt`, `Fix`, `Verification`, `PersonaFact` 후보를 추출할 수 있어야 한다.
- 새 세션 시작 시 최신 작업, 중단 지점, 관련 파일, 결정, 미완료 항목, 주의점을 포함한 ContextPack을 만들 수 있어야 한다.
- 과거 incident와 troubleshooting 흐름을 keyword, semantic, graph relation, time filter로 찾을 수 있어야 한다.
- 설계/스키마/persona/project assumption drift를 time-aware relation으로 설명할 수 있어야 한다.
- PersonaFact는 evidence, scope, confidence, lifecycle state를 가져야 하며 자동 승격은 fail-closed여야 한다.
- `SourceRef`와 `SpanRef`는 raw private path/body를 노출하지 않고 source evidence로 연결되어야 한다.
- per-PC local brain과 optional central brain 모두 지원 가능해야 한다.
- central sync는 graph DB file sync가 아니라 event/episode sync와 central rebuild 방식이어야 한다.
- RAGFlow는 bridge/fallback으로만 호출되어야 하며 core 기능의 required dependency가 아니어야 한다.
- Agent-facing surface는 `brain_context_resolve`, `brain_memory_search`, `brain_incident_search`, `brain_persona_check`, `brain_evidence_get`에 준하는 작은 read-oriented API로 시작해야 한다.
- 기존 `autopilot_cli.py`/`autopilot_loop.py` 계열은 live mutation 전 dry-run, redaction, bounded cycle, forbidden operation block을 유지해야 한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Local-first | 각 PC가 offline 상태에서도 자기 기억을 조회할 수 있어야 한다. |
| Portability | Git repo, `.env`, Docker Compose, export/import, event replay를 지원해야 한다. |
| Safety | raw transcript, raw private path, secret, token, raw dataset/document id를 출력하지 않는다. |
| Replaceability | OpenClaw에 종속되지 않고 MCP/stdio/HTTP adapter를 통해 다른 agent와 연결 가능해야 한다. |
| Provider support | Ollama를 필수 지원 대상으로 두고, OpenAI-compatible provider 교체가 가능해야 한다. |
| Incrementality | 기존 RAGFlow와 session-memory runtime을 즉시 삭제하지 않고 bridge로 보존한다. |
| Testability | RAGFlow 없이도 core acceptance test가 통과해야 한다. |
| Idempotency | event replay, MemoryCard candidate, graph projection, sync는 재실행 안전해야 한다. |
| Observability | ContextPack 생성, graph projection lag, source resolution failure, persona conflict를 진단 가능해야 한다. |
| Autopilot readiness | implementation milestone은 검증 가능한 done 기준과 rollback/abort 조건을 가져야 한다. |

## 사용자 시나리오

- 사용자가 새 Codex/OpenClaw 세션을 시작하면 현재 repo와 요청을 기준으로 마지막 미완료 Task와 최신 Decision을 받는다.
- 사용자가 에러 메시지를 제시하면 과거 유사 Incident, 실패한 Attempt, 성공한 Fix, Verification을 본다.
- 사용자가 설계를 바꾸면 이전 Decision이 언제부터 superseded 되었는지 timeline으로 설명된다.
- 사용자가 새 계획을 세우면 PersonaFact와 프로젝트 constraint 충돌 여부가 `aligned`, `possible_conflict`, `persona_drift`, `insufficient_evidence`로 나온다.
- 사용자가 PC A에서 작업한 파일을 PC B나 central brain에서 조회할 때 raw path/body가 아니라 `SourceRef` 기반으로 unresolved/resolved 상태가 나온다.
- 사용자가 RAGFlow 없이 local brain을 띄워도 AI session-memory, Task/Decision, Incident, PersonaFact, ContextPack은 동작한다.
- 사용자가 기존 RAGFlow corpus가 필요한 질문을 하면 RAGFlow bridge가 document/citation fallback으로만 호출된다.

## Acceptance Gates

- RAGFlow disabled 상태에서 latest work ContextPack test가 통과한다.
- RAGFlow disabled 상태에서 Decision drift timeline test가 통과한다.
- RAGFlow disabled 상태에서 Incident search/replay test가 통과한다.
- RAGFlow disabled 상태에서 PersonaFact candidate/check test가 통과한다.
- `SourceRef`가 raw private path/body를 출력하지 않는 redaction test가 통과한다.
- 기존 `dendrite` client boundary와 `neurons` server authority guard test가 유지된다.
- 기존 llm-brain/autopilot 단위 테스트는 설계 변경 후에도 동등하거나 더 강한 safety gate로 유지된다.
- RAGFlow bridge를 켜도 canonical memory winner는 `session-memory` artifact와 MemoryCard ledger이며, graph store는 derived index, RAGFlow는 bridge lag로만 취급된다.
- central sync는 v1에서 transport 제품까지 만들지 않더라도 `BrainEvent` envelope, idempotent replay, tombstone, duplicate/out-of-order handling, central rebuild shadow test로 검증된다.

## Tech-Debt Backlog Seed

| Phase | Debt | Category | Location | Tax | Impact | Risk | Effort | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | RAGFlow projection naming이 core memory처럼 읽힘 | Docs/knowledge | `README.md`, `specs/recall-cutover/*`, `worker/lib/.../ragflow_projection.py` | 설계자가 RAGFlow를 canonical brain으로 오해함 | 5 | 4 | 2 | 36 |
| 1 | Brain query와 projection state가 한 응답에서 섞임 | Design/architecture | `brain_query.py` | query correctness와 mirror freshness 판단이 결합됨 | 4 | 4 | 2 | 32 |
| 2 | Graph-backed memory adapter seam 부재 | Design/architecture | `session_memory/*` | Graphiti/Neo4j 도입 시 RAGFlow path를 우회하기 어려움 | 5 | 4 | 3 | 27 |
| 2 | SourceRef catalog가 dendrite/neurons contract로 아직 고정되지 않음 | Design/architecture | `dendrite`, `neurons` boundary | PC file evidence 연결이 ad-hoc으로 흐를 수 있음 | 4 | 4 | 3 | 24 |
| 2 | per-PC/central sync state model 부재 | Operational/infra | new core area | 나중에 graph DB file sync 같은 위험한 설계로 회귀 가능 | 4 | 4 | 4 | 16 |
| 3 | LinkML/TerminusDB governance 부재 | Dependency/platform | watch | schema governance가 커질 때 migration cost 발생 | 2 | 2 | 4 | 8 |

## Architecture Decision Seed

### 권장 접근: In-repo Graph Core

- `neurons` 안에 LLM-Brain Core v1을 구현한다.
- 기존 MemoryCard/ledger/autopilot/brain query를 보존한다.
- RAGFlow projection은 bridge/fallback으로 격하한다.
- Graph-backed adapter와 ContextPack builder를 추가한다.

### 대안 1: New `llm-brain` Repo

- 장점: 배포/브랜딩/독립성이 높다.
- 단점: 기존 `neurons` 구현과 pipeline 이관 비용이 크고, 지금은 ownership이 분산된다.
- 판정: v1 이후 추출 후보.

### 대안 2: RAGFlow 중심 확장

- 장점: 기존 corpus와 retrieval UI를 빠르게 재사용한다.
- 단점: portable/local-first, ontology drift, SourceRef, persona evidence 요구와 맞지 않는다.
- 판정: core에서는 제외.

## System Design Constraints

- Agent-facing API는 storage backend를 숨긴다.
- Graph store는 derived index이며 raw/session/event SoT가 아니다.
- RAGFlow write/delete/disable은 ordinary autopilot 범위가 아니다.
- central graph는 각 PC graph DB file을 병합하지 않는다.
- failure mode는 partial success보다 fail-closed와 diagnosable lag를 우선한다.

## 미결정 항목

- Graph DB backend 기본값은 design에서 Neo4j 우선으로 검토하되, FalkorDB/Kuzu/Neptune은 v1 blocking decision으로 두지 않는다.

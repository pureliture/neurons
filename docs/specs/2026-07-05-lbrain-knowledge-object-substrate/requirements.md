# LBrain Knowledge Object Substrate Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: 생성하지 않음. 현재 Phase 1 초안은 Markdown만으로 검토 가능하다.
- Phase 2 `design.md`: 이 문서가 사용자에게 승인된 뒤에만 작성한다.

## 현재 단계

이 문서는 `grill-to-spec` Phase 1 요구사항 초안이다. 구현 설계, storage schema, MCP API 세부, migration order는 아직 확정하지 않는다. 요구사항은 Palantir Ontology reference corpus, 현재 LBrain read path, 기존 `neurons` repo 계약, 그리고 앞선 LBrain knowledge quality 실패 사례를 근거로 작성한다.

## 문제 정의

현재 LBrain은 session archive, accepted/current MemoryCard, graph projection, ContextPack 같은 부품은 갖고 있지만, 제품 확장마다 별도 기능을 개발해야 하는 형상에 가깝다.

문서 정리 질문에서는 current/stale/superseded 판단을 바로 제공하지 못했고, 코드 변경 영향 분석, 배포 truth, 개인 code style, HTML review preference, handoff/debugging knowledge에도 같은 문제가 반복된다. 원인은 단순 retrieval 품질이 아니라, repo 작업에 필요한 문서, 코드, 세션, PR, 배포, 선호, 외부 reference를 공통으로 표현하는 typed knowledge object substrate가 부족하기 때문이다.

목표는 LBrain을 단순 기억 저장소가 아니라 다음 세 층을 가진 작업 지식 시스템으로 요구사항화하는 것이다.

```text
reference/corpus layer
processing/object layer
authority/review layer
```

이 substrate는 문서 전용이 아니어야 한다. 문서, 코드 스타일, HTML 리뷰 취향, PR/commit 이력, 배포 상태, 운영 runbook, 외부 제품 docs, 디버깅 사건을 같은 object/edge/evidence/lifecycle 모델로 다뤄야 한다.

## 실패 사례

### F1. 문서 정리 질문 저품질

사용자는 `neurons` repo 문서를 최신화하고 과거 문서를 정리하기 위한 구체적 근거를 원했다.

기대 결과:

- 현재 authoritative 문서
- stale 또는 과거 문서 후보
- 문서별 최신화, 보존, 퇴역 판단
- 관련 개념의 현재 상태
- 문서 간 replacement/supersession 관계
- 정리 우선순위
- 근거와 신뢰도

실제 결과는 live mutation approval, stable host, graph drift, freshness stale 같은 고수준 safety/status 카드 중심이었다. 즉 LBrain은 documentation cleanup intent를 `RepoDocument`/`SupersessionEdge`/`AuthorityDecision`으로 변환하지 못했다.

### F2. 날짜 기반 작업 회상 실패

`어제 이 repo에서 뭐 했어?` 같은 질문은 session artifact 나열이 아니라 `WorkUnit`으로 답해야 한다. 현재 모델은 task/session/spec/PR/commit/test/deploy evidence를 하나의 작업 단위로 안정적으로 묶지 못한다.

### F3. 코드 변경 영향 분석 부족

`이 파일을 바꾸면 어떤 테스트/런타임 영향이 있어?` 같은 질문은 file, component, MCP tool, docs, tests, runtime surface 관계가 필요하다. 현재는 typed `ChangeImpact` object와 edge가 충분하지 않다.

### F4. 배포/운영 truth 혼선 위험

사용자는 merge와 deploy를 분리해서 보길 기대한다. 현재 LBrain은 public CI, private deploy authority, live runtime evidence, unverified gap을 하나의 `RuntimeTruth`로 안정적으로 반환하는 substrate가 부족하다.

### F5. 선호/스타일 기억 부족

사용자는 Java coding style, HTML 기반 review/visualization preference를 기억하고 다른 AI tool이 시작부터 반영하길 원한다. 현재 `PreferenceRuleCard`와 `RepoStyleProfile` 씨앗은 있으나, corpus 분석, approval, scope conflict, diff review, agent context injection까지 닫힌 제품 흐름은 아니다.

### F6. 외부 reference corpus 관리 부족

Palantir 자료처럼 65개 이상의 외부 reference 문서를 넣으면, LBrain은 단순 요약 저장이 아니라 문서 자체의 source, snapshot, version, hash, freshness, supersession, authority role을 관리해야 한다. 현재는 local normalized corpus가 준비됐지만 LBrain의 living reference corpus로 ingest된 상태는 아니다.

## 사용자 기대

LBrain은 질문에 대해 관련 카드나 검색 결과를 나열하는 것이 아니라, 작업자가 바로 쓸 수 있는 typed object pack을 반환해야 한다.

최소 답변 단위:

| 필드 | 의미 |
| --- | --- |
| `object_type` | `ReferenceDocument`, `RepoDocument`, `WorkUnit`, `RuntimeTruth`, `StyleRule`, `PreferencePack`, `Evidence` 등 |
| `object_id` | public-safe stable id |
| `scope` | user-global, repo-local, project, device, branch, runtime, corpus 등 |
| `lifecycle_status` | observed, extracted, proposed, accepted, current, stale, superseded, retired, rejected |
| `authority_status` | reference_only, candidate, accepted_current, proposal_only, runtime_verified, unverified |
| `relationships` | typed edge 목록 |
| `evidence` | source hash, commit, PR, test, runtime smoke, source ref, review decision 등 |
| `freshness` | 관찰 시점, source version, recheck 필요 여부 |
| `recommended_action` | keep, update, merge, supersede, archive, retire, request_evidence, review |
| `confidence` | 판단 신뢰도와 근거 |
| `gaps` | 확정하지 못하는 이유 |

## Palantir Corpus Research Inputs

### Corpus 상태

`Palantir Study` NotebookLM export는 local LBrain reference corpus 후보로 정규화되어 있다.

- raw Markdown: 65개
- JSON source metadata: 65개
- normalized Markdown: 65개
- source URL 있음: 39개
- manual text라 source URL 보강 필요: 26개
- source type: PDF 6개, web page 33개, text 26개
- ingest boundary: raw/normalized corpus는 local reference material이며 accepted/current memory가 아니다.

### 핵심 reference evidence

| Corpus id | 문서 | 요구사항으로 환원한 신호 |
| --- | --- | --- |
| `palantir-ontology-020` | Overview / Ontology | Ontology는 object/link/action/function/interface/application/governance를 한 모델로 다룬다. LBrain도 단일 문서 기능이 아니라 work knowledge substrate가 필요하다. |
| `palantir-ontology-017` | Ontology design best practices | object는 source schema가 아니라 real-world/domain concept를 모델링해야 한다. LBrain object도 파일/테이블 복사가 아니라 작업자가 묻는 domain object여야 한다. |
| `palantir-ontology-026` | The Ontology system | data, logic, action, security가 함께 decision model을 만든다. LBrain도 corpus/search만이 아니라 action/review/permission/audit layer가 필요하다. |
| `palantir-ontology-038` | Document Intelligence overview | document extraction은 strategy, evaluation, chunking, deployment를 가진 workflow다. LBrain corpus ingest도 extraction/eval/freshness lifecycle이 필요하다. |
| `palantir-ontology-050` | Logic core concepts | functions, blocks, evaluations, debugging은 처리 layer의 검증 단위다. LBrain processing layer도 extractor/evaluator/debug evidence를 남겨야 한다. |
| `palantir-ontology-056` | Palantir MCP overview | MCP는 context와 tool/action을 같이 제공한다. LBrain MCP도 context pack뿐 아니라 object 탐색, proposal, action gating을 분리해 제공해야 한다. |

### Palantir 개념의 LBrain 환원

Palantir Ontology를 그대로 복제하지 않는다. LBrain에 필요한 수준으로 다음처럼 환원한다.

| Palantir류 개념 | LBrain 요구사항 환원 |
| --- | --- |
| Object type | `KnowledgeObjectType`: repo 작업자가 묻는 domain object. 예: `RepoDocument`, `WorkUnit`, `RuntimeTruth`, `StyleRule`. |
| Link type | `KnowledgeEdgeType`: `derived_from`, `supersedes`, `impacts`, `validated_by`, `requires_evidence` 같은 typed relationship. |
| Action type | `KnowledgeActionType`: `propose_current`, `propose_stale`, `propose_supersede`, `accept_current`, `retire`, `request_evidence`. |
| Function / Logic | extractor, classifier, freshness checker, impact analyzer, context pack builder, evaluator. |
| Pipeline / Transform | corpus ingest, snapshot, chunking, extraction, object mapping, review proposal generation. |
| Branch / Proposal | accepted/current lane과 proposal/review queue 분리. |
| Security / Governance | scope, permission, raw content policy, public-safe projection, audit trail. |
| Toolchain / MCP | agent-facing context, object query, proposal/action APIs. |

## 현재 LBrain 구조에서 확인한 사실

### 이미 있는 씨앗

- `MemoryCard`는 card type, lifecycle, approval, freshness, currentness, supersedes를 가진다.
- `Brain Steward MCP`는 proposal-only model을 갖고 accepted/current와 review queue를 분리한다.
- `SourceRefRecord`는 source identity, device hash, relative path hash, content hash, sync policy를 가진다.
- `OntologyEpisode`는 derived graph episode로 entity type, lifecycle, currentness, source refs, relations를 가진다.
- `DocumentAuthorityCard`는 문서 path, status, evidence edge, archive proposal flag를 가진다.
- `PreferenceRuleCard`와 `RepoStyleProfile`은 preference/style authority의 초기 형태를 제공한다.
- `ContextPack`은 memory, graph, bridge, authority, gaps를 담을 수 있다.

### 부족한 부분

- `ReferenceCorpus`, `DocumentSnapshot`, `DocumentVersion`, `DocumentChunk`, `FreshnessCheck` 같은 corpus lifecycle object가 없다.
- `KnowledgeObject` 공통 substrate가 없어 문서, 코드 영향, 배포 truth, 선호, handoff가 서로 다른 임시 모델로 흩어진다.
- graph/search/archive hit이 accepted/current authority와 candidate evidence 중 어디에 속하는지 일관되게 설명하지 못한다.
- review queue가 비어 있을 때 필요한 candidate/proposal을 생성해야 한다는 품질 gap이 도출되지 않는다.
- `brain_context_resolve`에서 authority documents/workflows/preferences가 비어 있으면 사용자가 바로 쓸 next action을 얻기 어렵다.
- current/stale/superseded/retired 판단을 생성, 검증, 갱신하는 lifecycle이 기능별로 닫혀 있지 않다.
- local corpus 원문과 accepted/current memory 사이의 경계가 제품 요구사항으로 명확히 고정되어 있지 않다.

## Non-goals

- Phase 1에서 storage schema, DB table, MCP tool signatures, code path를 확정하지 않는다.
- Palantir Ontology를 그대로 복제하거나 enterprise-wide ontology product를 만들지 않는다.
- raw Palantir corpus, raw private transcript, secret, private runtime evidence를 public repo에 넣지 않는다.
- raw document text를 곧바로 accepted/current memory로 승격하지 않는다.
- graph/Qdrant/search mirror를 canonical authority로 만들지 않는다.
- UI를 이번 요구사항의 필수 조건으로 두지 않는다. CLI/worker/MCP ingest가 우선 가능해야 한다.
- live mutation, proposal write, ledger write, deployment/GitOps mutation은 승인된 design과 별도 실행 gate 전에는 수행하지 않는다.

## 선택된 요구사항 방향

이번 요구사항의 방향은 **LBrain Knowledge Object Substrate**다.

문서 전용 `brain_docs_*` 확장이 아니라, 문서/코드/세션/PR/배포/선호/외부 reference를 공통으로 표현하는 object substrate를 요구사항으로 잡는다. 단, 첫 구현 slice는 Palantir reference corpus와 repo documentation cleanup golden query를 검증 대상으로 삼을 수 있다.

핵심 원칙:

1. Raw corpus와 accepted/current authority는 분리한다.
2. Source schema를 그대로 object로 베끼지 않고, 작업자가 묻는 domain object를 모델링한다.
3. 모든 판단은 evidence edge와 freshness/gap을 가져야 한다.
4. Proposal/review lane은 accepted/current lane과 분리한다.
5. MCP는 context만 주는 것이 아니라 typed object 탐색과 proposal-safe action을 제공해야 한다.
6. Agent context pack은 Codex/Claude/Gemini/Hermes가 시작부터 repo/user preference와 authority gap을 반영하게 해야 한다.

### Corpus storage mode 결정

LBrain은 raw reference corpus 저장 방식을 하나로 고정하지 않고, corpus별 policy로 여러 storage mode를 지원해야 한다.

필수 storage mode:

1. `external_object_store`
   원문은 local/private filesystem 또는 object store에 두고, LBrain은 manifest, source metadata, hash, chunk summary, evidence edge, freshness state를 관리한다.
2. `managed_snapshot`
   LBrain이 raw 또는 redacted text snapshot/chunk를 직접 관리한다. 공개 reference docs, 사용자가 명시적으로 넣은 corpus, 재현 가능한 evaluation corpus에 사용할 수 있다.
3. `metadata_only`
   raw text나 chunk summary 없이 source identity, hash, locator, lifecycle, evidence edge만 관리한다. private source, 민감 repo/session 자료, 라이선스가 애매한 자료에 사용한다.

요구사항:

- `storage_mode`는 `ReferenceCorpus`와 `DocumentSource`에 명시되어야 한다.
- storage mode는 accepted/current authority 여부와 독립이다. raw snapshot이 있어도 자동으로 accepted/current가 되지 않는다.
- private/source-sensitive corpus는 기본값이 `metadata_only` 또는 `external_object_store`여야 한다.
- 공개 external reference corpus는 사용자가 승인하면 `managed_snapshot`을 허용할 수 있다.
- 각 mode는 freshness, hash verification, citation, re-ingest, deletion/retention policy를 가져야 한다.
- answer path는 raw body가 필요한 경우에도 public-safe summary와 citation/evidence ref를 우선 반환하고, raw body 반환은 별도 권한과 범위를 가져야 한다.

### OKF 역할 결정

OKF는 이번 mega-program에서 **export-only review/exchange companion**으로 포함한다.

역할:

- LBrain `KnowledgeObject`와 typed edge/evidence를 사람이 검토 가능한 bundle로 내보낸다.
- PR review, design review, handoff, 다른 AI tool 전달에 쓸 portable artifact가 된다.
- Markdown/YAML 기반 review surface로 사람이 diff를 볼 수 있게 한다.

명시적 비역할:

- OKF는 canonical store가 아니다.
- OKF는 authority ledger가 아니다.
- OKF는 ontology/object substrate 자체가 아니다.
- OKF import는 1차 mega-program 범위에 포함하지 않는다.

경계:

- Canonical truth는 LBrain object + ledger + authority/review lifecycle이다.
- Graph/search는 derived projection이다.
- OKF는 human/agent exchange surface다.
- OKF export에는 raw private body, secret, private runtime evidence, raw dataset/document id를 포함하지 않는다.

### UI scope 결정

1차 mega-program에서 UI는 제외한다.

범위:

- CLI
- worker pipeline
- MCP read/proposal-safe surfaces
- test/eval harness
- OKF export companion

비범위:

- review queue UI
- corpus management UI
- object graph browser UI
- HTML dashboard as official approval artifact

이유:

- 먼저 substrate, authority lane, storage mode, eval quality gate를 닫는다.
- UI는 review workflow와 object contract가 안정된 뒤 별도 요구사항으로 다룬다.
- 사람이 보기 쉬운 산출물은 OKF export companion과 Markdown report로 충분한 1차 review surface를 제공한다.

### Mega-program scope 결정

사용자는 4개 slice를 모두 포함한 대규모 작업으로 정의하기로 했다. 따라서 Phase 2 `design.md`는 단일 작은 feature design이 아니라 `LBrain Knowledge Object Substrate` mega-program design이어야 한다.

포함 slice:

1. `ReferenceCorpus -> KnowledgeObject`
   Palantir corpus ingest, metadata/hash/freshness gap, reference-only object query를 닫는다.
2. `Documentation Cleanup`
   기존 문서 정리 실패를 바로 고쳐 `RepoDocument`, `SupersessionEdge`, `AuthorityDecision`을 닫는다.
3. `Golden Query Evaluation`
   10개 이상 golden query evaluation harness로 현재 실패와 개선 결과를 측정한다.
4. `Agent Context Pack`
   Codex/Claude/Gemini/Hermes 시작 context와 preference/style/document authority pack을 개선한다.

범위 통제:

- 네 slice는 하나의 product program으로 묶되, 구현은 milestone/evidence gate로 분해한다.
- 각 slice는 독립 acceptance criteria와 regression evidence를 가져야 한다.
- slice 간 공통 substrate는 중복 구현하지 않는다.
- 하나의 mega-program이라도 raw corpus, proposal write, accepted/current promotion, live mutation은 각각 별도 safety gate를 가져야 한다.
- Phase 2는 전체 program architecture와 milestone dependency를 정의해야 하며, `agentic-execution`은 milestone별 act/observe/adjust 루프로 수행할 수 있어야 한다.
- 실행 모양은 단일 approved `design.md` + milestone별 `agentic-execution`으로 확정한다. 전체 architecture와 공통 substrate는 한 번에 승인하고, 각 slice는 milestone gate, evidence, rollback/gap 판단을 분리한다.

## 기능 요구사항

### FR1. Reference Corpus Lifecycle

LBrain은 외부 reference corpus를 1급 object로 다뤄야 한다.

필수 object:

- `ReferenceCorpus`
- `DocumentSource`
- `DocumentSnapshot`
- `DocumentVersion`
- `DocumentChunk`
- `ExtractionRun`
- `FreshnessCheck`
- `CorpusManifest`

필수 동작:

- normalized corpus manifest를 ingest 대상으로 인식한다.
- 문서별 source type, source URL presence, local snapshot hash, content hash, extraction status를 저장한다.
- URL이 없는 manual text source는 `source_url_status=missing_manual_text`와 `freshness_gap`을 가진다.
- raw corpus text는 reference material로 저장하되 accepted/current memory로 자동 승격하지 않는다.
- source URL이 있는 문서는 freshness recheck 대상이 될 수 있어야 한다.
- extraction/chunking/evaluation 결과는 별도 run object로 남긴다.

### FR2. Knowledge Object Substrate

LBrain은 모든 작업 지식을 공통 object envelope로 표현해야 한다.

공통 필드:

- `object_id`
- `object_type`
- `scope`
- `title`
- `summary`
- `lifecycle_status`
- `authority_status`
- `source_refs`
- `evidence_refs`
- `content_hash`
- `observed_at`
- `valid_from`
- `valid_to`
- `confidence`
- `review_state`
- `privacy_class`

초기 object type:

- `ReferenceDocument`
- `RepoDocument`
- `RepoFile`
- `Component`
- `McpTool`
- `Spec`
- `WorkUnit`
- `Session`
- `PullRequest`
- `Commit`
- `Test`
- `RuntimeSurface`
- `DeploymentTarget`
- `RuntimeTruth`
- `ChangeImpact`
- `StyleRule`
- `StyleProfile`
- `ArtifactPreference`
- `HtmlReviewProfile`
- `VisualizationProfile`
- `ToolHandoffContext`
- `Evidence`
- `AuthorityDecision`
- `ReviewProposal`

### FR3. Typed Relationship Model

LBrain은 object 간 관계를 typed edge로 표현해야 한다.

초기 edge type:

- `derived_from`
- `extracted_from`
- `references`
- `documents`
- `implements`
- `tests`
- `touches`
- `impacts`
- `depends_on`
- `supersedes`
- `superseded_by`
- `replaces`
- `contradicts`
- `validates`
- `deployed_by`
- `requires_live_evidence`
- `requires_review`
- `promoted_from`
- `rejected_as`
- `applies_to_repo`
- `applies_to_user`
- `matches_preference`
- `violates_preference`
- `violates_style`
- `used_for`

Edge는 direction, confidence, evidence refs, freshness를 가져야 한다.

### FR4. Action / Review Lifecycle

LBrain은 object를 바로 authoritative로 만들지 않고 proposal/review lifecycle을 가져야 한다.

필수 action:

- `propose_reference_object`
- `propose_current`
- `propose_stale`
- `propose_supersede`
- `propose_retire`
- `request_evidence`
- `accept_current`
- `reject_candidate`
- `commit_supersession`
- `commit_stale`
- `mark_runtime_unverified`

요구사항:

- agent는 proposal을 만들 수 있지만 accepted/current authority 변경은 restricted gate를 거친다.
- review queue가 비어 있더라도 query가 gap을 발견하면 candidate proposal 필요성을 답변에 노출한다.
- stale/supersede/retire 판단은 원본 object를 즉시 수정하지 않고 proposal로 남긴다.

### FR5. Authority Lane Separation

모든 query answer는 다음 lane을 분리해야 한다.

- accepted/current authority
- reference-only corpus
- proposal/review queue
- archive/session memory
- derived graph/search mirror
- runtime verified evidence
- unverified gap

합격 조건:

- reference-only corpus 문서를 current truth처럼 답하지 않는다.
- graph/search/archive hit을 ledger-backed authority처럼 답하지 않는다.
- accepted/current가 비어 있으면 empty authority를 명확히 표시한다.
- runtime truth는 last verified evidence와 unverified gap을 분리한다.

### FR6. Query Intent Routing

LBrain은 최소 golden query route를 가져야 한다.

필수 route:

- temporal work recall
- reference corpus research
- documentation cleanup
- stale/archive discovery
- current authority vs archive separation
- code change impact analysis
- PR merge truth
- deployment/runtime truth
- code style preference
- HTML/visualization preference
- handoff/debugging pack

각 route는 선택한 source lane, confidence, stop reason, missing evidence를 반환해야 한다.

### FR7. Documentation Cleanup Pack

문서 정리 질문에 대해 LBrain은 다음을 반환해야 한다.

- current authoritative docs
- active docs
- generated companions/previews
- stale/superseded/retired/archive candidates
- doc-to-doc supersession edges
- doc-to-concept edges
- recommended action
- evidence and freshness
- confidence/gaps

### FR8. Code Change Impact Pack

코드 또는 diff 변경 질문에 대해 LBrain은 다음을 반환해야 한다.

- impacted files/components/MCP tools/API contracts
- impacted docs/specs/tests
- runtime/deploy evidence 필요 여부
- known prior incidents or failed attempts
- recommended verification
- confidence/gaps

### FR9. Runtime Truth Pack

배포/운영 질문에 대해 LBrain은 merge, CI, deploy, live runtime을 분리해야 한다.

반환해야 할 object:

- `PullRequest`
- `Commit`
- `CIStatus`
- `DeploymentTarget`
- `RuntimeSurface`
- `RuntimeTruth`
- `LiveEvidenceGap`

합격 조건:

- merge만으로 deployed라고 답하지 않는다.
- public repo CI와 private deploy authority를 분리한다.
- live evidence가 없으면 `runtime_evidence_unverified`로 표시한다.

### FR10. Preference and Style Pack

LBrain은 user-global, repo-local, provider-specific, task-local preference를 분리해야 한다.

필수 profile:

- `PersonalCodeStyleProfile`
- `RepoStyleProfile`
- `HtmlReviewProfile`
- `VisualizationProfile`
- `ArtifactPreferencePack`
- `ReviewTonePreference`

요구사항:

- inferred preference와 accepted preference를 분리한다.
- 오래된 코드 관성은 style rule로 자동 확정하지 않는다.
- preferred examples는 raw file body가 아니라 hash, locator, redacted summary, evidence ref로 저장한다.
- Codex/Claude/Gemini/Hermes 시작 context에 compact preference pack을 제공할 수 있어야 한다.

### FR11. Agent Context Pack

LBrain은 agent가 작업 시작 시 바로 쓸 수 있는 compact context pack을 만들어야 한다.

포함 항목:

- current authority facts
- relevant reference corpus objects
- repo boundary and safety guardrails
- style/preference pack
- current work units and unfinished tasks
- required tests/runtime checks
- do-not-touch boundaries
- gaps and evidence needed

Consumer별 차이는 표시하되, 같은 authority substrate에서 생성해야 한다.

### FR12. Evaluation and Golden Queries

LBrain은 golden query 평가를 가져야 한다.

초기 golden queries:

```text
1. 어제 이 repo에서 뭐 했어?
2. 이 repo 문서 최신화하려면 뭘 봐야 해?
3. 오래된 문서/개념 후보 알려줘.
4. 이 파일 바꾸면 어떤 테스트/런타임 영향 있어?
5. 이 PR merge됐어? 배포도 됐어?
6. 지금 current SoT와 stale archive를 분리해서 말해줘.
7. 이 Palantir reference 문서는 공식/current/source URL 확인 가능 자료야?
8. 이 corpus에서 LBrain object model 설계에 필요한 개념만 뽑아줘.
9. 내 Java code style과 다른 diff를 찾아줘.
10. 내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.
```

합격은 검색 결과 반환이 아니라 object, edge, evidence, freshness, gap, recommended action을 포함한 답변이다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Safety | raw private transcript, secret, token, private runtime evidence, raw dataset/document id를 출력하거나 public repo에 저장하지 않는다. |
| Corpus boundary | raw external corpus는 local/private reference material이며 accepted/current memory가 아니다. |
| Authority | accepted/current는 ledger-backed authority만 가능하다. graph/search/archive는 candidate evidence다. |
| Reviewability | proposed object, stale, supersede, preference inference는 review queue에서 확인 가능해야 한다. |
| Traceability | 모든 answer object는 evidence refs, source hash 또는 missing evidence gap을 가져야 한다. |
| Freshness | source URL이 있는 external reference는 freshness check 대상이 되고, URL 없는 manual source는 보강 gap을 표시한다. |
| Portability | OKF/Markdown/YAML/JSONL 같은 exchange format은 가능하지만 canonical authority를 대체하지 않는다. |
| Privacy | public-safe projection과 raw/local/private store를 분리한다. |
| Testability | golden query eval과 object-level fixture가 있어야 한다. |
| Degradability | source lane이 비어 있거나 stale이면 답을 꾸미지 않고 lane/gap을 표시한다. |

## 사용자 시나리오

### S1. Palantir corpus 기반 LBrain substrate research

사용자가 Palantir reference corpus를 LBrain에 넣고 LBrain 설계에 참고하려 한다. LBrain은 raw corpus를 accepted memory로 승격하지 않고 `ReferenceCorpus`로 관리하며, object/action/security/freshness 관련 개념을 설계 implication과 rejected analogy로 분리한다.

### S2. Repo 문서 최신화/과거 문서 정리

사용자가 repo 문서 정리를 요청한다. LBrain은 current SoT, generated companion, historical docs, retired docs, stale candidates, supersession edge, 추천 action을 근거와 함께 반환한다.

### S3. 코드 변경 영향 분석

사용자가 특정 파일 또는 diff를 바꾸려 한다. LBrain은 affected components, tests, docs, MCP contracts, runtime surfaces를 object graph로 보여주고, 필요한 검증을 제안한다.

### S4. 배포/운영 truth 확인

사용자가 PR이 merge/deploy됐는지 묻는다. LBrain은 merge, CI, private deploy authority, live runtime evidence를 분리해서 답하고, 확인되지 않은 부분은 gap으로 둔다.

### S5. AI tool context injection

Codex/Claude/Gemini/Hermes가 repo 작업을 시작한다. LBrain은 current authority, safety boundary, style preference, unfinished work, relevant corpus object를 compact pack으로 제공한다.

### S6. 개인 code style / HTML review preference

사용자가 선호하는 code style 또는 HTML review artifact 기준을 기억시키고 싶어 한다. LBrain은 raw source/body를 저장하지 않고 pattern summary, evidence hash, example ref, review state를 저장하며, 새 diff/artifact를 suggestion으로 평가한다.

## Acceptance Criteria

요구사항이 승인되려면 다음을 만족해야 한다.

- 현재 LBrain 조회가 왜 저품질이었는지 object model/lane separation 관점에서 설명한다.
- Palantir corpus를 단순 비유가 아니라 object, link, action, function, pipeline, governance 개념으로 환원한다.
- raw corpus 저장, reference-only object, accepted/current memory의 경계를 명확히 분리한다.
- 문서 전용 기능이 아니라 문서, 코드 영향, 배포 truth, preference/style, handoff를 공통 substrate로 다룬다.
- 최소 3개 core use case를 비교한다: 문서 정리, 코드 변경 영향 분석, 배포/운영 truth 확인.
- 외부 corpus 65개가 LBrain ingest-ready가 되기 위한 metadata/hash/freshness/source URL gap 요구사항을 포함한다.
- current/stale/superseded/retired 판단이 proposal/review/accepted lifecycle을 통해 생성, 검증, 갱신되어야 함을 명시한다.
- accepted/current memory와 proposal/review queue가 분리되어야 함을 명시한다.
- agent context pack과 MCP read/proposal/action 경계가 요구사항에 포함된다.
- safety/privacy 요구사항은 public/private repo boundary와 raw/private data 금지를 반영한다.
- 최종 요구사항은 구현자가 `design.md` 후보 접근을 만들 수 있을 만큼 object vocabulary, edge vocabulary, lifecycle, golden queries를 포함한다.

## 확정된 Phase 2 결정

### D1. Mega-program execution shape

확정: 단일 approved `design.md` + milestone별 `agentic-execution`.

전체 architecture와 공통 substrate는 한 번에 승인한다. 실행은 다음 slice를 milestone gate로 분리한다.

- `ReferenceCorpus -> KnowledgeObject`
- `Documentation Cleanup`
- `Golden Query Evaluation`
- `Agent Context Pack`

각 milestone은 독립 done 정의, evidence, rollback/gap 판단을 가져야 한다.

### D2. Raw reference corpus 저장 정책

확정: 둘 다 지원한다.

LBrain은 `external_object_store`, `managed_snapshot`, `metadata_only` storage mode를 가져야 한다. mode 선택은 corpus별 policy로 결정한다. raw snapshot 보유 여부는 accepted/current authority 여부와 독립이다.

### D3. OKF 역할

확정: 1차 mega-program에는 OKF export-only companion을 포함한다.

OKF는 LBrain object/edge/evidence를 human/agent review bundle로 내보내는 형식이다. OKF는 canonical store, authority ledger, object substrate가 아니며, import는 후속 범위다.

### D4. UI 필요 여부

확정: UI는 1차 mega-program에서 제외한다.

CLI, worker pipeline, MCP read/proposal-safe surfaces, test/eval harness, OKF export companion만 포함한다. Review UI, corpus management UI, object graph browser UI는 후속 범위다.

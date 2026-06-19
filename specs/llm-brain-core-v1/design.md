# LLM-Brain Core v1 Design Spec

## Overview

LLM-Brain Core v1은 `neurons` 안에서 AI session, MemoryCard, source evidence, temporal ontology relation을 결합해 현재 작업 ContextPack과 장기 작업 기억을 제공한다. RAGFlow는 기존 문서/citation bridge로 보존하지만, canonical memory, ontology store, core acceptance path에서는 제외한다.

## Requirements Reference

- Phase 1 source: `specs/llm-brain-core-v1/requirements.md`
- Preview companion: `specs/llm-brain-core-v1/requirements.html`
- 승인 상태: 사용자 hard gate 사전승인으로 Phase 2 진행.
- 핵심 기능 요구사항:
  - `neurons`에 구현한다.
  - 기존 `dendrite`/`neurons` 수집 파이프라인과 안전한 `MemoryCard`/ledger/read-model 자산을 재사용한다.
  - RAGFlow disabled 상태에서 ContextPack, Decision drift, Incident search, PersonaFact check가 통과해야 한다.
  - graph DB는 derived index이며 raw/session/event SoT가 아니다.
  - RAGFlow는 external document bridge로만 남긴다.

## Approach Proposal

### Recommended: In-repo Brain Core with Derived Graph Adapter

`neurons`에 새 LLM-Brain core seam을 추가한다. Core는 `OntologyEpisode`, `GraphMemoryResult`, `ContextPack`, `SourceRef` 같은 backend-neutral model만 알며, Graphiti/Neo4j는 M6 이후 `GraphitiNeo4jGraphMemoryAdapter`에서만 다룬다.

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium |
| Cost | 기존 code reuse로 낮음 |
| Scalability | per-PC local + central shadow rebuild로 확장 |
| Team familiarity | 현재 repo ownership과 가장 잘 맞음 |

Pros:
- 기존 `neurons` server authority와 일치한다.
- 새 repo 이관 비용이 없다.
- RAGFlow dependency를 M0 invariant로 core에서 제거한다.
- autopilot은 검증 가능한 작은 milestone으로 자를 수 있다.

Cons:
- 기존 `session_memory` naming debt가 남는다.
- Graph adapter seam과 SourceRef contract를 새로 고정해야 한다.

### Alternative A: New `llm-brain` Repository

독립 제품성은 좋지만, 기존 `MemoryCard`, ledger, `brain_query`, autopilot, `dendrite`/`neurons` pipeline을 다시 연결해야 한다.

Decision: v1 이후 extraction candidate로만 둔다.

### Alternative B: RAGFlow Core Extension

기존 corpus 활용은 빠르지만, portable local brain, SourceRef, ontology drift, persona evidence, event replay 요구와 맞지 않는다.

Decision: core에서 제외한다.

## Authority Precedence

| Layer | Authority role | Winner? | Notes |
| --- | --- | --- | --- |
| CouchDB raw session | AI session raw SoT | Yes for raw session body | Raw body is not copied into graph or public output. |
| NATS/event ledger | replay/order/idempotency SoT | Yes for event replay | Defines `source_event_id`, idempotency, tombstone, retry state. |
| SessionMemoryArtifactStore | materialized session-memory SoT | Yes for session-memory artifact | RAGFlow-free acceptance gate. |
| MemoryCard ledger | accepted/superseded memory fact SoT | Yes for durable extracted memory | Existing `MemoryCard` lifecycle/currentness invariants preserved. |
| GraphMemoryAdapter | derived relation/search index | No | Can be stale/unavailable without changing canonical truth. |
| RAGFlow bridge | external document/citation fallback | No | Never wins over artifact or MemoryCard ledger. |

Any response that contains graph or bridge evidence must include status fields that distinguish canonical memory from derived index freshness and external bridge availability.

## Architecture

```text
                         per-PC local lane

AI tools ──► dendrite capture/spool ──► neurons ingest ──► CouchDB raw session
                    │                         │                  │
                    │                         │                  ▼
                    │                         │          SessionMemoryArtifactStore
                    │                         │                  │
                    │                         ▼                  ▼
                    │                 LLM-Brain Core       MemoryCard ledger
                    │                 - BrainReadService          │
                    │                 - ContextPackBuilder        │
                    │                 - OntologyEpisodeMapper     │
                    │                 - PersonaChecker            │
                    │                         │                    │
                    ▼                         ▼                    ▼
           SourceRef metadata         GraphMemoryAdapter ──► derived graph index
           from dendrite              (null/fake/Graphiti)

Agent/OpenClaw/Codex/Claude ── thin MCP/stdio/HTTP ──► Brain read API

                         optional bridge

Brain read API ──► RAGFlow bridge ──► existing Ubuntu RAGFlow corpus
                 (complex docs/citation only)
```

### Boundary Rules

- `dendrite` owns local provider hook, locator-only capture, local source catalog, same-device source resolution, thin shipper.
- `neurons` owns ingest, raw-session materialization, session-memory artifact, MemoryCard, ontology episode mapping, graph adapter, brain query, sync state, GC safety.
- `workspace-ragflow-advisor` owns Ubuntu RAGFlow operations and live RAGFlow management, not LLM-Brain product core.
- Agent-facing tools call Brain API only; they do not call Graphiti, Neo4j, CouchDB, or RAGFlow directly.
- M1-M5 must not instantiate a RAGFlow client, require network credentials, mutate Docker, or add new runtime dependencies.

## Data Flow

### Flow 1: Session to Core Memory

1. `dendrite` captures AI tool session locator and redacted enqueue payload.
2. `neurons` ingest writes/updates CouchDB raw session documents and event ledger entries.
3. `SessionMemoryArtifactBuilder` creates deterministic session-memory artifact from conversation chunks and tool evidence.
4. `MemoryCandidateMiner` extracts candidate `Task`, `Decision`, `Incident`, `PersonaFact`, `Fix`, `Verification`.
5. `MemoryCardService` accepts/supersedes candidates through existing approval policy.
6. `OntologyEpisodeMapper` maps artifacts and accepted/current MemoryCards into backend-neutral `OntologyEpisode`.
7. `GraphProjectionWorker` writes `OntologyEpisode` to the configured `GraphMemoryAdapter`; graph write lag is diagnostic only.
8. `BrainReadService` resolves ContextPack from artifact store + MemoryCard ledger + optional graph results.

### Flow 2: ContextPack Resolve

1. Agent submits `repository`, `branch`, `current_files`, `current_request`.
2. `BrainReadService` normalizes scope to `brain_id`.
3. Query planner searches:
   - current accepted MemoryCards;
   - recent session-memory artifacts;
   - optional `GraphMemoryResult` for `Task`, `Decision`, `Incident`, `File`, `PersonaFact`;
   - unresolved SourceRefs relevant to current files.
4. ContextPack builder returns latest task, last stop point, unfinished items, current decisions, similar incidents, persona constraints, source refs, confidence and gaps.
5. Response separates:
   - `memory_status`: canonical artifact/ledger read status;
   - `graph_status`: derived index freshness;
   - `bridge_status`: external document bridge status.

### Flow 3: Incident Search

1. Error/symptom text enters `brain_incident_search`.
2. Keyword + semantic + graph traversal search `Incident`, `Symptom`, `Attempt`, `RootCause`, `Fix`, `Verification`.
3. Results are ranked by similarity, project/repo/file proximity, recency/currentness, and verification strength.
4. Response separates reusable fixes from "similar but do not apply" cases.

### Flow 4: SourceRef Resolution

1. Graph stores opaque `source_ref_id`, `span_ref_id`, hashes, scope, device/root identifiers, timestamps, and sync policy.
2. Normal ContextPack never includes raw absolute paths or raw file bodies.
3. If an agent needs evidence, it calls `brain_evidence_get`.
4. `neurons` evaluates policy and returns one of:
   - metadata only;
   - redacted derived summary/span;
   - unresolved same-device required;
   - permission revoked;
   - stale hash;
   - deleted source;
   - bounded redacted content from a delegated same-device `dendrite source_resolve` action.
5. `neurons` itself does not directly read arbitrary PC files for central requests.

### Flow 5: Central Sync Shadow

1. Each PC may emit `BrainEvent` envelopes: source event, accepted MemoryCard delta, ontology episode payload, SourceRef metadata, tombstone.
2. Central deployment of `neurons` consumes envelopes with idempotency keys.
3. Duplicate and out-of-order events are ignored or quarantined deterministically.
4. Central graph is rebuilt/projected from event envelopes.
5. Graph DB data directories are never file-synced between PCs.

V1 designs the envelope and shadow replay gate. A production transport product is outside M1-M7 and must not block local brain milestones.

## Component Details

### LLMBrainCore Package

Proposed package: `worker/lib/agent_knowledge/llm_brain_core/`.

Purpose:
- isolate new core interfaces from legacy RAGFlow-centric module paths;
- expose stable interfaces consumed by CLI/MCP/autopilot;
- allow later extraction to a standalone repo without moving all `neurons` internals.

Dependencies:
- existing `agent_knowledge.session_memory` for safe MemoryCard envelope and promotion primitives;
- `Ledger` for accepted card and feedback records;
- artifact store interface;
- graph adapter interface.

Do not reuse without isolation before M9:
- `autopilot_cli.main`;
- `mine_live_candidates`;
- `autopilot_loop._autopilot_projection_approval`;
- `ragflow_projection.py` write path;
- `Ledger.promote_session_memory` path that requires RAGFlow dataset/document identifiers.

Safe reuse candidates:
- `LLMBrainMemoryService` canonical MemoryCard write methods, excluding projection execution;
- `memory_card.py`;
- `memory_miner.py` and `llm_brain_miner.py` with injected non-RAGFlow source spans;
- `memory_evaluation.py`;
- `memory_promotion.py`;
- `brain_query.run_brain_query_v2` read-model ideas, not response shape wholesale.

### SessionMemoryArtifactStore

Purpose:
- durable materialized session memory from CouchDB/session chunks/tool evidence;
- canonical input for current-work recall and candidate mining;
- no raw transcript duplication beyond existing CouchDB source;
- no RAGFlow dataset/document id requirement.

Core fields:

```json
{
  "artifact_id": "session-memory:<session_id_hash>:<version>",
  "session_id_hash": "sha256:...",
  "project": "neurons",
  "provider": "codex",
  "source_event_ids": ["..."],
  "chunk_refs": ["..."],
  "tool_evidence_refs": ["..."],
  "summary": "...",
  "content_hash": "sha256:...",
  "ontology_version": "1.0.0",
  "extractor_version": "0.1.0",
  "created_at": "..."
}
```

Initial implementation:
- use existing ledger/SQLite-compatible pattern if present;
- keep PostgreSQL/SQLite storage behind an interface;
- do not require Graphiti, Neo4j, or RAGFlow for artifact writes.

### OntologyEpisode and GraphMemoryResult

Core model, backend-neutral:

```json
{
  "episode_id": "ontology_episode:<hash>",
  "event_id": "evt_...",
  "idempotency_key": "ontology_episode:<hash>",
  "entity_type": "Decision",
  "natural_id": "decision:<project>:<hash>",
  "lifecycle_state": "accepted",
  "currentness": "current",
  "source_event_ids": ["evt_..."],
  "source_ref_ids": ["src_..."],
  "valid_from": "2026-06-18T00:00:00Z",
  "valid_to": "",
  "observed_at": "2026-06-18T00:00:01Z",
  "reference_time": "2026-06-18T00:00:00Z",
  "content_hash": "sha256:...",
  "ontology_version": "1.0.0",
  "extractor_version": "0.1.0"
}
```

`GraphitiNeo4jGraphMemoryAdapter` converts this model to Graphiti-specific episodes in M6. Core code does not import Graphiti types before M6.

### Ontology Model

Minimum schema invariants:

| Entity | Natural key | Lifecycle/currentness | Temporal fields |
| --- | --- | --- | --- |
| Session | `session_id_hash` | observed/imported/quarantined | `reference_time`, `ingested_at` |
| Task | project + normalized title/hash | current/superseded/resolved | `valid_from`, `valid_to` |
| Decision | project + decision hash | current/superseded | `valid_from`, `valid_to` |
| Incident | project + symptom/root hash | open/resolved/reopened | `observed_at`, `resolved_at` |
| PersonaFact | scope + predicate + value hash | candidate/accepted/superseded/rejected | `valid_from`, `valid_to` |
| SourceRef | opaque `source_ref_id` | active/deleted/revoked/unresolved | `last_seen_at`, `deleted_at` |
| SpanRef | opaque `span_ref_id` | active/stale/revoked | `observed_at` |

Minimum relations:

```text
Session WORKED_ON Task
Task AFFECTS Repository
Task TOUCHES File
Decision SUPERSEDES Decision
Incident HAS_SYMPTOM Symptom
Attempt TESTS Hypothesis
Fix RESOLVES Incident
Commit IMPLEMENTS Fix
PersonaFact EVIDENCED_BY SourceRef
Document DESCRIBES Project
SourceRef LOCATES FileSnapshot
SpanRef DERIVED_FROM SourceRef
```

### SourceRef Catalog Boundary

`dendrite` produces local source catalog events. `neurons` consumes only metadata and policy state.

SourceRef shape stored in `neurons` and graph:

```json
{
  "source_ref_id": "src_<opaque_hash>",
  "device_id_hash": "sha256:...",
  "root_id": "project-root",
  "relative_path_hash": "sha256:...",
  "content_hash": "sha256:...",
  "mtime": "...",
  "size": 1234,
  "sync_policy": "metadata_only",
  "permission_scope": "project",
  "last_seen_at": "...",
  "deleted_at": ""
}
```

No raw `relative_path` or absolute path is stored in central graph by default.

`brain_evidence_get` request must include:

```json
{
  "source_ref_id": "src_...",
  "span_ref_id": "span_...",
  "requesting_device_id_hash": "sha256:...",
  "approval_ref": "",
  "max_bytes": 4096,
  "redaction_profile": "public_safe"
}
```

Response must include:

```json
{
  "resolution_state": "metadata_only",
  "reason_code": "policy_metadata_only",
  "policy": "metadata_only",
  "same_device_proof": "not_required",
  "approval_ref": "",
  "audit_event_id": "audit_...",
  "content": "",
  "metadata": {}
}
```

### Brain Read API

Initial internal API surface:

```text
brain_context_resolve
brain_memory_search
brain_incident_search
brain_persona_check
brain_evidence_get
```

M1 fixes the internal `BrainReadService` contract. M8 adds thin MCP/stdio wrappers. MCP is not a separate gateway product.

### RAGFlow Bridge

Purpose:
- search existing Ubuntu RAGFlow corpus for complex document/citation use cases;
- migrate needed evidence into SourceRef/episode form;
- preserve existing RAGFlow while core matures.

Rules:
- M0 invariant: not core, not canonical, not default dependency.
- no RAGFlow client instantiation before M9;
- no RAGFlow delete/disable in ordinary autopilot;
- RAGFlow result never wins over canonical artifact or MemoryCard state;
- RAGFlow failures produce bridge unavailable diagnostics, not core failure.

### BrainEvent Envelope

V1 central sync shadow uses this minimal envelope:

```json
{
  "event_id": "evt_...",
  "idempotency_key": "brain_event:<hash>",
  "device_id_hash": "sha256:...",
  "event_type": "memory_card_delta",
  "occurred_at": "...",
  "observed_at": "...",
  "ontology_version": "1.0.0",
  "payload_hash": "sha256:...",
  "tombstone": false,
  "payload": {}
}
```

Ordering rule:
- first order by `occurred_at`;
- tie-break by `event_id`;
- duplicate `idempotency_key` is ignored;
- conflicting tombstone/current deltas go to quarantine unless supersession relation resolves them.

## Error Handling

Durable state enum:

```text
pending
running
succeeded
retryable_failed
quarantined
terminal_failed
```

| Scenario | State | Recovery |
| --- | --- | --- |
| CouchDB unavailable | `retryable_failed` | retry with same idempotency key; do not mine candidates. |
| Extractor timeout | `retryable_failed` | bounded retry; after threshold quarantine. |
| Invalid candidate envelope | `quarantined` | keep source refs and validation reason; no auto-accept. |
| Partial graph write | `retryable_failed` | ledger/artifact remains winner; retry projection. |
| Permission revoked SourceRef | `terminal_failed` for content fetch | keep metadata; return `permission_revoked`. |
| Poison event / schema mismatch | `quarantined` | no graph projection until migration/review. |
| Ontology migration missing | `quarantined` | require explicit migration handler. |
| RAGFlow bridge down | bridge unavailable | core query succeeds with bridge gap. |

## Testing Strategy

### Contract Tests

- `BrainReadService` request/response schema.
- `GraphMemoryAdapter` contract with `NullGraphMemoryAdapter` and fake adapter.
- `SourceRef` policy golden outputs for `metadata_only`, `derived_only`, `local_only`, `full_sync`, revoked, deleted, stale hash.
- `BrainEvent` replay: duplicate, out-of-order, tombstone, conflict quarantine.
- no RAGFlow client instantiation before M9.

### Unit Tests

- `SessionMemoryArtifactStore` writes/read/idempotency without RAGFlow.
- MemoryCard to `OntologyEpisode` mapping.
- ContextPack builder ranking and redaction.
- Incident search ranking and "do not apply" classification.
- PersonaFact candidate/check states.

### Integration Tests

- CouchDB fixture session -> artifact -> MemoryCard candidate -> ontology episode -> ContextPack.
- RAGFlow disabled mode: all core acceptance gates pass.
- graph-disabled degradation: ContextPack returns canonical memory with `graph_status=unavailable`.
- RAGFlow bridge fake client: bridge result is included as document evidence but does not override canonical memory.
- MCP/stdio roundtrip for `brain_context_resolve` and `brain_incident_search` after M8.

### Required Negative Tests

```text
test_llm_brain_core_ragflow_disabled.py
test_autopilot_no_ragflow_client_before_m9.py
test_contextpack_no_raw_source_refs.py
test_brain_event_replay_idempotency.py
test_source_ref_policy_resolution.py
```

### Regression Tests

- Existing `worker` llm-brain/autopilot tests remain green where they are not RAGFlow-live paths.
- Existing `dendrite`/`neurons` boundary tests remain green.
- No raw transcript/private path/dataset id/document id in public outputs.

Recommended local check set:

```bash
cd worker
uv run pytest -q tests/test_llm_brain_integration.py \
  tests/test_llm_brain_miner.py \
  tests/test_autopilot_cli.py \
  tests/test_autopilot_loop.py \
  tests/test_brain_query.py \
  tests/test_neuron_mcp_stdio.py
```

Full worker check:

```bash
cd worker
uv run pytest -q
```

## Tech-Debt Paydown Plan

| Phase | Item | Done |
| --- | --- | --- |
| Quick win | Document RAGFlow as bridge, not core | README/spec wording no longer implies RAGFlow is canonical brain. |
| Quick win | Split query correctness from projection freshness | ContextPack response has separate `memory_status`, `graph_status`, `bridge_status`. |
| Quick win | Isolate unsafe autopilot reuse | `autopilot_cli.main`, live mining, self-minted projection approval blocked before M9. |
| Investment | Add graph adapter seam | `GraphMemoryAdapter` + null/fake tests exist. |
| Investment | Add ContextPack builder | RAGFlow-disabled latest-work acceptance test passes. |
| Investment | Add SourceRef contract | metadata-only evidence path and redaction tests pass. |
| Watch | LinkML/TerminusDB schema governance | Activate only if ontology migrations become a governance bottleneck. |

## Milestones

### M0: Design Freeze and Safety Baseline

Done:
- `requirements.md` and `design.md` approved.
- RAGFlow demotion is documented as invariant, not a late milestone.
- Existing targeted tests pass or legacy RAGFlow-live tests are explicitly classified as bridge/compat.
- No live mutation.

### M1: Core Contracts and Safety Guards

Scope:
- add `agent_knowledge.llm_brain_core` package skeleton;
- define `BrainReadService`, `ContextPack`, `SourceRef`, `BrainEvent`, `OntologyEpisode`, `GraphMemoryAdapter`;
- implement `NullGraphMemoryAdapter`;
- add guard test that M1-M5 code path does not instantiate RAGFlow clients.

Done:
- RAGFlow-disabled ContextPack fixture returns structured gaps rather than failure.
- `test_autopilot_no_ragflow_client_before_m9.py` exists.
- No existing public CLI break.

### M2: RAGFlow-free Artifact and Replay Store

Scope:
- implement `SessionMemoryArtifactStore` behind interface;
- map CouchDB/session fixture to artifact without `ragflow_dataset_id` or `ragflow_document_id`;
- add `BrainEvent` idempotent replay model.

Done:
- artifact write/read/idempotency tests pass without RAGFlow.
- duplicate/out-of-order/tombstone replay tests pass.

### M3: SourceRef Resolver Contract

Scope:
- define opaque `source_ref_id`/`span_ref_id` model;
- implement policy evaluator for `brain_evidence_get`;
- add golden outputs for metadata-only, derived-only, local-only, revoked, stale, deleted.

Done:
- raw path/body never appears in normal ContextPack or graph payload.
- `test_source_ref_policy_resolution.py` passes.

### M4: ContextPack Builder

Scope:
- implement latest-work resolver from artifacts + accepted MemoryCards + null/fake graph results;
- split `memory_status`, `graph_status`, `bridge_status`.

Done:
- latest task, stop point, unfinished items, current decisions, persona constraints return in one ContextPack.
- RAGFlow disabled test passes.

### M5: Incident, Drift, Persona

Scope:
- incident replay response;
- decision supersession/drift explanation;
- PersonaFact candidate/check states;
- "similar but do not apply" lane.

Done:
- incident fixture returns Symptom, Attempt, Fix, Verification.
- drift fixture explains prior and current Decision.
- `aligned`, `possible_conflict`, `persona_drift`, `insufficient_evidence` fixture tests pass.

### M6a: Graph Adapter Interface and Fake Backend

Scope:
- implement fake adapter contract and ontology episode mapping.

Done:
- fake graph integration proves upsert/search without external dependency.
- core tests still pass with graph disabled.

### M6b: Graphiti/Neo4j Dependency Approval Gate

Scope:
- prepare dependency/lockfile/Docker change proposal only.

Done:
- exact dependency and compose changes listed.
- stop for explicit approval before lockfile or Docker mutation.

### M6c: Graphiti/Neo4j Ubuntu Integration

Scope:
- implement `GraphitiNeo4jGraphMemoryAdapter`;
- run integration against the Ubuntu container deployment target only after dependency/Docker approval.

Done:
- episode upsert/search works.
- disabling graph still leaves core ContextPack green.

### M7: Central Sync Shadow

Scope:
- implement `BrainEvent` envelope replay shadow for local and central deployment modes;
- no production transport product yet.

Done:
- duplicate/out-of-order/tombstone/conflict tests pass.
- central rebuild from event fixture produces deterministic ontology episodes.

### M8: Thin MCP/stdio Surface

Scope:
- expose `brain_context_resolve`, `brain_memory_search`, `brain_incident_search`, `brain_persona_check`, `brain_evidence_get` through existing `neuron-knowledge mcp-stdio` pattern.

Done:
- MCP/stdio roundtrip tests pass.
- tools do not expose raw backend identifiers or private paths.

### M9: RAGFlow Bridge Compatibility

Scope:
- existing RAGFlow projection/search remains as optional bridge;
- response status separates `core_memory`, `derived_graph`, and `ragflow_bridge`.

Done:
- RAGFlow unavailable does not fail core ContextPack.
- bridge hit is labeled as external document evidence.
- no RAGFlow write/delete/disable is added without separate approval.

## Autopilot Execution Contract

Recommended design worktree:

```text
cwd: /Users/example/Projects/neurons/.worktrees/llm-brain-core-design
branch: codex/llm-brain-core-design
mode: docs/design first, then agentic-execution after design approval
```

After design approval, implementation autopilot should start a fresh dedicated worktree, for example:

```text
branch: codex/llm-brain-graph-core-m1
worktree: /Users/example/Projects/neurons/.worktrees/llm-brain-graph-core-m1
first milestone: M1 only
```

Stop conditions:
- acceptance test requires live RAGFlow mutation;
- any M1-M5 code path needs RAGFlow client instantiation, network, credential, dataset id, or document id;
- raw private transcript/file body is needed;
- raw backend ids or private paths appear in public output;
- design SoT must change;
- dependency or lockfile change is needed before M6b approval;
- Docker/systemd/firewall/package/credential mutation is needed;
- existing boundary tests fail for reasons outside the milestone.

## Resolved V1 Boundaries

- Neo4j is the v1 default graph backend recommendation for the Ubuntu container deployment target.
- Graph core stays in `neurons` for v1. Moving it to a separate repo is not part of the v1 contract.
- Central sync v1 owns `BrainEvent` envelope, idempotent replay, and rebuild shadow. Production transport is a separate interface and is not part of v1.

## Review Fixes Applied

- Added authority precedence table so graph is derived index, not canonical winner.
- Moved RAGFlow demotion to M0 invariant and renamed final milestone to bridge compatibility.
- Replaced direct Graphiti leakage with backend-neutral `OntologyEpisode` and `GraphMemoryResult`.
- Made SourceRef opaque and delegated same-device content fetch to `dendrite`.
- Added `BrainEvent` envelope, ordering, tombstone, duplicate, and conflict rules.
- Split graph integration milestone into interface, dependency approval, and Ubuntu integration stages.
- Added negative tests and stop conditions for RAGFlow-coupling regression.

## Self-Review

- No implementation code is included.
- RAGFlow is not required for core acceptance.
- Raw SoT, artifact store, MemoryCard ledger, graph index, and bridge roles are separated.
- Milestones are independently testable and autopilot-friendly.
- V1 non-goals are explicit boundaries, not hidden acceptance gaps.

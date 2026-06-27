# Neurons Context Authority Roadmap

## Status

This roadmap is the post-`LLM-Brain Core v1` product roadmap for turning
`neurons` from a memory/retrieval system into a Context Authority Engine.

This is a roadmap artifact, not a `grill-to-spec` `requirements.md` /
`design.md` pair. It uses the grill-to-spec questioning style to fix product
direction, milestone order, and execution gates.

Primary references:

- `specs/llm-brain-core-v1/requirements.md`
- `specs/llm-brain-core-v1/design.md`
- `specs/llm-brain-core-v1/implementation-matrix.md`
- `specs/llm-brain-bulk-semantic-lane/requirements.md`
- `specs/llm-brain-bulk-semantic-lane/design.md`
- `docs/specs/2026-06-21-qdrant-docling-searchable-mirror/requirements.md`
- `docs/specs/2026-06-24-qdrant-mirror-cutover/requirements.md`
- GitHub issues #25 and #27 for container baseline and k3s PoC

Historical migration docs may still mention retired RAGFlow lanes. This roadmap
does not include RAGFlow as an active architecture component.

## Product Frame

`neurons` should not be framed as a memory storage system. It should become a
Context Authority Engine.

The product promise:

> A coding agent should not repeatedly ask the user to restate decisions,
> workflows, document status, preferences, repo boundaries, or prior
> corrections. `neurons` should synthesize the currently authoritative context
> and provide it at task start.

The roadmap shape is product + execution hybrid:

- first define the product direction;
- then deliver the first agent-start Context Authority Pack;
- then deepen the evidence-backed cards and APIs;
- then add local/central federation after the evidence layer is strong enough.

The center of gravity:

```text
Raw Memory
  -> Semantic Graph / Search Mirrors
  -> Authority Synthesis
  -> Context Authority Pack
  -> Reviewable Authority Bundle
  -> Local / Central Federation
```

The important upgrade is not more recall. It is deciding which recalled item is
current, relevant, superseded, stale, generated, private, or actionable.

## Current Foundation

`LLM-Brain Core v1` has already established the core runtime shape:

- CouchDB keeps raw AI session source.
- Ledger-backed state keeps accepted memory, replay, and lifecycle decisions.
- `SessionMemoryArtifactStore` materializes session-memory without requiring a
  retired document bridge runtime.
- `BrainReadService` and MCP/stdio expose backend-neutral read APIs.
- Graphiti/Neo4j exists as a derived graph index, not raw source of truth.
- Qdrant/Docling is scoped as a searchable mirror for PDF/doc/document support,
  not canonical authority.
- Dendrite owns local capture and source resolution; `neurons` owns server-side
  brain authority.
- Bulk semantic lane moves expensive Graphiti entity/relation extraction out of
  the hot path.

This roadmap should not rebuild that foundation. It adds authority products on
top of it.

## Component Roles

| Component | Roadmap role | Authority stance |
| --- | --- | --- |
| CouchDB | Raw transcript/session source | Raw source authority |
| Ledger / current state adapter | Current lifecycle, replay, accepted memory, approval state | Current implementation seam; PG/SQLite are storage adapters to hide behind authority-card stores |
| Neo4j | Queryable relationship graph and Authority Graph Workbench surface | Derived index and inspection UI, never raw truth by itself |
| Graphiti | Graph episode/entity/relation extraction and Neo4j writer/reader adapter | Extraction/projection layer, not product authority |
| Qdrant | Searchable mirror for document/PDF/doc support and fuzzy recall | Mirror only, must authority-join before product use |
| Dendrite | Local capture, tool/file/git event sensing, same-device source resolve | Sensor, not brain |
| neurons-local | Per-PC local index, file/session graph, offline recall, sync artifact | Local authority for that device's observed facts |
| neurons-central | Cross-device federation, dedupe, authority synthesis, context pack | Product authority layer |
| Hermes | Read-only Context Authority consumer in this roadmap | Same consumer level as Codex/Claude Code; self-improvement/proposal loop out of scope |

## Boundary Guardrails

These guardrails apply to every milestone. Each milestone's done criteria must
include a short boundary cross-check.

- Agent-facing products call `neurons` brain APIs, not Graphiti/Neo4j/Qdrant
  directly.
- Neo4j is a derived relationship substrate and Authority Graph Workbench
  surface. It does not replace `neurons`.
- Graphiti is the semantic extraction/projection path. It is not the product
  authority layer.
- Qdrant is a searchable document mirror. It is not canonical memory.
- Retired RAGFlow lanes must not be reintroduced as core Context Authority.
- Dendrite captures local evidence and source locators. It does not make
  authority decisions.
- Central does not replicate all PC files. It stores metadata, hashes, derived
  summaries, source refs, and authority artifacts by policy.
- Graph DB files are not synced between PCs.
- HTML outputs are first-class inventory objects, but usually generated
  companions or human previews, not source of truth.
- Archive/delete recommendations are proposals only. No automatic delete.
- Hermes self-improvement, proposal generation, skill updates, and cleanup loops
  are outside this roadmap.

## Neo4j Workbench Strategy

Separate UI burden from product authority. Do not build a custom operations UI
first. Project authority cards and evidence edges into Neo4j and use Neo4j
Browser/Bloom/Workspace as the first human graph workbench.

Neo4j is strong at:

- graph storage and traversal;
- Cypher query and visual inspection;
- vector/full-text/hybrid search surfaces;
- GraphRAG and MCP-enabled graph access;
- authority graph exploration for humans and power users.

The boundary is not because Neo4j is weak. The boundary exists because product
authority still requires application policy:

- document source-of-truth vs generated companion status;
- stale/superseded/archive candidate lifecycle;
- workflow contract and skill evolution evidence;
- user preference scope/confidence/exception policy;
- local privacy and same-device source resolution;
- agent-start Context Pack response contract.

The right position:

```text
Neo4j = derived relationship substrate + authority workbench
Graphiti = semantic projection / extraction path
Qdrant = searchable document mirror
neurons = authority synthesis and agent-facing brain API
```

Neo4j MCP can be useful for operator/debug graph inspection and advanced agent
debugging. The default coding-agent product API is still
`brain_context_resolve`, because it combines graph hits with ledger authority,
document mirror status, privacy policy, workflow contracts, preferences, and
evidence gaps.

## External Patterns To Absorb

The recent AI brain research notes point toward the same architecture:

- AI Research OS: preserve immutable raw source, machine-readable index, and
  human-readable wiki; useful queries should save back durable knowledge.
- Claude Second Brain levels: choose the simplest retrieval layer that solves
  the pain point; routers, wiki/index, vector search, graph, and always-on sync
  are different tools, not a mandatory ladder.
- AutoResearch: long-running agent work needs clear objectives, immutable
  evaluation, constrained edit surface, measurable gates, and ratcheting
  improvements.
- OKF: folder/Markdown/frontmatter/link bundles are portable knowledge
  artifacts, not a vector DB replacement. They are strong as human/agent-readable
  authority surfaces above indexes.

For `neurons`, durable product artifacts should often be source-grounded cards
or Markdown/OKF-style bundles, not only graph nodes or vector hits.

## Roadmap Principles

1. Authority first, search second.
   Search finds candidates. Product value comes from deciding what currently
   wins.

2. Context7-style dynamic resolver first, hybrid artifact later.
   M1 starts by extending `brain_context_resolve`. The long-term target is MCP
   tools plus reviewable Markdown/OKF bundles.

3. Neo4j workbench before custom UI.
   Project authority cards and evidence edges into Neo4j so Browser/Bloom/
   Workspace can serve as the initial human UI. Build custom UI only after a
   real screen gap remains.

4. Evidence-backed over guessy.
   If a Git repo exists, document authority uses files, HTML companions,
   sessions, commits, PRs, and live evidence. Without Git, it still uses files,
   HTML companions, and session evidence.

5. Scope and confidence are mandatory.
   Workflow defaults and user preferences must carry scope, evidence,
   confidence, and exceptions.

6. Infrastructure follows product proof, except compose baseline.
   Compose/container hardening runs in a parallel infra track with M1. k3s stays
   a later PoC.

## Product Milestones

### M1: Project Context Authority MVP

Goal: give Codex/Claude Code class coding agents an authoritative startup
Context Pack for one repo, starting with `neurons`.

Primary user:

- Codex/Claude Code coding agent first.
- Hermes is only a read-only consumer at the same level as coding agents.

Delivery:

- Extend `brain_context_resolve` with authority sections.
- Use MCP smoke to prove a coding agent can receive the pack.
- Project first authority nodes/edges into Neo4j for Browser/Bloom/Workspace
  inspection.
- Long-term path remains MCP tool + Markdown/OKF bundle, but M1 starts with the
  dynamic resolver.

Context Pack contents:

- evidence-backed current docs;
- workflow defaults from skill files and session evidence;
- user preferences with scope/confidence;
- risk-ranked evidence gaps with action checklist;
- boundary guardrail summary.

Document inventory rules:

- Include Markdown docs, HTML previews/generated companions, milestone files,
  implementation matrices, review docs, and generated human-readable artifacts.
- If Git exists, connect documents to session, commit, PR, and live/runtime
  evidence.
- If Git does not exist, connect documents to file inventory and session
  evidence.

Done when:

- A real `neurons` repo Context Authority Pack is generated.
- `brain_context_resolve` returns authority sections.
- Codex/Claude Code/Hermes can read it as equivalent read-only consumers.
- MCP smoke proves an agent receives the pack.
- Neo4j workbench can inspect the same authority graph without becoming the
  default agent API.
- Runtime claims without Ubuntu evidence are surfaced as evidence gaps, not
  trusted facts.
- Boundary cross-check passes.

### M2: Document Authority Synthesis

Goal: turn M1's document/evidence logic into a formal Document Authority model.

Core objects:

- `DocumentNode`
- `DocumentSnapshot`
- `DocumentEvidenceEdge`
- `DocumentAuthorityCard`
- `DocumentStatus`

Statuses:

- `source_of_truth`
- `active`
- `generated_companion`
- `human_preview`
- `historical`
- `superseded`
- `stale`
- `archive_candidate`
- `unknown`

Read paths:

- `brain_docs_current`
- `brain_docs_explain`
- `brain_docs_archive_candidates`

Done when:

- Each known doc can get status, reason, confidence, and evidence refs.
- Markdown source and HTML/generated companion are not confused.
- Session, commit, PR, and live evidence can support status when available.
- Archive/delete remains proposal-only.
- M1 Context Pack consumes `DocumentAuthorityCard`.
- Boundary cross-check passes.

### M3: Workflow Contract Memory

Goal: formalize repeated work procedures as evidence-backed workflow contracts.

Initial contracts:

- use grill-to-spec-style questioning for fuzzy roadmap/spec work;
- after approved design/roadmap, use agentic-execution-style long loop for
  implementation;
- keep TDD-first and evidence-gated act/observe/adjust loops for code work;
- use dedicated worktree/branch before edits;
- verify `neurons` runtime claims against `ragflow-ubuntu`;
- keep backend boundaries: Neo4j/Qdrant/Graphiti behind brain APIs.

Core objects:

- `SkillEvolutionCard`
- `WorkflowDefaultCard`
- `WorkflowContractCard`

Done when:

- Current skill files and prior session corrections/evolution evidence are
  connected.
- Workflow defaults include scope, evidence, confidence, reason, and exceptions.
- Context Pack consumes workflow contracts.
- No skill update/proposal loop is introduced.
- Boundary cross-check passes.

### M4: User Preference Memory

Goal: formalize user preferences as scoped, evidence-backed cards.

Preference classes:

- global communication preferences;
- repo-specific runtime/proof preferences;
- task-specific roadmap/spec/documentation preferences;
- writing style preferences;
- operational preferences such as audit-first, runtime truth first, and
  exact-number choice following.

Core object:

- `PreferenceRuleCard`

Required fields:

- rule;
- scope;
- evidence;
- confidence;
- applies_when;
- exceptions.

Done when:

- Context Pack consumes preference cards.
- Workflow contracts and preferences are clearly separated.
- Preferences can be global, repo-level, or task-level.
- Boundary cross-check passes.

### M5: OKF/Markdown Authority Bundle

Goal: export M1-M4 authority results into reviewable Markdown/frontmatter
bundles.

Candidate layout:

```text
context-authority/
  index.md
  log.md
  documents/
  workflows/
  preferences/
  evidence-gaps/
```

Done when:

- Bundle export includes source, evidence, status, confidence, and generated
  artifact markers.
- The bundle is readable by humans and agents.
- Git diff/review is possible.
- Context Pack and bundle drift can be detected.
- Boundary cross-check passes.

### M6: Context Pack API Hardening

Goal: harden the M1 dynamic resolver into a stable agent-start product API.

Scope:

- Codex / Claude Code / Hermes read-only consumer shapes;
- compact/full output modes;
- token budget handling;
- freshness and degraded-mode fields;
- graph/Qdrant/document-mirror unavailable behavior;
- evidence gap response contract;
- backend boundary hiding.

Done when:

- Agent-specific MCP smokes pass.
- Compact/full response contracts are tested.
- Stale/degraded state tests pass.
- OKF bundle and Context Pack drift check exists.
- Neo4j workbench projection remains consistent with API output.
- Boundary guardrail tests pass.

### M7: Dendrite Local Evidence Capture

Goal: strengthen the local evidence layer required for later local/central
federation.

Dendrite captures:

- tool event normalization;
- file read/edit/mention events;
- generated-doc events;
- repo-relative paths;
- content hashes;
- git branch/diff/commit state;
- local source locators;
- redaction and sync-policy hints.

Dendrite does not decide:

- document authority;
- archive candidates;
- workflow defaults;
- cross-device dedupe;
- graph synthesis.

Done when:

- `Session -> File` and `Commit -> File` edge inputs are reliably available.
- `neurons-local` can consume the capture artifacts later.
- Raw local file bodies remain local by default.
- Boundary cross-check passes.

### M8: neurons-local per-PC Brain

Goal: give each PC a standalone local memory/index node.

Scope:

- local session memory;
- local file manifest and snapshots;
- session-file graph;
- local semantic extraction;
- offline recall;
- sync artifact generation;
- central retry queue;
- local privacy policy enforcement.

Models:

- `FileIdentity`
- `FileSnapshot`
- `SessionFileEdge`
- `LocalContextPack`
- `SyncArtifact`

Done when:

- A PC can answer local context questions offline.
- It can emit central-safe sync artifacts.
- Central sync can be rebuilt from redacted/derived artifacts rather than raw
  file bodies.
- Boundary cross-check passes.

### M9: neurons-central Federation

Goal: merge multiple PC memory nodes without unsafe file or graph DB sync.

Scope:

- device identity;
- repo identity;
- cross-device file identity dedupe;
- sync artifact ingest;
- central authority synthesis;
- tombstone and deletion policy;
- conflict explanation.

Models:

- `DeviceNode`
- `RepoIdentity`
- `GlobalFileIdentity`
- `FederatedAuthorityCard`
- `ArchivePlan`

Done when:

- Central can synthesize project authority from multiple devices.
- Conflicts are visible and explainable.
- Graph DB files and raw PC file bodies are not synced.
- Boundary cross-check passes.

### M10: Repo Style Profile

Goal: extract codebase style and architecture taste after file/session/git
evidence and federation are strong enough.

Scope:

- architecture patterns;
- module boundaries;
- test strategy;
- comments and naming;
- refactor preferences;
- failure modes the user rejected.

Core objects:

- `RepoStyleProfile`
- `CodebaseTasteCard`
- `ArchitecturePreferenceCard`

Done when:

- A current task can import a scoped style profile from past repos without
  rereading the entire archive codebase.
- Style claims link to concrete files, commits, sessions, and repo scope.
- The system can distinguish user preference from accidental historical code.
- Boundary cross-check passes.

## Parallel Infra Track

### Infra-A: Container Baseline / Compose Hardening

Timing: in parallel with M1.

Source: GitHub issue #25.

Goal: stabilize the current deployment surface without turning the product
roadmap into an infrastructure migration.

Scope:

- Dockerfile and compose baseline;
- healthcheck, restart, volumes, config checks;
- image tag and CI build/config validation;
- Qdrant/document-mirror/external state-store contract clarity;
- safe shadow/delivery-off defaults.

Done when:

- The runtime is reproducible enough for Context Authority work.
- Compose remains the near-term operational target.
- This does not imply production k3s migration.

### Infra-B: k3s PoC

Timing: after M1-M3 product proof.

Source: GitHub issue #27.

Goal: test orchestration fit without forcing production migration.

Scope:

- non-production namespace;
- Tailscale/private access policy;
- stateless worker/MCP canary first;
- no stateful DB migration first;
- rollback to compose remains straightforward.

Production k3s migration is not part of this roadmap. Re-evaluate after M6 or
after federation stabilizes.

## Recommended Ordering

```text
Product:
  M1  Project Context Authority MVP
  M2  Document Authority Synthesis
  M3  Workflow Contract Memory
  M4  User Preference Memory
  M5  OKF/Markdown Authority Bundle
  M6  Context Pack API Hardening
  M7  Dendrite Local Evidence Capture
  M8  neurons-local per-PC Brain
  M9  neurons-central Federation
  M10 Repo Style Profile

Parallel infra:
  Infra-A Container Baseline / Compose Hardening, with M1
  Infra-B k3s PoC, after M1-M3 proof
```

## Anti-Goals

- Do not centrally replicate all PC files.
- Do not sync graph DB files between PCs.
- Do not make Neo4j the raw source of truth.
- Do not make Qdrant canonical memory.
- Do not reintroduce RAGFlow as a core Context Authority dependency.
- Do not let Graphiti semantic extraction sit in the hot path.
- Do not auto-delete archive candidates.
- Do not auto-edit skills.
- Do not include Hermes self-improvement or proposal loop in this roadmap.
- Do not start production k3s migration before product authority APIs are
  useful and workload boundaries are stable.

## Success Criteria

The roadmap is working when a fresh coding agent can start a repo task and
receive:

- current source-of-truth documents;
- generated companions and stale/archive candidates with evidence;
- workflow defaults the user should not need to restate;
- scoped user preferences with confidence;
- graph/search/bridge freshness status;
- risk-ranked evidence gaps with next actions;
- boundary guardrails for backend roles;
- a reviewable Markdown/OKF authority bundle when needed.

At that point `neurons` has moved from memory retrieval to context authority.
